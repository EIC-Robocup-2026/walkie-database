"""Object ids, new object rows, tombstones and object vectors.

Everything here takes a plain :class:`sqlite3.Connection`, as
:func:`walkie_database.db.connect` opens it, and plain Python values, so the
tests can call it without ROS. Nothing here decides a state, merges objects or
reads a detection.

An object id is durable. It is reserved in a committed transaction before
anyone sees it, and it is never given again, also after a delete, a restart or
a crash. A crash can leave gaps. That is the cost, and it is harmless.

A vector names its model, and the model is looked up in the schema, as for
people (ADR-0007). An object's vector carries the map, room, state and layer of
its row, so that a search filters on them before it ranks.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from walkie_database.db import transaction
from walkie_database.people import Refused, unit_vector, vector_tables

MAX_LINK_HOPS = 32  # the most links followed from one object
MERGED_INTO, SPLIT_FROM = "merged_into", "split_from"
_TOMBSTONE_STATES = ("discarded", "merged")
OBJECT = "object"  # the kind of a vector table, as in object_clip_vit_b16
MAX_SEARCH_RESULTS = 4096  # the largest k sqlite-vec takes


def require_transaction(conn: sqlite3.Connection, helper: str) -> None:
    """Refuse a helper that must commit together with the caller's other writes."""
    if not conn.in_transaction:
        raise Refused(f"{helper} runs inside the caller's transaction.")


@dataclass(frozen=True)
class IdReservation:
    """A block of object ids, already committed, from *first* to *last*.

    Ids the caller does not use stay unused. A reservation lasts as long as
    the connection that made it.
    """

    first: int
    last: int

    def __contains__(self, object_id: object) -> bool:
        return isinstance(object_id, int) and self.first <= object_id <= self.last

    def __iter__(self) -> Iterator[int]:
        return iter(range(self.first, self.last + 1))

    def __len__(self) -> int:
        return self.last - self.first + 1


def reserve_object_ids(conn: sqlite3.Connection, count: int = 1) -> IdReservation:
    """Commit a new block of *count* ids, and only then return it.

    Call this outside any transaction, so that the ids are durable whatever
    the caller's work does next. Raises Refused inside one.

    In WAL mode, synchronous=NORMAL may lose the last commits on a power cut.
    A lost reservation would give an id twice, so this commit syncs to disk.
    SQLite refuses to change the setting inside a transaction, so it changes
    before BEGIN and goes back after COMMIT.
    """
    if conn.in_transaction:
        raise Refused("Object ids are reserved outside a transaction, and one is open.")
    if count < 1:
        raise Refused(f"Cannot reserve {count} object ids.")
    previous = conn.execute("PRAGMA synchronous").fetchone()[0]
    conn.execute("PRAGMA synchronous = FULL")
    try:
        with transaction(conn):
            last = conn.execute(
                "UPDATE last_id SET id = id + ? WHERE table_name = 'object' RETURNING id",
                (count,),
            ).fetchone()[0]
    except BaseException:
        # transaction() does not roll back a COMMIT that failed.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute(f"PRAGMA synchronous = {int(previous)}")
    return IdReservation(last - count + 1, last)


def object_id_highwater(conn: sqlite3.Connection) -> int:
    """The largest object id ever given or reserved."""
    return conn.execute("SELECT id FROM last_id WHERE table_name = 'object'").fetchone()[0]


@dataclass(frozen=True)
class ObjectBox:
    """A measured oriented box in the map frame."""

    center: tuple[float, float, float]
    size: tuple[float, float, float]  # full side lengths
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)  # qx, qy, qz, qw
    stamp: float | None = None  # when it was observed


@dataclass(frozen=True)
class NewObject:
    """One new object row. A *box* of None means no measured geometry."""

    map_id: str
    label: str
    first_seen: float
    last_seen: float
    room_id: int | None = None
    caption: str | None = None
    source: str = "perception"
    key: str | None = None
    layer: str = "movable"
    state: str = "candidate"
    p_exist: float = 0.5
    box: ObjectBox | None = None


def _aabb(box: ObjectBox) -> tuple[float, float, float, float, float, float]:
    """The box aligned with the map's axes around an oriented box, as object_box takes it."""
    qx, qy, qz, qw = box.rotation
    # The rotation matrix of a unit quaternion. Each half extent is the sum of
    # the box's half sides along that map axis.
    matrix = (
        (1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)),
        (2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)),
        (2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)),
    )
    bounds = []
    for center, row in zip(box.center, matrix):
        half = sum(abs(value) * side / 2 for value, side in zip(row, box.size))
        bounds += [center - half, center + half]
    return tuple(bounds)


