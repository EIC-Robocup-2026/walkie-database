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

``~/localize_objects`` (``walkie_database_msgs/srv/LocalizeObjects``)
    The centroid in the map of each detection of an array, with TF at the array's
    stamp. It stores nothing (ADR-0019).

``~/search_objects``, ``~/get_object``, ``~/find_near``, ``~/list_rooms`` and ``~/get_room_contents``
    Reads of what the store holds, through ObjectRepository (ADR-0021).

``~/remember_event`` and ``~/find_events``
    The task's log. The node adds the clock, the robot's pose and its room (ADR-0022).

``~/get_store_status``
    What this store is and what is switched on. It answers in either mode.

``~/objects/snapshot`` (``std_msgs/String``)
    The store's objects as JSON, published once when the anchors are stamped. Only
    memory mode publishes it. Transient local, so a late subscriber still gets it.

``~/set_object_state``, ``~/report_sighting``, ``~/arena_reset``,
``~/submit_detections`` and ``~/describe_object``
    Their types are final, and they answer NOT_READY until the object memory
    exists. They read nothing and keep nothing.

The node keeps no session state in memory: it is all in the file.

It holds the store's lock (``<db_path>.lock``) for its whole life, so a second
node or an offline tool on the same file refuses to start (ADR-0023).

With ``memory_enabled`` false, the default, it runs as it always has: it needs no
map, and it creates the file at ``db_path`` if needed. With ``memory_enabled`` true
it passes the map gate first. ``db_path``, ``map_folder`` and ``map_id`` are
required and read-only, and the store's map row, full digest, baseline and clock
must match them and ``use_sim_time``. A mismatch stops the start before any service
exists. A seeded store's anchors are stamped once, on the first clock sample.

Memory mode also takes the arrays on the ``objects_topic`` parameter's topic. It
checks and counts each one, and keeps only the newest valid one. No array creates an
object (ADR-0019). Without memory mode there is no subscription, and the services
that need a map answer NOT_READY.

Every start commits a new ``store_id``. Timestamps come from the node's clock, so
in a simulation they are sim time.
"""

from __future__ import annotations

import functools
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import rclpy
from geometry_msgs.msg import Point, Quaternion, Vector3
from rcl_interfaces.msg import ParameterDescriptor, SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rosgraph_msgs.msg import Clock
from std_msgs.msg import String
from walkie_database_msgs.msg import (
    Fact,
    ModelInfo,
    OperationStatus,
    PersonMatch,
    PersonMatches,
    RememberedObject,
)
from walkie_database_msgs.srv import (
    ArenaReset,
    DescribeObject,
    EndSession,
    FindEvents,
    FindNear,
    FindObjects,
    GetObject,
    GetRoomContents,
    GetStoreStatus,
    ListRooms,
    LocalizeObjects,
    MatchPeople,
    RememberEvent,
    RememberPerson,
    ReportSighting,
    SearchObjects,
    SetObjectState,
    StartSession,
    SubmitDetections,
)
from walkie_vision_msgs.msg import ObjectDetectionArray

from walkie_database import people
from walkie_database.db import connect, transaction
from walkie_database.events import NewEvent
from walkie_database.geometry import FramePose
from walkie_database.ingress import ObservationSlot, check_observation
from walkie_database.localize import FrameSource, localize
from walkie_database.map_config import (
    ROS_SIM,
    WALL,
    MapFolder,
    MapGateError,
    check_store,
    read_map_folder,
)
from walkie_database.migrate import migrate
from walkie_database.object_context import Rejected, WriteConflict
from walkie_database.objects import OBJECT
from walkie_database.queries import DEFAULT_THING_COSINE, ObjectRepository
from walkie_database.results import (
    cloud_message,
    event_message,
    object_match,
    object_summary,
    prior_message,
    room_summary,
    snapshot_text,
    surface_message,
    to_query,
)
from walkie_database.results import to_time as _time
from walkie_database.store_lock import StoreLock, StoreLocked

# max_results of FindObjects and MatchPeople when the caller sends 0, and the
# most it may ask for. The node runs one callback at a time, so a search must
# stay short.
DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_LIMIT = 100

# The range of RememberedObject.n_detections and of the status counters (uint32).
_UINT32_MAX = 2**32 - 1
_NANOSEC_PER_S = 1_000_000_000

# Every position query filters by map (ADR-0008). An empty label matches
# every object. COLLATE NOCASE compares ASCII letters ignoring case, so "Cup"
# finds "cup". RememberedObject has a position and no state, so a seeded anchor
# with no measured box, or a tombstone, would read as an object at the origin.
_FIND_OBJECTS_SQL = """
SELECT o.id, o.label, o.caption, o.map_id, m.frame_id,
       o.center_x, o.center_y, o.center_z, o.qx, o.qy, o.qz, o.qw,
       o.size_x, o.size_y, o.size_z, r.name AS room, o.n_detections, o.last_seen
