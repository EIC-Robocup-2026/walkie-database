"""The object services and the snapshot, served by a node in memory mode over a seed."""

import json
import math
import time

import pytest
from memory_helpers import call_service, camera_transform, memory_node, memory_store, to_time
from query_helpers import MODEL, Store, direction, with_cosine
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time as RclTime
from rosgraph_msgs.msg import Clock
from seed_helpers import dump_rows, map_id_of, write_map_folder
from std_msgs.msg import String
from walkie_database_msgs.msg import Fact, ModelVector, ObjectQuery, OperationStatus
from walkie_database_msgs.srv import (
    ArenaReset,
    DescribeObject,
    FindEvents,
    FindNear,
    GetObject,
    GetRoomContents,
    GetStoreStatus,
    ListRooms,
    RememberEvent,
    ReportSighting,
    SearchObjects,
    SetObjectState,
    SubmitDetections,
)

from walkie_database.db import connect, transaction
from walkie_database.map_config import MapGateError
from walkie_database.node import DatabaseNode
from walkie_database.queries import ObjectRecord
from walkie_database.results import SNAPSHOT_SCHEMA_VERSION, object_summary, to_time as result_time

OK = OperationStatus.OK
INVALID = OperationStatus.INVALID
NOT_FOUND = OperationStatus.NOT_FOUND
TF_UNAVAILABLE = OperationStatus.TF_UNAVAILABLE
WRONG_MAP = OperationStatus.WRONG_MAP
NOT_READY = OperationStatus.NOT_READY
INTERNAL = OperationStatus.INTERNAL
CONFLICT = OperationStatus.CONFLICT
ROBOT = "base_footprint"
SIM_TIME = 1000.0


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


@pytest.fixture
def store(tmp_path, folder):
    return memory_store(tmp_path, folder)


def add_objects(store_path, folder, build):
    """Add rows to a stopped node's store, as a later core would have."""
    conn = connect(store_path)
    try:
        return build(Store.existing(conn, map_id_of(folder)))
    finally:
        conn.close()


@pytest.fixture
def objects(store, folder):
    """A cup on the table top, with a vector and a cloud, and a box with no measurement."""

    def build(rows):
        table = rows.conn.execute("SELECT id FROM object WHERE key = 'table'").fetchone()["id"]
        top = rows.conn.execute("SELECT id FROM surface WHERE key = 'table_top'").fetchone()["id"]
        item = rows.conn.execute("SELECT id FROM object_catalogue").fetchone()["id"]
        living = rows.conn.execute("SELECT id FROM room WHERE key = 'living_room'").fetchone()["id"]
        cup = rows.add("cup", center=(2.1, 2.0, 0.85), room=living, catalogue=item, vector=with_cosine(0.9),
                       last_verified=time.time(), caption="A blue mug", attributes={"material": "ceramic"})
        rows.on(cup, table, top)
        with transaction(rows.conn):
            rows.conn.execute(
                "INSERT INTO object_cloud (object_id, voxel_size, n_points, points) VALUES (?, 0.01, 2, ?)",
                (cup, bytes(24)),
            )
        boxless = rows.add("box", center=None, room=living, last_verified=None)
        return {"table": table, "cup": cup, "boxless": boxless, "living": living}

    return add_objects(store, folder, build)


@pytest.fixture
def node(ros, store, folder, objects):
    node = memory_node(store, folder)
    yield node
    node.destroy_node()


def wall_now(node) -> float:
    return node.get_clock().now().nanoseconds / 1e9


def teach_robot(node, position=(1.0, 1.0, 0.0)) -> None:
    """Robot poses around now, so that a lookup at any moment of the test interpolates."""
    now = wall_now(node)
    for stamp in (now - 1.0, now + 60.0):
        node._frames.buffer.set_transform(camera_transform(stamp, position, child=ROBOT), "test")


