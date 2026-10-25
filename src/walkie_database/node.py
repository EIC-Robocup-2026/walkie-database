"""The database node: owns Walkie's SQLite file.

It opens the file at the ``db_path`` parameter and brings its schema up to
date. It is the only process that writes the file (ADR-0005). Other
processes read through its topics and services:

``~/find_objects`` (``walkie_database_msgs/srv/FindObjects``)
    Remembered objects with a given label in one map, most recently seen
    first. A request that names no map searches the ``map_id`` parameter's.

``~/start_session`` (``walkie_database_msgs/srv/StartSession``)
    Resumes the open session, or forgets it and starts a new one. At most
    one session is open (ADR-0017).

``~/end_session`` (``walkie_database_msgs/srv/EndSession``)
    Forgets a session, with the people met in it (ADR-0010).

``~/remember_person`` (``walkie_database_msgs/srv/RememberPerson``)
    Stores a new person in the open session, with facts, faces and
    appearances.

``~/match_faces`` and ``~/match_appearances`` (``walkie_database_msgs/srv/MatchPeople``)
    For each query, the people nearest by face or by appearance, each once,
    nearest first (ADR-0018).

The node keeps no session state in memory: it is all in the file.
"""

from __future__ import annotations

import math
import sqlite3
import time

import rclpy
from builtin_interfaces.msg import Time
from geometry_msgs.msg import Point, Quaternion, Vector3
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from walkie_database_msgs.msg import Fact, ModelInfo, PersonMatch, PersonMatches, RememberedObject
from walkie_database_msgs.srv import (
    EndSession,
    FindObjects,
    MatchPeople,
    RememberPerson,
    StartSession,
)

from walkie_database import people
from walkie_database.db import connect
from walkie_database.migrate import migrate

# max_results of FindObjects and MatchPeople when the caller sends 0, and the
# most it may ask for. The node runs one callback at a time, so a search must
# stay short.
DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_LIMIT = 100

# The ranges of builtin_interfaces/Time.sec (int32) and of
# RememberedObject.n_detections (uint32).
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
_UINT32_MAX = 2**32 - 1

# Every position query filters by map (ADR-0008). An empty label matches
# every object. COLLATE NOCASE compares ASCII letters ignoring case, so "Cup"
# finds "cup".
_FIND_OBJECTS_SQL = """
SELECT o.id, o.label, o.caption, o.map_id, m.frame_id,
       o.center_x, o.center_y, o.center_z, o.qx, o.qy, o.qz, o.qw,
       o.size_x, o.size_y, o.size_z, r.name AS room, o.n_detections, o.last_seen
FROM object AS o
JOIN map AS m ON m.id = o.map_id
LEFT JOIN room AS r ON r.id = o.room_id
WHERE o.map_id = :map_id
  AND (:label = '' OR o.label = :label COLLATE NOCASE)
ORDER BY o.last_seen DESC
LIMIT :limit
"""


class DatabaseNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__("walkie_database", **kwargs)
        self.db_path = self.declare_parameter("db_path", "data/walkie.db").value
        # The map the robot is using (map.id), for requests that name none.
        # find_objects reads it on every request, so `ros2 param set` takes
        # effect at once.
        self.declare_parameter("map_id", "")
        # Use this connection only from the executor's thread. sqlite3
        # connections refuse calls from other threads by default.
        self.conn = connect(self.db_path)
        version = migrate(self.conn)
        self.get_logger().info(f"Opened {self.db_path} at schema version {version}")
        map_id = self._map_id_parameter()
        if map_id and not self._map_exists(map_id):
            self.get_logger().warning(
                f"The map_id parameter names '{map_id}', which is not in the map "
                f"table of {self.db_path}. Searches that name no map fail until "
                "it is."
            )
        session = people.open_session(self.conn)
        if session is not None:
            count = people.count_people(self.conn, session["id"])
            self.get_logger().info(f"Session {session['id']} is open, with {_people(count)}.")
        for kind in people.KINDS:
            for table in people.vector_tables(self.conn, kind).unreadable:
                self.get_logger().warning(
                    f"The node cannot read the length of the embedding column of "
                    f"{table}, so it refuses vectors of that model."
                )
        self.create_service(FindObjects, "~/find_objects", self.find_objects)
        self.create_service(StartSession, "~/start_session", self.start_session)
        self.create_service(EndSession, "~/end_session", self.end_session)
        self.create_service(RememberPerson, "~/remember_person", self.remember_person)
        self.create_service(MatchPeople, "~/match_faces", self.match_faces)
        self.create_service(MatchPeople, "~/match_appearances", self.match_appearances)

    def find_objects(
        self, request: FindObjects.Request, response: FindObjects.Response
    ) -> FindObjects.Response:
        map_id = request.map_id.strip()
        source = "the request"
        if not map_id:
            map_id = self._map_id_parameter()
            source = "the node's map_id parameter"
        if not map_id:
            response.success = False
            response.message = (
                "No map to search: the request names none and the node's "
                "map_id parameter is empty."
            )
            return response
        limit = min(request.max_results or DEFAULT_MAX_RESULTS, MAX_RESULTS_LIMIT)
        try:
            if not self._map_exists(map_id):
                response.success = False
                response.message = (
                    f"Unknown map '{map_id}', from {source}: the database has no "
                    "map with that id."
                )
                return response
            # One row more than the limit shows whether any were left out.
            rows = self.conn.execute(
                _FIND_OBJECTS_SQL,
                {"map_id": map_id, "label": request.label.strip(), "limit": limit + 1},
            ).fetchall()
            objects = [_remembered_object(row) for row in rows[:limit]]
        except (sqlite3.Error, ValueError) as e:
            self.get_logger().error(f"find_objects failed: {e}")
            response.success = False
            response.message = f"The database could not search its objects: {e}"
            return response
        response.objects = objects
        response.success = True
        response.message = (
            f"Found {len(objects)} object{'' if len(objects) == 1 else 's'}"
            + (" (more not shown)." if len(rows) > limit else ".")
        )
        return response

    def start_session(
        self, request: StartSession.Request, response: StartSession.Response
    ) -> StartSession.Response:
        try:
            started = people.start_session(
                self.conn, request.label, request.resume, time.time()
            )
            started_at = _time(started.started_at, f"session {started.id} has started_at")
            models = {kind: people.vector_tables(self.conn, kind) for kind in people.KINDS}
        except (sqlite3.Error, ValueError) as e:
            return self._failed(response, "start_session", "start a session", e)
        response.success = True
        response.session_id = started.id
        response.resumed = started.resumed
        response.people = started.people
        response.started_at = started_at
        response.face_models = _model_infos(models[people.FACE])
        response.appearance_models = _model_infos(models[people.APPEARANCE])
        if started.resumed:
            response.message = f"Resumed session {started.id}, with {_people(started.people)}."
        elif request.resume:
            response.message = f"No session was open, so started session {started.id}."
        elif started.forgot_id:
            response.message = (
                f"{_forgot(started.forgot_id, started.forgot_people)}, "
                f"and started session {started.id}."
            )
        else:
            response.message = f"Started session {started.id}."
        self.get_logger().info(response.message)
        return response

    def end_session(
        self, request: EndSession.Request, response: EndSession.Response
    ) -> EndSession.Response:
        try:
            forgot_id, count = people.end_session(self.conn, request.session_id)
        except (sqlite3.Error, ValueError) as e:
            return self._failed(response, "end_session", "forget the session", e)
        response.success = True
        response.session_id = forgot_id
        response.people = count
        if forgot_id:
            response.message = f"{_forgot(forgot_id, count)}."
            self.get_logger().info(response.message)
        elif request.session_id == 0:
            response.message = "No session is open, so nothing was forgotten."
        else:
            response.message = (
                f"Session {request.session_id} is not in the database, so nothing "
                "was forgotten."
            )
        return response

    def remember_person(
        self, request: RememberPerson.Request, response: RememberPerson.Response
    ) -> RememberPerson.Response:
        try:
            remembered = people.remember_person(
                self.conn,
                request.name,
                [(f.key, f.value) for f in request.facts],
                [(v.model, v.embedding) for v in request.faces],
                [(v.model, v.embedding) for v in request.appearances],
                time.time(),
            )
        except people.Refused as e:
            return self._refused(response, "remember_person", e)
        except (sqlite3.Error, ValueError) as e:
            return self._failed(response, "remember_person", "remember the person", e)
        name = request.name.strip()
        response.success = True
        response.person_id = remembered.person_id
        response.session_id = remembered.session_id
        response.message = (
            f"Remembered {name or 'a person with no name'} as person "
            f"{remembered.person_id} in session {remembered.session_id}, from "
            f"{_count(remembered.faces, 'face')} and "
            f"{_count(remembered.appearances, 'appearance')}."
        )
        if remembered.started_session:
            response.message += (
                f" Session {remembered.session_id} was started for them, because no "
                "session was open."
            )
        return response

    def match_faces(
        self, request: MatchPeople.Request, response: MatchPeople.Response
    ) -> MatchPeople.Response:
        return self._match(request, response, people.FACE, "match_faces", "match faces")

    def match_appearances(
        self, request: MatchPeople.Request, response: MatchPeople.Response
    ) -> MatchPeople.Response:
        return self._match(
            request, response, people.APPEARANCE, "match_appearances", "match appearances"
        )

    def _match(
        self,
        request: MatchPeople.Request,
        response: MatchPeople.Response,
        kind: str,
        service: str,
        verb: str,
    ) -> MatchPeople.Response:
        if not request.queries:
            # Answered without reading the database.
            response.success = True
            response.message = "No queries, so nothing was matched."
            return response
        limit = min(request.max_results or DEFAULT_MAX_RESULTS, MAX_RESULTS_LIMIT)
        try:
            matched = people.match(
                self.conn, kind, [(q.model, q.embedding) for q in request.queries], limit
            )
            results = [
                PersonMatches(people=[_person_match(row) for row in rows])
                for rows in matched.results
            ]
        except people.Refused as e:
            return self._refused(response, service, e)
        except (sqlite3.Error, ValueError) as e:
            return self._failed(response, service, verb, e)
        n = len(results)
        response.success = True
        response.results = results
        response.message = f"Matched {n} {'query' if n == 1 else 'queries'}" + (
            " (more not shown)." if matched.more else "."
        )
        if matched.session_id is None:
            response.message += (
                " No session is open, so only people kept across sessions were searched."
                if kind == people.FACE
                else " No session is open, so no appearance was searched."
            )
        return response

    def _refused(self, response, service: str, e: people.Refused):
        # A caller or configuration bug, such as a model with no table.
        self.get_logger().warning(f"{service} refused: {e}")
        response.success = False
        response.message = str(e)
        return response

    def _failed(self, response, service: str, verb: str, e: Exception):
        self.get_logger().error(f"{service} failed: {e}")
        response.success = False
        # Callers tell a database failure from a refusal by this prefix.
        response.message = f"The database could not {verb}: {e}"
        return response

    def destroy_node(self) -> None:
        self.conn.close()
        super().destroy_node()

    def _map_id_parameter(self) -> str:
        return self.get_parameter("map_id").value.strip()

    def _map_exists(self, map_id: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM map WHERE id = ?", (map_id,)).fetchone()
            is not None
        )


