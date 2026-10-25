"""Sessions and people: the work behind the node's people services.

Everything here takes a plain :class:`sqlite3.Connection`, as
:func:`walkie_database.db.connect` opens it, and plain Python values, so the
tests can call it without ROS.

The open session is the ``session`` row with ``ended_at`` NULL. Migration
0003 allows at most one (ADR-0017). Nothing about it is kept in memory.

A vector names its model, and only vectors of one model are compared
(ADR-0006, ADR-0007). The models are found in the schema on every call: a
model is one vec0 table named ``<kind>_<model>``. A model string from a
request is only ever looked up in that list. It never goes into SQL text.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from typing import NamedTuple

import numpy as np

from walkie_database.db import transaction

FACE, APPEARANCE = "face", "appearance"
KINDS = (FACE, APPEARANCE)
MAX_QUERIES = 32  # per match call
MAX_SAMPLES = 100  # faces, and appearances, per remember_person
MAX_FACTS = 32
MAX_NAME = 100
MAX_FACT_KEY = 64
MAX_FACT_VALUE = 256

# A (model, values) pair, as ModelVector carries it.
Vector = tuple[str, Sequence[float]]


class Refused(Exception):
    """A request the database will not run. str(e) is the message for the caller."""


# The vec0 shadow tables (*_chunks, *_rowids and so on) are plain CREATE
# TABLE, so the LIKE leaves them out.
_VEC0_TABLES_SQL = """
SELECT name, sql FROM sqlite_master
WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE % USING vec0(%'
ORDER BY name
"""
_EMBEDDING_LENGTH = re.compile(r"\bembedding\s+float\[(\d+)\]", re.IGNORECASE)


class VectorTables(NamedTuple):
    # Model name -> how many values its vectors have.
    lengths: dict[str, int]
    # vec0 tables of the kind whose embedding column could not be read. Their
    # models are not used.
    unreadable: list[str]


def vector_tables(conn: sqlite3.Connection, kind: str) -> VectorTables:
    """The models of one kind (face or appearance) that have a vector table."""
    prefix = f"{kind}_"
    lengths: dict[str, int] = {}
    unreadable: list[str] = []
    for row in conn.execute(_VEC0_TABLES_SQL):
        name = row["name"]
        if not name.startswith(prefix):
            continue
        match = _EMBEDDING_LENGTH.search(row["sql"])
        if match is None:
            unreadable.append(name)
        else:
            lengths[name.removeprefix(prefix)] = int(match.group(1))
    return VectorTables(lengths, unreadable)


def unit_vector(
    values: Sequence[float], *, model: str, tables: VectorTables, kind: str, where: str
) -> bytes:
    """*values* scaled to length 1, as the bytes vec0 takes.

    *where* names the field in the request, for example ``faces[3]``.
    Raises Refused for an unknown model, a wrong length, NaN or infinity, or
    all zeros, in that order.
    """
    length = tables.lengths.get(model)
    if length is None:
        known = ", ".join(sorted(tables.lengths)) or "none"
        message = (
            f"{where} names the model '{model}', which has no {kind} table. "
            f"{kind.capitalize()} models in the database: {known}."
        )
        if f"{kind}_{model}" in tables.unreadable:
            message += (
                f" The table {kind}_{model} exists, but the node cannot read "
                "the length of its embedding column."
            )
        raise Refused(message)
    v = np.asarray(values, dtype=np.float64).ravel()
    if v.size != length:
        raise Refused(f"{where} has {v.size} values, but {model} vectors have {length}.")
    if not np.isfinite(v).all():
        raise Refused(f"{where} has a NaN or infinite value.")
    # In float64, because the square of a large float32 value can overflow.
    norm = float(np.linalg.norm(v))
    if norm == 0.0:
        raise Refused(f"{where} is all zeros, so it has no direction.")
    # Cast before tobytes(): the bytes of a float64 array read as a vector
    # twice as long, and vec0 refuses it.
    return (v / norm).astype(np.float32).tobytes()


def open_session(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """``id, label, started_at`` of the open session, or None."""
    return conn.execute(
        "SELECT id, label, started_at FROM session WHERE ended_at IS NULL"
    ).fetchone()


def count_people(conn: sqlite3.Connection, session_id: int) -> int:
    """How many people the session met."""
    return conn.execute(
        "SELECT count(*) FROM person WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


def _next_id(conn: sqlite3.Connection, table: str) -> int:
    """An id for a new session or person that no row has ever had (migration 0003)."""
    row = conn.execute("SELECT id + 1 FROM last_id WHERE table_name = ?", (table,)).fetchone()
    if row is None:
        raise ValueError(f"the last_id table has no row for {table}")
    return row[0]


def _insert_session(conn: sqlite3.Connection, label: str | None, now: float) -> int:
    session_id = _next_id(conn, "session")
    conn.execute(
        "INSERT INTO session (id, label, started_at) VALUES (?, ?, ?)",
        (session_id, label, now),
    )
    return session_id


class StartedSession(NamedTuple):
    id: int
    started_at: float
    resumed: bool
    people: int  # in the open session
    forgot_id: int  # the session forgotten first, or 0
    forgot_people: int


def start_session(
    conn: sqlite3.Connection, label: str, resume: bool, now: float
) -> StartedSession:
    """Resume the open session, or forget it and start a new one.

    One transaction, so there is never a moment with no open session.
    """
    with transaction(conn):
        current = open_session(conn)
        forgot_id = forgot_people = 0
        if current is not None:
            people = count_people(conn, current["id"])
            if resume:
                return StartedSession(current["id"], current["started_at"], True, people, 0, 0)
            # The people, their faces and appearances, and the conversations
            # go with it (ON DELETE CASCADE and the delete triggers).
            conn.execute("DELETE FROM session WHERE id = ?", (current["id"],))
            forgot_id, forgot_people = current["id"], people
        session_id = _insert_session(conn, label.strip() or None, now)
    return StartedSession(session_id, now, False, 0, forgot_id, forgot_people)


def end_session(conn: sqlite3.Connection, session_id: int) -> tuple[int, int]:
    """Forget a session, or the open one for 0.

    Returns the id of the session forgotten, or 0 when there was none, and
    how many people went with it.
    """
    with transaction(conn):
        if session_id == 0:
            current = open_session(conn)
            if current is None:
                return 0, 0
            session_id = current["id"]
        people = count_people(conn, session_id)
        if conn.execute("DELETE FROM session WHERE id = ?", (session_id,)).rowcount == 0:
            return 0, 0
    return session_id, people


class Remembered(NamedTuple):
    person_id: int
    session_id: int
    started_session: bool  # True when no session was open, so one was started
    faces: int
    appearances: int


def remember_person(
    conn: sqlite3.Connection,
    name: str,
    facts: Sequence[tuple[str, str]],
    faces: Sequence[Vector],
    appearances: Sequence[Vector],
    now: float,
) -> Remembered:
    """Store a new person in the open session, starting one if none is open.

    Everything is checked before anything is written, and the writes are one
    transaction, so a refusal or an error stores nothing.
    """
    name = name.strip()
    if len(name) > MAX_NAME:
        raise Refused(f"The name is longer than {MAX_NAME} characters.")
    attributes = _attributes(facts)
    for field, samples in (("faces", faces), ("appearances", appearances)):
        if len(samples) > MAX_SAMPLES:
            raise Refused(f"At most {MAX_SAMPLES} {field}, got {len(samples)}.")
    if not faces and not appearances:
        raise Refused(
            "A person needs at least one face or appearance vector, or they "
            "could never be matched."
        )
    face_rows = _vectors(conn, FACE, "faces", faces)
    appearance_rows = _vectors(conn, APPEARANCE, "appearances", appearances)

    with transaction(conn):
        session = open_session(conn)
        started = session is None
        session_id = _insert_session(conn, None, now) if started else session["id"]
        person_id = _next_id(conn, "person")
        conn.execute(
            """INSERT INTO person (id, session_id, name, attributes, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (person_id, session_id, name or None, attributes, now, now),
        )
        for table, embedding in face_rows:
            face_id = conn.execute(
                "INSERT INTO face (person_id, ts) VALUES (?, ?)", (person_id, now)
            ).lastrowid
            conn.execute(
                f'INSERT INTO "{_quoted(table)}" (face_id, embedding, person_id) '
                "VALUES (?, ?, ?)",
                (face_id, embedding, person_id),
            )
        for table, embedding in appearance_rows:
            appearance_id = conn.execute(
                "INSERT INTO appearance (person_id, session_id, ts) VALUES (?, ?, ?)",
                (person_id, session_id, now),
            ).lastrowid
            conn.execute(
                f'INSERT INTO "{_quoted(table)}" '
                "(appearance_id, embedding, person_id, session_id) VALUES (?, ?, ?, ?)",
                (appearance_id, embedding, person_id, session_id),
            )
    return Remembered(person_id, session_id, started, len(face_rows), len(appearance_rows))


def _attributes(facts: Sequence[tuple[str, str]]) -> str:
    """The facts as person.attributes, a JSON object, or Refused."""
    if len(facts) > MAX_FACTS:
        raise Refused(f"At most {MAX_FACTS} facts, got {len(facts)}.")
    stored: dict[str, str] = {}
    for i, (key, value) in enumerate(facts):
        key, value = key.strip(), value.strip()
        where = f"facts[{i}]"
        if not key:
            raise Refused(f"{where} has an empty key.")
        if not value:
            raise Refused(f"{where} has an empty value.")
        if len(key) > MAX_FACT_KEY:
            raise Refused(f"{where}'s key is longer than {MAX_FACT_KEY} characters.")
        if len(value) > MAX_FACT_VALUE:
            raise Refused(f"{where}'s value is longer than {MAX_FACT_VALUE} characters.")
        if key in stored:
            raise Refused(f"{where} repeats the key '{key}'.")
        stored[key] = value
    return json.dumps(stored, ensure_ascii=False)


def _vectors(
    conn: sqlite3.Connection, kind: str, field: str, samples: Sequence[Vector]
) -> list[tuple[str, bytes]]:
    """Each sample's vector table and unit vector, or Refused."""
    if not samples:
        return []
    tables = vector_tables(conn, kind)
    return [
        (
            f"{kind}_{model}",
            unit_vector(values, model=model, tables=tables, kind=kind, where=f"{field}[{i}]"),
        )
        for i, (model, values) in enumerate(samples)
    ]


# Every face sample's distance to the query, then each candidate's nearest.
# A face has no session: its person's session decides. People kept across
# sessions and the open session's people are candidates. Guests of other
# sessions are not. :session is NULL when no session is open, and then only
# kept people match.
# MATERIALIZED and CROSS JOIN keep the scan outside the joins, so each sample
# is read once. CROSS JOIN fixes the join order in SQLite (ADR-0018).
# A zero vector has no direction, and its distance is NULL.
_MATCH_FACES_SQL = """
WITH sample AS MATERIALIZED (
    SELECT face_id, vec_distance_cosine(embedding, :query) AS distance
    FROM "{table}"
)
SELECT p.id AS person_id, p.session_id, p.name, p.attributes,
       min(s.distance) AS distance
FROM sample AS s
CROSS JOIN face AS f ON f.id = s.face_id
CROSS JOIN person AS p ON p.id = f.person_id
WHERE s.distance IS NOT NULL
  AND (p.session_id IS NULL OR p.session_id = :session)
GROUP BY p.id
ORDER BY distance, p.id
LIMIT :limit
"""

# session_id in the vec0 table is a copy of appearance.session_id. Filtering
# on it in the scan skips reading other sessions' vectors. The join checks
# the real column. A kept person's appearance recorded in the open session is
# a candidate; their appearance from another session is not.
_MATCH_APPEARANCES_SQL = """
WITH sample AS MATERIALIZED (
    SELECT appearance_id, vec_distance_cosine(embedding, :query) AS distance
    FROM "{table}"
    WHERE session_id = :session
)
SELECT p.id AS person_id, p.session_id, p.name, p.attributes,
       min(s.distance) AS distance
FROM sample AS s
CROSS JOIN appearance AS a ON a.id = s.appearance_id
CROSS JOIN person AS p ON p.id = a.person_id
WHERE s.distance IS NOT NULL AND a.session_id = :session
GROUP BY p.id
ORDER BY distance, p.id
LIMIT :limit
"""

_MATCH_SQL = {FACE: _MATCH_FACES_SQL, APPEARANCE: _MATCH_APPEARANCES_SQL}


class Matched(NamedTuple):
    # One list per query, nearest person first. Each row has person_id,
    # session_id, name, attributes and distance.
    results: list[list[sqlite3.Row]]
    # True when the limit left people out of any list.
    more: bool
    # The open session, or None.
    session_id: int | None


def match(
    conn: sqlite3.Connection, kind: str, queries: Sequence[Vector], limit: int
) -> Matched:
    """The people nearest each query, at most *limit* per query.

    Every query is checked first, and one bad query refuses the whole call.
    Faces are matched against kept people and the open session's people.
    Appearances are matched only within the open session, so with no open
    session every list is empty.
    """
    if len(queries) > MAX_QUERIES:
        raise Refused(f"At most {MAX_QUERIES} queries, got {len(queries)}.")
    sql = _MATCH_SQL[kind]
    tables = vector_tables(conn, kind)
    vectors = [
        (
            f"{kind}_{model}",
            unit_vector(values, model=model, tables=tables, kind=kind, where=f"queries[{i}]"),
        )
        for i, (model, values) in enumerate(queries)
    ]
    session = open_session(conn)
    session_id = None if session is None else session["id"]
    if kind == APPEARANCE and session_id is None:
        return Matched([[] for _ in vectors], False, None)
    results, more = [], False
    for table, query in vectors:
        # One row more than the limit shows whether any were left out.
        rows = conn.execute(
            sql.format(table=_quoted(table)),
            {"query": query, "session": session_id, "limit": limit + 1},
        ).fetchall()
        more = more or len(rows) > limit
        results.append(rows[:limit])
    return Matched(results, more, session_id)


def facts_of(attributes: str, person_id: int) -> list[tuple[str, str]]:
    """person.attributes as (key, value) text pairs, in the order stored.

    A value that is not a JSON string comes back as its JSON text, so true
    becomes "true". Raises ValueError when the attributes are not a JSON
    object.
    """
    stored = json.loads(attributes)
    if not isinstance(stored, dict):
        raise ValueError(f"person {person_id} has attributes that are not a JSON object")
    return [
        (key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        for key, value in stored.items()
    ]


def _quoted(name: str) -> str:
    """*name* for use between double quotes in SQL."""
    return name.replace('"', '""')