def query(**fields) -> ObjectQuery:
    attributes = [Fact(key=key, value=value) for key, value in fields.pop("attributes", ())]
    return ObjectQuery(attributes=attributes, **fields)


def rows_of(store) -> dict:
    conn = connect(store, readonly=True)
    try:
        return dump_rows(conn)
    finally:
        conn.close()


# SearchObjects ---------------------------------------------------------------


def test_search_answers_over_the_wire_with_full_summaries(node, objects, folder):
    request = SearchObjects.Request(query=query(thing="mug", attributes=[("colour", "blue"), ("size", "small")]))

    response = call_service(node, SearchObjects, "search_objects", request)

    assert response.status.code == OK
    assert response.lexical_only
    [match] = response.matches
    cup = match.object
    assert cup.id == objects["cup"]
    assert cup.map_id == map_id_of(folder)
    assert cup.labels == ["cup", "mug"]
    assert (cup.official_name, cup.category, cup.room, cup.place) == ("blue cup", "cup", "living room", "table_top")
    assert {(fact.key, fact.value) for fact in cup.attributes} == {
        ("colour", "blue"), ("fragile", "true"), ("material", "ceramic")
    }
    assert cup.has_weight and cup.weight_g == pytest.approx(250.0)
    assert cup.geometry_valid
    assert cup.pose.header.frame_id == "map"
    assert cup.pose.pose.position.x == pytest.approx(2.1)
    assert cup.has_last_verified
    assert 0.0 <= cup.last_verified_age_s < 60.0
    assert list(match.matched_attributes) == ["colour"]
    assert list(match.unknown_attributes) == ["size"]
    assert not match.has_similarity


def test_a_record_with_no_box_and_no_weight_says_so_on_the_wire(node, objects):
    response = call_service(node, SearchObjects, "search_objects", SearchObjects.Request(query=query(thing="box")))

    [match] = response.matches
    box = match.object
    assert not box.geometry_valid
    assert box.pose.header.frame_id == ""
    assert (box.size.x, box.size.y, box.size.z) == (0.0, 0.0, 0.0)
    assert not box.has_weight and box.weight_g == 0.0
    assert not box.has_last_verified
    assert box.p_now == 0.0 and box.last_verified_age_s == 0.0


def test_an_unseen_catalogue_item_is_a_prior_and_not_a_match(ros, store, folder):
    node = memory_node(store, folder)
    try:
        response = call_service(node, SearchObjects, "search_objects", SearchObjects.Request(query=query(thing="mug")))
    finally:
        node.destroy_node()

    assert response.status.code == OK
    assert response.matches == []
    [prior] = response.priors
    assert (prior.official_name, prior.place, prior.room) == ("blue cup", "table", "living room")


def test_a_vector_search_reports_its_similarity(node, objects):
    request = SearchObjects.Request(
        query=query(thing="cup"), use_embedding=True, vector=ModelVector(model=MODEL, embedding=direction(1.0))
    )

    response = call_service(node, SearchObjects, "search_objects", request)

    assert response.status.code == OK
    assert not response.lexical_only
    [match] = response.matches
    assert match.has_similarity
    assert match.similarity == pytest.approx(0.9, abs=1e-5)


def test_a_vector_of_an_unknown_model_is_unsupported(node):
    request = SearchObjects.Request(use_embedding=True, vector=ModelVector(model="fake_clip", embedding=[1.0]))
    assert node.search_objects(request, SearchObjects.Response()).status.code == OperationStatus.UNSUPPORTED


def test_a_search_of_another_map_is_wrong_map(node):
    request = SearchObjects.Request(query=query(map_id="elsewhere-2026-01-01-00000000"))
    assert node.search_objects(request, SearchObjects.Response()).status.code == WRONG_MAP


def test_a_bad_request_is_invalid_and_returns_no_results(node):
    response = node.search_objects(SearchObjects.Request(max_results=0), SearchObjects.Response())
    assert response.status.code == INVALID
    assert response.matches == []


