"""The reads of the object memory, behind one small API.

The node's handlers call ObjectRepository and never see its connection. Today it reads
SQL, on the executor thread that owns the connection (ADR-0005). The object memory
will replace where live state and poses come from behind this same API, and keep
SQLite for vectors and history (ADR-0021).

No ROS here. A transform comes through the *locate* function that the node passes in.
Nothing here writes, except RememberEvent through walkie_database.events.

Every query runs as hard filters first, then ranks all that is left, then cuts to the
limit. So an object elsewhere can never take a place in the results. The ranking
constants are prototype defaults and make no claim about accuracy.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from walkie_database.events import EventRecord, NewEvent, event_record, remember_event
from walkie_database.geometry import FramePose, Quaternion, Vector
from walkie_database.migrate import schema_version
from walkie_database.object_context import (
    ATTRIBUTE_VALUES,
    BOOLEAN_ATTRIBUTES,
    INSIDE,
    ON,
    Rejected,
    current_object,
    find_events,
    relations,
    room_outline,
    usable_surfaces,
)
from walkie_database.objects import OBJECT
from walkie_database.people import unit_vector, vector_tables

STATES = ("candidate", "present", "grasping", "held", "missing", "handed_over", "discarded", "merged")
DEFAULT_STATES = ("present", "held")
_TOMBSTONES = ("discarded", "merged")
LAYERS = ("", "anchor", "movable")
NEAR, IN_ROOM = "near", "in_room"
RELATIONS = ("", ON, INSIDE, NEAR, IN_ROOM)
# Whether a superlative's largest value comes first.
_LARGEST_FIRST = {
    "largest": True,
    "smallest": False,
    "thickest": True,
    "thinnest": False,
    "heaviest": True,
    "lightest": False,
    "nearest": False,
    "farthest": True,
    "leftmost": True,
    "rightmost": False,
}
SUPERLATIVES = ("", *_LARGEST_FIRST)
_FRAME_SUPERLATIVES = ("nearest", "farthest", "leftmost", "rightmost")

MAX_RESULTS = 100
MAX_QUERY_TEXT = 512
MOVABLE_HALF_LIFE_S = 1800.0
ANCHOR_HALF_LIFE_S = 86400.0
MAX_P_NOW = 0.95
# Below this, a thing with no word match is not the thing. A labelled prototype
# value for CLIP, set per model, pending the phrase benchmark.
DEFAULT_THING_COSINE = 0.25
MAX_CAPTION = 512
MAX_PRINTED_TEXT = 256

_TOKEN = re.compile(r"\w+")
_NO_ROTATION = (0.0, 0.0, 0.0, 1.0)
_ZERO = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class ObjectFilter:
    """The fields of an ObjectQuery, with no map: a handler checks that first."""

    room: str = ""
    states: tuple[str, ...] = ()
    layer: str = ""
    thing: str = ""
    category: str = ""
    attributes: tuple[tuple[str, str], ...] = ()
    relation: str = ""
    reference_id: int = 0
    reference_key: str = ""
    near_distance_m: float = 0.6
    superlative: str = ""
    reference_frame: str = "base_footprint"


@dataclass(frozen=True)
class ObjectRecord:
    id: int
    map_id: str
    frame_id: str
    revision: int
    key: str
    label: str
    labels: tuple[str, ...]
    official_name: str
    category: str
    layer: str
    state: str
    source: str
    room_id: int
    room: str
    place: str
    place_anchor_id: int
    attributes: Mapping[str, str]
    caption: str
    printed_text: str
    caption_model: str
    prompt_version: str
    p_exist: float
    p_now: float
    last_verified: float | None
    age_s: float
    first_seen: float
    last_seen: float
    n_detections: int
    geometry_valid: bool
    geometry_stamp: float | None
    center: Vector
    rotation: Quaternion
    size: Vector
    weight_g: float | None
    in_baseline: bool
    depth_seen: bool
    merged_into: int
    split_from: int


@dataclass(frozen=True)
class Match:
    record: ObjectRecord
    score: float
    similarity: float | None = None
    matched: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    measure_unknown: bool = False


@dataclass(frozen=True)
class Matches:
    matches: list[Match] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class Prior:
    official_name: str
    category: str
    place: str
    room: str


@dataclass(frozen=True)
class SearchResult:
    matches: list[Match]
    priors: list[Prior]
    lexical_only: bool
    truncated: bool


@dataclass(frozen=True)
class RoomRecord:
    id: int
    map_id: str
    name: str
    description: str
    floor: int
    outline: list[list[float]]
    stand_pose: tuple[float, float, float] | None  # x, y and yaw in the map


@dataclass(frozen=True)
class SurfaceRecord:
    key: str
    anchor_id: int
    map_id: str
    z: float
    outline: list[list[float]]
    geometry_valid: bool


@dataclass(frozen=True)
class CloudRecord:
    n_points: int
    points: bytes  # n_points x (x, y, z) float32, little-endian
    stamp: float | None


@dataclass(frozen=True)
class CurrentObject:
    requested_id: int
    merged: bool
    record: ObjectRecord
    surfaces: list[SurfaceRecord]
    cloud: CloudRecord | None


@dataclass(frozen=True)
class StoreStatus:
    map_hash: str
    baseline_rev: str
    store_id: str
    schema_version: int
    models: list[str]


@dataclass(frozen=True)
class Snapshot:
    map_id: str
    store_id: str
    records: list[ObjectRecord]


@dataclass(frozen=True)
class _Plan:
    """What a query's filters came to, once its references were resolved."""

    states: tuple[str, ...]
    room_id: int | None = None
    members: frozenset[int] | None = None  # None means that no stored relation limits it
    near_center: Vector | None = None
    exclude_id: int | None = None
    frame: FramePose | None = None


