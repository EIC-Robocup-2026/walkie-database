"""Write the object memory's tracks to the store, and read them back at start.

The node calls :func:`save` on its timer. It reserves the ids of newly confirmed
tracks outside any transaction, as ``reserve_object_ids`` requires, then writes
everything in one transaction: tombstones of merged objects, new rows, changed
rows with their box, cloud, vector and its weight, label counts and sightings, the
top surface of each anchor, and the ``on`` relations. A failure rolls it all back, and the tracks
keep their changes for the next round.

:func:`load_tracks` reads the live objects of a map, so a restarted node goes on
merging into the same objects.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from walkie_database.db import transaction
from walkie_database.geometry import yaw_of
from walkie_database.object_context import add_surface, room_at, set_relation, usable_surfaces
from walkie_database.object_memory import ANCHOR, MOVABLE, Box, Maintenance, MergeConfig, ObjectMemory, Track
from walkie_database.objects import (
    OBJECT,
    NewObject,
    ObjectBox,
    _aabb,
    insert_object,
    require_transaction,
    reserve_object_ids,
    tombstone_object,
    write_object_vector,
)
from walkie_database.people import vector_tables

CONFIRMED_STATE = "present"
CONFIRMED_P_EXIST = 0.9
RELATION_WEIGHT = 0.9
# An object rests on a surface when its bottom is this close to the surface's height.
ON_BELOW_M = 0.06
ON_ABOVE_M = 0.10
_LIVE = "state NOT IN ('discarded', 'merged')"


@dataclass
class SaveResult:
    inserted: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    merged: list[tuple[int, int]] = field(default_factory=list)
    relations: dict[int, tuple[int, int]] = field(default_factory=dict)  # object -> (anchor, surface)


def surface_key(object_id: int) -> str:
    """The key of the top surface the memory gives an anchor it made."""
    return f"object-{object_id}-top"


def save(conn: sqlite3.Connection, memory: ObjectMemory, maintenance: Maintenance, *, map_id: str) -> SaveResult:
    """Write the tracks that changed. On success the tracks are marked clean.

    *maintenance* holds the merges of the last upkeep. Its list is emptied once they
    are written, and kept for the next try if the write fails.
    """
    config = memory.config
    new = [t for t in memory.confirmed() if t.object_id is None]
    changed = [t for t in memory.confirmed() if t.dirty and t.object_id is not None and not t.fixed]
    fixed = [t for t in memory.confirmed() if t.dirty and t.fixed]
    if not (new or changed or fixed or maintenance.merged):
        return SaveResult()
    # Committed on its own, so a failed save leaves these ids unused. Ids are never
    # given out twice, so that only leaves a gap.
    reservation = reserve_object_ids(conn, len(new)) if new else None
    ids = dict(zip((t.key for t in new), reservation)) if reservation else {}
    result = SaveResult(merged=list(maintenance.merged))
    views: dict[int, int] = {}  # track key -> arrays counted against the store
    with transaction(conn):
        for loser, winner in maintenance.merged:
            tombstone_object(conn, loser, merged_into=winner)
        for track in new:
            object_id = ids[track.key]
            insert_object(
                conn,
                NewObject(
                    map_id=map_id,
                    label=track.label,
                    first_seen=track.first_seen,
                    last_seen=track.last_seen,
                    layer=track.layer,
                    state=CONFIRMED_STATE,
                    p_exist=CONFIRMED_P_EXIST,
                    box=_object_box(track.box, track.last_seen),
                ),
                object_id,
                reservation=reservation,
            )
            views[track.key] = _write_track(conn, track, object_id, map_id, config)
            result.inserted.append(object_id)
        for track in changed:
            views[track.key] = _write_track(conn, track, track.object_id, map_id, config)
            result.updated.append(track.object_id)
        for track in fixed:
            views[track.key] = _write_fixed(conn, track)
            result.updated.append(track.object_id)
        result.relations = _write_relations(conn, memory, map_id, ids)
    # Only after the commit: a rollback must leave the tracks as they were.
    for track in new:
        track.object_id = ids[track.key]
    for track in (*new, *changed, *fixed):
        track.saved(views[track.key])
    maintenance.merged.clear()
    return result


def _object_box(box: Box | None, stamp: float) -> ObjectBox | None:
    if box is None:
        return None
    return ObjectBox(center=box.center, size=box.size, rotation=box.rotation, stamp=stamp)


def _write_track(conn: sqlite3.Connection, track: Track, object_id: int, map_id: str, config: MergeConfig) -> int:
    """Write a track the memory measured. Returns its views, counted against the store."""
    n_views = _count_views(conn, track, object_id)
    box = track.box
    room_id = room_at(conn, map_id, box.center[0], box.center[1]) if box else None
    center = box.center if box else (0.0, 0.0, 0.0)
    size = box.size if box else (0.0, 0.0, 0.0)
    rotation = box.rotation if box else (0.0, 0.0, 0.0, 1.0)
    conn.execute(
        """UPDATE object SET label = ?, room_id = ?, center_x = ?, center_y = ?, center_z = ?,
                             size_x = ?, size_y = ?, size_z = ?, qx = ?, qy = ?, qz = ?, qw = ?,
                             n_detections = ?, first_seen = ?, last_seen = ?, last_verified = ?,
                             state = CASE WHEN state = 'candidate' THEN ? ELSE state END,
                             p_exist = max(p_exist, ?), geometry_valid = ?, geometry_stamp = ?,
                             depth_seen = 1, best_view_quality = ?, viewpoints = ?,
                             revision = revision + 1
           WHERE id = ?""",
        (track.label, room_id, *center, *size, *rotation, track.n_detections,
         track.first_seen, track.last_seen, track.last_seen, CONFIRMED_STATE, CONFIRMED_P_EXIST,
         1 if box else 0, track.last_seen if box else None, track.best_view_quality,
         n_views, object_id),
    )
    if box:
        conn.execute("DELETE FROM object_box WHERE id = ?", (object_id,))
        conn.execute(
            "INSERT INTO object_box (id, min_x, max_x, min_y, max_y, min_z, max_z) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (object_id, *_aabb(_object_box(box, track.last_seen))),
        )
    points = np.ascontiguousarray(track.points, dtype="<f4")
    conn.execute(
        """INSERT INTO object_cloud (object_id, voxel_size, n_points, points, colors)
           VALUES (?, ?, ?, ?, NULL)
           ON CONFLICT (object_id) DO UPDATE SET voxel_size = excluded.voxel_size,
               n_points = excluded.n_points, points = excluded.points, colors = NULL""",
        (object_id, config.voxel_size, len(points), points.tobytes()),
    )
    embedding = track.embedding
    if embedding is not None:
        write_object_vector(conn, object_id, track.model, embedding)
        # A track holds one model's mean. The vector of a model it left would load in
        # place of this one after a restart, with this one's weight.
        _delete_other_vectors(conn, object_id, track.model)
        # The stored vector is the direction of the weighted sum, and this its length,
        # so load_tracks rebuilds the same sum and later vectors move it as before.
        conn.execute("UPDATE object SET fingerprint_weight = ? WHERE id = ?",
                     (float(np.linalg.norm(track.embedding_sum)), object_id))
    for label, count in track.labels.items():
        conn.execute(
            """INSERT INTO object_label (object_id, label, count) VALUES (?, ?, ?)
               ON CONFLICT (object_id, label) DO UPDATE SET count = excluded.count""",
            (object_id, label, count),
        )
    _write_sightings(conn, track, object_id)
    if track.layer == ANCHOR and box:
        _write_top_surface(conn, object_id, box, box.top if track.surface_z is None else track.surface_z)
    return n_views


def _write_fixed(conn: sqlite3.Connection, track: Track) -> int:
    """A seeded anchor seen again: its counters and times change, its geometry does not.

    Returns its views, counted against the store.
    """
    n_views = _count_views(conn, track, track.object_id)
    conn.execute(
        """UPDATE object SET n_detections = ?, last_seen = max(last_seen, ?),
                             last_verified = max(coalesce(last_verified, 0), ?),
                             best_view_quality = max(best_view_quality, ?), viewpoints = ?,
                             revision = revision + 1
           WHERE id = ?""",
        (track.n_detections, track.last_seen, track.last_seen, track.best_view_quality,
         n_views, track.object_id),
    )
    _write_sightings(conn, track, track.object_id)
    return n_views


def _count_views(conn: sqlite3.Connection, track: Track, object_id: int) -> int:
    """How many arrays showed the object: those its stored observations hold, and the new ones.

    Its observations are its own and those of every object merged into it, and an
    array is known by its stamp. Called before the track's sightings are written, so
    a new array adds one unless the store already holds its stamp. Once a save has
    counted a track, the next ones start from the stored count, so a save costs one
    indexed lookup per new array. The first save in a process, and the first after
    the track absorbed a stored object, count the stored observations again.
    """
    family = [row[0] for row in conn.execute(
        """WITH RECURSIVE family (id) AS (
               SELECT ? UNION SELECT o.id FROM object AS o JOIN family AS f ON o.merged_into = f.id)
           SELECT id FROM family""",
        (object_id,),
    )]
    marks = ", ".join("?" * len(family))
    if track.views_counted:
        stored = conn.execute("SELECT viewpoints FROM object WHERE id = ?", (object_id,)).fetchone()[0]
    else:
        stored = conn.execute(
            f"SELECT count(DISTINCT ts) FROM object_observation WHERE object_id IN ({marks})", family
        ).fetchone()[0]
    held = f"SELECT 1 FROM object_observation WHERE object_id IN ({marks}) AND ts = ? LIMIT 1"
    return stored + sum(
        1 for stamp in track.unsaved_views if conn.execute(held, (*family, stamp)).fetchone() is None
    )


def _delete_other_vectors(conn: sqlite3.Connection, object_id: int, model: str) -> None:
    """Remove the object's vectors of every model but *model*, inside the caller's transaction."""
    for other in vector_tables(conn, OBJECT).lengths:
        if other != model:
            # The name comes from the schema's own vec0 tables, so it is safe in SQL text.
            conn.execute(f"DELETE FROM {OBJECT}_{other} WHERE object_id = ?", (object_id,))


def _write_sightings(conn: sqlite3.Connection, track: Track, object_id: int) -> None:
    for s in track.sightings:
        camera = (*(s.camera_position or (None,) * 3), *(s.camera_rotation or (None,) * 4))
        conn.execute(
            """INSERT INTO object_observation (object_id, ts, label, score, n_points,
                   center_x, center_y, center_z, view_score,
                   camera_x, camera_y, camera_z, camera_qx, camera_qy, camera_qz, camera_qw,
                   color_shares)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (object_id, s.stamp, s.label, s.score, s.n_points, *s.center, s.view_quality, *camera,
             json.dumps(list(s.color_shares)) if s.color_shares else None),
        )