def test_a_robot_frame_query_needs_its_transform(node):
    request = SearchObjects.Request(query=query(superlative="nearest"))
    assert node.search_objects(request, SearchObjects.Response()).status.code == TF_UNAVAILABLE

    teach_robot(node, (2.0, 2.0, 0.0))
    response = node.search_objects(request, SearchObjects.Response())
    assert response.status.code == OK
    assert response.matches[-1].measure_unknown


def test_the_reference_frames_are_a_parameter(ros, store, folder, objects):
    node = memory_node(store, folder, reference_frames=["base_link"])
    try:
        request = SearchObjects.Request(query=query(superlative="nearest"))
        assert node.search_objects(request, SearchObjects.Response()).status.code == INVALID
    finally:
        node.destroy_node()


def test_a_thing_cosine_outside_minus_one_to_one_stops_the_start(ros, store, folder):
    with pytest.raises(MapGateError, match="thing_cosine.clip_vit_b16"):
        memory_node(store, folder, **{f"thing_cosine.{MODEL}": 1.5})


def test_a_thing_cosine_is_read_per_model(ros, store, folder, objects):
    node = memory_node(store, folder, **{f"thing_cosine.{MODEL}": 0.95})
    try:
        request = SearchObjects.Request(
            query=query(thing="vase"), use_embedding=True, vector=ModelVector(model=MODEL, embedding=direction(1.0))
        )
        assert node.search_objects(request, SearchObjects.Response()).matches == []
    finally:
        node.destroy_node()


# GetObject -------------------------------------------------------------------


def test_get_object_returns_an_anchors_surfaces(node, objects):
    response = call_service(node, GetObject, "get_object", GetObject.Request(object_id=objects["table"]))

    assert response.status.code == OK
    assert (response.requested_id, response.merged) == (objects["table"], False)
    assert response.object.key == "table"
    [surface] = response.surfaces
    assert (surface.key, surface.anchor_id, surface.geometry_valid) == ("table_top", objects["table"], True)
    assert surface.z == pytest.approx(0.8)
    assert len(surface.outline.points) == 4
    assert all(point.z == pytest.approx(0.8) for point in surface.outline.points)
    assert response.cloud.width == 0


def test_get_object_sends_the_cloud_only_when_asked(node, objects):
    without = call_service(node, GetObject, "get_object", GetObject.Request(object_id=objects["cup"]))
    with_cloud = call_service(
        node, GetObject, "get_object", GetObject.Request(object_id=objects["cup"], include_cloud=True)
    )

    assert without.cloud.width == 0
    cloud = with_cloud.cloud
    assert (cloud.header.frame_id, cloud.width, cloud.height, cloud.point_step) == ("map", 2, 1, 12)
    assert len(cloud.data) == 24
    assert [field.name for field in cloud.fields] == ["x", "y", "z"]


@pytest.mark.parametrize("object_id, code", [(0, INVALID), (99_999, NOT_FOUND)])
def test_get_object_refuses_ids_it_cannot_answer(node, object_id, code):
    assert node.get_object(GetObject.Request(object_id=object_id), GetObject.Response()).status.code == code


def test_a_broken_merge_chain_is_internal_and_the_node_keeps_serving(ros, store, folder):
    def build(rows):
        a, b = rows.add("cup"), rows.add("cup")
        rows.update(a, state="merged", merged_into=b)
        rows.update(b, state="merged", merged_into=a)
        return a

    looped = add_objects(store, folder, build)
    node = memory_node(store, folder)
    try:
        response = call_service(node, GetObject, "get_object", GetObject.Request(object_id=looped))
        assert response.status.code == INTERNAL
        assert "cycle" in response.status.message
        assert call_service(node, ListRooms, "list_rooms", ListRooms.Request()).status.code == OK
    finally:
        node.destroy_node()


# FindNear, rooms and contents ------------------------------------------------