def tokens(text: str | None) -> list[str]:
    """Whole words, with Unicode case folded."""
    return _TOKEN.findall(text.casefold()) if text else []


def belief_now(p_exist: float, layer: str, last_verified: float | None, now: float) -> tuple[float, float]:
    """The belief at read time, and the age in seconds. Age alone never changes a state.

    An unknown verification time gets a belief of 0 and an age of 0, and claims nothing.
    """
    if last_verified is None:
        return 0.0, 0.0
    age = max(0.0, now - last_verified)
    half_life = ANCHOR_HALF_LIFE_S if layer == "anchor" else MOVABLE_HALF_LIFE_S
    return min(MAX_P_NOW, max(0.0, p_exist * 0.5 ** (age / half_life))), age


def score_of(*, similarity: float | None, lexical_match: float, p_now: float, attribute_fraction: float) -> float:
    closeness = (similarity + 1.0) / 2.0 if similarity is not None else lexical_match
    return min(1.0, max(0.0, 0.6 * closeness + 0.25 * p_now + 0.15 * attribute_fraction))


def _invalid(message: str) -> Rejected:
    return Rejected("INVALID", message)


def _attribute_text(value: object) -> str:
    """A stored attribute as a request names it. A boolean is the text true or false."""
    return value if isinstance(value, str) else json.dumps(value)


def _stored_attributes(text: str, where: str) -> dict[str, str]:
    """A row's attribute column as text. A column that is no JSON object raises ValueError."""
    values = json.loads(text)
    if not isinstance(values, dict):
        raise ValueError(f"{where} has attributes that are not a JSON object")
    return {key: _attribute_text(value) for key, value in values.items()}