def _check_box(box: ObjectBox) -> None:
    values = (*box.center, *box.size, *box.rotation)
    if not all(math.isfinite(value) for value in values):
        raise Refused("The object's box has a NaN or infinite value.")
    if any(side < 0 for side in box.size):
        raise Refused("The object's box has a negative side.")
    norm = math.sqrt(sum(value * value for value in box.rotation))
    if not math.isclose(norm, 1.0, abs_tol=1e-3):
        raise Refused("The object's box rotation is not a unit quaternion.")


def _claim_new_id(
    conn: sqlite3.Connection,
    object_id: int,
    reservation: IdReservation | None,
    retained_ids: Collection[int],
) -> None:
    """Refuse an id that this reservation does not own, or has already used."""
    if object_id in retained_ids:
        return
    if reservation is None or object_id not in reservation:
        raise Refused(
            f"Object id {object_id} is not in the current reservation. "
            "New objects use ids from reserve_object_ids."
        )
    # Remembered in a temporary table because it rolls back with the caller's
    # transaction, so a failed write can retry the id. It also lasts exactly
    # as long as the connection, and so as long as the reservation.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS object_id_used (id INTEGER PRIMARY KEY)")
    try:
        conn.execute("INSERT INTO temp.object_id_used (id) VALUES (?)", (object_id,))
    except sqlite3.IntegrityError:
        raise Refused(f"Object id {object_id} was already used, and is never given twice.") from None


def insert_object(
    conn: sqlite3.Connection,
    new: NewObject,
    object_id: int,
    *,
    reservation: IdReservation | None = None,
    retained_ids: Collection[int] = (),
) -> None:
    """Write one object row and, when it has a measured box, its object_box row.

    Runs inside the caller's transaction, so that the object and whatever
    belongs with it commit together. *object_id* must come from *reservation*,
    or be one of *retained_ids*, the anchors a seed keeps across runs. A test
    fixture that needs fixed ids writes SQL into a fresh store instead.
    """
    require_transaction(conn, "insert_object")
    if new.box is not None:
        _check_box(new.box)
    _claim_new_id(conn, object_id, reservation, retained_ids)
    box = new.box
    center = box.center if box else (0.0, 0.0, 0.0)
    size = box.size if box else (0.0, 0.0, 0.0)
    rotation = box.rotation if box else (0.0, 0.0, 0.0, 1.0)
    conn.execute(
        """INSERT INTO object (id, map_id, room_id, label, caption, source,
                               center_x, center_y, center_z, size_x, size_y, size_z,
                               qx, qy, qz, qw, first_seen, last_seen,
                               key, layer, state, p_exist, geometry_valid, geometry_stamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (object_id, new.map_id, new.room_id, new.label, new.caption, new.source,
         *center, *size, *rotation, new.first_seen, new.last_seen,
         new.key, new.layer, new.state, new.p_exist, 1 if box else 0,
         box.stamp if box else None),
    )
    if box:
        conn.execute(
            """INSERT INTO object_box (id, min_x, max_x, min_y, max_y, min_z, max_z)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (object_id, *_aabb(box)),
        )


def _check_link(conn: sqlite3.Connection, column: str, object_id: int, target_id: int) -> None:
    """Refuse a link to itself, across maps, to a missing object, or that makes a cycle.

    *column* is MERGED_INTO or SPLIT_FROM, never text from a request.
    """
    if target_id == object_id:
        raise Refused(f"Object {object_id} cannot link to itself.")
    maps = {
        row["id"]: row["map_id"]
        for row in conn.execute(
            "SELECT id, map_id FROM object WHERE id IN (?, ?)", (object_id, target_id)
        )
    }
    for wanted in (object_id, target_id):
        if wanted not in maps:
            raise Refused(f"Object {wanted} does not exist.")
    if maps[object_id] != maps[target_id]:
        raise Refused(f"Objects {object_id} and {target_id} are on different maps.")
    seen = {object_id}
    current = target_id
    for _ in range(MAX_LINK_HOPS):
        if current in seen:
            raise Refused(f"Linking object {object_id} to {target_id} would make a cycle.")
        seen.add(current)
        row = conn.execute(f"SELECT {column} FROM object WHERE id = ?", (current,)).fetchone()
        if row is None or row[0] is None:
            return
        current = row[0]
    raise Refused(f"The {column} chain from object {target_id} is over {MAX_LINK_HOPS} links.")