def test_find_near_reads_the_stored_relation(node, objects):
    request = FindNear.Request(query=query(relation="on", reference_key="table_top"))

    response = call_service(node, FindNear, "find_near", request)

    assert response.status.code == OK
    assert [match.object.id for match in response.matches] == [objects["cup"]]


def test_find_near_the_robot_uses_tf_at_the_nodes_time(node, objects):
    request = FindNear.Request(query=query(relation="near", near_distance_m=0.5))
    assert node.find_near(request, FindNear.Response()).status.code == TF_UNAVAILABLE

    # 0.35 m from the cup's centre, and 0.8 m from the table's.
    teach_robot(node, (2.1, 2.0, 1.2))
    response = node.find_near(request, FindNear.Response())

    assert response.status.code == OK
    assert [match.object.id for match in response.matches] == [objects["cup"]]


def test_list_rooms_sorts_by_name_and_sends_the_outlines(node):
    response = call_service(node, ListRooms, "list_rooms", ListRooms.Request())

    assert response.status.code == OK
    assert [room.name for room in response.rooms] == ["kitchen room", "living room"]
    living = response.rooms[1]
    assert living.has_stand_pose
    assert living.stand_pose.header.frame_id == "map"
    assert (living.stand_pose.pose.position.x, living.stand_pose.pose.position.y) == (1.0, 1.0)
    assert sorted((p.x, p.y) for p in living.outline.points) == [(0.0, 0.0), (0.0, 4.0), (4.0, 0.0), (4.0, 4.0)]


def test_get_room_contents_lists_one_room(node, objects):
    request = GetRoomContents.Request(query=query(room="living room"))

    response = call_service(node, GetRoomContents, "get_room_contents", request)

    assert response.status.code == OK
    assert response.room.name == "living room"
    labels = sorted(match.object.label for match in response.objects)
    assert labels == ["box", "cup", "table", "wardrobe"]
    assert not response.truncated


def test_get_room_contents_needs_a_known_room(node):
    for room, code in (("", INVALID), ("attic", NOT_FOUND)):
        request = GetRoomContents.Request(query=query(room=room))
        assert node.get_room_contents(request, GetRoomContents.Response()).status.code == code


# The task's log --------------------------------------------------------------


def remember(node, request_id="r1", text="Put the cup on the table.", **fields):
    request = RememberEvent.Request(
        request_id=request_id, task_id="t1", cause="a test", kind="action", text=text, **fields
    )
    return call_service(node, RememberEvent, "remember_event", request)


def test_an_event_carries_the_robot_pose_at_the_nodes_time(node, objects):
    teach_robot(node)

    response = remember(node, object_id=objects["cup"])

    assert response.status.code == OK
    line = response.event
    assert line.has_robot_pose
    assert line.robot_pose.header.frame_id == "map"
    assert line.robot_pose.header.stamp == line.stamp
    assert (line.robot_pose.pose.position.x, line.robot_pose.pose.position.y) == (1.0, 1.0)
    assert line.room_id == objects["living"]
    assert line.object_id == objects["cup"]
    assert abs(line.stamp.sec - wall_now(node)) < 60


def test_an_event_with_no_transform_has_no_pose_and_no_room(node):
    response = remember(node)

    assert response.status.code == OK
    assert not response.event.has_robot_pose
    assert response.event.robot_pose.header.frame_id == ""
    assert response.event.room_id == 0


def test_an_event_retry_over_the_wire_answers_the_same_line(node, store):
    first = remember(node)
    again = remember(node)
    other = remember(node, text="Another line.")

    assert again.status.code == OK
    assert again.event.id == first.event.id
    assert again.event.stamp == first.event.stamp
    assert other.status.code == CONFLICT
    assert len(rows_of(store)["event"]) == 1


def test_an_event_for_another_map_writes_nothing(node, store):
    response = remember(node, map_id="elsewhere-2026-01-01-00000000")
    assert response.status.code == WRONG_MAP
    assert rows_of(store)["event"] == []
    assert rows_of(store)["write_request"] == []


