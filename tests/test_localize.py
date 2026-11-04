"""LocalizeObjects, the observation topic and the writes that answer NOT_READY."""

import math
import time

import pytest
from memory_helpers import (
    CAMERA,
    MAP,
    STREAM,
    block_of_points,
    call_service,
    camera_transform,
    memory_node,
    memory_store,
    observation,
    to_time,
)
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time as RclTime
from rosgraph_msgs.msg import Clock
from seed_helpers import dump_rows, map_id_of, write_map_folder
from tf2_msgs.msg import TFMessage
from walkie_database_msgs.msg import OperationStatus
from walkie_database_msgs.srv import (
    ArenaReset,
    DescribeObject,
    LocalizeObjects,
    ReportSighting,
    SetObjectState,
    SubmitDetections,
)
from walkie_vision_msgs.msg import ObjectDetectionArray

from walkie_database.db import connect
from walkie_database.localize import LOOKUP_TIMEOUT
from walkie_database.node import DatabaseNode

OK, INVALID, NOT_FOUND, STALE, TF_UNAVAILABLE, WRONG_MAP, NOT_READY = (
    OperationStatus.OK,
    OperationStatus.INVALID,
    OperationStatus.NOT_FOUND,
    OperationStatus.STALE,
    OperationStatus.TF_UNAVAILABLE,
    OperationStatus.WRONG_MAP,
    OperationStatus.NOT_READY,
)
SIM_TIME = 1000.0


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


@pytest.fixture
def store(tmp_path, folder):
    return memory_store(tmp_path, folder)


@pytest.fixture
def node(ros, store, folder):
    node = memory_node(store, folder)
    yield node
    node.destroy_node()


def wall_now(node) -> float:
    return node.get_clock().now().nanoseconds / 1e9


def teach(node, *transforms) -> None:
    for transform in transforms:
        node._frames.buffer.set_transform(transform, "test")


def localize(node, objects, map_id=""):
    return node.localize_objects(
        LocalizeObjects.Request(map_id=map_id, objects=objects), LocalizeObjects.Response()
    )


def rows_of(store) -> dict:
    conn = connect(store, readonly=True)
    try:
        return dump_rows(conn)
    finally:
        conn.close()


# Exact stamps ----------------------------------------------------------------


def test_each_array_uses_the_camera_pose_at_its_own_stamp(node):
    now = wall_now(node)
    earlier, later = now - 0.2, now - 0.1
    teach(node, camera_transform(earlier, (1.0, 2.0, 0.5)), camera_transform(later, (5.0, 2.0, 0.5)))

    first = localize(node, observation(earlier, [block_of_points((0.1, 0.2, 1.0))]))
    second = localize(node, observation(later, [block_of_points((0.1, 0.2, 1.0))]))

    assert first.status.code == OK and second.status.code == OK
    assert first.results[0].centroid.point.x == pytest.approx(1.1)
    assert second.results[0].centroid.point.x == pytest.approx(5.1)
    assert first.results[0].centroid.header.stamp == to_time(earlier)
    assert first.results[0].centroid.header.frame_id == MAP


def test_a_stamp_past_the_newest_transform_does_not_use_the_newest(node):
    now = wall_now(node)
    teach(node, camera_transform(now - 0.2), camera_transform(now - 0.1))

    response = localize(node, observation(now - 0.05, [block_of_points()]))

    assert response.status.code == OK
    assert response.results[0].status.code == TF_UNAVAILABLE
    assert response.results[0].points_used == 0
    assert response.results[0].centroid.header.frame_id == ""


def test_a_stamp_before_the_oldest_transform_does_not_use_the_oldest(node):
    now = wall_now(node)
    teach(node, camera_transform(now - 0.1))

    response = localize(node, observation(now - 0.2, [block_of_points()]))

    assert response.results[0].status.code == TF_UNAVAILABLE


def test_a_lookup_waits_for_a_transform_that_arrives_in_time(node, monkeypatch):
    now = wall_now(node)
    buffer = node._frames.buffer
    poll = buffer.can_transform_core

    def arrives_after_the_first_poll(*args):
        answer = poll(*args)
        teach(node, camera_transform(now))
        return answer

    monkeypatch.setattr(buffer, "can_transform_core", arrives_after_the_first_poll)

    response = localize(node, observation(now, [block_of_points()]))

    assert response.results[0].status.code == OK


def test_a_missing_transform_answers_within_the_timeout(node):
    started = time.monotonic()
    response = localize(node, observation(wall_now(node), [block_of_points()]))
    # The wait ends at the timeout, and one polling step of tf2's loop may follow.
    assert time.monotonic() - started < LOOKUP_TIMEOUT.nanoseconds / 1e9 + 1.0
    assert response.results[0].status.code == TF_UNAVAILABLE