def tombstone_object(
    conn: sqlite3.Connection, object_id: int, *, merged_into: int | None = None
) -> None:
    """Mark an object as gone, keeping its row and its id.

    With *merged_into* the state is ``merged`` and the row points to the
    winner. Without it the state is ``discarded``. Runs inside the caller's
    transaction. The row stays, so that a client holding the id still gets an
    answer about it.
    """
    require_transaction(conn, "tombstone_object")
    row = conn.execute("SELECT state FROM object WHERE id = ?", (object_id,)).fetchone()
    if row is None:
        raise Refused(f"Object {object_id} does not exist.")
    if row["state"] in _TOMBSTONE_STATES:
        raise Refused(f"Object {object_id} is already {row['state']}.")
    if merged_into is not None:
        _check_link(conn, MERGED_INTO, object_id, merged_into)
    conn.execute(
        "UPDATE object SET state = ?, merged_into = ?, revision = revision + 1 WHERE id = ?",
        ("merged" if merged_into is not None else "discarded", merged_into, object_id),
    )


def set_split_from(conn: sqlite3.Connection, object_id: int, parent_id: int) -> None:
    """Record that *object_id* was split from *parent_id*, inside the caller's transaction."""
    require_transaction(conn, "set_split_from")
    _check_link(conn, SPLIT_FROM, object_id, parent_id)
    conn.execute(
        "UPDATE object SET split_from = ?, revision = revision + 1 WHERE id = ?",
        (parent_id, object_id),
    )


def _object_vector(
    conn: sqlite3.Connection, model: str, values: Sequence[float], where: str
) -> tuple[str, bytes]:
    """The vector table of *model*, and *values* as the bytes it takes.

    unit_vector refuses a model with no table in the schema, so the table name
    is safe to put in SQL text.
    """
    tables = vector_tables(conn, OBJECT)
    embedding = unit_vector(values, model=model, tables=tables, kind=OBJECT, where=where)
    return f"{OBJECT}_{model}", embedding


def write_object_vector(
    conn: sqlite3.Connection, object_id: int, model: str, values: Sequence[float]
) -> None:
    """Store an object's vector for *model*, replacing any it has.

    Runs inside the caller's transaction, because a failed vec0 write can leave
    half a row behind. The metadata comes from the object row. Later changes to
    that row reach the vector through the migration's triggers.
    """
    require_transaction(conn, "write_object_vector")
    row = conn.execute(
        "SELECT map_id, coalesce(room_id, 0), state, layer FROM object WHERE id = ?", (object_id,)
    ).fetchone()
    if row is None:
        raise Refused(f"Object {object_id} does not exist.")
    table, embedding = _object_vector(conn, model, values, f"The vector of object {object_id}")
    # A vec0 table has no upsert.
    conn.execute(f"DELETE FROM {table} WHERE object_id = ?", (object_id,))
    conn.execute(
        f"""INSERT INTO {table} (object_id, embedding, map_id, room_id, state, layer)
            VALUES (?, ?, ?, ?, ?, ?)""",
        (object_id, embedding, *row),
    )


class VectorMatch(NamedTuple):
    object_id: int
    distance: float  # cosine distance: 0 is the same direction, 1 is unrelated


def search_object_vectors(
    conn: sqlite3.Connection,
    model: str,
    query: Sequence[float],
    *,
    map_id: str,
    max_results: int = 10,
    room_id: int | None = None,
    states: Sequence[str] = (),
    layers: Sequence[str] = (),
) -> list[VectorMatch]:
    """The objects nearest *query* among those that pass every filter, nearest first.

    The filters run inside the vec0 search, so an object elsewhere cannot take a
    place in the results, however similar it is. *map_id* is required, because
    coordinates mean nothing across maps. A *room_id* of None means any room and
    0 means no room. Empty *states* or *layers* mean any.
    """
    if not 1 <= max_results <= MAX_SEARCH_RESULTS:
        raise Refused(f"max_results must be from 1 to {MAX_SEARCH_RESULTS}, not {max_results}.")
    table, embedding = _object_vector(conn, model, query, "The query")
    filters = "embedding MATCH ? AND k = ? AND map_id = ?"
    parameters: list[object] = [embedding, max_results, map_id]
    if room_id is not None:
        filters += " AND room_id = ?"
        parameters.append(room_id)
    for column, values in (("state", states), ("layer", layers)):
        if values:
            marks = ", ".join("?" * len(values))
            filters += f" AND {column} IN ({marks})"
            parameters += values
    # A vec0 search takes only ORDER BY distance. MATERIALIZED keeps SQLite from
    # moving the outer ORDER BY into it, so ties can sort by id out here.
    sql = f"""WITH matches AS MATERIALIZED (
                  SELECT object_id, distance FROM {table} WHERE {filters}
              )
              SELECT object_id, distance FROM matches ORDER BY distance, object_id"""
    return [VectorMatch(row[0], row[1]) for row in conn.execute(sql, parameters)]