def _write_top_surface(conn: sqlite3.Connection, object_id: int, box: Box, z: float) -> None:
    """Give an anchor the memory made a measured top surface at height *z*, or move the one it has.

    The outline is the box's footprint. *z* is the densest high layer of the anchor's
    points (``object_memory.surface_height``), not the box top, which the things
    standing on the anchor push up.
    """
    require_transaction(conn, "_write_top_surface")
    corners = [list(corner) for corner in box.footprint()]
    key = surface_key(object_id)
    row = conn.execute("SELECT id FROM surface WHERE anchor_id = ? AND key = ?", (object_id, key)).fetchone()
    if row is None:
        add_surface(conn, object_id, key, z, corners)
    else:
        conn.execute("UPDATE surface SET z = ?, outline = ?, geometry_valid = 1 WHERE id = ?",
                     (z, json.dumps(corners), row["id"]))


def _write_relations(
    conn: sqlite3.Connection, memory: ObjectMemory, map_id: str, new_ids: dict[int, int]
) -> dict[int, tuple[int, int]]:
    """Store each movable object's ``on`` relation to the surface it rests on, if any.

    The surfaces are every usable one in the map, seeded or made by the memory. An
    object keeps at most one ``on`` row: the surface nearest below its bottom.
    """
    surfaces = []
    for anchor in conn.execute(
        f"SELECT id FROM object WHERE map_id = ? AND layer = 'anchor' AND geometry_valid = 1 AND {_LIVE}",
        (map_id,),
    ):
        for surface in usable_surfaces(conn, anchor["id"]):
            outline = json.loads(surface["outline"])
            if len(outline) >= 3:
                surfaces.append((anchor["id"], surface["id"], surface["z"], outline))
    found: dict[int, tuple[int, int]] = {}
    for track in memory.confirmed():
        object_id = track.object_id if track.object_id is not None else new_ids.get(track.key)
        if object_id is None or track.layer != MOVABLE or track.box is None:
            continue
        x, y, _ = track.box.center
        bottom = track.box.bottom
        best = None
        for anchor_id, surface_id, z, outline in surfaces:
            gap = bottom - z
            if -ON_BELOW_M <= gap <= ON_ABOVE_M and _inside(x, y, outline):
                if best is None or abs(gap) < best[0]:
                    best = (abs(gap), anchor_id, surface_id)
        if best is None:
            conn.execute("DELETE FROM object_relation WHERE src_id = ? AND predicate = 'on'", (object_id,))
            continue
        _, anchor_id, surface_id = best
        conn.execute(
            "DELETE FROM object_relation WHERE src_id = ? AND predicate = 'on' AND NOT (dst_id = ? AND surface_id = ?)",
            (object_id, anchor_id, surface_id),
        )
        set_relation(conn, object_id, anchor_id, "on", RELATION_WEIGHT, track.last_seen, surface_id=surface_id)
        found[object_id] = (anchor_id, surface_id)
    return found