def test_the_rotation_of_the_camera_applies_to_the_centroid(node):
    now = wall_now(node)
    half = math.sqrt(0.5)
    # Turned a quarter turn about z, so camera +x points along map +y.
    teach(node, camera_transform(now, (1.0, 2.0, 0.5), (0.0, 0.0, half, half)))

    point = localize(node, observation(now, [block_of_points((0.1, 0.2, 1.0))])).results[0].centroid.point

    assert (point.x, point.y, point.z) == pytest.approx((1.0 - 0.2, 2.0 + 0.1, 1.5))


def test_the_centroid_is_the_plain_average_of_the_points(node):
    now = wall_now(node)
    teach(node, camera_transform(now, (0.0, 0.0, 0.0)))
    points = [[0.0, 0.0, 1.0]] * 30 + [[3.0, 3.0, 4.0]] * 10

    result = localize(node, observation(now, [points])).results[0]

    assert result.points_used == 40
    assert (result.centroid.point.x, result.centroid.point.y, result.centroid.point.z) == pytest.approx(
        (0.75, 0.75, 1.75)
    )


# Every entry, in order -------------------------------------------------------


def test_results_keep_the_index_of_each_detection(node):
    now = wall_now(node)
    teach(node, camera_transform(now, (0.0, 0.0, 0.0)))

    response = localize(
        node,
        observation(now, [block_of_points((1.0, 0.0, 1.0)), None, block_of_points((2.0, 0.0, 1.0)), None]),
    )

    assert response.status.code == OK
    assert [r.detection_index for r in response.results] == [0, 1, 2, 3]
    assert [r.status.code for r in response.results] == [OK, NOT_FOUND, OK, NOT_FOUND]
    assert response.results[0].centroid.point.x == pytest.approx(1.0)
    assert response.results[2].centroid.point.x == pytest.approx(2.0)
    assert response.results[1].points_used == 0
    assert response.results[1].centroid.header.frame_id == ""


def test_with_no_transform_every_entry_says_so(node):
    response = localize(node, observation(wall_now(node), [block_of_points(), None]))

    assert response.status.code == OK
    assert [r.detection_index for r in response.results] == [0, 1]
    assert [r.status.code for r in response.results] == [TF_UNAVAILABLE, TF_UNAVAILABLE]


def test_an_empty_array_succeeds_with_no_results_and_no_transform(node):
    response = localize(node, observation(wall_now(node)))
    assert response.status.code == OK
    assert list(response.results) == []


# Requests that fail as a whole -----------------------------------------------


def test_another_map_is_refused(node):
    response = localize(node, observation(wall_now(node), [None]), map_id="other-map")
    assert response.status.code == WRONG_MAP
    assert list(response.results) == []


def test_naming_the_node_map_is_the_same_as_naming_none(node, folder):
    response = localize(node, observation(wall_now(node)), map_id=map_id_of(folder))
    assert response.status.code == OK


def test_a_malformed_entry_fails_the_whole_request(node):
    now = wall_now(node)
    teach(node, camera_transform(now))
    objects = observation(now, [block_of_points(), block_of_points()])
    objects.detections[1].points.is_dense = False

    response = localize(node, objects)

    assert response.status.code == INVALID
    assert "Detection 1" in response.status.message
    assert list(response.results) == []


def test_a_fake_model_fails_the_whole_request(node):
    now = wall_now(node)
    objects = observation(now, [None])
    objects.detections[0].embedding_model = "fake_clip"
    objects.detections[0].embedding = [1.0] + [0.0] * 511
    assert localize(node, objects).status.code == INVALID


def test_stale_and_future_arrays_are_refused(node):
    now = wall_now(node)
    assert localize(node, observation(now - 2.0, [None])).status.code == STALE
    assert localize(node, observation(now + 2.0, [None])).status.code == STALE


def test_a_long_status_message_stays_within_the_contract(node):
    now = wall_now(node)
    objects = observation(now, [None])
    objects.detections[0].label = "x" * 2000
    assert len(localize(node, objects).status.message) == 512


# Without memory mode ---------------------------------------------------------


def test_legacy_mode_has_no_map_so_it_answers_not_ready(ros, tmp_path):
    from rclpy.parameter import Parameter

    node = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(tmp_path / "walkie.db"))])
    try:
        assert node._frames is None
        assert node.subscriptions == [] or all(
            s.msg_type is not ObjectDetectionArray for s in node.subscriptions
        )
        assert localize(node, observation(1000.0)).status.code == NOT_READY
    finally:
        node.destroy_node()


# The clock -------------------------------------------------------------------


