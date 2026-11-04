"""Records and queries for rooms, surfaces, the catalogue, relations and events.

Everything here takes a plain :class:`sqlite3.Connection` and plain Python values,
so the tests can call it without ROS. Helpers that write run inside the caller's
transaction. Nothing here infers a relation, moves a state or runs when a timer
fires. Those arrive with the object memory (ADR-0021, ADR-0022).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from walkie_database.objects import require_transaction
from walkie_database.people import Refused

ON, INSIDE = "on", "inside"
PREDICATES = (ON, INSIDE)
EVENT_KINDS = ("action", "note", "error", "handover")
MAX_EVENT_TEXT = 512
MAX_EVENT_RESULTS = 100  # per find_events call

MAX_REQUEST_ID = 128
# JSON for the write ledger. The array and string limits keep a vector, a crop or
# a mask out, whatever it is called.
MAX_PAYLOAD_JSON = 16 * 1024
MAX_OUTCOME_JSON = 4 * 1024
MAX_JSON_ARRAY = 64
MAX_JSON_STRING = 1024
MAX_JSON_DEPTH = 8

# What makes an anchor something an object can stand on or be inside.
_USABLE_ANCHOR = "a.layer = 'anchor' AND a.geometry_valid = 1 AND a.state NOT IN ('discarded', 'merged')"


class WriteConflict(Refused):
    """A request id came back with a different operation or payload."""


@contextmanager
def _integrity_as_refused() -> Iterator[None]:
    """Turn a constraint failure into a refusal that names the rule it broke."""
    try:
        yield
    except sqlite3.IntegrityError as error:
        raise Refused(str(error)) from None


def open_ring(outline: Sequence[Sequence[float]], where: str) -> list[list[float]]:
    """*outline* as corners with no repeated last corner, or Refused.

    It needs three corners and some area. Crossing edges are not detected.
    """
    try:
        points = [[float(x), float(y)] for x, y in outline]
    except (TypeError, ValueError):
        raise Refused(f"{where} must be a list of [x, y] corners.") from None
    if not all(math.isfinite(value) for point in points for value in point):
        raise Refused(f"{where} has a NaN or infinite value.")
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        raise Refused(f"{where} needs at least 3 corners, and has {len(points)}.")
    area = sum(
        a[0] * b[1] - b[0] * a[1] for a, b in zip(points, [*points[1:], points[0]])
    )
    if area == 0:
        raise Refused(f"{where} has no area.")
    return points


# Rooms -----------------------------------------------------------------------


def set_room_shape(
    conn: sqlite3.Connection, room_id: int, outline: Sequence[Sequence[float]]
) -> None:
    """Replace a room's outline, in any winding order, with or without a closing corner."""
    require_transaction(conn, "set_room_shape")
    if conn.execute("SELECT 1 FROM room WHERE id = ?", (room_id,)).fetchone() is None:
        raise Refused(f"Room {room_id} does not exist.")
    points = open_ring(outline, f"The outline of room {room_id}")
    conn.execute("DELETE FROM room_shape WHERE rowid = ?", (room_id,))
    conn.execute(
        "INSERT INTO room_shape (rowid, _shape) VALUES (?, geopoly_ccw(?))",
        (room_id, json.dumps([*points, points[0]])),
    )


def room_outline(conn: sqlite3.Connection, room_id: int) -> list[list[float]]:
    """A room's corners, counterclockwise with no repeated last corner. Empty when it has none."""
    row = conn.execute(
        "SELECT geopoly_json(_shape) FROM room_shape WHERE rowid = ?", (room_id,)
    ).fetchone()
    return [] if row is None else json.loads(row[0])[:-1]


def room_at(conn: sqlite3.Connection, map_id: str, x: float, y: float) -> int | None:
    """The room of a point in *map_id*, or None.

    A point on a shared wall counts as in both rooms, so a room that holds it
    strictly inside wins, and then the lowest id.
    """
    row = conn.execute(
        """SELECT r.id, geopoly_contains_point(s._shape, ?, ?) AS inside
           FROM room_shape AS s JOIN room AS r ON r.id = s.rowid
           WHERE r.map_id = ? AND inside > 0
           ORDER BY inside DESC, r.id LIMIT 1""",
        (x, y, map_id),
    ).fetchone()
    return None if row is None else row["id"]


def room_by_key(conn: sqlite3.Connection, map_id: str, key: str) -> sqlite3.Row | None:
    """The room with exactly this machine key. A display name does not match."""
    return conn.execute(
        "SELECT * FROM room WHERE map_id = ? AND key = ?", (map_id, key)
    ).fetchone()