def test_find_events_over_the_wire(node):
    remember(node, "a", "Opened the front door.")
    remember(node, "b", "Put the blue cup on the table.")

    response = call_service(node, FindEvents, "find_events", FindEvents.Request(text="cup"))

    assert response.status.code == OK
    assert [line.text for line in response.events] == ["Put the blue cup on the table."]
    everything = call_service(node, FindEvents, "find_events", FindEvents.Request())
    assert [line.text for line in everything.events] == ["Put the blue cup on the table.", "Opened the front door."]


def test_find_events_since(node):
    remember(node, "a", "Opened the front door.")
    later = wall_now(node) + 3600
    request = FindEvents.Request(has_since=True, since=to_time(later))
    assert node.find_events(request, FindEvents.Response()).events == []

    bad = FindEvents.Request(has_since=True)
    bad.since.nanosec = 1_000_000_000
    assert node.find_events(bad, FindEvents.Response()).status.code == INVALID


def test_find_events_limits(node):
    assert node.find_events(FindEvents.Request(max_results=0), FindEvents.Response()).status.code == INVALID
    request = FindEvents.Request(room="attic")
    assert node.find_events(request, FindEvents.Response()).status.code == NOT_FOUND


# GetStoreStatus --------------------------------------------------------------


def test_the_store_status_in_memory_mode(node, store, folder):
    response = call_service(node, GetStoreStatus, "get_store_status", GetStoreStatus.Request())

    conn = connect(store, readonly=True)
    try:
        meta = conn.execute("SELECT store_id FROM store_meta").fetchone()
        digest = conn.execute("SELECT map_hash FROM map").fetchone()["map_hash"]
    finally:
        conn.close()
    assert response.status.code == OK
    assert response.map_id == map_id_of(folder)
    assert response.map_hash == digest and len(digest) == 64
    assert response.baseline_rev == ""
    assert response.store_id == meta["store_id"]
    assert response.clock_kind == "wall"
    assert response.schema_version >= 6
    assert response.map_valid
    assert not response.ingest_enabled and not response.captions_enabled
    assert list(response.object_embedding_models) == [MODEL]
    assert (response.arrays_received, response.arrays_rejected) == (0, 0)


def test_the_store_status_in_legacy_mode(ros, tmp_path):
    node = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(tmp_path / "walkie.db"))])
    try:
        response = call_service(node, GetStoreStatus, "get_store_status", GetStoreStatus.Request())
    finally:
        node.destroy_node()

    assert response.status.code == OK
    assert not response.map_valid
    assert response.map_id == ""
    assert response.map_hash == ""
    # A legacy store was never seeded, so it has no store_meta row and no store_id.
    assert response.store_id == ""
    assert response.clock_kind == "wall"
    assert response.schema_version >= 6


# Without memory mode or a clock ----------------------------------------------


READS = [
    (SearchObjects, "search_objects", {}),
    (GetObject, "get_object", {"object_id": 1}),
    (FindNear, "find_near", {"query": ObjectQuery(relation="on", reference_id=1)}),
    (ListRooms, "list_rooms", {}),
    (GetRoomContents, "get_room_contents", {"query": ObjectQuery(room="living room")}),
    (RememberEvent, "remember_event", {"request_id": "r1", "task_id": "t1", "cause": "c", "kind": "note",
                                       "text": "A line."}),
    (FindEvents, "find_events", {}),
]
WRITES = [
    (SetObjectState, "set_object_state", {"request_id": "r1", "object_id": 1, "operation": "begin_grasp"}),
    (ReportSighting, "report_sighting", {"request_id": "r1", "object_id": 1, "seen": True}),
    (ArenaReset, "arena_reset", {"request_id": "r1"}),
    (SubmitDetections, "submit_detections", {}),
    (DescribeObject, "describe_object", {"request_id": "r1", "object_id": 1}),
]


