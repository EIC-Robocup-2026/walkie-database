"""The node in memory mode merges arrays into objects, stores them and draws them."""

import time

import numpy as np
import pytest
from memory_helpers import MAP, camera_transform, detection, memory_node, memory_store, observation
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from seed_helpers import write_map_folder
from sensor_msgs.msg import PointCloud2
from test_object_memory import blob, embedding_near, unit
from visualization_msgs.msg import Marker, MarkerArray
from walkie_database.map_config import MapGateError
from walkie_database_msgs.msg import ModelVector, ObjectQuery, OperationStatus
from walkie_database_msgs.srv import GetStoreStatus, SearchObjects

MODEL = "clip_vit_b16"


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


@pytest.fixture
def store(tmp_path, folder):
    return memory_store(tmp_path, folder)


@pytest.fixture
def node(ros, store, folder):
    node = memory_node(store, folder, confirm_views=2)
    yield node
    node.destroy_node()


@pytest.fixture
def cup_vector():
    return unit(np.random.default_rng(42).normal(size=512))


def wall_now(node) -> float:
    return node.get_clock().now().nanoseconds / 1e9


def cup_array(node, stamp, vector, seed):
    """An array with one cup, 1 m in front of a camera that sits at (1, 2, 0.5) in the map."""
    built = observation(stamp, [])
    built.detections = [
        detection(built.header, label="cup",
                  points=blob((0.1, 0.2, 1.0), (0.08, 0.08, 0.1), count=200, seed=seed),
                  embedding_model=MODEL, embedding=embedding_near(vector, seed).astype(np.float32).tolist())
    ]
    return built


def feed(node, vector, views=2):
    for seed in range(views):
        stamp = wall_now(node)
        node._frames.buffer.set_transform(camera_transform(stamp), "test")
        node._on_objects(cup_array(node, stamp, vector, seed))
    node._maintain_objects()


def test_arrays_with_tf_become_a_stored_object_that_search_finds(node, cup_vector):
    feed(node, cup_vector)

    # The test seed's measured anchor is in the store too, so ask for movable objects.
    request = SearchObjects.Request(
        query=ObjectQuery(layer="movable"), use_embedding=True,
        vector=ModelVector(model=MODEL, embedding=cup_vector.astype(np.float32).tolist()), max_results=5,
    )
    response = node.search_objects(request, SearchObjects.Response())
    assert response.status.code == OperationStatus.OK
    [match] = response.matches
    cup = match.object
    assert cup.labels[0] == "cup"
    assert cup.geometry_valid
    position = cup.pose.pose.position
    assert (position.x, position.y, position.z) == pytest.approx((1.1, 2.2, 1.5), abs=0.03)
    assert cup.pose.header.frame_id == MAP


def merged(node):
    """The tracks the memory made, without the seed's fixed anchors."""
    return [t for t in node._memory.tracks if not t.fixed]


def test_an_array_with_no_transform_is_counted_and_not_merged(node, cup_vector):
    node._on_objects(cup_array(node, wall_now(node), cup_vector, 0))
    assert node.tf_misses == 1
    assert merged(node) == []


@pytest.mark.parametrize("thickness", [-0.1, float("nan"), float("inf")])
def test_a_slab_thickness_out_of_range_stops_the_start(ros, store, folder, thickness):
    with pytest.raises(MapGateError, match="slab_thickness must be finite and at least 0"):
        memory_node(store, folder, slab_thickness=thickness)


def test_one_view_is_not_enough_to_store_an_object(node, cup_vector):
    feed(node, cup_vector, views=1)
    [track] = merged(node)
    assert track.object_id is None
    response = node.get_store_status(GetStoreStatus.Request(), GetStoreStatus.Response())
    assert response.ingest_enabled


def test_markers_and_cloud_reach_a_late_subscriber(node, cup_vector):
    feed(node, cup_vector)
    helper = Node("object_markers_listener")
    latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
    got = {}
    helper.create_subscription(MarkerArray, "/walkie_database/objects/markers",
                               lambda m: got.setdefault("markers", m), latched)
    helper.create_subscription(PointCloud2, "/walkie_database/objects/cloud",
                               lambda m: got.setdefault("cloud", m), latched)
    executor = SingleThreadedExecutor()
    executor.add_node(helper)
    executor.add_node(node)
    try:
        deadline = time.monotonic() + 10.0
        while len(got) < 2 and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
    finally:
        executor.remove_node(node)
        executor.shutdown()
        helper.destroy_node()

    [cup_id] = [t.object_id for t in merged(node)]
    markers = got["markers"].markers
    assert markers[0].action == Marker.DELETEALL
    [box] = [m for m in markers if m.ns == "objects" and m.id == cup_id]
    [text] = [m for m in markers if m.ns == "labels" and m.id == cup_id]
    assert box.header.frame_id == MAP and box.type == Marker.CUBE
    assert text.text.startswith(f"#{cup_id} cup")
    cloud = got["cloud"]
    assert cloud.header.frame_id == MAP and cloud.width > 0
    assert [f.name for f in cloud.fields] == ["x", "y", "z", "rgb", "object_id"]


def test_a_restarted_node_keeps_merging_into_the_same_object(ros, store, folder, cup_vector):
    first = memory_node(store, folder, confirm_views=2)
    try:
        feed(first, cup_vector)
        [object_id] = [t.object_id for t in merged(first)]
    finally:
        first.destroy_node()
    second = memory_node(store, folder, confirm_views=2)
    try:
        assert [t.object_id for t in merged(second)] == [object_id]
        feed(second, cup_vector, views=1)
        [track] = merged(second)
        assert (track.object_id, track.n_detections) == (object_id, 3)
    finally:
        second.destroy_node()