# Surfaces --------------------------------------------------------------------


def add_surface(
    conn: sqlite3.Connection,
    anchor_id: int,
    key: str,
    z: float,
    outline: Sequence[Sequence[float]] = (),
    *,
    geometry_valid: bool = True,
) -> int:
    """Add a surface to an anchor, and return its id.

    A surface with *geometry_valid* False is known by name but not measured. Its
    outline may be empty, and it is not usable.
    """
    require_transaction(conn, "add_surface")
    if not key.strip():
        raise Refused("A surface needs a key.")
    if not math.isfinite(z):
        raise Refused(f"Surface {key} has a NaN or infinite height.")
    points = open_ring(outline, f"The outline of surface {key}") if geometry_valid or outline else []
    row = conn.execute("SELECT map_id FROM object WHERE id = ?", (anchor_id,)).fetchone()
    if row is None:
        raise Refused(f"Object {anchor_id} does not exist.")
    with _integrity_as_refused():
        return conn.execute(
            """INSERT INTO surface (map_id, anchor_id, key, z, outline, geometry_valid)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (row["map_id"], anchor_id, key, z, json.dumps(points), int(geometry_valid)),
        ).lastrowid


def usable_surfaces(conn: sqlite3.Connection, anchor_id: int) -> list[sqlite3.Row]:
    """The measured surfaces of a measured anchor, in key order.

    An anchor with no measured box has none, whatever its surfaces say.
    """
    return conn.execute(
        f"""SELECT s.* FROM surface AS s JOIN object AS a ON a.id = s.anchor_id
            WHERE s.anchor_id = ? AND s.geometry_valid = 1 AND {_USABLE_ANCHOR}
            ORDER BY s.key""",
        (anchor_id,),
    ).fetchall()


# Catalogue -------------------------------------------------------------------

# The fixed vocabulary of an attribute. A missing key means unknown, never guessed.
ATTRIBUTE_VALUES = {
    "colour": ("black", "white", "grey", "red", "orange", "yellow",
               "green", "blue", "purple", "pink", "brown", "beige"),
    "material": ("metal", "plastic", "glass", "ceramic", "wood",
                 "paper", "cardboard", "fabric", "rubber"),
    "shape": ("round", "rectangular", "cylindrical", "irregular"),
    "size": ("small", "medium", "large"),
}
BOOLEAN_ATTRIBUTES = ("fragile", "container")


def check_attributes(attributes: object, where: str) -> dict[str, object]:
    """*attributes* if every key and value is in the fixed vocabulary, or Refused."""
    if not isinstance(attributes, Mapping):
        raise Refused(f"{where} must be a mapping.")
    for key, value in attributes.items():
        if key in BOOLEAN_ATTRIBUTES:
            if not isinstance(value, bool):
                raise Refused(f"{where} has {key} {value!r}, which is not true or false.")
        elif key in ATTRIBUTE_VALUES:
            if not isinstance(value, str) or value not in ATTRIBUTE_VALUES[key]:
                raise Refused(f"{where} has {key} {value!r}, which is not in the vocabulary.")
        else:
            raise Refused(f"{where} has the key {key!r}, which is not in the vocabulary.")
    return dict(attributes)


def _normal_alias(alias: str) -> str:
    return " ".join(alias.split()).casefold()


def add_catalogue_item(
    conn: sqlite3.Connection,
    map_id: str,
    official_name: str,
    *,
    category: str | None = None,
    default_place: str | None = None,
    weight_g: float | None = None,
    attributes: Mapping[str, object] | None = None,
    aliases: Sequence[str] = (),
) -> int:
    """Add a known item to a map's catalogue, and return its id.

    Aliases are stored without case or extra spaces. A blank one is refused.
    """
    require_transaction(conn, "add_catalogue_item")
    if not official_name.strip():
        raise Refused("A catalogue item needs an official name.")
    normal = {_normal_alias(alias) for alias in aliases}
    if "" in normal:
        raise Refused(f"Catalogue item {official_name} has a blank alias.")
    with _integrity_as_refused():
        catalogue_id = conn.execute(
            """INSERT INTO object_catalogue
                   (map_id, official_name, category, default_place, weight_g, attributes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (map_id, official_name, category, default_place, weight_g,
             json.dumps(dict(attributes or {}), sort_keys=True)),
        ).lastrowid
        conn.executemany(
            "INSERT INTO catalogue_alias (catalogue_id, alias) VALUES (?, ?)",
            [(catalogue_id, alias) for alias in sorted(normal)],
        )
    return catalogue_id


def find_catalogue_items(conn: sqlite3.Connection, map_id: str, name: str) -> list[sqlite3.Row]:
    """Items of *map_id* with this exact official name or this alias, in id order.

    An alias may belong to several items, so the caller sees all of them.
    """
    return conn.execute(
        """SELECT DISTINCT c.* FROM object_catalogue AS c
           LEFT JOIN catalogue_alias AS a ON a.catalogue_id = c.id
           WHERE c.map_id = ? AND (c.official_name = ? OR a.alias = ?)
           ORDER BY c.id""",
        (map_id, name, _normal_alias(name)),
    ).fetchall()


def link_catalogue(conn: sqlite3.Connection, object_id: int, catalogue_id: int | None) -> None:
    """Point an object at a catalogue item of its own map, or at none."""
    require_transaction(conn, "link_catalogue")
    row = conn.execute("SELECT map_id FROM object WHERE id = ?", (object_id,)).fetchone()
    if row is None:
        raise Refused(f"Object {object_id} does not exist.")
    if catalogue_id is not None:
        item = conn.execute(
            "SELECT map_id FROM object_catalogue WHERE id = ?", (catalogue_id,)
        ).fetchone()
        if item is None:
            raise Refused(f"Catalogue item {catalogue_id} does not exist.")
        if item["map_id"] != row["map_id"]:
            raise Refused(f"Object {object_id} and catalogue item {catalogue_id} are on different maps.")
    conn.execute(
        "UPDATE object SET catalogue_id = ?, revision = revision + 1 WHERE id = ?",
        (catalogue_id, object_id),
    )


def object_weight_g(conn: sqlite3.Connection, object_id: int) -> float | None:
    """The object's weight from its catalogue item, or None when it is not known."""
    row = conn.execute(
        """SELECT c.weight_g FROM object AS o
           JOIN object_catalogue AS c ON c.id = o.catalogue_id WHERE o.id = ?""",
        (object_id,),
    ).fetchone()
    return None if row is None else row["weight_g"]


# Relations -------------------------------------------------------------------


def set_relation(
    conn: sqlite3.Connection,
    src_id: int,
    dst_id: int,
    predicate: str,
    weight: float,
    now: float,
    *,
    surface_id: int | None = None,
) -> None:
    """Store that *src_id* is on a surface of *dst_id*, or inside it, or update the row.

    The caller decides that this is true. This only refuses a row the schema does
    not allow, and an anchor that cannot hold it: one with no measured box, an on
    row on an unmeasured surface, or an inside row in an anchor that is not enclosed.
    """
    require_transaction(conn, "set_relation")
    if predicate not in PREDICATES:
        raise Refused(f"A relation is on or inside, not {predicate}. Others are computed when asked.")
    if not 0 <= weight <= 1:
        raise Refused(f"A relation's weight is from 0 to 1, not {weight}.")
    anchor = conn.execute(
        f"SELECT a.enclosed FROM object AS a WHERE a.id = ? AND {_USABLE_ANCHOR}", (dst_id,)
    ).fetchone()
    if anchor is None:
        raise Refused(f"Object {dst_id} is not a measured anchor, so nothing can be {predicate} it.")
    if predicate == ON and surface_id not in {s["id"] for s in usable_surfaces(conn, dst_id)}:
        raise Refused(f"Surface {surface_id} is not a usable surface of anchor {dst_id}.")
    if predicate == INSIDE and not anchor["enclosed"]:
        raise Refused(f"Anchor {dst_id} is not enclosed, so nothing can be inside it.")
    target = (
        "(src_id, dst_id, surface_id) WHERE predicate = 'on'"
        if predicate == ON
        else "(src_id, dst_id) WHERE predicate = 'inside'"
    )
    with _integrity_as_refused():
        conn.execute(
            f"""INSERT INTO object_relation (src_id, dst_id, predicate, surface_id, weight, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT {target} DO UPDATE
                SET weight = excluded.weight, updated_at = excluded.updated_at""",
            (src_id, dst_id, predicate, surface_id, weight, now),
        )


def relations(
    conn: sqlite3.Connection,
    *,
    src_id: int | None = None,
    dst_id: int | None = None,
    surface_id: int | None = None,
) -> list[sqlite3.Row]:
    """The stored rows that match every id given, with the surface's key.

    A row stays stored when its anchor or surface stops being usable, and it is
    not returned until the anchor is usable again.
    """
    filters = [(column, value) for column, value in
               (("r.src_id", src_id), ("r.dst_id", dst_id), ("r.surface_id", surface_id))
               if value is not None]
    if not filters:
        raise Refused("A relation query needs an object or a surface.")
    where = " AND ".join(f"{column} = ?" for column, _ in filters)
    return conn.execute(
        f"""SELECT r.src_id, r.dst_id, r.predicate, r.surface_id, s.key AS surface_key,
                   r.weight, r.updated_at
            FROM object_relation AS r
            JOIN object AS a ON a.id = r.dst_id
            LEFT JOIN surface AS s ON s.id = r.surface_id
            WHERE {where} AND {_USABLE_ANCHOR}
              AND (r.surface_id IS NULL OR s.geometry_valid = 1)
              AND (r.predicate != 'inside' OR a.enclosed = 1)
            ORDER BY r.src_id, r.dst_id, r.surface_id""",
        [value for _, value in filters],
    ).fetchall()


# State changes ---------------------------------------------------------------


def add_object_event(
    conn: sqlite3.Connection,
    object_id: int,
    old_state: str | None,
    new_state: str,
    now: float,
    *,
    cause: str | None = None,
    task_id: str | None = None,
) -> int:
    """Record one state change of an object, and return its id. It changes no state."""
    require_transaction(conn, "add_object_event")
    with _integrity_as_refused():
        return conn.execute(
            """INSERT INTO object_event (object_id, ts, old_state, new_state, cause, task_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (object_id, now, old_state, new_state, cause, task_id),
        ).lastrowid


def object_events(conn: sqlite3.Connection, object_id: int) -> list[sqlite3.Row]:
    """An object's state changes, oldest first."""
    return conn.execute(
        "SELECT * FROM object_event WHERE object_id = ? ORDER BY ts, id", (object_id,)
    ).fetchall()


# The task's log --------------------------------------------------------------


def add_event(
    conn: sqlite3.Connection,
    map_id: str,
    kind: str,
    text: str,
    now: float,
    *,
    pose: tuple[float, float, float] | None = None,
    room_id: int | None = None,
    object_id: int | None = None,
    person_id: int | None = None,
    session_id: int | None = None,
    task_id: str | None = None,
    cause: str | None = None,
) -> int:
    """Add one event to the log, and return its id.

    *pose* is x, y and yaw in the map, or None when it is not known. A person who
    belongs to a session can be named only with that session, so that forgetting
    it finds the event. The text must carry no personal details. Length and kind
    can be checked here, and the meaning of free text cannot (ADR-0022).
    """
    require_transaction(conn, "add_event")
    if kind not in EVENT_KINDS:
        raise Refused(f"An event's kind is one of {', '.join(EVENT_KINDS)}, not {kind}.")
    text = text.strip()
    if not 1 <= len(text) <= MAX_EVENT_TEXT:
        raise Refused(f"An event's text is 1 to {MAX_EVENT_TEXT} characters, not {len(text)}.")
    if "\x00" in text:
        raise Refused("An event's text has a NUL character.")
    if pose is not None and not all(math.isfinite(value) for value in pose):
        raise Refused("An event's pose has a NaN or infinite value.")
    for table, row_id in (("room", room_id), ("object", object_id)):
        if row_id is not None:
            row = conn.execute(f"SELECT map_id FROM {table} WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                raise Refused(f"The event names {table} {row_id}, which does not exist.")
            if row["map_id"] != map_id:
                raise Refused(f"The event's {table} {row_id} is not on map {map_id}.")
    if person_id is not None:
        person = conn.execute("SELECT session_id FROM person WHERE id = ?", (person_id,)).fetchone()
        if person is None:
            raise Refused(f"The event names person {person_id}, who does not exist.")
        if person["session_id"] is not None and person["session_id"] != session_id:
            raise Refused(f"Person {person_id} belongs to session {person['session_id']}.")
    x, y, yaw = pose if pose is not None else (None, None, None)
    with _integrity_as_refused():
        return conn.execute(
            """INSERT INTO event (ts, kind, text, map_id, x, y, yaw, room_id, object_id,
                                  person_id, session_id, task_id, cause)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, kind, text, map_id, x, y, yaw, room_id, object_id, person_id, session_id,
             task_id, cause),
        ).lastrowid


def _match_terms(text: str) -> str:
    """Words as FTS5 strings, so that a caller's text is never MATCH syntax."""
    words = text.replace("\x00", " ").split()
    return " ".join('"' + word.replace('"', '""') + '"' for word in words)


def find_events(
    conn: sqlite3.Connection,
    map_id: str,
    *,
    text: str = "",
    since: float | None = None,
    room_id: int | None = None,
    max_results: int = 20,
) -> tuple[list[sqlite3.Row], bool]:
    """Events of *map_id*, newest first, and whether more matched than were returned.

    Every word of *text* must match, in any word form. Blank *text* means any.
    """
    if not 1 <= max_results <= MAX_EVENT_RESULTS:
        raise Refused(f"max_results must be from 1 to {MAX_EVENT_RESULTS}, not {max_results}.")
    where = "e.map_id = ?"
    parameters: list[object] = [map_id]
    terms = _match_terms(text)
    if terms:
        where += " AND e.id IN (SELECT rowid FROM event_fts WHERE event_fts MATCH ?)"
        parameters.append(terms)
    if since is not None:
        where += " AND e.ts >= ?"
        parameters.append(since)
    if room_id is not None:
        where += " AND e.room_id = ?"
        parameters.append(room_id)
    rows = conn.execute(
        f"SELECT e.* FROM event AS e WHERE {where} ORDER BY e.ts DESC, e.id DESC LIMIT ?",
        [*parameters, max_results + 1],
    ).fetchall()
    return rows[:max_results], len(rows) > max_results


# Retried writes --------------------------------------------------------------


def _check_json(value: object, where: str, depth: int = 0) -> None:
    """Refuse anything that is not small, plain JSON."""
    if depth > MAX_JSON_DEPTH:
        raise Refused(f"{where} is nested over {MAX_JSON_DEPTH} levels.")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Refused(f"{where} has a NaN or infinite value.")
    elif isinstance(value, str):
        if len(value) > MAX_JSON_STRING:
            raise Refused(f"{where} has a string over {MAX_JSON_STRING} characters.")
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_ARRAY:
            raise Refused(f"{where} has an array over {MAX_JSON_ARRAY} items. Raw data stays out.")
        for item in value:
            _check_json(item, where, depth + 1)
    elif isinstance(value, dict):
        if len(value) > MAX_JSON_ARRAY:
            raise Refused(f"{where} has an object over {MAX_JSON_ARRAY} keys.")
        for key, item in value.items():
            if not isinstance(key, str):
                raise Refused(f"{where} has a key that is not a string.")
            _check_json(key, where, depth + 1)
            _check_json(item, where, depth + 1)
    else:
        raise Refused(f"{where} holds {type(value).__name__}, which is not JSON.")


def canonical_json(value: object, *, limit: int, where: str = "The value") -> str:
    """*value* as one exact JSON text: sorted keys, no spaces, at most *limit* bytes."""
    _check_json(value, where)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)
    try:
        size = len(text.encode())
    except UnicodeEncodeError:
        raise Refused(f"{where} has text that is not valid Unicode.") from None
    if size > limit:
        raise Refused(f"{where} is over {limit} bytes of JSON.")
    return text


def payload_hash(payload: object) -> str:
    """The SHA-256 of a request's canonical JSON, as lowercase hex."""
    text = canonical_json(payload, limit=MAX_PAYLOAD_JSON, where="The payload")
    return hashlib.sha256(text.encode()).hexdigest()


def find_write(
    conn: sqlite3.Connection, request_id: str, operation: str, payload: object
) -> dict | None:
    """The recorded outcome of this same request, or None when it is new.

    Raises WriteConflict when the id was used with another operation or payload.
    """
    if not 1 <= len(request_id) <= MAX_REQUEST_ID:
        raise Refused(f"A request id is 1 to {MAX_REQUEST_ID} characters, not {len(request_id)}.")
    digest = payload_hash(payload)
    row = conn.execute(
        "SELECT operation, payload_hash, outcome_json FROM write_request WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    if row["operation"] != operation:
        raise WriteConflict(f"Request {request_id} was already used for {row['operation']}.")
    if row["payload_hash"] != digest:
        raise WriteConflict(f"Request {request_id} was already used with a different payload.")
    return json.loads(row["outcome_json"])


def record_write(
    conn: sqlite3.Connection,
    request_id: str,
    task_id: str,
    operation: str,
    payload: object,
    outcome: dict[str, object],
    now: float,
) -> dict:
    """Record a write's outcome, in the transaction of the write it describes.

    Returns the outcome to answer with: the earlier one, when this same request was
    already recorded. The outcome is ids and statuses. An event's text or a person's
    details stay out of it, because forgetting a session does not read this table.
    """
    require_transaction(conn, "record_write")
    if not isinstance(outcome, dict):
        raise Refused("A write's outcome is a JSON object.")
    earlier = find_write(conn, request_id, operation, payload)
    if earlier is not None:
        return earlier
    text = canonical_json(outcome, limit=MAX_OUTCOME_JSON, where="The outcome")
    conn.execute(
        """INSERT INTO write_request
               (request_id, task_id, operation, payload_hash, outcome_json, committed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (request_id, task_id, operation, payload_hash(payload), text, now),
    )
    return json.loads(text)