FROM object AS o
JOIN map AS m ON m.id = o.map_id
LEFT JOIN room AS r ON r.id = o.room_id
WHERE o.map_id = :map_id
  AND o.geometry_valid = 1
  AND o.state NOT IN ('discarded', 'merged')
  AND (:label = '' OR o.label = :label COLLATE NOCASE)
ORDER BY o.last_seen DESC
LIMIT :limit
"""

_READ_ONLY = ParameterDescriptor(read_only=True)
# As rclpy's TimeSource subscribes, which any /clock publisher can reach.
_CLOCK_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
# Only the newest observation matters, and a late one is stale anyway. A reliable
# publisher also matches this.
_OBSERVATION_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)
# A late subscriber, such as a baseline overlay, still gets the seeded boxes.
_SNAPSHOT_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)
_REJECTED_LOG_PERIOD_S = 5.0
_MAX_STATUS_MESSAGE = 512


class _ClockNotReady(ValueError):
    """Use_sim_time is on, and no /clock sample has arrived."""


def _answer(response, code: str, message: str):
    """Set a response's status. *code* names an OperationStatus constant."""
    response.status = OperationStatus(
        code=getattr(OperationStatus, code), message=message[:_MAX_STATUS_MESSAGE]
    )
    return response


def _status_handler(verb: str):
    """Answer a handler's refusals and failures as a status, and its return as OK.

    The handler returns the OK message, or None. It assigns its results last, so that
    a failure sends none of them.
    """

    def wrap(method):
        @functools.wraps(method)
        def handler(self, request, response):
            try:
                message = method(self, request, response)
            except WriteConflict as e:
                return _answer(response, "CONFLICT", str(e))
            except Rejected as e:
                return _answer(response, e.code, str(e))
            except people.Refused as e:
                return _answer(response, "INVALID", str(e))
            except (sqlite3.Error, ValueError) as e:
                self.get_logger().error(f"{method.__name__} failed: {e}")
                return _answer(response, "INTERNAL", f"The database could not {verb}: {e}")
            return _answer(response, "OK", message or "")

        return handler

    return wrap


class DatabaseNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__("walkie_database", **kwargs)
        # Use this connection only from the executor's thread. sqlite3
        # connections refuse calls from other threads by default.
        self.conn: sqlite3.Connection | None = None
        self._store_lock: StoreLock | None = None
        self._first_clock = None
        self._waiting_for_clock = False
        self._refresh_pending = False
        # The snapshot's sequence starts at zero for each store_id.
        self.publication_sequence = 0
        self._frames: FrameSource | None = None
        self._observations: ObservationSlot | None = None
        self._map_frame = ""
        self._object_models: dict[str, int] = {}
        # The only way a query handler reaches the store (ADR-0021).
        self._objects: ObjectRepository | None = None
        self._snapshot_publisher = None
        try:
            self._start()
        except BaseException:
            self.destroy_node()
            raise

    def _start(self) -> None:
        memory = self.declare_parameter("memory_enabled", False, _READ_ONLY).value
        startup = _READ_ONLY if memory else ParameterDescriptor()
        # Memory mode has no default store, so it never opens the owner's by accident.
        self.db_path = self.declare_parameter(
            "db_path", "" if memory else "data/walkie.db", startup
        ).value
        # The map the robot is using (map.id), for requests that name none.
        # find_objects reads it on every request. Without memory mode,
        # `ros2 param set` takes effect at once. In memory mode the gate fixed it.
        self.declare_parameter("map_id", "", startup)
        map_folder = self.declare_parameter("map_folder", "", _READ_ONLY).value
        objects_topic = self.declare_parameter(
            "objects_topic", "/walkie_vision/objects", _READ_ONLY
        ).value
        reference_frames = self.declare_parameter(
            "reference_frames", ["base_footprint"], _READ_ONLY
        ).value
        robot_frame = self.declare_parameter("robot_frame", "base_footprint", _READ_ONLY).value
        self.memory_enabled = memory
        self._sim_time = bool(self.get_parameter("use_sim_time").value)
        self.add_on_set_parameters_callback(self._keep_clock)

        folder = self._read_gate_inputs(map_folder) if memory else None
        self._store_lock = StoreLock(self.db_path)
        self._store_lock.acquire()
        if folder is not None:
            self._check_map_gate(folder)
        self.conn = connect(self.db_path)
        version = migrate(self.conn)
        self.get_logger().info(f"Opened {self.db_path} at schema version {version}")
        # A store with no store_meta row, as the old people-only files, has none to change.
        with transaction(self.conn):
            self.conn.execute("UPDATE store_meta SET store_id = ?", (str(uuid.uuid4()),))
        self._start_clock()
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
        self.create_service(LocalizeObjects, "~/localize_objects", self.localize_objects)
        self.create_service(SearchObjects, "~/search_objects", self.search_objects)
        self.create_service(GetObject, "~/get_object", self.get_object)
        self.create_service(FindNear, "~/find_near", self.find_near)
        self.create_service(ListRooms, "~/list_rooms", self.list_rooms)
        self.create_service(GetRoomContents, "~/get_room_contents", self.get_room_contents)
        self.create_service(RememberEvent, "~/remember_event", self.remember_event)
        self.create_service(FindEvents, "~/find_events", self.find_events)
        self.create_service(GetStoreStatus, "~/get_store_status", self.get_store_status)
        for service_type, name in (
            (SetObjectState, "set_object_state"),
            (ReportSighting, "report_sighting"),
            (ArenaReset, "arena_reset"),
            (SubmitDetections, "submit_detections"),
            (DescribeObject, "describe_object"),
        ):
            self.create_service(service_type, f"~/{name}", self._not_built)
        if memory:
            self._start_observations(objects_topic)
            self._objects = ObjectRepository(
                self.conn,
                map_id=self._map_id_parameter(),
                frame_id=self._map_frame,
                locate=self._locate,
                reference_frames=reference_frames,
                robot_frame=robot_frame,
                thing_cosine=self._read_thing_cosines(),
            )
            self._snapshot_publisher = self.create_publisher(
                String, "~/objects/snapshot", _SNAPSHOT_QOS
            )
            if not self._sim_time:
                self._publish_snapshot(self._now())
        else:
            self._objects = ObjectRepository(self.conn)

    def _read_thing_cosines(self) -> dict[str, float]:
        """Per model, the cosine that stands in for a thing's words. A value that cannot be one stops the start."""
        thresholds = {}
        for model in sorted(self._object_models):
            name = f"thing_cosine.{model}"
            value = self.declare_parameter(name, DEFAULT_THING_COSINE, _READ_ONLY).value
            if not -1.0 <= value <= 1.0:
                raise MapGateError(f"{name} is a cosine from -1 to 1, not {value}.")
            thresholds[model] = value
        return thresholds

    def _publish_snapshot(self, now: float) -> None:
        """Publish the store as seeded. A failure is logged, and the services stay up."""
        try:
            text = snapshot_text(
                self._objects.snapshot(now), sequence=self.publication_sequence, now=now
            )
        except (sqlite3.Error, ValueError) as e:
            self.get_logger().error(f"Could not build the object snapshot: {e}")
            return
        self._snapshot_publisher.publish(String(data=text))

    def _locate(self, frame: str, now: float) -> FramePose | None:
        """Where *frame* is in the map at exactly *now*, or None. Never the latest instead.

        Time 0 means the latest transform to tf2, so a sim at time 0 has no answer.
        """
        if now <= 0:
            return None
        stamp = rclpy.time.Time.from_msg(_time(now, "the node's clock has the time"))
        found = self._frames.lookup(self._map_frame, frame, stamp)
        if found is None:
            return None
        t, r = found.transform.translation, found.transform.rotation
        return FramePose((t.x, t.y, t.z), (r.x, r.y, r.z, r.w))

    def _start_observations(self, objects_topic: str) -> None:
        """Read what never changes while the node runs, then listen for arrays and TF.

        The schema and the map are fixed for the node's life, so a topic callback runs
        no SQL.
        """
        row = self.conn.execute(
            "SELECT frame_id FROM map WHERE id = ?", (self._map_id_parameter(),)
        ).fetchone()
        self._map_frame = row["frame_id"]
        self._object_models = dict(people.vector_tables(self.conn, OBJECT).lengths)
        self._observations = ObservationSlot(self._object_models, self._map_frame)
        self._frames = FrameSource(self.get_clock())
        self.create_subscription(
            ObjectDetectionArray, objects_topic, self._on_objects, _OBSERVATION_QOS
        )

    def _on_objects(self, message: ObjectDetectionArray) -> None:
        try:
            now = self._now()
        except _ClockNotReady:
            now = None
        problem = self._observations.receive(message, now)
        if problem is not None:
            self.get_logger().warning(
                f"Refused an array: {problem}", throttle_duration_sec=_REJECTED_LOG_PERIOD_S
            )

    def _read_gate_inputs(self, map_folder: str) -> MapFolder:
        """Check what memory mode needs, before anything is written beside the store."""
        values = {"db_path": self.db_path, "map_folder": map_folder, "map_id": self._map_id_parameter()}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise MapGateError(f"Memory mode needs the parameters {', '.join(missing)}.")
        folder = read_map_folder(map_folder)
        if not Path(self.db_path).is_file():
            raise MapGateError(
                f"{self.db_path} does not exist, and memory mode never creates a store. "
                "Restore a seed to it first."
            )
        return folder

    def _check_map_gate(self, folder: MapFolder) -> None:
        """Refuse a store of another map or clock. This only reads, so a refused store stays as it was."""
        clock_kind = ROS_SIM if self._sim_time else WALL
        try:
            with closing(connect(self.db_path, readonly=True)) as conn:
                check_store(conn, folder, clock_kind, self._map_id_parameter())
        except sqlite3.DatabaseError as e:
            raise MapGateError(f"{self.db_path} cannot be read: {e}") from e

    def _keep_clock(self, parameters: list[Parameter]) -> SetParametersResult:
        """Refuse a clock change. The store's times must all come from one clock."""
        for parameter in parameters:
            if parameter.name == "use_sim_time" and bool(parameter.value) != self._sim_time:
                return SetParametersResult(
                    successful=False, reason="The node's clock is fixed at start-up."
                )
        return SetParametersResult(successful=True)

    def _start_clock(self) -> None:
        """Stamp a seeded store's anchors now, or wait for the first /clock sample.

        The refresh runs once per seed or restore. A later start, also after a
        crash, finds the flag clear and leaves the times alone, so that a reboot
        does not make stale anchors look fresh.

        This subscribes separately from rclpy's own clock. rclpy's reads 0 both
        before a sample and for a sample at time 0, and a sim may sit at 0.
        """
        if self.memory_enabled:
            row = self.conn.execute("SELECT refresh_anchors_pending FROM store_meta").fetchone()
            self._refresh_pending = bool(row["refresh_anchors_pending"])
        if self._sim_time:
            self._waiting_for_clock = True
            self._first_clock = self.create_subscription(
                Clock, "/clock", self._on_first_clock, _CLOCK_QOS
            )
        elif self._refresh_pending:
            self._stamp_seeded_anchors(self._now())

    def _on_first_clock(self, message: Clock) -> None:
        try:
            if self._refresh_pending:
                self._stamp_seeded_anchors(message.clock.sec + message.clock.nanosec / 1e9)
        except sqlite3.Error as e:
            # An exception here would stop the node. The next sample tries again.
            self.get_logger().error(f"Could not stamp the seeded anchors: {e}")
            return
        # rclpy's own subscription may not have this sample yet, and _now must not read 0.
        self.get_clock().set_ros_time_override(rclpy.time.Time.from_msg(message.clock))
        self._waiting_for_clock = False
        self.destroy_subscription(self._first_clock)
        self._first_clock = None
        if self.memory_enabled:
            self._publish_snapshot(message.clock.sec + message.clock.nanosec / 1e9)

    def _stamp_seeded_anchors(self, now: float) -> None:
        """Stamp the anchors and clear the flag together, in one transaction."""
        with transaction(self.conn):
            self.conn.execute(
                """UPDATE object SET first_seen = :now, last_seen = :now, last_verified = :now
                   WHERE layer = 'anchor' AND source = 'map'
                     AND state NOT IN ('discarded', 'merged')""",
                {"now": now},
            )
            self.conn.execute("UPDATE store_meta SET refresh_anchors_pending = 0")
        self._refresh_pending = False
        self.get_logger().info(f"Stamped the seeded anchors at {now:.3f}.")

    def _now(self) -> float:
        """Seconds on the node's clock: wall time, or /clock with use_sim_time.

        Raises ValueError before the first sample, which callers report as a
        database failure, because the same request may work later. A time of 0
        would claim that this happened at the start of the sim.
        """
        if self._waiting_for_clock:
            raise _ClockNotReady("no /clock sample has arrived yet")
        return self.get_clock().now().nanoseconds / 1e9

    def _memory_now(self) -> float:
        """The clock's time for a service that needs a map, or a NOT_READY refusal."""
        if not self.memory_enabled:
            raise Rejected("NOT_READY", "The node runs without memory mode, so it has no map.")
        try:
            return self._now()
        except _ClockNotReady as e:
            raise Rejected("NOT_READY", f"The node's clock is not ready: {e}.") from None

    def _check_map(self, map_id: str) -> None:
        """Refuse a request that names another map. An empty name selects the node's."""
        if map_id and map_id != self._map_id_parameter():
            raise Rejected(
                "WRONG_MAP", f"This node serves the map {self._map_id_parameter()}, not {map_id}."
            )

    def _not_built(self, _request, response):
        """Answer a write that arrives with the object memory. This reads and keeps nothing."""
        return _answer(response, "NOT_READY", "This service arrives with the object memory.")

    @_status_handler("localize the objects")
    def localize_objects(self, request, response) -> None:
        now = self._memory_now()
        self._check_map(request.map_id)
        check_observation(
            request.objects, models=self._object_models, map_frame=self._map_frame, now=now
        )
        response.results = localize(request.objects, map_frame=self._map_frame, frames=self._frames)

    @_status_handler("search the objects")
    def search_objects(self, request, response) -> None:
        now = self._memory_now()
        self._check_map(request.query.map_id)
        vector = (request.vector.model, request.vector.embedding) if request.use_embedding else None
        found = self._objects.search(
            to_query(request.query),
            vector=vector,
            text=request.text,
            max_results=request.max_results,
            now=now,
        )
        matches = [object_match(match) for match in found.matches]
        response.matches = matches
        response.priors = [prior_message(prior) for prior in found.priors]
        response.lexical_only = found.lexical_only
        response.truncated = found.truncated

    @_status_handler("read the object")
    def get_object(self, request, response) -> None:
        now = self._memory_now()
        self._check_map(request.map_id)
        found = self._objects.get_object(
            request.object_id, include_cloud=request.include_cloud, now=now
        )
        summary = object_summary(found.record)
        surfaces = [surface_message(surface) for surface in found.surfaces]
        cloud = cloud_message(found.cloud, self._map_frame) if found.cloud is not None else None
        response.requested_id = found.requested_id
        response.merged = found.merged
        response.object = summary
        response.surfaces = surfaces
        if cloud is not None:
            response.cloud = cloud

    @_status_handler("find the nearby objects")
    def find_near(self, request, response) -> None:
        now = self._memory_now()
        self._check_map(request.query.map_id)
        found = self._objects.find_near(
            to_query(request.query), max_results=request.max_results, now=now
        )
        response.matches = [object_match(match) for match in found.matches]
        response.truncated = found.truncated

    @_status_handler("list the rooms")
    def list_rooms(self, request, response) -> None:
        self._memory_now()
        self._check_map(request.map_id)
        response.rooms = [room_summary(room, self._map_frame) for room in self._objects.rooms()]

    @_status_handler("read the room's contents")
    def get_room_contents(self, request, response) -> None:
        now = self._memory_now()
        self._check_map(request.query.map_id)
        room, found = self._objects.room_contents(
            to_query(request.query), max_results=request.max_results, now=now
        )
        summary = room_summary(room, self._map_frame)
        objects = [object_match(match) for match in found.matches]
        response.room = summary
        response.objects = objects
        response.truncated = found.truncated

    @_status_handler("remember the event")
    def remember_event(self, request, response) -> None:
        now = self._memory_now()
        self._check_map(request.map_id)
        event = self._objects.remember_event(
            NewEvent(
                request_id=request.request_id,
                task_id=request.task_id,
                cause=request.cause,
                kind=request.kind,
                text=request.text,
                object_id=request.object_id,
                person_id=request.person_id,
                session_id=request.session_id,
            ),
            now=now,
        )
        response.event = event_message(event, self._map_frame)

    @_status_handler("find the events")
    def find_events(self, request, response) -> None:
        self._memory_now()
        self._check_map(request.map_id)
        since = None
        if request.has_since:
            if request.since.nanosec >= _NANOSEC_PER_S:
                raise Rejected("INVALID", f"since has nanosec {request.since.nanosec}.")
            since = request.since.sec + request.since.nanosec / _NANOSEC_PER_S
        events, truncated = self._objects.find_events(
            text=request.text, since=since, room=request.room, max_results=request.max_results
        )
        response.events = [event_message(event, self._map_frame) for event in events]
        response.truncated = truncated

    @_status_handler("read the store's status")
    def get_store_status(self, _request, response) -> None:
        """Answers in either mode and needs no clock. It is how a client learns the mode."""
        status = self._objects.store_status()
        response.map_id = self._map_id_parameter()
        response.map_hash = status.map_hash
        response.baseline_rev = status.baseline_rev
        response.store_id = status.store_id
        response.clock_kind = ROS_SIM if self._sim_time else WALL
        response.schema_version = status.schema_version
        response.map_valid = self.memory_enabled
        # Nothing feeds the object memory, and no caption is made, in this stage.
        response.ingest_enabled = False
        response.captions_enabled = False
        response.object_embedding_models = status.models
        if self._observations is not None:
            response.arrays_received = min(self._observations.received, _UINT32_MAX)
            response.arrays_rejected = min(self._observations.rejected, _UINT32_MAX)

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
                self.conn, request.label, request.resume, self._now()
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
                self._now(),
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
        if self._frames is not None:
            self._frames.close()
            self._frames = None
        if self.conn is not None:
            self.conn.close()
        # After the close, which is the store's last write.
        if self._store_lock is not None:
            self._store_lock.release()
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


def main() -> None:
    rclpy.init()
    try:
        node = DatabaseNode()
    except (MapGateError, StoreLocked) as e:
        rclpy.logging.get_logger("walkie_database").fatal(f"Not starting: {e}")
        rclpy.try_shutdown()
        raise SystemExit(1) from None
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
