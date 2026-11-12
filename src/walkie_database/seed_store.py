"""Build a store's seed: one map, its rooms, its anchors and its catalogue.

A seed holds what crosses from one test to the next, and nothing else. It has no
movable objects, sessions, people, conversations, events, observations or request
ledger (ADR-0023). This is not the sample house: ``walkie_database.seed`` writes
that, with made-up people and vectors, and nothing here reads it.

Every path is an argument, and nothing has a default location::

    python -m walkie_database.seed_store --map-folder PATH --arena NAME \\
        --date YYYY-MM-DD --rooms PATH --anchors PATH --catalogue PATH \\
        --clock-kind wall|ros_sim --output PATH [--previous-seed PATH] \\
        [--id-floor N] [--replace]

The inputs are YAML or JSON. All of them are read and checked before anything is
written, so a bad input leaves no output, temporary file or lock file. The map ID
is computed from the map folder, never given.

An anchor keeps its ID when a new seed is built from a previous one. A new key gets
an ID above the previous high-water mark. An anchor that is gone stays as a
tombstone, so its ID is never given to anything else.

A previous seed does not know the IDs that a store restored from it has given out
since. ``--id-floor N`` gives new keys IDs above N as well, and makes the seed's
object counter at least N. N is the store's high-water mark, which the restore
tool names when it refuses a seed that would give one of those IDs again. The
default, 0, changes nothing. The floor moves only keys that the previous seed
lacks, so the rebuild must start from the seed the store was restored from, not
from the refused one. For that reason ``--output`` may not be the previous seed.

The same inputs and the same previous seed give the same rows. So ``created_at``
comes from the date and the seed's ``store_id`` is a constant. The restore tool and
the node each write a fresh ``store_id``, because a seed is not a run.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path

from walkie_database.db import MEMORY, connect, transaction
from walkie_database.events import REMEMBER_EVENT
from walkie_database.map_config import (
    CLOCK_KINDS,
    FRAME_ID,
    MapFolder,
    MapGateError,
    check_baseline,
    parse_yaml_mapping,
    read_map_folder,
)
from walkie_database.migrate import load_migrations, migrate, schema_version
from walkie_database.object_context import (
    add_catalogue_item,
    add_surface,
    check_attributes,
    open_ring,
    set_room_shape,
)
from walkie_database.objects import (
    OBJECT,
    NewObject,
    ObjectBox,
    insert_object,
    reserve_object_ids,
    tombstone_object,
    write_object_vector,
)
from walkie_database.people import Refused, unit_vector, vector_tables
from walkie_database.store_lock import (
    StoreLock,
    StoreLocked,
    checkpoint_closed_store,
    discard_temp,
    install_store,
    real_path,
    temp_beside,
)
from walkie_map_id import make_map_id

SCHEMA_VERSION = 1  # of the rooms, anchors and catalogue files
SEED_STORE_ID = "seed"  # replaced by the restore tool and on every node start
COUNTERS = ("object", "person", "session", "event")
LARGEST_ID = 2**63 - 1  # SQLite's largest INTEGER
SEED_P_EXIST = 0.95  # what migration 0004 gives a map object
GONE_P_EXIST = 0.05
FINGERPRINT_WEIGHT = 1.0  # one real crop
_FAKE_MODEL_PREFIX = "fake_"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
# The oldest schema with object keys, last_id for objects and store_meta.
_OLDEST_LINEAGE_SCHEMA = 4
# A seed has no rows in these. Vectors are not listed: a seed may hold real ones.
RUN_TABLES = (
    "session", "person", "face", "appearance", "conversation", "message",
    "object_observation", "object_cloud", "object_relation", "object_event",
    "event", "write_request",
)


# Reading the inputs ----------------------------------------------------------


def read_document(path: str | Path, what: str) -> dict[str, object]:
    """The mapping in a YAML or JSON file, or Refused."""
    try:
        return parse_yaml_mapping(Path(path).read_bytes(), f"The {what} file {path}")
    except OSError as error:
        raise Refused(f"The {what} file cannot be read: {error}") from error
    except ValueError as error:
        raise Refused(str(error)) from error


def check_fields(item: object, where: str, required: Sequence[str], optional: Sequence[str] = ()) -> Mapping:
    """*item* as a mapping with every required key and no unknown one, or Refused."""
    if not isinstance(item, Mapping):
        raise Refused(f"{where} must be a mapping.")
    missing = [key for key in required if key not in item]
    if missing:
        raise Refused(f"{where} is missing {', '.join(missing)}.")
    unknown = [str(key) for key in item if key not in (*required, *optional)]
    if unknown:
        raise Refused(f"{where} has the unknown key {', '.join(unknown)}.")
    return item


def _key(value: object, where: str) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise Refused(f"{where} has the key {value!r}. A key looks like living_room.")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{where} must be text that is not blank.")
    return value


def _any_text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise Refused(f"{where} must be text.")
    return value


def _optional_text(value: object, where: str) -> str | None:
    return None if value is None else _any_text(value, where)


def _flag(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise Refused(f"{where} must be true or false.")
    return value


def _number(value: object, where: str) -> float:
    # float("1.5") and float(True) both work, so check the type first.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused(f"{where} must be a number, not {value!r}.")
    try:
        number = float(value)
    except OverflowError:
        raise Refused(f"{where} is too large.") from None
    if not math.isfinite(number):
        raise Refused(f"{where} has a NaN or infinite value.")
    return number


def _numbers(value: object, count: int, where: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != count:
        raise Refused(f"{where} must be a list of {count} numbers.")
    return tuple(_number(item, where) for item in value)


def _items(value: object, where: str) -> Sequence:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise Refused(f"{where} must be a list.")
    return value


def _outline(value: object, where: str) -> list[list[float]]:
    """A polygon as corners with no repeated last corner, or Refused."""
    corners = [_numbers(corner, 2, where) for corner in _items(value, where)]
    return open_ring(corners, where)


def _header(document: Mapping, what: str, map_id: str | None) -> None:
    version = document["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise Refused(f"The {what} file has schema_version {version!r}, not {SCHEMA_VERSION}.")
    if map_id is not None and document["map_id"] != map_id:
        raise Refused(f"The {what} file names the map {document['map_id']!r}, not {map_id!r}.")


def _unique(values: Sequence[str], what: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise Refused(f"{what} {value!r} appears twice.")
        seen.add(value)


def _contains(outline: list[list[float]], x: float, y: float) -> bool:
    """Whether a point is in an outline, by the same geopoly test as room_at."""
    with closing(sqlite3.connect(MEMORY)) as conn:
        inside = conn.execute(
            "SELECT geopoly_contains_point(geopoly_ccw(?), ?, ?)",
            (json.dumps([*outline, outline[0]]), x, y),
        ).fetchone()[0]
    return bool(inside)


@dataclass(frozen=True)
class Room:
    key: str
    name: str
    description: str
    floor: int
    outline: list[list[float]]
    stand_pose: tuple[float, float, float]


def parse_rooms(document: Mapping, map_id: str) -> list[Room]:
    """Every room, sorted by key, or Refused. A seed has at least one room."""
    check_fields(document, "The rooms file", ("schema_version", "map_id", "rooms"))
    _header(document, "rooms", map_id)
    rooms = []
    for i, item in enumerate(_items(document["rooms"], "rooms")):
        where = f"rooms[{i}]"
        check_fields(item, where, ("key", "name", "description", "floor", "polygon", "stand_pose"))
        key = _key(item["key"], where)
        where = f"Room {key}"
        floor = item["floor"]
        if isinstance(floor, bool) or not isinstance(floor, int):
            raise Refused(f"{where} must have a whole number as its floor.")
        outline = _outline(item["polygon"], f"The polygon of room {key}")
        pose = _numbers(item["stand_pose"], 3, f"The stand pose of room {key}")
        if not _contains(outline, pose[0], pose[1]):
            raise Refused(f"The stand pose of room {key} is outside its polygon.")
        rooms.append(
            Room(key, _text(item["name"], f"The name of room {key}"),
                 _any_text(item["description"], f"The description of room {key}"),
                 floor, outline, pose)
        )
    if not rooms:
        raise Refused("The rooms file lists no rooms, and a seed needs at least one.")
    _unique([room.key for room in rooms], "The room key")
    _unique([room.name for room in rooms], "The room name")
    return sorted(rooms, key=lambda room: room.key)


@dataclass(frozen=True)
class Surface:
    key: str
    z: float
    outline: list[list[float]]  # empty: known by name, not measured


@dataclass(frozen=True)
class Fingerprint:
    model: str
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class Anchor:
    key: str
    label: str
    room_key: str
    box: ObjectBox | None  # None: no measured geometry
    enclosed: bool
    surfaces: list[Surface]
    fingerprint: Fingerprint | None
    official_name: str | None = None  # the task-level name, as "coffee table" for a "table"


def _parse_box(item: Mapping, key: str) -> ObjectBox | None:
    """The anchor's measured box. A geometryless anchor gives no numbers at all.

    Nothing turns a stand pose or a flat polygon into a box, so a number here with
    geometry_valid false would be dropped silently. It is refused instead.
    """
    where = f"Anchor {key}"
    if not _flag(item["geometry_valid"], f"geometry_valid of anchor {key}"):
        given = [name for name in ("center", "size", "yaw") if item.get(name) is not None]
        if given:
            raise Refused(f"{where} has geometry_valid false, and gives {', '.join(given)}.")
        return None
    for name in ("center", "size", "yaw"):
        if item.get(name) is None:
            raise Refused(f"{where} has geometry_valid true, and gives no {name}.")
    center = _numbers(item["center"], 3, f"The center of anchor {key}")
    size = _numbers(item["size"], 3, f"The size of anchor {key}")
    yaw = _number(item["yaw"], f"The yaw of anchor {key}")
    if any(side <= 0 for side in size):
        raise Refused(f"{where} has geometry_valid true, and a size that is not positive.")
    return ObjectBox(center, size, (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)))


def _parse_fingerprint(value: object, key: str) -> Fingerprint:
    """A real crop's vector, or Refused.

    This checks the form of the provenance and never runs a model, so it cannot
    prove that the hashes are honest. The reviewer of the input file does that.
    """
    where = f"The fingerprint of anchor {key}"
    item = check_fields(value, where, ("model", "dimension", "embedding", "provenance"))
    model = _text(item["model"], f"The model of {where.lower()}")
    if model.startswith(_FAKE_MODEL_PREFIX):
        raise Refused(f"{where} names the fake model {model!r}, which a real store refuses.")
    provenance = check_fields(item["provenance"], f"The provenance of anchor {key}",
                              ("weights_sha256", "crop_sha256"))
    for name in ("weights_sha256", "crop_sha256"):
        if not isinstance(provenance[name], str) or _SHA256.fullmatch(provenance[name]) is None:
            raise Refused(f"The provenance of anchor {key} needs {name} as a SHA-256.")
    dimension = item["dimension"]
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
        raise Refused(f"{where} needs a positive whole dimension.")
    return Fingerprint(model, _numbers(item["embedding"], dimension, where))


def _parse_surface(value: object, key: str, i: int) -> Surface:
    where = f"surfaces[{i}] of anchor {key}"
    item = check_fields(value, where, ("key", "z", "outline"))
    surface_key = _key(item["key"], where)
    z = _number(item["z"], f"The height of surface {surface_key}")
    corners = _items(item["outline"], f"The outline of surface {surface_key}")
    outline = _outline(corners, f"The outline of surface {surface_key}") if corners else []
    return Surface(surface_key, z, outline)


def parse_anchors(document: Mapping, map_id: str, rooms: Sequence[Room]) -> tuple[str, list[Anchor]]:
    """The baseline revision and every anchor, sorted by key, or Refused."""
    check_fields(document, "The anchors file", ("schema_version", "map_id", "baseline_rev", "anchors"))
    _header(document, "anchors", map_id)
    baseline_rev = _any_text(document["baseline_rev"], "baseline_rev")
    room_keys = {room.key for room in rooms}
    anchors = []
    for i, item in enumerate(_items(document["anchors"], "anchors")):
        where = f"anchors[{i}]"
        check_fields(item, where, ("key", "label", "room_key", "geometry_valid", "enclosed", "surfaces"),
                     ("center", "size", "yaw", "fingerprint", "official_name"))
        key = _key(item["key"], where)
        if item["room_key"] not in room_keys:
            raise Refused(f"Anchor {key} names the room {item['room_key']!r}, which is not listed.")
        surfaces = [
            _parse_surface(surface, key, j)
            for j, surface in enumerate(_items(item["surfaces"], f"surfaces of anchor {key}"))
        ]
        fingerprint = item.get("fingerprint")
        # Absent means no name. Given, even as null, it must be text, as in the furniture file.
        official_name = (_text(item["official_name"], f"The official name of anchor {key}")
                         if "official_name" in item else None)
        anchors.append(
            Anchor(key, _text(item["label"], f"The label of anchor {key}"), item["room_key"],
                   _parse_box(item, key), _flag(item["enclosed"], f"enclosed of anchor {key}"),
                   sorted(surfaces, key=lambda surface: surface.key),
                   None if fingerprint is None else _parse_fingerprint(fingerprint, key),
                   official_name)
        )
    _unique([anchor.key for anchor in anchors], "The anchor key")
    _unique([s.key for anchor in anchors for s in anchor.surfaces], "The surface key")
    return baseline_rev, sorted(anchors, key=lambda anchor: anchor.key)


@dataclass(frozen=True)
class CatalogueItem:
    official_name: str
    category: str | None
    aliases: list[str]
    default_place: str | None
    weight_g: float | None  # None: not measured. A label never implies a weight.
    attributes: dict[str, object]


def parse_catalogue(document: Mapping) -> list[CatalogueItem]:
    """Every catalogue item, sorted by official name, or Refused."""
    check_fields(document, "The catalogue file", ("schema_version", "objects"))
    _header(document, "catalogue", None)
    items = []
    for i, item in enumerate(_items(document["objects"], "objects")):
        where = f"objects[{i}]"
        check_fields(item, where, ("official_name",),
                     ("category", "aliases", "default_place", "weight_g", "attributes"))
        name = _text(item["official_name"], f"The official name of {where}")
        where = f"Catalogue item {name}"
        weight = item.get("weight_g")
        if weight is not None:
            weight = _number(weight, f"The weight of {where}")
            if weight <= 0:
                raise Refused(f"{where} has a weight that is not positive.")
        aliases = [
            _text(alias, f"An alias of {where}")
            for alias in _items(item.get("aliases", []), f"The aliases of {where}")
        ]
        items.append(
            CatalogueItem(name, _optional_text(item.get("category"), f"The category of {where}"),
                          aliases,
                          _optional_text(item.get("default_place"), f"The default place of {where}"),
                          weight, check_attributes(item.get("attributes", {}), f"{where}'s attributes"))
        )
    _unique([item.official_name for item in items], "The official name")
    return sorted(items, key=lambda item: item.official_name)


# The previous seed -----------------------------------------------------------


@dataclass(frozen=True)
class PreviousAnchor:
    id: int
    label: str
    room_key: str | None
    official_name: str | None = None


@dataclass(frozen=True)
class Lineage:
    """What a new seed inherits, so that no ID is ever given twice."""

    counters: dict[str, int]
    anchors: dict[str, PreviousAnchor]


def read_counters(conn: sqlite3.Connection) -> dict[str, int]:
    """The largest ID ever given in each table of COUNTERS, from ``last_id``.

    A store from before migration 0007 has no event counter. It gets the value that
    migration starts the counter at: the largest event ID in the log or in a
    ``remember_event`` outcome of the ledger. A store from before migration 0006 has
    neither table, and so has no event IDs.
    """
    counters = {row["table_name"]: row["id"] for row in conn.execute("SELECT * FROM last_id")}
    has_log = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event'").fetchone()
    if "event" not in counters and has_log:
        counters["event"] = conn.execute(
            """SELECT max((SELECT coalesce(max(id), 0) FROM event),
                          (SELECT coalesce(max(json_extract(outcome_json, '$.event_id')), 0)
                           FROM write_request
                           WHERE operation = ? AND json_type(outcome_json, '$.event_id') = 'integer'))""",
            (REMEMBER_EVENT,),
        ).fetchone()[0]
    return {name: counters.get(name, 0) for name in COUNTERS}


def read_lineage(path: Path, map_id: str, digest: str) -> Lineage:
    """The anchor IDs and the counters of a previous seed of this map, or Refused."""
    if not path.is_file():
        raise Refused(f"The previous seed {path} is not a file.")
    with closing(connect(path, readonly=True)) as conn:
        version = schema_version(conn)
        if not _OLDEST_LINEAGE_SCHEMA <= version <= load_migrations()[-1].version:
            raise Refused(f"The previous seed {path} is at schema {version}, which this code cannot read.")
        meta = conn.execute("SELECT map_id FROM store_meta WHERE id = 1").fetchone()
        row = conn.execute(
            "SELECT map_hash FROM map WHERE id = ?", (meta["map_id"] if meta else "",)
        ).fetchone()
        if row is None or meta["map_id"] != map_id or row["map_hash"] != digest:
            raise Refused(
                f"The previous seed {path} is not of the map {map_id}. "
                "A new map starts a new lineage, so build its seed without --previous-seed."
            )
        counters = read_counters(conn)
        room_key = "r.key" if version >= 6 else "NULL"
        anchors = {
            r["key"]: PreviousAnchor(r["id"], r["label"], r["room_key"], r["official_name"])
            for r in conn.execute(
                f"""SELECT o.id, o.key, o.label, {room_key} AS room_key, o.official_name
                    FROM object AS o LEFT JOIN room AS r ON r.id = o.room_id
                    WHERE o.map_id = ? AND o.layer = 'anchor' AND o.key IS NOT NULL""",
                (map_id,),
            )
        }
    return Lineage(counters, anchors)


def check_seed_contents(conn: sqlite3.Connection, what: object) -> None:
    """Refuse a store that holds anything a seed must not hold.

    An older schema lacks some of the tables, and so has no such rows.
    """
    marks = ", ".join("?" * len(RUN_TABLES))
    present = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({marks})", RUN_TABLES
    ).fetchall()
    for table in sorted(row["name"] for row in present):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise Refused(f"{what} is not a seed: its {table} table has rows.")
    if conn.execute("SELECT 1 FROM object WHERE layer != 'anchor' LIMIT 1").fetchone() is not None:
        raise Refused(f"{what} is not a seed: it holds movable objects.")


def _check_output(output: Path, map_id: str, previous: Path | None, replace: bool) -> None:
    """Refuse an output path that this run must not overwrite.

    --replace only ever replaces a seed, so it cannot destroy a robot's memory. A
    seed of the same map needs --previous-seed too. Without it, IDs would start
    over, and an anchor would get an ID that another anchor had.
    """
    if not output.exists():
        return
    if not replace:
        raise Refused(f"{output} exists. Pass --replace to replace it.")
    if not output.is_file():
        raise Refused(f"{output} is not a file.")
    try:
        with closing(connect(output, readonly=True)) as conn:
            meta = conn.execute("SELECT map_id FROM store_meta WHERE id = 1").fetchone()
            if meta is None:
                raise Refused(f"{output} is not a seed: it has no store_meta row.")
            check_seed_contents(conn, output)
    except sqlite3.Error as error:
        raise Refused(f"{output} is not a seed, so it stays: {error}") from error
    if meta["map_id"] == map_id and previous is None:
        raise Refused(
            f"{output} is a seed of the map {map_id}. Pass --previous-seed so that its "
            "anchors keep their IDs."
        )


# Building the seed -----------------------------------------------------------


def _build(
    conn: sqlite3.Connection,
    *,
    map_id: str,
    digest: str,
    arena: str,
    date: str,
    clock_kind: str,
    baseline_rev: str,
    rooms: Sequence[Room],
    anchors: Sequence[Anchor],
    catalogue: Sequence[CatalogueItem],
    lineage: Lineage,
    id_floor: int = 0,
) -> None:
    """Fill a new store from checked inputs and the old key-to-ID map, in one transaction.

    New keys get IDs above both the previous seed's object counter and *id_floor*.
    """
    counters = {**lineage.counters, "object": max(lineage.counters["object"], id_floor)}
    for name, value in counters.items():
        conn.execute("UPDATE last_id SET id = ? WHERE table_name = ?", (value, name))
    kept = {key: previous.id for key, previous in lineage.anchors.items()}
    new_keys = [anchor.key for anchor in anchors if anchor.key not in kept]
    reservation = reserve_object_ids(conn, len(new_keys)) if new_keys else None
    new_ids = dict(zip(new_keys, reservation or ()))
    created_at = datetime.datetime.fromisoformat(date).replace(tzinfo=datetime.UTC).timestamp()

    with transaction(conn):
        conn.execute(
            """INSERT INTO map (id, frame_id, created_at, description, map_hash, baseline_rev, clock_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (map_id, FRAME_ID, created_at, f"Seed of {arena}, mapped {date}", digest, baseline_rev,
             clock_kind),
        )
        conn.execute(
            """INSERT INTO store_meta (id, store_id, clock_kind, map_id, created_at, refresh_anchors_pending)
               VALUES (1, ?, ?, ?, ?, 1)""",
            (SEED_STORE_ID, clock_kind, map_id, created_at),
        )
        room_ids = {}
        for room in rooms:
            room_ids[room.key] = conn.execute(
                """INSERT INTO room (map_id, key, name, description, floor, x, y, yaw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (map_id, room.key, room.name, room.description, room.floor, *room.stand_pose),
            ).lastrowid
            set_room_shape(conn, room_ids[room.key], room.outline)

        for anchor in anchors:
            anchor_id = kept.get(anchor.key, new_ids.get(anchor.key))
            # A seed has no clock, so the times stay 0 until the node's first
            # clock sample stamps them (refresh_anchors_pending).
            insert_object(
                conn,
                NewObject(
                    map_id, anchor.label, 0.0, 0.0, room_id=room_ids[anchor.room_key], source="map",
                    key=anchor.key, layer="anchor", state="present", p_exist=SEED_P_EXIST,
                    box=anchor.box,
                ),
                anchor_id,
                reservation=reservation,
                retained_ids=kept.values(),
            )
            conn.execute(
                """UPDATE object SET enclosed = ?, in_baseline = ?, n_detections = 0, official_name = ?
                   WHERE id = ?""",
                (int(anchor.enclosed), int(bool(baseline_rev)), anchor.official_name, anchor_id),
            )
            for surface in anchor.surfaces:
                add_surface(conn, anchor_id, surface.key, surface.z, surface.outline,
                            geometry_valid=bool(surface.outline))
            if anchor.fingerprint is not None:
                write_object_vector(conn, anchor_id, anchor.fingerprint.model, anchor.fingerprint.embedding)
                conn.execute("UPDATE object SET fingerprint_weight = ? WHERE id = ?",
                             (FINGERPRINT_WEIGHT, anchor_id))

        listed = {anchor.key for anchor in anchors}
        for key, previous in sorted(lineage.anchors.items()):
            if key in listed:
                continue
            insert_object(
                conn,
                NewObject(
                    map_id, previous.label, 0.0, 0.0, room_id=room_ids.get(previous.room_key),
                    source="map", key=key, layer="anchor", state="present", p_exist=GONE_P_EXIST,
                ),
                previous.id,
                retained_ids=kept.values(),
            )
            conn.execute("UPDATE object SET n_detections = 0, official_name = ? WHERE id = ?",
                         (previous.official_name, previous.id))
            tombstone_object(conn, previous.id)

        for item in catalogue:
            add_catalogue_item(
                conn, map_id, item.official_name, category=item.category,
                default_place=item.default_place, weight_g=item.weight_g,
                attributes=item.attributes, aliases=item.aliases,
            )


def _check_fingerprints(anchors: Sequence[Anchor]) -> None:
    """Refuse a vector this schema has no table for, before anything is written."""
    fingerprints = [(a.key, a.fingerprint) for a in anchors if a.fingerprint is not None]
    if not fingerprints:
        return
    with closing(connect(MEMORY)) as conn:
        migrate(conn)
        tables = vector_tables(conn, OBJECT)
    for key, fingerprint in fingerprints:
        unit_vector(fingerprint.embedding, model=fingerprint.model, tables=tables, kind=OBJECT,
                    where=f"The fingerprint of anchor {key}")


def identify_map(map_folder: str | Path, arena: str, date: str) -> tuple[MapFolder, str]:
    """The map folder and the map ID computed from it, or an error that says why not."""
    folder = read_map_folder(map_folder)
    try:
        return folder, make_map_id(arena, date, folder.digest)
    except ValueError as error:
        raise Refused(f"The map ID cannot be computed: {error}") from error


def empty_anchors(map_id: str) -> dict[str, object]:
    """The anchors document of a rooms-only seed."""
    return {"schema_version": SCHEMA_VERSION, "map_id": map_id, "baseline_rev": "", "anchors": []}


def empty_catalogue() -> dict[str, object]:
    """The catalogue document of a rooms-only seed."""
    return {"schema_version": SCHEMA_VERSION, "objects": []}


@dataclass(frozen=True)
class SeedSummary:
    output: Path
    map_id: str
    rooms: int
    anchors: int
    tombstones: int
    catalogue: int


def write_seed(
    *,
    folder: MapFolder,
    map_id: str,
    arena: str,
    date: str,
    clock_kind: str,
    rooms_document: Mapping,
    anchors_document: Mapping,
    catalogue_document: Mapping,
    output: str | Path,
    previous_seed: str | Path | None = None,
    replace: bool = False,
    id_floor: int = 0,
) -> SeedSummary:
    """Check every input, then write the seed at *output*, or raise and write nothing.

    New anchors get IDs above *id_floor*, as well as above the previous seed's.
    """
    if clock_kind not in CLOCK_KINDS:
        raise Refused(f"The clock kind {clock_kind!r} is not one of {', '.join(CLOCK_KINDS)}.")
    if isinstance(id_floor, bool) or not isinstance(id_floor, int) or not 0 <= id_floor <= LARGEST_ID:
        raise Refused(f"The ID floor must be a whole number from 0 to {LARGEST_ID}, not {id_floor!r}.")
    rooms = parse_rooms(rooms_document, map_id)
    baseline_rev, anchors = parse_anchors(anchors_document, map_id, rooms)
    catalogue = parse_catalogue(catalogue_document)
    _check_fingerprints(anchors)
    check_baseline(baseline_rev, folder, map_id)
    target = real_path(output)
    previous = real_path(previous_seed) if previous_seed is not None else None
    if previous is not None and (
        previous == target or (previous.exists() and target.exists() and previous.samefile(target))
    ):
        raise Refused(
            f"The output {target} is the previous seed. Write the new seed to another path and keep "
            "this one: if a restore refuses the new seed, the rebuild with --id-floor starts from it again."
        )
    # Read before any lock, because a lock file is a write.
    _check_output(target, map_id, previous, replace)
    if previous is not None:
        read_lineage(previous, map_id, folder.digest)

    with (nullcontext() if previous is None else StoreLock(previous)), StoreLock(target):
        lineage = Lineage({name: 0 for name in COUNTERS}, {})
        if previous is not None:
            checkpoint_closed_store(previous)
            lineage = read_lineage(previous, map_id, folder.digest)
        if target.exists():
            # A node may have run on the old file since the check above.
            checkpoint_closed_store(target)
            _check_output(target, map_id, previous, replace)
        temp = temp_beside(target)
        try:
            with closing(connect(MEMORY)) as conn:
                migrate(conn)
                _build(
                    conn, map_id=map_id, digest=folder.digest, arena=arena, date=date,
                    clock_kind=clock_kind, baseline_rev=baseline_rev, rooms=rooms,
                    anchors=anchors, catalogue=catalogue, lineage=lineage, id_floor=id_floor,
                )
                conn.execute("VACUUM INTO ?", (str(temp),))
            install_store(temp, target)
        finally:
            discard_temp(temp)
    tombstones = len({key for key in lineage.anchors} - {anchor.key for anchor in anchors})
    return SeedSummary(target, map_id, len(rooms), len(anchors), tombstones, len(catalogue))


# The command line ------------------------------------------------------------

# What main turns into a message and exit status 1, for this tool and its callers.
REFUSALS = (Refused, MapGateError, StoreLocked, OSError, sqlite3.Error)


def _id_floor_argument(text: str) -> int:
    """The value of --id-floor: digits only, from 0 to the largest ID."""
    if re.fullmatch(r"[0-9]+", text) is None or int(text) > LARGEST_ID:
        raise argparse.ArgumentTypeError(f"must be a whole number from 0 to {LARGEST_ID}, not {text!r}")
    return int(text)


def add_seed_arguments(parser: argparse.ArgumentParser) -> None:
    """The map, identity and output arguments that seed_store and seed_sim share."""
    parser.add_argument("--map-folder", required=True, help="the folder with map.yaml and its image")
    parser.add_argument("--arena", required=True, help="the arena name in the map ID")
    parser.add_argument("--date", required=True, help="the map's date, YYYY-MM-DD")
    parser.add_argument("--clock-kind", required=True, choices=CLOCK_KINDS,
                        help="wall on the robot, ros_sim in a simulation")
    parser.add_argument("--output", required=True, help="the seed file to write")
    parser.add_argument("--previous-seed", help="a closed seed of this map, whose anchor IDs carry over")
    parser.add_argument("--id-floor", type=_id_floor_argument, default=0, metavar="N",
                        help="give new anchors IDs above N, a store's high-water mark that the restore "
                             "tool names (default 0)")
    parser.add_argument("--replace", action="store_true", help="replace --output if it exists")


def report(summary: SeedSummary) -> None:
    print(
        f"Wrote {summary.output}: map {summary.map_id}, {summary.rooms} rooms, "
        f"{summary.anchors} anchors, {summary.tombstones} tombstones and "
        f"{summary.catalogue} catalogue items."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m walkie_database.seed_store",
        description="Build a seed from reviewed room, anchor and catalogue files.",
    )
    add_seed_arguments(parser)
    parser.add_argument("--rooms", required=True, help="the rooms file")
    parser.add_argument("--anchors", required=True, help="the anchors file, which may list none")
    parser.add_argument("--catalogue", required=True, help="the catalogue file, which may list none")
    args = parser.parse_args(argv)
    try:
        folder, map_id = identify_map(args.map_folder, args.arena, args.date)
        summary = write_seed(
            folder=folder, map_id=map_id, arena=args.arena, date=args.date,
            clock_kind=args.clock_kind,
            rooms_document=read_document(args.rooms, "rooms"),
            anchors_document=read_document(args.anchors, "anchors"),
            catalogue_document=read_document(args.catalogue, "catalogue"),
            output=args.output, previous_seed=args.previous_seed, replace=args.replace,
            id_floor=args.id_floor,
        )
    except REFUSALS as error:
        print(f"Refused, and wrote nothing: {error}", file=sys.stderr)
        return 1
    report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
