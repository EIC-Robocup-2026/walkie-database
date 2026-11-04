"""A seeded store, a node in memory mode, a service call and a synthetic observation."""

from __future__ import annotations

import array
import time
import uuid
from pathlib import Path

import numpy as np
from builtin_interfaces.msg import Time
from geometry_msgs.msg import TransformStamped
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from sensor_msgs.msg import CameraInfo, CompressedImage, PointCloud2, PointField
from seed_helpers import map_id_of, seed_argv
from std_msgs.msg import Header
from walkie_vision_msgs.msg import ObjectDetection, ObjectDetectionArray

from walkie_database import seed_store
from walkie_database.node import DatabaseNode
from walkie_database.restore import restore

CAMERA = "head_camera_optical"
MAP = "map"
STREAM = str(uuid.UUID("7f3a9c1e-52b4-4d10-a8e6-0c9b7d2e4f61"))


def memory_store(
    tmp_path: Path, folder: Path, clock_kind: str = "wall", *, anchors=None, catalogue=None
) -> Path:
    """A restored store, as a node finds it. Returns its path."""
    seed = tmp_path / "seeds" / f"{clock_kind}.db"
    argv = seed_argv(tmp_path, folder, seed, clock_kind=clock_kind, anchors=anchors, catalogue=catalogue)
    assert seed_store.main(argv) == 0
    db = tmp_path / "run" / "walkie.db"
    db.parent.mkdir(exist_ok=True)
    restore(seed, db, folder, clock_kind)
    return db


def memory_node(db: Path, folder: Path, *, sim: bool = False, **parameters: object) -> DatabaseNode:
    overrides = [
        Parameter("memory_enabled", value=True),
        Parameter("db_path", value=str(db)),
        Parameter("map_folder", value=str(folder)),
        Parameter("map_id", value=map_id_of(folder)),
    ]
    if sim:
        overrides.append(Parameter("use_sim_time", value=True))
    overrides.extend(Parameter(name, value=value) for name, value in parameters.items())
    return DatabaseNode(parameter_overrides=overrides)


def call_service(node: Node, service_type, name: str, request, *, timeout: float = 10.0):
    """Send *request* over the middleware, so that the response must also serialize.

    *request* may be a function that builds it once the service has appeared, for a
    request that holds a stamp and must not age during discovery.
    """
    helper = Node("memory_test_client")
    client = helper.create_client(service_type, f"/walkie_database/{name}")
    executor = SingleThreadedExecutor()
    executor.add_node(helper)
    executor.add_node(node)
    try:
        deadline = time.monotonic() + timeout
        while not client.service_is_ready():
            assert time.monotonic() < deadline, f"The service {name} did not appear."
            executor.spin_once(timeout_sec=0.01)
        future = client.call_async(request() if callable(request) else request)
        executor.spin_until_future_complete(future, timeout_sec=timeout)
        assert future.done(), f"The service {name} did not answer."
        return future.result()
    finally:
        executor.remove_node(node)
        executor.shutdown()
        helper.destroy_node()


def to_time(seconds: float) -> Time:
    sec = int(seconds)
    return Time(sec=sec, nanosec=int(round((seconds - sec) * 1e9)))


def header_at(seconds: float, frame: str = CAMERA) -> Header:
    return Header(stamp=to_time(seconds), frame_id=frame)


def cloud_fields() -> list[PointField]:
    return [
        PointField(name=name, offset=offset, datatype=PointField.FLOAT32, count=1)
        for name, offset in (("x", 0), ("y", 4), ("z", 8))
    ]


def cloud(header: Header, points=None) -> PointCloud2:
    """A cloud in the layout vision publishes. No *points* makes an empty one."""
    data = np.zeros((0, 3), dtype="<f4") if points is None else np.asarray(points, dtype="<f4")
    return PointCloud2(
        header=header,
        height=1,
        width=len(data),
        fields=cloud_fields(),
        is_bigendian=False,
        point_step=12,
        row_step=12 * len(data),
        data=array.array("B", data.tobytes()),
        is_dense=True,
    )


def block_of_points(center=(0.1, 0.2, 1.0), count: int = 40) -> np.ndarray:
    """Points whose average is exactly *center*: pairs on either side of it."""
    half = np.arange(1, count // 2 + 1, dtype=np.float64)[:, None] * [0.001, 0.002, 0.001]
    return np.concatenate([np.asarray(center) + half, np.asarray(center) - half])


def jpeg_bytes(width: int = 64, height: int = 48) -> bytes:
    """The start of a JPEG, up to its frame header. Nothing here decodes pixels."""
    app0 = b"\xff\xe0\x00\x04\x00\x00"
    frame = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + frame + b"\xff\xd9"


def detection(header: Header, label: str = "cup", points=None, **fields: object) -> ObjectDetection:
    built = ObjectDetection(label=label, score=0.9, view_quality=0.5, points=cloud(header, points))
    built.bbox.size_x = 10.0
    built.bbox.size_y = 20.0
    built.crop = CompressedImage(header=header)
    for name, value in fields.items():
        setattr(built, name, value)
    return built


def observation(seconds: float, detections=(), *, classes=("cup", "bottle"), stream=STREAM) -> ObjectDetectionArray:
    """An array stamped at *seconds*. Each item of *detections* is a list of points, or None."""
    header = header_at(seconds)
    info = CameraInfo(header=header, width=640, height=480, distortion_model="plumb_bob")
    info.k = [500.0, 0.0, 320.0, 0.0, 500.0, 240.0, 0.0, 0.0, 1.0]
    return ObjectDetectionArray(
        header=header,
        camera_info=info,
        classes=list(classes),
        detections=[detection(header, points=points) for points in detections],
        stream_id=stream,
    )


def camera_transform(
    seconds: float, position=(1.0, 2.0, 0.5), rotation=(0.0, 0.0, 0.0, 1.0), *, child: str = CAMERA
) -> TransformStamped:
    """The pose of *child* in the map at *seconds*."""
    transform = TransformStamped(header=header_at(seconds, MAP), child_frame_id=child)
    t, r = transform.transform.translation, transform.transform.rotation
    t.x, t.y, t.z = (float(value) for value in position)
    r.x, r.y, r.z, r.w = (float(value) for value in rotation)
    return transform