@pytest.fixture
def legacy(ros, tmp_path):
    node = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(tmp_path / "walkie.db"))])
    yield node
    node.destroy_node()


@pytest.mark.parametrize("service_type, name, fields", READS + WRITES, ids=[n for _, n, _ in READS + WRITES])
def test_without_memory_mode_the_map_services_answer_not_ready(legacy, tmp_path, service_type, name, fields):
    response = call_service(legacy, service_type, name, service_type.Request(**fields))

    assert response.status.code == NOT_READY
    conn = connect(tmp_path / "walkie.db", readonly=True)
    try:
        assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM write_request").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.fixture
def sim_node(ros, tmp_path, folder):
    node = memory_node(memory_store(tmp_path, folder, "ros_sim"), folder, sim=True)
    yield node
    node.destroy_node()


@pytest.mark.parametrize("service_type, name, fields", READS, ids=[n for _, n, _ in READS])
def test_before_the_first_clock_sample_the_reads_answer_not_ready(sim_node, service_type, name, fields):
    handler = getattr(sim_node, name)
    response = handler(service_type.Request(**fields), service_type.Response())
    assert response.status.code == NOT_READY


def test_the_store_status_needs_no_clock(sim_node):
    response = sim_node.get_store_status(GetStoreStatus.Request(), GetStoreStatus.Response())
    assert response.status.code == OK
    assert response.clock_kind == "ros_sim"


def test_after_a_short_sim_reset_an_event_has_no_pose_from_the_old_run(sim_node):
    sim_node._on_first_clock(Clock(clock=to_time(0.5)))
    sim_node._frames.buffer.set_transform(camera_transform(0.2, (1.0, 1.0, 0.0), child=ROBOT), "test")
    sim_node.get_clock().set_ros_time_override(RclTime(seconds=0.0))
    sim_node.get_clock().set_ros_time_override(RclTime(seconds=0.2))

    request = RememberEvent.Request(
        request_id="r1", task_id="t1", cause="a test", kind="action", text="Put the cup on the table."
    )
    response = sim_node.remember_event(request, RememberEvent.Response())

    assert response.status.code == OK
    assert response.event.stamp == to_time(0.2)
    assert not response.event.has_robot_pose
    assert response.event.room_id == 0


# The snapshot ----------------------------------------------------------------