class SimClock:
    """A real /clock publisher, and a spin that also runs the node under test."""

    def __init__(self, node):
        self.helper = Node("localize_test_clock")
        self.publisher = self.helper.create_publisher(
            Clock, "/clock", QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        )
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.helper)
        self.executor.add_node(node)
        self.node = node

    def set(self, seconds: float) -> None:
        deadline = time.monotonic() + 10.0
        while self.node.get_clock().now().nanoseconds / 1e9 != seconds or self.node._waiting_for_clock:
            assert time.monotonic() < deadline, "The clock sample did not arrive."
            self.publisher.publish(Clock(clock=to_time(seconds)))
            self.executor.spin_once(timeout_sec=0.02)

    def close(self) -> None:
        self.executor.remove_node(self.node)
        self.executor.shutdown()
        self.helper.destroy_node()


@pytest.fixture
def sim_node(ros, tmp_path, folder):
    node = memory_node(memory_store(tmp_path, folder, "ros_sim"), folder, sim=True)
    yield node
    node.destroy_node()


def test_before_the_first_clock_sample_it_answers_not_ready(sim_node):
    assert localize(sim_node, observation(SIM_TIME, [None])).status.code == NOT_READY


def test_ages_follow_the_sim_clock(sim_node):
    clock = SimClock(sim_node)
    try:
        clock.set(SIM_TIME)
        teach(sim_node, camera_transform(SIM_TIME))
        assert localize(sim_node, observation(SIM_TIME, [block_of_points()])).results[0].status.code == OK
        clock.set(SIM_TIME + 5.0)
        assert localize(sim_node, observation(SIM_TIME, [None])).status.code == STALE
    finally:
        clock.close()


def test_a_clock_reset_clears_old_transforms_and_refuses_old_stamps(sim_node):
    clock = SimClock(sim_node)
    try:
        clock.set(SIM_TIME)
        teach(sim_node, camera_transform(SIM_TIME, (1.0, 0.0, 0.0)))
        assert sim_node._frames.buffer.can_transform_core(MAP, CAMERA, RclTime(seconds=SIM_TIME))[0]

        clock.set(10.0)

        # What the old run saw no longer answers for any time.
        assert not sim_node._frames.buffer.can_transform_core(MAP, CAMERA, RclTime(seconds=SIM_TIME))[0]
        # Its arrays now read as from the future.
        assert localize(sim_node, observation(SIM_TIME, [None])).status.code == STALE
        assert localize(sim_node, observation(10.0, [block_of_points()])).results[0].status.code == TF_UNAVAILABLE
        # The new run's transforms are taken, though they are older than the old run's.
        teach(sim_node, camera_transform(10.0, (7.0, 0.0, 0.0)))
        result = localize(sim_node, observation(10.0, [block_of_points((0.0, 0.0, 1.0))])).results[0]
        assert result.centroid.point.x == pytest.approx(7.0)
    finally:
        clock.close()


def test_a_clock_reset_keeps_static_transforms(sim_node):
    clock = SimClock(sim_node)
    try:
        clock.set(SIM_TIME)
        sim_node._frames.buffer.set_transform_static(camera_transform(SIM_TIME, (3.0, 0.0, 0.0)), "test")

        clock.set(10.0)

        result = localize(sim_node, observation(10.0, [block_of_points((0.0, 0.0, 1.0))])).results[0]
        assert result.status.code == OK
        assert result.centroid.point.x == pytest.approx(3.0)
    finally:
        clock.close()


# The listener ----------------------------------------------------------------


def test_the_listener_does_not_take_over_the_database_node(node):
    # Its executor runs on another thread. With the node on it, every service would
    # run there too, and SQLite refuses a connection from another thread.
    assert node._frames._listener.node is not node
    assert node.executor is None


def test_the_listener_receives_tf_while_the_node_is_not_spinning(node):
    helper = Node("localize_test_tf")
    publisher = helper.create_publisher(TFMessage, "/tf", QoSProfile(depth=100))
    executor = SingleThreadedExecutor()
    executor.add_node(helper)
    try:
        deadline = time.monotonic() + 10.0
        while True:
            assert time.monotonic() < deadline, "The transform did not arrive."
            # Fresh stamps each time, because an array may be at most a second old.
            now = wall_now(node)
            earlier, later = now - 0.2, now - 0.1
            publisher.publish(
                TFMessage(
                    transforms=[
                        camera_transform(earlier, (1.0, 0.0, 0.0)),
                        camera_transform(later, (5.0, 0.0, 0.0)),
                    ]
                )
            )
            executor.spin_once(timeout_sec=0.05)
            exact = RclTime.from_msg(to_time(later))
            if node._frames.buffer.can_transform_core(MAP, CAMERA, exact)[0]:
                break

        result = localize(node, observation(earlier, [block_of_points((0.0, 0.0, 1.0))])).results[0]

        assert result.status.code == OK
        assert result.centroid.point.x == pytest.approx(1.0)
    finally:
        executor.shutdown()
        helper.destroy_node()