def _classify(
    requested: Sequence[tuple[str, str]], known: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The requested keys that match, are unknown and differ. Unknown is not a mismatch."""
    matched, unknown, mismatched = [], [], []
    for key, value in requested:
        if key not in known:
            unknown.append(key)
        elif known[key] == value:
            matched.append(key)
        else:
            mismatched.append(key)
    return tuple(matched), tuple(unknown), tuple(mismatched)


def _measure(superlative: str, record: ObjectRecord, frame: FramePose | None) -> float | None:
    """What a superlative compares, or None when this object cannot be measured."""
    if superlative in ("heaviest", "lightest"):
        return record.weight_g
    if not record.geometry_valid:
        return None
    if superlative in ("largest", "smallest"):
        return record.size[0] * record.size[1] * record.size[2]
    if superlative in ("thickest", "thinnest"):
        return min(record.size)
    x, y, z = frame.local(record.center)
    if superlative in ("nearest", "farthest"):
        return math.sqrt(x * x + y * y + z * z)
    return y  # Positive y is left.


def _by_score(match: Match) -> tuple[float, int]:
    return -match.score, match.record.id


def _ordered(matches: Iterable[Match], superlative: str, frame: FramePose | None) -> list[Match]:
    """Score, then id. A superlative orders by its measure first, with the unmeasured after."""
    if not superlative:
        return sorted(matches, key=_by_score)
    measured: list[tuple[float, Match]] = []
    unmeasured: list[Match] = []
    for match in matches:
        value = _measure(superlative, match.record, frame)
        if value is None:
            unmeasured.append(replace(match, measure_unknown=True))
        else:
            measured.append((-value if _LARGEST_FIRST[superlative] else value, match))
    measured.sort(key=lambda pair: (pair[0], *_by_score(pair[1])))
    return [match for _, match in measured] + sorted(unmeasured, key=_by_score)


class ObjectRepository:
    """Everything a query handler may ask. It holds the connection, and hands it to nobody."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        map_id: str = "",
        frame_id: str = "",
        locate: Callable[[str, float], FramePose | None] | None = None,
        reference_frames: Sequence[str] = (),
        robot_frame: str = "",
        thing_cosine: Mapping[str, float] | None = None,
    ) -> None:
        self._conn = conn
        self.map_id = map_id
        self.frame_id = frame_id
        self._locate = locate
        self._reference_frames = frozenset(reference_frames)
        self._robot_frame = robot_frame
        self._thing_cosine = dict(thing_cosine or {})

    # Rooms -------------------------------------------------------------------

    def rooms(self) -> list[RoomRecord]:
        """The map's rooms, by name."""
        rows = self._conn.execute(
            "SELECT * FROM room WHERE map_id = ? ORDER BY name", (self.map_id,)
        ).fetchall()
        return [self._room_record(row) for row in rows]

    def _room_record(self, row: sqlite3.Row) -> RoomRecord:
        known = row["x"] is not None and row["y"] is not None and row["yaw"] is not None
        return RoomRecord(
            id=row["id"],
            map_id=row["map_id"],
            name=row["name"],
            description=row["description"] or "",
            floor=row["floor"],
            outline=room_outline(self._conn, row["id"]),
            stand_pose=(row["x"], row["y"], row["yaw"]) if known else None,
        )

    def _room_named(self, name: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM room WHERE map_id = ? AND name = ?", (self.map_id, name)
        ).fetchone()
        if row is None:
            raise Rejected("NOT_FOUND", f"The map has no room named {name!r}.")
        return row

    # One object --------------------------------------------------------------

    def get_object(self, object_id: int, *, include_cloud: bool, now: float) -> CurrentObject:
        """The current row an id stands for, following a merge to its winner."""
        row = current_object(self._conn, self.map_id, object_id)
        record = self._records([row], now)[0]
        record = self._with_place(record)
        return CurrentObject(
            requested_id=object_id,
            merged=record.id != object_id,
            record=record,
            surfaces=self._surfaces(record.id),
            cloud=self._cloud(record) if include_cloud else None,
        )

    def _surfaces(self, anchor_id: int) -> list[SurfaceRecord]:
        """Every surface of an object, in key order.

        A surface is valid only when it is measured and its anchor is usable, so a
        surface of an unmeasured or discarded anchor comes back flagged as invalid.
        """
        usable = {row["id"] for row in usable_surfaces(self._conn, anchor_id)}
        rows = self._conn.execute(
            "SELECT * FROM surface WHERE anchor_id = ? ORDER BY key", (anchor_id,)
        ).fetchall()
        return [
            SurfaceRecord(
                key=row["key"],
                anchor_id=row["anchor_id"],
                map_id=row["map_id"],
                z=row["z"],
                outline=json.loads(row["outline"]),
                geometry_valid=row["id"] in usable,
            )
            for row in rows
        ]

    def _cloud(self, record: ObjectRecord) -> CloudRecord | None:
        if record.state in _TOMBSTONES or not record.geometry_valid:
            return None
        row = self._conn.execute(
            "SELECT n_points, points FROM object_cloud WHERE object_id = ?", (record.id,)
        ).fetchone()
        if row is None:
            return None
        return CloudRecord(n_points=row["n_points"], points=row["points"], stamp=record.geometry_stamp)

    # Searches ----------------------------------------------------------------

    def search(
        self,
        query: ObjectFilter,
        *,
        vector: tuple[str, Sequence[float]] | None,
        text: str,
        max_results: int,
        now: float,
    ) -> SearchResult:
        """Filter, then rank every object that passed. *vector* is a model and its values."""
        self._check_limit(max_results)
        if len(text) > MAX_QUERY_TEXT:
            raise _invalid(f"text is at most {MAX_QUERY_TEXT} characters, not {len(text)}.")
        self._check_filter(query)
        similarities, threshold = None, 0.0
        if vector is not None:
            similarities = self._similarities(*vector)
            threshold = self._thing_cosine.get(vector[0], DEFAULT_THING_COSINE)
        found = self._matches(
            query, text=text, similarities=similarities, threshold=threshold, max_results=max_results, now=now
        )
        priors, more_priors = self._priors(query, text, max_results)
        return SearchResult(
            matches=found.matches,
            priors=priors,
            lexical_only=vector is None,
            truncated=found.truncated or more_priors,
        )

    def find_near(self, query: ObjectFilter, *, max_results: int, now: float) -> Matches:
        """Objects in a relation to a reference. Near needs a box: no box is no match."""
        self._check_limit(max_results)
        if not query.relation:
            raise _invalid("FindNear needs a relation.")
        self._check_filter(query)
        return self._matches(query, max_results=max_results, now=now)

    def room_contents(
        self, query: ObjectFilter, *, max_results: int, now: float
    ) -> tuple[RoomRecord, Matches]:
        """What memory holds in one room, with the same filters as a search."""
        self._check_limit(max_results)
        if not query.room:
            raise _invalid("GetRoomContents needs a room.")
        self._check_filter(query)
        room = self._room_record(self._room_named(query.room))
        return room, self._matches(query, max_results=max_results, now=now)

    @staticmethod
    def _check_limit(max_results: int) -> None:
        if not 1 <= max_results <= MAX_RESULTS:
            raise _invalid(f"max_results must be from 1 to {MAX_RESULTS}, not {max_results}.")

    def _check_filter(self, query: ObjectFilter) -> None:
        """Refuse a query that does not make sense, before any row or transform is read."""
        unknown = sorted(set(query.states) - set(STATES))
        if unknown:
            raise _invalid(f"Unknown states: {', '.join(unknown)}.")
        if query.layer not in LAYERS:
            raise _invalid(f"layer is empty, anchor or movable, not {query.layer!r}.")
        for key, value in query.attributes:
            allowed = ("true", "false") if key in BOOLEAN_ATTRIBUTES else ATTRIBUTE_VALUES.get(key)
            if allowed is None:
                raise _invalid(f"The attribute {key!r} is not in the vocabulary.")
            if value not in allowed:
                raise _invalid(f"The attribute {key} cannot be {value!r}.")
        if query.relation not in RELATIONS:
            raise _invalid(f"relation is one of on, inside, near or in_room, not {query.relation!r}.")
        if not math.isfinite(query.near_distance_m) or query.near_distance_m <= 0:
            raise _invalid(f"near_distance_m must be finite and above 0, not {query.near_distance_m}.")
        if query.superlative not in SUPERLATIVES:
            raise _invalid(f"Unknown superlative: {query.superlative!r}.")
        if query.thing.strip() and not tokens(query.thing):
            raise _invalid(f"thing has no word to match: {query.thing!r}.")
        if query.reference_id < 0:
            raise _invalid(f"reference_id must not be negative, not {query.reference_id}.")
        if query.reference_id and query.reference_key:
            raise _invalid("Give reference_id or reference_key, not both.")
        has_reference = bool(query.reference_id or query.reference_key)
        if query.relation == IN_ROOM:
            if not query.room:
                raise _invalid("in_room needs a room.")
            if has_reference:
                raise _invalid("in_room takes a room, not an object reference.")
        elif query.relation in (ON, INSIDE):
            if not has_reference:
                raise _invalid(f"{query.relation} needs one reference.")
        elif query.relation != NEAR and has_reference:
            raise _invalid("A reference needs a relation.")
        if self._needs_frame(query) and query.reference_frame not in self._reference_frames:
            raise _invalid(f"The reference frame {query.reference_frame!r} is not allowed here.")

    @staticmethod
    def _needs_frame(query: ObjectFilter) -> bool:
        no_reference = not (query.reference_id or query.reference_key)
        return (query.relation == NEAR and no_reference) or query.superlative in _FRAME_SUPERLATIVES

    def _plan(self, query: ObjectFilter, now: float) -> _Plan | None:
        """Resolve a checked query's room, references and frame. None means nothing can match."""
        plan = _Plan(states=query.states or DEFAULT_STATES)
        if query.room:
            plan = replace(plan, room_id=self._room_named(query.room)["id"])
        if query.reference_id or query.reference_key:
            anchor, surface_id = self._reference(query)
            if query.relation == INSIDE and surface_id is not None:
                raise _invalid(f"inside needs an anchor, and {query.reference_key!r} names a surface.")
            if query.relation == NEAR:
                if anchor["state"] in _TOMBSTONES:
                    return None
                if not anchor["geometry_valid"]:
                    raise _invalid(f"Object {anchor['id']} has no measured box to be near.")
                center = (anchor["center_x"], anchor["center_y"], anchor["center_z"])
                plan = replace(plan, near_center=center, exclude_id=anchor["id"])
            else:
                members = [
                    row["src_id"]
                    for row in relations(self._conn, dst_id=anchor["id"])
                    if row["predicate"] == query.relation and (surface_id is None or row["surface_id"] == surface_id)
                ]
                if not members:
                    return None
                plan = replace(plan, members=frozenset(members))
        if self._needs_frame(query):
            frame = self._locate(query.reference_frame, now) if self._locate else None
            if frame is None:
                raise Rejected(
                    "TF_UNAVAILABLE",
                    f"No transform from {query.reference_frame} to {self.frame_id} at the query's time.",
                )
            plan = replace(plan, frame=frame)
            if query.relation == NEAR and plan.near_center is None:
                plan = replace(plan, near_center=frame.position)
        return plan

    def _reference(self, query: ObjectFilter) -> tuple[sqlite3.Row, int | None]:
        """The object a query refers to, and the surface when its key names only that."""
        if query.reference_id:
            return current_object(self._conn, self.map_id, query.reference_id), None
        anchor = self._conn.execute(
            "SELECT * FROM object WHERE map_id = ? AND key = ?", (self.map_id, query.reference_key)
        ).fetchone()
        surface = self._conn.execute(
            "SELECT id, anchor_id FROM surface WHERE map_id = ? AND key = ?", (self.map_id, query.reference_key)
        ).fetchone()
        if anchor is None and surface is None:
            raise Rejected("NOT_FOUND", f"The map has no anchor or surface with the key {query.reference_key!r}.")
        if anchor is not None:
            if surface is not None and surface["anchor_id"] != anchor["id"]:
                raise _invalid(f"The key {query.reference_key!r} names an anchor and a surface of another anchor.")
            return current_object(self._conn, self.map_id, anchor["id"]), None
        return current_object(self._conn, self.map_id, surface["anchor_id"]), surface["id"]

    def _matches(
        self,
        query: ObjectFilter,
        *,
        text: str = "",
        similarities: Mapping[int, float] | None = None,
        threshold: float = 0.0,
        max_results: int,
        now: float,
    ) -> Matches:
        plan = self._plan(query, now)
        if plan is None:
            return Matches()
        records = self._records(self._candidate_rows(query, plan), now)
        thing, words = tokens(query.thing), tokens(text)
        lexical_match = 1.0 if thing or words else 0.0
        category = query.category.casefold()
        clean: list[Match] = []
        mismatching: list[Match] = []
        for record in records:
            if plan.members is not None and record.id not in plan.members:
                continue
            if record.id == plan.exclude_id:
                continue
            if plan.near_center is not None and math.dist(record.center, plan.near_center) > query.near_distance_m:
                continue
            if category and record.category.casefold() != category:
                continue
            similarity = None if similarities is None else similarities.get(record.id)
            if not self._passes_words(record, thing, words, similarity, threshold):
                continue
            matched, unknown, mismatched = _classify(query.attributes, record.attributes)
            fraction = len(matched) / len(query.attributes) if query.attributes else 0.0
            match = Match(
                record=record,
                score=score_of(
                    similarity=similarity,
                    lexical_match=lexical_match,
                    p_now=record.p_now,
                    attribute_fraction=fraction,
                ),
                similarity=similarity,
                matched=matched,
                unknown=unknown,
                mismatched=mismatched,
            )
            (mismatching if mismatched else clean).append(match)
        # A mismatch comes back only when nothing else passed the hard filters.
        ordered = _ordered(clean or mismatching, query.superlative, plan.frame)
        kept = [
            replace(match, record=self._with_place(match.record))
            for match in ordered[:max_results]
        ]
        return Matches(matches=kept, truncated=len(ordered) > max_results)

    @staticmethod
    def _passes_words(
        record: ObjectRecord, thing: list[str], words: list[str], similarity: float | None, threshold: float
    ) -> bool:
        """Every word must match. With a vector, cosine can stand in for a thing's words."""
        names = _name_words(record)
        thing_hit = all(word in names for word in thing)
        if similarity is not None:
            return thing_hit or similarity >= threshold
        all_words = names | set(tokens(record.caption)) | set(tokens(record.printed_text))
        return thing_hit and all(word in all_words for word in words)

    def _candidate_rows(self, query: ObjectFilter, plan: _Plan) -> list[sqlite3.Row]:
        """The rows that pass the filters SQL can apply, using the box index for near."""
        marks = ", ".join("?" * len(plan.states))
        sql = f"SELECT o.* FROM object AS o WHERE o.map_id = ? AND o.state IN ({marks})"
        parameters: list[object] = [self.map_id, *plan.states]
        if query.layer:
            sql += " AND o.layer = ?"
            parameters.append(query.layer)
        if plan.room_id is not None:
            sql += " AND o.room_id = ?"
            parameters.append(plan.room_id)
        if plan.near_center is not None:
            # A box holds its centre, so a centre in range has a box that reaches this cube.
            x, y, z = plan.near_center
            reach = query.near_distance_m
            sql += """ AND o.geometry_valid = 1 AND o.id IN (
                SELECT id FROM object_box
                WHERE max_x >= ? AND min_x <= ? AND max_y >= ? AND min_y <= ? AND max_z >= ? AND min_z <= ?)"""
            parameters += [x - reach, x + reach, y - reach, y + reach, z - reach, z + reach]
        return self._conn.execute(sql, parameters).fetchall()

    def _similarities(self, model: str, values: Sequence[float]) -> dict[int, float]:
        """Exact cosine of every object of the map with a vector of this model.

        Vectors of two models are never compared. vec0's KNN would need a test that it
        keeps every candidate after the other filters, and an arena is small.
        """
        tables = vector_tables(self._conn, OBJECT)
        if not model:
            raise _invalid("use_embedding needs a model.")
        if model not in tables.lengths:
            known = ", ".join(sorted(tables.lengths)) or "none"
            raise Rejected("UNSUPPORTED", f"The store has no vector table for {model!r}. Known models: {known}.")
        embedding = unit_vector(values, model=model, tables=tables, kind=OBJECT, where="The query vector")
        rows = self._conn.execute(
            f'SELECT object_id, vec_distance_cosine(embedding, ?) FROM "object_{model}" WHERE map_id = ?',
            (embedding, self.map_id),
        )
        return {row[0]: min(1.0, max(-1.0, 1.0 - row[1])) for row in rows}

    def _records(self, rows: Iterable[sqlite3.Row], now: float) -> list[ObjectRecord]:
        rows = list(rows)
        if not rows:
            return []
        catalogue = {row["id"]: row for row in self._conn.execute(
            "SELECT * FROM object_catalogue WHERE map_id = ?", (self.map_id,)
        )}
        aliases: dict[int, list[str]] = {}
        for row in self._conn.execute(
            """SELECT a.catalogue_id, a.alias FROM catalogue_alias AS a
               JOIN object_catalogue AS c ON c.id = a.catalogue_id WHERE c.map_id = ?""",
            (self.map_id,),
        ):
            aliases.setdefault(row["catalogue_id"], []).append(row["alias"])
        labels: dict[int, set[str]] = {}
        for row in self._conn.execute(
            """SELECT l.object_id, l.label FROM object_label AS l
               JOIN object AS o ON o.id = l.object_id WHERE o.map_id = ?""",
            (self.map_id,),
        ):
            labels.setdefault(row["object_id"], set()).add(row["label"])
        rooms = {row["id"]: row["name"] for row in self._conn.execute(
            "SELECT id, name FROM room WHERE map_id = ?", (self.map_id,)
        )}
        return [self._record(row, catalogue, aliases, labels, rooms, now) for row in rows]

    def _record(
        self,
        row: sqlite3.Row,
        catalogue: Mapping[int, sqlite3.Row],
        aliases: Mapping[int, list[str]],
        labels: Mapping[int, set[str]],
        rooms: Mapping[int, str],
        now: float,
    ) -> ObjectRecord:
        item = catalogue.get(row["catalogue_id"])
        attributes = _stored_attributes(item["attributes"], f"catalogue item {item['id']}") if item else {}
        attributes.update(_stored_attributes(row["attributes"], f"object {row['id']}"))
        names = {row["label"], *labels.get(row["id"], ()), *aliases.get(row["catalogue_id"], ())}
        valid = bool(row["geometry_valid"])
        p_now, age = belief_now(row["p_exist"], row["layer"], row["last_verified"], now)
        return ObjectRecord(
            id=row["id"],
            map_id=row["map_id"],
            frame_id=self.frame_id,
            revision=row["revision"],
            key=row["key"] or "",
            label=row["label"],
            labels=tuple(sorted(names)),
            official_name=row["official_name"] or (item["official_name"] if item else ""),
            category=row["category"] or (item["category"] if item and item["category"] else ""),
            layer=row["layer"],
            state=row["state"],
            source=row["source"],
            room_id=row["room_id"] or 0,
            room=rooms.get(row["room_id"], ""),
            place="",
            place_anchor_id=0,
            attributes=attributes,
            caption=(row["caption"] or "")[:MAX_CAPTION],
            printed_text=(row["printed_text"] or "")[:MAX_PRINTED_TEXT],
            caption_model=row["caption_model"] or "",
            prompt_version=row["prompt_version"] or "",
            p_exist=row["p_exist"],
            p_now=p_now,
            last_verified=row["last_verified"],
            age_s=age,
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            n_detections=row["n_detections"],
            geometry_valid=valid,
            geometry_stamp=row["geometry_stamp"] if valid else None,
            center=(row["center_x"], row["center_y"], row["center_z"]) if valid else _ZERO,
            rotation=(row["qx"], row["qy"], row["qz"], row["qw"]) if valid else _NO_ROTATION,
            size=(row["size_x"], row["size_y"], row["size_z"]) if valid else _ZERO,
            # Only ever measured catalogue data, never inferred from an image.
            weight_g=item["weight_g"] if item else None,
            in_baseline=bool(row["in_baseline"]),
            depth_seen=bool(row["depth_seen"]),
            merged_into=row["merged_into"] or 0,
            split_from=row["split_from"] or 0,
        )

    def _with_place(self, record: ObjectRecord) -> ObjectRecord:
        """The record with its stored place: the heaviest relation, then the newest, then the lowest ids.

        ``place`` is the relation's surface key, else its anchor's key, else empty.
        ``place_anchor_id`` is the anchor's id whenever there is a relation, so an
        anchor without a key is still named.
        """
        rows = relations(self._conn, src_id=record.id)
        if not rows:
            return record
        best = min(rows, key=lambda r: (-r["weight"], -r["updated_at"], r["dst_id"], r["surface_id"] or 0))
        place = best["surface_key"]
        if not place:
            anchor = self._conn.execute("SELECT key FROM object WHERE id = ?", (best["dst_id"],)).fetchone()
            place = anchor["key"] or ""
        return replace(record, place=place, place_anchor_id=best["dst_id"])

    def _priors(self, query: ObjectFilter, text: str, max_results: int) -> tuple[list[Prior], bool]:
        """Catalogue items that no object stands for yet, with where they usually are.

        Separate from objects: a prior is not an observation and has no id. An anchor
        or a stored relation cannot be a prior, so those queries have none.
        """
        if query.layer == "anchor" or query.relation:
            return [], False
        rows = self._conn.execute(
            """SELECT c.* FROM object_catalogue AS c
               WHERE c.map_id = ? AND NOT EXISTS (
                   SELECT 1 FROM object AS o
                   WHERE o.catalogue_id = c.id AND o.state NOT IN ('discarded', 'merged'))
               ORDER BY c.official_name""",
            (self.map_id,),
        ).fetchall()
        if not rows:
            return [], False
        aliases: dict[int, list[str]] = {}
        for row in self._conn.execute(
            """SELECT a.catalogue_id, a.alias FROM catalogue_alias AS a
               JOIN object_catalogue AS c ON c.id = a.catalogue_id WHERE c.map_id = ?""",
            (self.map_id,),
        ):
            aliases.setdefault(row["catalogue_id"], []).append(row["alias"])
        rooms = self._rooms_by_place()
        wanted = [*tokens(query.thing), *tokens(text)]
        category = query.category.casefold()
        priors: list[Prior] = []
        for row in rows:
            names = {
                word
                for name in (row["official_name"], row["category"], *aliases.get(row["id"], ()))
                for word in tokens(name)
            }
            if not all(word in names for word in wanted):
                continue
            if category and (row["category"] or "").casefold() != category:
                continue
            known = _stored_attributes(row["attributes"], f"catalogue item {row['id']}")
            if _classify(query.attributes, known)[2]:
                continue
            room = rooms.get(row["default_place"], "")
            if query.room and room != query.room:
                continue
            priors.append(
                Prior(
                    official_name=row["official_name"],
                    category=row["category"] or "",
                    place=row["default_place"] or "",
                    room=room,
                )
            )
        return priors[:max_results], len(priors) > max_results

    def _rooms_by_place(self) -> dict[str, str]:
        """The room name of each surface key and anchor key. An anchor's key wins."""
        places = {
            row["key"]: row["name"]
            for row in self._conn.execute(
                """SELECT s.key, r.name FROM surface AS s
                   JOIN object AS o ON o.id = s.anchor_id JOIN room AS r ON r.id = o.room_id
                   WHERE s.map_id = ?""",
                (self.map_id,),
            )
        }
        for row in self._conn.execute(
            """SELECT o.key, r.name FROM object AS o JOIN room AS r ON r.id = o.room_id
               WHERE o.map_id = ? AND o.key IS NOT NULL""",
            (self.map_id,),
        ):
            places[row["key"]] = row["name"]
        return places

    # Events ------------------------------------------------------------------

    def remember_event(self, new: NewEvent, *, now: float) -> EventRecord:
        def locate() -> FramePose | None:
            return self._locate(self._robot_frame, now) if self._locate else None

        return remember_event(self._conn, new, map_id=self.map_id, now=now, locate=locate)

    def find_events(
        self, *, text: str, since: float | None, room: str, max_results: int
    ) -> tuple[list[EventRecord], bool]:
        if len(text) > MAX_QUERY_TEXT:
            raise _invalid(f"text is at most {MAX_QUERY_TEXT} characters, not {len(text)}.")
        room_id = self._room_named(room)["id"] if room else None
        rows, truncated = find_events(
            self._conn, self.map_id, text=text, since=since, room_id=room_id, max_results=max_results
        )
        return [event_record(row) for row in rows], truncated

    # The store ---------------------------------------------------------------

    def store_status(self) -> StoreStatus:
        row = self._conn.execute(
            "SELECT map_hash, baseline_rev FROM map WHERE id = ?", (self.map_id,)
        ).fetchone()
        meta = self._conn.execute("SELECT store_id FROM store_meta").fetchone()
        # A legacy store's map rows may have no hash or baseline.
        return StoreStatus(
            map_hash=(row["map_hash"] or "") if row else "",
            baseline_rev=(row["baseline_rev"] or "") if row else "",
            store_id=(meta["store_id"] or "") if meta else "",
            schema_version=schema_version(self._conn),
            models=sorted(vector_tables(self._conn, OBJECT).lengths),
        )

    def snapshot(self, now: float) -> Snapshot:
        """Every object in the scene. A tombstone is not in it."""
        rows = self._conn.execute(
            """SELECT * FROM object WHERE map_id = ? AND state NOT IN ('discarded', 'merged')
               ORDER BY id""",
            (self.map_id,),
        ).fetchall()
        return Snapshot(
            map_id=self.map_id,
            store_id=self.store_status().store_id,
            records=self._records(rows, now),
        )


def _name_words(record: ObjectRecord) -> set[str]:
    """The words a thing can match: the label set, official name and category."""
    return {
        word
        for name in (*record.labels, record.official_name, record.category)
        for word in tokens(name)
    }