class SnapshotReader:
    """A late subscriber to the snapshot, as a baseline overlay would be."""

    def __init__(self, node, *, durability=DurabilityPolicy.TRANSIENT_LOCAL):
        self.helper = Node("snapshot_test_reader")
        self.received: list[dict] = []
        self.helper.create_subscription(
            String,
            "/walkie_database/objects/snapshot",
            lambda message: self.received.append(json.loads(message.data)),
            QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1, reliability=ReliabilityPolicy.RELIABLE,
                       durability=durability),
        )
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.helper)
        self.executor.add_node(node)
        self.node = node

    def spin_until(self, condition, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, "The snapshot did not arrive."
            self.executor.spin_once(timeout_sec=0.05)

    def close(self) -> None:
        self.executor.remove_node(self.node)
        self.executor.shutdown()
        self.helper.destroy_node()


def test_a_late_subscriber_gets_the_seeded_snapshot(node, store, objects):
    reader = SnapshotReader(node)
    try:
        reader.spin_until(lambda: reader.received)
    finally:
        reader.close()

    [snapshot] = reader.received
    conn = connect(store, readonly=True)
    try:
        store_id = conn.execute("SELECT store_id FROM store_meta").fetchone()["store_id"]
        stamped = conn.execute("SELECT last_verified FROM object WHERE key = 'table'").fetchone()[0]
    finally:
        conn.close()
    assert snapshot["schema_version"] == SNAPSHOT_SCHEMA_VERSION == 1
    assert snapshot["store_id"] == store_id
    assert snapshot["sequence"] == 0
    assert set(snapshot) == {"schema_version", "map_id", "store_id", "sequence", "stamp", "objects"}
    by_key = {entry["key"] or entry["label"]: entry for entry in snapshot["objects"]}
    assert set(by_key) == {"table", "wardrobe", "cup", "box"}
    table = by_key["table"]
    assert set(table) == {"id", "key", "label", "layer", "state", "geometry_valid", "frame_id", "position",
                          "orientation", "size", "p_exist", "last_verified"}
    assert (table["layer"], table["state"], table["geometry_valid"], table["frame_id"]) == (
        "anchor", "present", True, "map"
    )
    assert table["position"] == pytest.approx([2.0, 2.0, 0.4])
    assert table["size"] == pytest.approx([1.0, 0.5, 0.8])
    verified = result_time(stamped, "the table")
    assert table["last_verified"] == {"sec": verified.sec, "nanosec": verified.nanosec}
    assert by_key["wardrobe"]["geometry_valid"] is False
    assert by_key["box"]["last_verified"] is None


def test_the_snapshot_publisher_is_reliable_and_transient_local(node):
    profile = node._snapshot_publisher.qos_profile
    assert profile.reliability == ReliabilityPolicy.RELIABLE
    assert profile.durability == DurabilityPolicy.TRANSIENT_LOCAL
    assert profile.depth == 1


def test_legacy_mode_publishes_no_snapshot(legacy):
    assert legacy._snapshot_publisher is None
    names = {name for name, _ in legacy.get_publisher_names_and_types_by_node("walkie_database", "/")}
    assert "/walkie_database/objects/snapshot" not in names


def test_in_a_sim_the_snapshot_waits_for_the_first_clock_sample(sim_node):
    reader = SnapshotReader(sim_node)
    clock = Node("snapshot_test_clock")
    publisher = clock.create_publisher(Clock, "/clock", QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
    reader.executor.add_node(clock)
    try:
        reader.spin_until(lambda: sim_node._snapshot_publisher.get_subscription_count() > 0)
        for _ in range(10):
            reader.executor.spin_once(timeout_sec=0.02)
        assert reader.received == []

        def tick():
            publisher.publish(Clock(clock=to_time(SIM_TIME)))
            return bool(reader.received)

        reader.spin_until(tick)
    finally:
        reader.executor.remove_node(clock)
        reader.close()
        clock.destroy_node()

    [snapshot] = reader.received
    assert snapshot["stamp"] == {"sec": int(SIM_TIME), "nanosec": 0}
    table = next(entry for entry in snapshot["objects"] if entry["key"] == "table")
    assert table["last_verified"] == {"sec": int(SIM_TIME), "nanosec": 0}


# Conversion guards -----------------------------------------------------------


def record(**changes) -> ObjectRecord:
    fields = dict(
        id=7, map_id="m", frame_id="map", revision=0, key="", label="cup", labels=("cup",), official_name="",
        category="", layer="movable", state="present", source="perception", room_id=0, room="", place="",
        attributes={}, caption="", printed_text="", caption_model="", prompt_version="", p_exist=0.5, p_now=0.5,
        last_verified=None, age_s=0.0, first_seen=0.0, last_seen=0.0, n_detections=1, geometry_valid=False,
        geometry_stamp=None, center=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0, 1.0), size=(0.0, 0.0, 0.0),
        weight_g=None, in_baseline=False, depth_seen=False, merged_into=0, split_from=0,
    )
    return ObjectRecord(**(fields | changes))


@pytest.mark.parametrize(
    "changes",
    [{"n_detections": 2**32}, {"revision": -1}, {"weight_g": math.inf}, {"last_seen": math.nan},
     {"last_verified": 2.0**40}],
    ids=["n_detections", "revision", "weight", "last_seen", "last_verified"],
)
def test_a_value_a_message_cannot_hold_raises_before_the_response(changes):
    with pytest.raises(ValueError):
        object_summary(record(**changes))