def test_a_real_request_runs_on_the_thread_that_owns_the_connection(node):
    def fresh_request():
        now = wall_now(node)
        teach(node, camera_transform(now))
        return LocalizeObjects.Request(objects=observation(now, [block_of_points(), None]))

    response = call_service(node, LocalizeObjects, "localize_objects", fresh_request)

    assert response.status.code == OK
    assert [r.status.code for r in response.results] == [OK, NOT_FOUND]
    assert node.conn.execute("SELECT 1").fetchone()[0] == 1


# The topic -------------------------------------------------------------------


def publish_until(publisher, executor, build, count_of, *, want: int) -> None:
    """Publish fresh arrays, because discovery may take longer than an array stays valid."""
    deadline = time.monotonic() + 10.0
    while count_of() < want:
        assert time.monotonic() < deadline, "The array did not arrive."
        publisher.publish(build())
        executor.spin_once(timeout_sec=0.05)


@pytest.mark.parametrize(
    "reliability", [ReliabilityPolicy.BEST_EFFORT, ReliabilityPolicy.RELIABLE], ids=["best_effort", "reliable"]
)
def test_arrays_on_the_topic_are_counted_and_create_no_rows(node, store, reliability):
    before = rows_of(store)
    helper = Node("localize_test_publisher")
    publisher = helper.create_publisher(
        ObjectDetectionArray,
        "/walkie_vision/objects",
        QoSProfile(depth=5, reliability=reliability, durability=DurabilityPolicy.VOLATILE),
    )
    executor = SingleThreadedExecutor()
    executor.add_node(helper)
    executor.add_node(node)
    slot = node._observations

    def good():
        return observation(wall_now(node), [block_of_points(), None])

    def fake():
        built = observation(wall_now(node), [None])
        built.detections[0].embedding_model = "fake_clip"
        built.detections[0].embedding = [1.0] + [0.0] * 511
        return built

    def malformed():
        return observation(wall_now(node), [None], classes=())

    try:
        publish_until(publisher, executor, good, lambda: slot.received, want=1)
        assert slot.rejected == 0
        assert slot.latest.stream_id == STREAM
        assert len(slot.latest.detections) == 2

        publish_until(publisher, executor, fake, lambda: slot.rejected, want=1)
        publish_until(publisher, executor, malformed, lambda: slot.rejected, want=2)

        # The good array has two detections, and the refused ones have one.
        assert len(slot.latest.detections) == 2
        assert slot.received >= 3
    finally:
        executor.remove_node(node)
        executor.shutdown()
        helper.destroy_node()

    assert rows_of(store) == before


def test_the_topic_name_is_a_parameter(ros, store, folder):
    node = memory_node(store, folder, objects_topic="/walkie_vision/sim/objects")
    try:
        assert [s.topic_name for s in node.subscriptions if s.msg_type is ObjectDetectionArray] == [
            "/walkie_vision/sim/objects"
        ]
    finally:
        node.destroy_node()


def test_the_subscription_uses_the_observation_profile(node):
    [subscription] = [s for s in node.subscriptions if s.msg_type is ObjectDetectionArray]
    profile = subscription.qos_profile
    assert profile.reliability == ReliabilityPolicy.BEST_EFFORT
    assert profile.durability == DurabilityPolicy.VOLATILE
    assert profile.depth == 1


# Writes that arrive with the object memory -----------------------------------


@pytest.mark.parametrize(
    "service_type, name, request_fields, flag",
    [
        (SetObjectState, "set_object_state", {"object_id": 1, "operation": "begin_grasp"}, None),
        (ReportSighting, "report_sighting", {"object_id": 1, "seen": True}, ("applied", False)),
        (ArenaReset, "arena_reset", {}, ("objects_changed", 0)),
        (SubmitDetections, "submit_detections", {}, ("queued", False)),
        (DescribeObject, "describe_object", {"object_id": 1}, ("queued", False)),
    ],
)
def test_writes_of_the_object_memory_answer_not_ready_and_keep_nothing(
    node, store, service_type, name, request_fields, flag
):
    before = rows_of(store)
    request = service_type.Request(request_id="r1", task_id="t1", cause="a test", **request_fields)

    response = call_service(node, service_type, name, request)

    assert response.status.code == NOT_READY
    if flag is not None:
        assert getattr(response, flag[0]) == flag[1]
    assert rows_of(store) == before
    assert rows_of(store)["write_request"] == []


def test_those_writes_also_answer_not_ready_with_no_ids_at_all(node):
    response = node._not_built(ArenaReset.Request(), ArenaReset.Response())
    assert response.status.code == NOT_READY
