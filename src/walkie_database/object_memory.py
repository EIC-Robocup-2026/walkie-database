"""Merge detections into objects, in the style of ConceptGraphs. No ROS and no SQL.

The node hands this module detections that are already in the map frame
(:class:`MapDetection`). Each one either joins the object it matches best, or
starts a new one. An object is a :class:`Track`: its merged cloud, its label
counts, a weighted mean of its embeddings, and its counters.

Matching (``ObjectMemory.add``)
    A detection whose label is in ``ignore_labels`` (walls, floors and other parts
    of the building) is dropped. Any other detection matches a track by a spatial
    similarity plus a visual one, each from 0 to 1, whose sum must reach
    ``match_threshold``. The spatial similarity is the share of the detection's
    points that lie within ``match_distance`` of the track's points. When the track
    answers to the detection's label, it is at least the share of the smaller of the
    two boxes that the boxes have in common, so that a partial view of a large piece
    of furniture still matches. The visual similarity is the cosine of the two
    embeddings, clipped at 0. Without both embeddings it is 1 when the track answers
    to the label, and otherwise ``other_label_similarity`` for a movable track and 0
    for an anchor. A movable track answers to every label it was ever given; an
    anchor only to its main label. A detection never joins a track of the other
    layer: furniture labels make anchors, and every other label makes movable
    objects.

Upkeep (``ObjectMemory.maintain``)
    Every few seconds: each changed track keeps its largest DBSCAN cluster and
    gets a new upright box, and an anchor gets the height of its top surface
    (:func:`surface_height`); an unconfirmed track that has not been seen for
    ``ghost_age_s`` is dropped; and two tracks that overlap and look alike merge,
    after which the merged track is cleaned and measured again in the same round.
    A track is confirmed once it has been seen in ``confirm_views`` arrays, each
    array counted once even when two merged tracks both saw it (for stored
    objects, the save counts the arrays against the store). Only a
    confirmed track gets an object id and a row in the store, so a one-off false
    detection never reaches it. An anchor track the memory made is not confirmed
    before ``confirm_views`` of its detections had enough points above the floor to
    measure (``slab_min_points``, ``slab_floor_clearance``, ``slab_cluster_share``,
    ``slab_cluster_gap``), nor while most of those were thin slabs
    (``slab_thickness``), however often it is seen: it is a wall or a door the
    detector named as furniture.

A seeded anchor is a fixed track: it has a box and no points. A detection of its
label that falls in its box confirms it, and changes no geometry. An anchor track
that lies mostly inside its box is merged into it at upkeep, whatever its label,
and the seeded anchor keeps its own.

See docs/architecture/object-memory.md, ADR-0025 (matching without embeddings) and
ADR-0026 (top surfaces and ``on`` relations).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree
from sklearn.cluster import DBSCAN

from walkie_database.geometry import quaternion_of_yaw

ANCHOR, MOVABLE = "anchor", "movable"
DEFAULT_ANCHOR_LABELS = ("chair", "table", "shelf", "cabinet", "sofa")
# Parts of the building, not objects. A detector that is asked for them names walls
# as walls instead of as the nearest furniture, and the memory drops them.
DEFAULT_IGNORED_LABELS = ("wall", "floor", "ceiling", "door", "window")


@dataclass(frozen=True)
class MergeConfig:
    voxel_size: float = 0.025  # metres, the grid every cloud is downsampled to
    # A point this close to a track's point overlaps it. It covers the error between
    # two visits: in the Chula drive, views of one object landed 0.10-0.20 m apart,
    # while AMCL's correction of the odometry, measured at the robot, peaked near 0.5 m.
    match_distance: float = 0.10
    match_threshold: float = 1.1  # spatial plus visual similarity needed to join
    min_spatial: float = 0.1  # below this a detection never joins, however alike
    max_points: int = 20000  # per track, after downsampling
    confirm_views: int = 3  # arrays that must show a track before it is stored
    ghost_age_s: float = 15.0  # an unconfirmed track unseen this long is dropped
    dbscan_eps: float = 0.08  # metres
    dbscan_min_points: int = 8
    duplicate_spatial: float = 0.5  # two tracks this entangled may be one object
    duplicate_visual: float = 0.8  # and look at least this alike (cosine)
    box_percentile: float = 2.0  # the box ignores this share of points on each side
    # Without both embeddings, the visual similarity of a movable track and a label it
    # never had. The same label counts 1. Above 0, a well-overlapping detection can
    # join under another name, which the detector gives a small object from some
    # views. Anchors always need a shared label: furniture stands within
    # match_distance of other furniture (stools at a table), so overlap alone would
    # join them.
    other_label_similarity: float = 0.5
    # The share of an anchor track's points inside a seeded anchor's box (grown by
    # match_distance) that makes it the seeded anchor, whatever its name. Furniture
    # does not share its space, so the detector has named part of the seeded anchor
    # otherwise: a strip of a sofa a table, the top of a table a chair.
    seed_containment: float = 0.8
    # A detection of an anchor is thin when its points, across their main horizontal
    # direction (box_percentile on each side), span less than this. An anchor track
    # the memory made is never confirmed while more than half of its detections are
    # thin: it is a wall, a partition or an open door that the detector named as
    # furniture. Each detection is one camera pose, so localization drift between
    # views cannot thicken it, as it does the merged cloud. In the simulated Chula
    # drive, the median detection of a wall or a door spanned at most 0.027 m, and of
    # furniture at least 0.10 m. 0 turns the rule off.
    slab_thickness: float = 0.05
    # A detection of an anchor is measured for slab_thickness only with at least this
    # many points after downsampling, and an anchor track the memory made is not
    # confirmed before confirm_views of its detections were measured. A sparse
    # fragment's span says nothing of its shape: in the Chula drive, seven fragments
    # of 14 to 34 points stored a partition before any view of its face, and nine in
    # ten detections of each piece of furniture had 50 points or more.
    slab_min_points: int = 50
    # Metres above the map frame's floor (z 0). A detection is measured without its
    # points below this. When one DBSCAN cluster then holds at least
    # slab_cluster_share of the rest, it is measured on that cluster and the clusters
    # within slab_cluster_gap of it. In a live Chula run, three masks of a wall's end
    # took in the seat of a stool, a cluster 0.96 to 1.04 m from the wall's with 9 to
    # 13% of the points above the floor, and two of them the floor in front of the
    # wall, which joins the wall's cluster. The wall was stored as a chair.
    slab_floor_clearance: float = 0.05
    # Below this share, the clusters are measured together, as a stool's legs. On the
    # Chula recordings, no detection of furniture changed its verdict at 0.8 from
    # measuring all its points; at 0.75 one of the shelf's turned thin, and at 0.9 two
    # of the three masks of the wall's end stayed thick.
    slab_cluster_share: float = 0.8
    # Metres. A cluster this close to the main one is part of the same thing, as a
    # chair's seat beside its back. On the Chula recordings any value from 0.15 to 0.7
    # gave the same verdicts.
    slab_cluster_gap: float = 0.5
    surface_bin: float = 0.02  # metres, the height bins that find an anchor's top surface
    surface_min_share: float = 0.3  # of the fullest bin's points, for a bin to be a surface
    anchor_labels: tuple[str, ...] = DEFAULT_ANCHOR_LABELS
    ignore_labels: tuple[str, ...] = DEFAULT_IGNORED_LABELS

    def layer_of(self, label: str) -> str:
        return ANCHOR if label in self.anchor_labels else MOVABLE


@dataclass(frozen=True)
class Box:
    """An upright box in the map: a centre, full side lengths and a yaw."""

    center: tuple[float, float, float]
    size: tuple[float, float, float]
    yaw: float = 0.0

    @property
    def rotation(self) -> tuple[float, float, float, float]:
        return quaternion_of_yaw(self.yaw)

    @property
    def bottom(self) -> float:
        return self.center[2] - self.size[2] / 2

    @property
    def top(self) -> float:
        return self.center[2] + self.size[2] / 2

    def footprint(self) -> list[tuple[float, float]]:
        """The four corners of the box seen from above, counter-clockwise."""
        cx, cy, _ = self.center
        hx, hy = self.size[0] / 2, self.size[1] / 2
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return [
            (cx + c * x - s * y, cy + s * x + c * y)
            for x, y in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
        ]

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """The axis-aligned bounds of the box, as two arrays of x, y, z."""
        corners = np.array(self.footprint())
        low = np.array([*corners.min(axis=0), self.bottom])
        high = np.array([*corners.max(axis=0), self.top])
        return low, high

    def contains(self, points: np.ndarray, margin: float = 0.0) -> np.ndarray:
        """Which of N points lie in the box grown by *margin* on every side."""
        local = points - np.asarray(self.center)
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        x = c * local[:, 0] + s * local[:, 1]
        y = -s * local[:, 0] + c * local[:, 1]
        half = np.asarray(self.size) / 2 + margin
        return (np.abs(x) <= half[0]) & (np.abs(y) <= half[1]) & (np.abs(local[:, 2]) <= half[2])


@dataclass(frozen=True)
class Sighting:
    """What the store keeps of one detection that joined a track (object_observation)."""

    stamp: float
    label: str
    score: float
    n_points: int
    center: tuple[float, float, float]
    view_quality: float
    camera_position: tuple[float, float, float] | None = None
    camera_rotation: tuple[float, float, float, float] | None = None
    color_shares: tuple[float, ...] | None = None


@dataclass
class MapDetection:
    """One detection, with its points already in the map frame."""

    label: str
    score: float
    points: np.ndarray  # N rows of x, y, z, float64
    stamp: float
    view_quality: float = 0.0
    model: str = ""  # empty when there is no embedding
    embedding: np.ndarray | None = None  # unit length
    camera_position: tuple[float, float, float] | None = None
    camera_rotation: tuple[float, float, float, float] | None = None
    color_shares: tuple[float, ...] | None = None


@dataclass
class Track:
    """One object being merged. *object_id* is None until the store holds it."""

    key: int  # in memory only, never shown
    layer: str
    points: np.ndarray
    labels: dict[str, int]
    first_seen: float
    last_seen: float
    n_detections: int = 0
    # Arrays that showed it; an array with two of its detections counts once. Exact
    # for a track the store has not seen. For a stored track, a merge with another
    # stored one can count an array both saw twice until the next save, which counts
    # it again against the stored observations.
    n_views: int = 0
    best_view_quality: float = 0.0
    model: str = ""
    embedding_sum: np.ndarray | None = None
    object_id: int | None = None
    fixed: bool = False  # a seeded anchor, whose box never changes
    box: Box | None = None
    surface_z: float | None = None  # an anchor's top surface, below the things on it
    dirty: bool = True  # changed since the store last saw it
    needs_cleanup: bool = True  # changed since its last denoise and box
    sightings: list[Sighting] = field(default_factory=list)  # not yet in the store
    # The stamps of the arrays counted in n_views that the store does not hold yet.
    # A save empties it with the sightings, so it stays as small as they are.
    unsaved_views: set[float] = field(default_factory=set)
    # False until a save has counted the views against the store, and again after the
    # track absorbs a stored object, whose history the store must count with its own.
    views_counted: bool = False
    # An anchor's detections measured for MergeConfig.slab_thickness, and how many of
    # them were thinner. In memory only: a stored track is never held back.
    measured_detections: int = 0
    thin_detections: int = 0
    _tree: cKDTree | None = None
    _bounds: tuple[np.ndarray, np.ndarray] | None = None

    @property
    def label(self) -> str:
        """The label given most often. A tie goes to the first name in order."""
        return min(self.labels, key=lambda name: (-self.labels[name], name))

    @property
    def embedding(self) -> np.ndarray | None:
        if self.embedding_sum is None:
            return None
        norm = float(np.linalg.norm(self.embedding_sum))
        return None if norm == 0.0 else self.embedding_sum / norm

    def confirmed(self, config: MergeConfig) -> bool:
        if self.fixed or self.object_id is not None:
            return True
        return self.n_views >= config.confirm_views and not self.held_back(config)

    def held_back(self, config: MergeConfig) -> bool:
        """Whether the track is an anchor the memory made that its detections have not shown to be furniture.

        That is, fewer than ``confirm_views`` of them were measured, or more than half
        of those were thin slabs (``MergeConfig.slab_thickness``). A seeded or stored
        track never is.
        """
        if self.layer != ANCHOR or self.fixed or self.object_id is not None or config.slab_thickness <= 0:
            return False
        return self.measured_detections < config.confirm_views or 2 * self.thin_detections > self.measured_detections

    def tree(self) -> cKDTree | None:
        if self._tree is None and len(self.points):
            self._tree = cKDTree(self.points)
        return self._tree

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if self._bounds is None:
            if self.fixed and self.box is not None:
                self._bounds = self.box.bounds()
            else:
                self._bounds = (self.points.min(axis=0), self.points.max(axis=0))
        return self._bounds

    def set_points(self, points: np.ndarray) -> None:
        self.points = points
        self._tree = None
        self._bounds = None

    def saved(self, n_views: int) -> None:
        """The store now holds the track, with *n_views* counted against its observations."""
        self.n_views = n_views
        self.dirty = False
        self.sightings.clear()
        self.unsaved_views.clear()
        self.views_counted = True


@dataclass
class Maintenance:
    """What one round of upkeep changed in the stored objects."""

    merged: list[tuple[int, int]] = field(default_factory=list)  # (loser id, winner id)
    dropped: int = 0  # unconfirmed tracks that never reached the store


def rotation_matrix(rotation: Sequence[float]) -> np.ndarray:
    """The 3 by 3 matrix of a unit quaternion (x, y, z, w), for many points at once."""
    x, y, z, w = rotation
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def voxel_downsample(points: np.ndarray, voxel: float) -> np.ndarray:
    """One point per occupied voxel: the mean of the points in it."""
    if len(points) == 0:
        return points.reshape(0, 3)
    cells = np.floor(points / voxel).astype(np.int64)
    _, inverse, counts = np.unique(cells, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    sums = np.zeros((len(counts), 3))
    np.add.at(sums, inverse, points)
    return sums / counts[:, None]


def upright_box(points: np.ndarray, *, percentile: float, minimum: float) -> Box:
    """A box whose sides follow the main horizontal direction of the points.

    The extent leaves out *percentile* per cent of the points on each side, so a few
    stray depth points do not stretch it. No side is below *minimum*.
    """
    xy = points[:, :2]
    middle = xy.mean(axis=0)
    yaw = 0.0
    if len(points) >= 3:
        covariance = np.cov((xy - middle).T)
        if np.all(np.isfinite(covariance)) and np.trace(covariance) > 1e-12:
            _, vectors = np.linalg.eigh(covariance)
            major = vectors[:, 1]
            yaw = math.atan2(major[1], major[0])
            # A box looks the same turned by half a turn, so keep the yaw in (-pi/2, pi/2].
            if yaw <= -math.pi / 2:
                yaw += math.pi
            elif yaw > math.pi / 2:
                yaw -= math.pi
    c, s = math.cos(yaw), math.sin(yaw)
    rotated = np.column_stack(
        (c * (xy[:, 0] - middle[0]) + s * (xy[:, 1] - middle[1]),
         -s * (xy[:, 0] - middle[0]) + c * (xy[:, 1] - middle[1]),
         points[:, 2])
    )
    low = np.percentile(rotated, percentile, axis=0)
    high = np.percentile(rotated, 100.0 - percentile, axis=0)
    mid = (low + high) / 2
    size = np.maximum(high - low, minimum)
    center = (middle[0] + c * mid[0] - s * mid[1], middle[1] + s * mid[0] + c * mid[1], mid[2])
    return Box(tuple(float(v) for v in center), tuple(float(v) for v in size), float(yaw))


def surface_height(points: np.ndarray, *, bin_size: float, min_share: float) -> float | None:
    """The height of the highest dense horizontal layer of points.

    A table's cloud holds the points of the things on it too, so the top of its box
    is the top of the tallest bottle. Its top is the highest height bin that holds at
    least *min_share* of the points of the fullest bin. Each bin counts with its two
    neighbours, so a plane that straddles a bin edge still counts in full.
    """
    if len(points) == 0:
        return None
    z = points[:, 2]
    bins = np.floor((z - z.min()) / bin_size).astype(np.int64)
    counts = np.bincount(bins)
    smooth = counts + np.r_[0, counts[:-1]] + np.r_[counts[1:], 0]
    top = int(np.flatnonzero(smooth >= min_share * smooth.max())[-1])
    window = counts[max(0, top - 1) : top + 2]
    peak = max(0, top - 1) + int(np.argmax(window))
    return float(np.median(z[bins == peak]))


def largest_cluster(points: np.ndarray, *, eps: float, min_points: int) -> np.ndarray:
    """The points of the largest DBSCAN cluster, or all of them when there is none."""
    if len(points) < min_points:
        return points
    labels = DBSCAN(eps=eps, min_samples=min_points).fit(points).labels_
    kept = labels[labels >= 0]
    if len(kept) == 0:
        return points
    biggest = np.bincount(kept).argmax()
    return points[labels == biggest]


def _box_overlap(a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray], pad: float) -> float:
    """The share of the smaller of two axis-aligned boxes that lies in both."""
    low = np.maximum(a[0] - pad, b[0] - pad)
    high = np.minimum(a[1] + pad, b[1] + pad)
    common = float(np.prod(np.clip(high - low, 0.0, None)))
    if common == 0.0:
        return 0.0
    volumes = [float(np.prod(box[1] - box[0] + 2 * pad)) for box in (a, b)]
    return min(1.0, common / min(volumes))


def _boxes_touch(a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray], pad: float) -> bool:
    return bool(np.all(a[0] - pad <= b[1]) and np.all(b[0] - pad <= a[1]))


def _is_called(track: Track, label: str) -> bool:
    """Whether a label is one of the track's names.

    A movable track answers to every label it was ever given, since the detector
    names a small object differently from some views. An anchor answers only to its
    main label: a sofa sometimes seen as a table must not pull in a table next to
    it, and a table seen once as a chair must not pull in the stools.
    """
    return label == track.label if track.layer == ANCHOR else label in track.labels


class ObjectMemory:
    """The tracks of one map, and the rules that merge detections into them."""

    def __init__(self, config: MergeConfig | None = None, tracks: Iterable[Track] = ()) -> None:
        self.config = config or MergeConfig()
        self.tracks: list[Track] = list(tracks)
        self._next_key = max((t.key for t in self.tracks), default=0) + 1

    # Matching ----------------------------------------------------------------

    def add(self, detections: Sequence[MapDetection]) -> list[Track]:
        """Merge each detection into its best match or a new track, in order."""
        joined = []
        for detection in detections:
            if detection.label in self.config.ignore_labels:
                continue
            points = voxel_downsample(np.asarray(detection.points, dtype=np.float64), self.config.voxel_size)
            if len(points) == 0:
                continue
            track = self._best_match(detection, points)
            if track is None:
                track = self._new_track(detection, points)
            else:
                self._merge_detection(track, detection, points)
            joined.append(track)
        return joined

    def _best_match(self, detection: MapDetection, points: np.ndarray) -> Track | None:
        layer = self.config.layer_of(detection.label)
        bounds = (points.min(axis=0), points.max(axis=0))
        best, best_score = None, self.config.match_threshold
        for track in self.tracks:
            if track.layer != layer or not _boxes_touch(track.bounds(), bounds, self.config.match_distance):
                continue
            spatial = self._spatial(track, points, bounds, same_label=_is_called(track, detection.label))
            if spatial < self.config.min_spatial:
                continue
            score = spatial + self._visual(track, detection.label, detection.model, detection.embedding)
            if score >= best_score:
                best, best_score = track, score
        return best

    def _spatial(self, track: Track, points: np.ndarray, bounds, *, same_label: bool) -> float:
        if track.fixed:
            # A seeded anchor has a box and no points. Only its own label is checked.
            if not same_label or track.box is None:
                return 0.0
            return float(track.box.contains(points, self.config.match_distance).mean())
        distances, _ = track.tree().query(points, distance_upper_bound=self.config.match_distance)
        overlap = float(np.isfinite(distances).mean())
        if same_label:
            overlap = max(overlap, _box_overlap(track.bounds(), bounds, self.config.voxel_size / 2))
        return overlap

    def _visual(self, track: Track, label: str, model: str, embedding: np.ndarray | None) -> float:
        mine = track.embedding
        if embedding is not None and mine is not None and model == track.model:
            return max(0.0, float(np.dot(mine, embedding)))
        return 1.0 if _is_called(track, label) else self._other_label(track.layer)

    def _other_label(self, layer: str) -> float:
        return self.config.other_label_similarity if layer == MOVABLE else 0.0

    def _new_track(self, detection: MapDetection, points: np.ndarray) -> Track:
        track = Track(
            key=self._next_key,
            layer=self.config.layer_of(detection.label),
            points=points,
            labels={},
            first_seen=detection.stamp,
            last_seen=detection.stamp,
        )
        self._next_key += 1
        self._merge_detection(track, detection, points, fresh=True)
        self.tracks.append(track)
        return track

    def _merge_detection(self, track: Track, detection: MapDetection, points: np.ndarray, *, fresh: bool = False) -> None:
        if not fresh and not track.fixed:
            track.set_points(self._capped(voxel_downsample(np.vstack((track.points, points)), self.config.voxel_size)))
        track.labels[detection.label] = track.labels.get(detection.label, 0) + 1
        track.n_detections += 1
        if track.layer == ANCHOR and not track.fixed:
            body = self._body(points)
            if len(body) >= self.config.slab_min_points:
                # One camera pose, so its thickness holds no drift between views.
                view = upright_box(body, percentile=self.config.box_percentile, minimum=0.0)
                track.measured_detections += 1
                track.thin_detections += int(min(view.size[:2]) < self.config.slab_thickness)
        if detection.stamp not in track.unsaved_views:
            track.unsaved_views.add(detection.stamp)
            track.n_views += 1
        track.first_seen = min(track.first_seen, detection.stamp)
        track.last_seen = max(track.last_seen, detection.stamp)
        track.best_view_quality = max(track.best_view_quality, detection.view_quality)
        if detection.embedding is not None and detection.model:
            weight = max(detection.score, 1e-3)
            if track.embedding_sum is None or track.model != detection.model:
                # One model per track. A new model starts the mean again.
                track.model = detection.model
                track.embedding_sum = weight * np.asarray(detection.embedding, dtype=np.float64)
            else:
                track.embedding_sum = track.embedding_sum + weight * np.asarray(detection.embedding, dtype=np.float64)
        center = tuple(float(v) for v in points.mean(axis=0))
        track.sightings.append(
            Sighting(
                stamp=detection.stamp,
                label=detection.label,
                score=detection.score,
                n_points=len(detection.points),
                center=center,
                view_quality=detection.view_quality,
                camera_position=detection.camera_position,
                camera_rotation=detection.camera_rotation,
                color_shares=detection.color_shares,
            )
        )
        track.dirty = True
        track.needs_cleanup = True

    def _capped(self, points: np.ndarray) -> np.ndarray:
        """At most max_points, chosen evenly, so a huge piece of furniture stays cheap."""
        if len(points) <= self.config.max_points:
            return points
        keep = np.linspace(0, len(points) - 1, self.config.max_points).astype(np.int64)
        return points[keep]

    # Upkeep ------------------------------------------------------------------

    def maintain(self, now: float) -> Maintenance:
        """Denoise and box changed tracks, drop ghosts, and merge duplicates.

        Every track leaves with its geometry measured from its current points, also a
        track that absorbed a duplicate in this round.
        """
        result = Maintenance()
        for track in self.tracks:
            self._clean(track)
        before = len(self.tracks)
        self.tracks = [
            t for t in self.tracks
            if t.confirmed(self.config) or now - t.last_seen <= self.config.ghost_age_s
        ]
        result.dropped = before - len(self.tracks)
        self._merge_duplicates(result)
        return result

    def _body(self, points: np.ndarray) -> np.ndarray:
        """The points of one detection that its thickness is measured on.

        Those at least ``slab_floor_clearance`` above the floor. When their largest
        DBSCAN cluster holds at least ``slab_cluster_share`` of them, only it and the
        clusters within ``slab_cluster_gap`` of it: a mask of a wall that takes in the
        floor in front of it, or a stool a metre away, would make the wall look deep.
        Otherwise, or with no cluster, all of them: the legs of a stool seen apart are
        all of it.
        """
        above = points[points[:, 2] >= self.config.slab_floor_clearance]
        if len(above) < self.config.dbscan_min_points:
            return above
        labels = DBSCAN(eps=self.config.dbscan_eps, min_samples=self.config.dbscan_min_points).fit(above).labels_
        if not (labels >= 0).any():
            return above
        main = np.bincount(labels[labels >= 0]).argmax()
        if (labels == main).sum() < self.config.slab_cluster_share * len(above):
            return above
        others = (labels >= 0) & (labels != main)
        kept = labels == main
        if others.any():
            distances, _ = cKDTree(above[kept]).query(above[others], distance_upper_bound=self.config.slab_cluster_gap)
            kept |= np.isin(labels, labels[others][np.isfinite(distances)])
        return above[kept]

    def _clean(self, track: Track) -> None:
        """Denoise a changed track, then measure its box and top surface again.

        New geometry marks the track dirty, so the next save writes it.
        """
        if track.needs_cleanup and not track.fixed:
            kept = largest_cluster(track.points, eps=self.config.dbscan_eps, min_points=self.config.dbscan_min_points)
            track.set_points(kept)
            track.box = upright_box(kept, percentile=self.config.box_percentile, minimum=self.config.voxel_size)
            if track.layer == ANCHOR:
                track.surface_z = surface_height(
                    kept, bin_size=self.config.surface_bin, min_share=self.config.surface_min_share
                )
            track.dirty = True
        track.needs_cleanup = False

    def _merge_duplicates(self, result: Maintenance) -> None:
        merged_any = True
        while merged_any:
            merged_any = False
            for i, a in enumerate(self.tracks):
                for b in self.tracks[i + 1 :]:
                    if self._are_duplicates(a, b):
                        winner, loser = self._winner(a, b)
                        self._absorb(winner, loser)
                        # Measured before the next pair is compared, and before the save.
                        self._clean(winner)
                        if loser.object_id is not None:
                            result.merged.append((loser.object_id, winner.object_id))
                        self.tracks.remove(loser)
                        merged_any = True
                        break
                if merged_any:
                    break

    def _are_duplicates(self, a: Track, b: Track) -> bool:
        if a.layer != b.layer or (a.fixed and b.fixed):
            return False
        if not _boxes_touch(a.bounds(), b.bounds(), self.config.match_distance):
            return False
        if a.layer == ANCHOR and (a.fixed or b.fixed) and self._inside_seeded(a, b):
            return True
        if a.layer == ANCHOR:
            same_label = a.label == b.label
        else:
            same_label = bool(set(a.labels) & set(b.labels))
        if a.fixed or b.fixed:
            fixed, other = (a, b) if a.fixed else (b, a)
            spatial = self._spatial(fixed, other.points, other.bounds(), same_label=same_label)
        else:
            spatial = max(
                self._spatial(a, b.points, b.bounds(), same_label=same_label),
                self._spatial(b, a.points, a.bounds(), same_label=same_label),
            )
        if spatial < self.config.duplicate_spatial:
            return False
        ea, eb = a.embedding, b.embedding
        if ea is not None and eb is not None and a.model == b.model:
            return float(np.dot(ea, eb)) >= self.config.duplicate_visual
        # Without embeddings, as a detection joins: a shared label, or enough overlap
        # to make up for another name.
        return same_label or spatial + self._other_label(a.layer) >= self.config.match_threshold

    def _inside_seeded(self, a: Track, b: Track) -> bool:
        """Whether one of two anchor tracks is seeded and holds most of the other's points.

        Only a seeded anchor's box is trusted for this. A box the memory measured
        can be wrong, as when stray points of a sofa reach over the coffee table
        in front of it.
        """
        fixed, other = (a, b) if a.fixed else (b, a)
        if fixed.box is None or len(other.points) == 0:
            return False
        share = float(fixed.box.contains(other.points, self.config.match_distance).mean())
        return share >= self.config.seed_containment

    @staticmethod
    def _winner(a: Track, b: Track) -> tuple[Track, Track]:
        """The track that keeps its id: a seeded anchor, then a stored one, then the most seen."""
        def rank(t: Track):
            return (t.fixed, t.object_id is not None, t.n_detections, -(t.object_id or 0), -t.key)

        return (a, b) if rank(a) >= rank(b) else (b, a)

    def _absorb(self, winner: Track, loser: Track) -> None:
        if not winner.fixed:
            winner.set_points(self._capped(voxel_downsample(np.vstack((winner.points, loser.points)), self.config.voxel_size)))
        # A seeded anchor keeps its seeded name: a track it absorbs under another
        # name counts as more sightings of it.
        own = winner.label if winner.fixed else None
        for name, count in loser.labels.items():
            name = own or name
            winner.labels[name] = winner.labels.get(name, 0) + count
        winner.n_detections += loser.n_detections
        winner.measured_detections += loser.measured_detections
        winner.thin_detections += loser.thin_detections
        # An array that showed both tracks is one view of the merged object. Arrays
        # the store already holds are compared there, by the next save.
        winner.n_views += loser.n_views - len(winner.unsaved_views & loser.unsaved_views)
        winner.unsaved_views |= loser.unsaved_views
        if loser.object_id is not None:
            winner.views_counted = False
        winner.first_seen = min(winner.first_seen, loser.first_seen)
        winner.last_seen = max(winner.last_seen, loser.last_seen)
        winner.best_view_quality = max(winner.best_view_quality, loser.best_view_quality)
        if loser.embedding_sum is not None:
            if winner.embedding_sum is None:
                winner.model, winner.embedding_sum = loser.model, loser.embedding_sum.copy()
            elif winner.model == loser.model:
                winner.embedding_sum = winner.embedding_sum + loser.embedding_sum
        winner.sightings.extend(loser.sightings)
        winner.dirty = True
        winner.needs_cleanup = True

    # Reading -----------------------------------------------------------------

    def confirmed(self) -> list[Track]:
        return [t for t in self.tracks if t.confirmed(self.config)]