def _remembered_object(row: sqlite3.Row) -> RememberedObject:
    """One row of _FIND_OBJECTS_SQL as a message.

    rclpy does not check integer ranges when it builds a message. A value out
    of range fails only when the response is sent, after the callback, and
    that stops the node. So this raises ValueError for such a row instead.
    """
    if not 0 <= row["n_detections"] <= _UINT32_MAX:
        raise ValueError(
            f"object {row['id']} has n_detections {row['n_detections']}, "
            "which uint32 cannot hold"
        )
    return RememberedObject(
        id=row["id"],
        label=row["label"],
        caption=row["caption"] or "",
        map_id=row["map_id"],
        frame_id=row["frame_id"],
        position=Point(x=row["center_x"], y=row["center_y"], z=row["center_z"]),
        orientation=Quaternion(x=row["qx"], y=row["qy"], z=row["qz"], w=row["qw"]),
        size=Vector3(x=row["size_x"], y=row["size_y"], z=row["size_z"]),
        room=row["room"] or "",
        n_detections=row["n_detections"],
        last_seen=_time(row["last_seen"], f"object {row['id']} has last_seen"),
    )


def _person_match(row: sqlite3.Row) -> PersonMatch:
    """One row of a match query as a message."""
    return PersonMatch(
        person_id=row["person_id"],
        session_id=row["session_id"] or 0,
        name=row["name"] or "",
        facts=[
            Fact(key=key, value=value)
            for key, value in people.facts_of(row["attributes"], row["person_id"])
        ],
        # About -3e-8 for the same direction, which float32 holds as is.
        distance=row["distance"],
    )


def _model_infos(tables: people.VectorTables) -> list[ModelInfo]:
    return [
        ModelInfo(model=model, length=length) for model, length in sorted(tables.lengths.items())
    ]


def _people(n: int) -> str:
    return "no people" if n == 0 else "1 person" if n == 1 else f"{n} people"


def _forgot(session_id: int, n: int) -> str:
    if n == 0:
        return f"Forgot session {session_id}, which had no people"
    return f"Forgot session {session_id} and its {_people(n)}"


def _count(n: int, noun: str) -> str:
    return f"no {noun}s" if n == 0 else f"1 {noun}" if n == 1 else f"{n} {noun}s"


def _time(seconds: float, what: str) -> Time:
    """Seconds since the Unix epoch as a builtin_interfaces/Time.

    *what* names the value for the error, for example "object 4 has
    last_seen".
    """
    if not math.isfinite(seconds) or not _INT32_MIN <= math.floor(seconds) <= _INT32_MAX:
        raise ValueError(f"{what} {seconds!r}, which builtin_interfaces/Time cannot hold")
    # floor, not int: Time.nanosec is never negative, also before 1970.
    sec = math.floor(seconds)
    # Rounding can reach a whole second, and nanosec must stay below one.
    return Time(sec=sec, nanosec=min(round((seconds - sec) * 1e9), 999_999_999))


def main() -> None:
    rclpy.init()
    node = DatabaseNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