def load_relations(conn: sqlite3.Connection, map_id: str) -> dict[int, int]:
    """The stored ``on`` relations of a map's live objects, as object id to anchor id."""
    rows = conn.execute(
        f"""SELECT r.src_id, r.dst_id FROM object_relation AS r JOIN object AS o ON o.id = r.src_id
            WHERE o.map_id = ? AND r.predicate = 'on' AND o.{_LIVE}
            ORDER BY r.src_id, r.weight DESC, r.dst_id""",
        (map_id,),
    )
    found: dict[int, int] = {}
    for src_id, dst_id in rows:
        found.setdefault(src_id, dst_id)
    return found


def _inside(x: float, y: float, outline: Sequence[Sequence[float]]) -> bool:
    """Whether a point lies in a polygon, by counting edge crossings."""
    inside = False
    j = len(outline) - 1
    for i in range(len(outline)):
        xi, yi = outline[i][0], outline[i][1]
        xj, yj = outline[j][0], outline[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def load_tracks(conn: sqlite3.Connection, map_id: str, config: MergeConfig) -> list[Track]:
    """The live objects of a map as tracks: those the memory made, and measured seeded anchors.

    An object with no cloud, as a seeded anchor, becomes a fixed track with its box.
    An anchor the memory made gets back the height of its top surface, and every
    track the weighted sum of its embeddings, so a restart merges as an
    uninterrupted run would.
    """
    tracks = []
    rows = conn.execute(
        f"""SELECT o.*, c.points AS cloud FROM object AS o
            LEFT JOIN object_cloud AS c ON c.object_id = o.id
            WHERE o.map_id = ? AND o.{_LIVE} AND o.geometry_valid = 1
              AND (o.source = 'perception' OR o.layer = 'anchor')
            ORDER BY o.id""",
        (map_id,),
    ).fetchall()
    for key, row in enumerate(rows, start=1):
        labels = {
            r["label"]: r["count"]
            for r in conn.execute("SELECT label, count FROM object_label WHERE object_id = ?", (row["id"],))
            if r["count"] > 0
        } or {row["label"]: max(1, row["n_detections"])}
        box = Box(
            (row["center_x"], row["center_y"], row["center_z"]),
            (row["size_x"], row["size_y"], row["size_z"]),
            yaw_of((row["qx"], row["qy"], row["qz"], row["qw"])),
        )
        fixed = row["cloud"] is None
        points = np.zeros((0, 3)) if fixed else np.frombuffer(row["cloud"], dtype="<f4").reshape(-1, 3).astype(np.float64)
        model, embedding = _stored_vector(conn, row["id"])
        # A row saved before the weight was stored has 0: count its vector as one detection's.
        weight = row["fingerprint_weight"] if row["fingerprint_weight"] > 0 else 1.0
        surface_z = None
        if not fixed and row["layer"] == ANCHOR:
            surface = conn.execute(
                "SELECT z FROM surface WHERE anchor_id = ? AND key = ? AND geometry_valid = 1",
                (row["id"], surface_key(row["id"])),
            ).fetchone()
            surface_z = None if surface is None else surface["z"]
        tracks.append(
            Track(
                key=key,
                layer=row["layer"] if row["layer"] in (ANCHOR, MOVABLE) else config.layer_of(row["label"]),
                points=points,
                labels=labels,
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                n_detections=row["n_detections"],
                n_views=row["viewpoints"],
                best_view_quality=row["best_view_quality"],
                model=model,
                embedding_sum=embedding * weight if embedding is not None else None,
                object_id=row["id"],
                fixed=fixed,
                box=box,
                surface_z=surface_z,
                dirty=False,
                needs_cleanup=False,
            )
        )
    return tracks


def _stored_vector(conn: sqlite3.Connection, object_id: int) -> tuple[str, np.ndarray | None]:
    for model in sorted(vector_tables(conn, OBJECT).lengths):
        row = conn.execute(
            f"SELECT embedding FROM {OBJECT}_{model} WHERE object_id = ?", (object_id,)
        ).fetchone()
        if row is not None:
            vector = np.frombuffer(row[0], dtype="<f4").astype(np.float64)
            norm = float(np.linalg.norm(vector))
            if norm > 0 and math.isfinite(norm):
                return model, vector / norm
    return "", None
