"""RememberEvent and FindEvents, on top of the task log's helpers.

A retried request gets the first answer and writes nothing. The ledger holds ids and
a time and never the text, so a retry after the session was forgotten gets no text
back (ADR-0022). The server cannot check that text has no personal details.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from walkie_database.db import transaction
from walkie_database.geometry import FramePose, yaw_of
from walkie_database.object_context import (
    Rejected,
    add_event,
    current_object,
    find_write,
    record_write,
    room_at,
)
from walkie_database.people import Refused, open_session

REMEMBER_EVENT = "remember_event"


@dataclass(frozen=True)
class NewEvent:
    request_id: str
    task_id: str
    cause: str
    kind: str
    text: str
    object_id: int = 0
    person_id: int = 0
    session_id: int = 0


@dataclass(frozen=True)
class EventRecord:
    id: int
    ts: float
    kind: str
    text: str
    map_id: str
    pose: tuple[float, float, float] | None = None  # x, y and yaw in the map
    room_id: int = 0
    object_id: int = 0
    person_id: int = 0
    session_id: int = 0
    task_id: str = ""
    cause: str = ""


def event_record(row: sqlite3.Row) -> EventRecord:
    known = row["x"] is not None and row["y"] is not None and row["yaw"] is not None
    return EventRecord(
        id=row["id"],
        ts=row["ts"],
        kind=row["kind"],
        text=row["text"],
        map_id=row["map_id"],
        pose=(row["x"], row["y"], row["yaw"]) if known else None,
        room_id=row["room_id"] or 0,
        object_id=row["object_id"] or 0,
        person_id=row["person_id"] or 0,
        session_id=row["session_id"] or 0,
        task_id=row["task_id"] or "",
        cause=row["cause"] or "",
    )


def remember_event(
    conn: sqlite3.Connection,
    new: NewEvent,
    *,
    map_id: str,
    now: float,
    locate: Callable[[], FramePose | None],
) -> EventRecord:
    """Add one line to the log, or answer a retry. *locate* gives the robot's pose now.

    A missing pose still records the line, with no pose and no room. TF is looked up
    outside the transaction, and only for a request that is not a retry.
    """
    for name, value in (("request_id", new.request_id), ("task_id", new.task_id), ("cause", new.cause)):
        if not value.strip():
            raise Refused(f"A write needs a {name}.")
    for name, value in (("object_id", new.object_id), ("person_id", new.person_id), ("session_id", new.session_id)):
        if value < 0:
            raise Refused(f"{name} must not be negative, not {value}.")
    payload = {
        "map_id": map_id,
        "task_id": new.task_id,
        "cause": new.cause,
        "kind": new.kind,
        "text": new.text,
        "object_id": new.object_id,
        "person_id": new.person_id,
        "session_id": new.session_id,
    }
    outcome = find_write(conn, new.request_id, REMEMBER_EVENT, payload)
    if outcome is not None:
        return _recorded(conn, new, outcome, map_id)
    object_id = current_object(conn, map_id, new.object_id)["id"] if new.object_id else None
    person_id, session_id = _person_and_session(conn, new)
    frame = locate()
    pose, room_id = None, None
    if frame is not None:
        pose = (frame.position[0], frame.position[1], yaw_of(frame.orientation))
        room_id = room_at(conn, map_id, pose[0], pose[1])
    with transaction(conn):
        event_id = add_event(
            conn,
            map_id,
            new.kind,
            new.text,
            now,
            pose=pose,
            room_id=room_id,
            object_id=object_id,
            person_id=person_id,
            session_id=session_id,
            task_id=new.task_id,
            cause=new.cause,
        )
        record_write(
            conn,
            new.request_id,
            new.task_id,
            REMEMBER_EVENT,
            payload,
            {"event_id": event_id, "ts": now},
            now,
        )
    return event_record(conn.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone())


def _recorded(conn: sqlite3.Connection, new: NewEvent, outcome: dict, map_id: str) -> EventRecord:
    """The first answer to a retried request.

    An event id is never given twice (migration 0007), so the row with the recorded
    id is the recorded line, and no row means the line was forgotten. The time and
    the task are compared too. That only matters for an id that SQLite gave twice
    before migration 0007, or that a writer outside the node let SQLite choose. A
    line that is gone keeps its id and time and has no text. The retry's own text is
    not stored or echoed, because the line was forgotten on purpose.
    """
    row = conn.execute("SELECT * FROM event WHERE id = ?", (outcome["event_id"],)).fetchone()
    if row is not None and row["ts"] == outcome["ts"] and row["task_id"] == new.task_id:
        return event_record(row)
    return EventRecord(
        id=outcome["event_id"],
        ts=outcome["ts"],
        kind=new.kind,
        text="",
        map_id=map_id,
        task_id=new.task_id,
        cause=new.cause,
    )


def _person_and_session(conn: sqlite3.Connection, new: NewEvent) -> tuple[int | None, int | None]:
    """The person and session a line names, checked against each other.

    A session of 0 selects the open one only when the line names a person. A person
    kept across sessions needs no session.
    """
    if new.session_id:
        found = conn.execute("SELECT id FROM session WHERE id = ?", (new.session_id,)).fetchone()
        if found is None:
            raise Rejected("NOT_FOUND", f"Session {new.session_id} does not exist.")
    if not new.person_id:
        return None, new.session_id or None
    person = conn.execute("SELECT session_id FROM person WHERE id = ?", (new.person_id,)).fetchone()
    if person is None:
        raise Rejected("NOT_FOUND", f"Person {new.person_id} does not exist.")
    session_id: int | None = new.session_id
    if not session_id:
        session = open_session(conn)
        session_id = None if session is None else session["id"]
    if person["session_id"] is not None and person["session_id"] != session_id:
        raise Refused(f"Person {new.person_id} belongs to session {person['session_id']}, not this line's.")
    return new.person_id, session_id
