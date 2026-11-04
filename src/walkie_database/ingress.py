"""Checks on an observation from vision, and the slot that keeps the newest one.

This is the database's own boundary. It takes nothing on trust from the publisher,
and it refuses a whole array when any part is malformed. LocalizeObjects uses the
same checks as the topic, and the object memory will use them too.

Nothing here reads or writes SQL, so a topic callback can never add a row. An array
that passes is counted and kept in one slot, which the next array replaces. Fusing
it into objects arrives with the object memory (ADR-0011).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping

import numpy as np
from builtin_interfaces.msg import Time
from sensor_msgs.msg import CompressedImage, PointCloud2, PointField
from std_msgs.msg import Header
from vision_msgs.msg import BoundingBox2D
from walkie_vision_msgs.msg import ObjectDetection, ObjectDetectionArray

from walkie_database.object_context import Rejected

MAX_ARRAY_AGE_S = 1.0
MAX_FUTURE_S = 0.005

MIN_CLOUD_POINTS = 30
MAX_CLOUD_POINTS = 2000
POINT_STEP = 12
_CLOUD_FIELDS = (("x", 0), ("y", 4), ("z", 8))

JPEG_FORMAT = "jpeg"
MAX_CROP_SIDE = 512
MAX_CROP_BYTES = 512 * 1024
_JPEG_START = b"\xff\xd8"
# Start-of-frame markers, which hold the image's size: 0xC0 to 0xCF, except DHT
# (0xC4), JPG (0xC8) and DAC (0xCC).
_JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

COLOR_SHARES = 12
_SHARES_SUM_TOLERANCE = 1e-4
_UNIT_NORM_TOLERANCE = 1e-3


def stamp_seconds(stamp: Time) -> float:
    return stamp.sec + stamp.nanosec / 1e9


def check_observation(
    msg: ObjectDetectionArray, *, models: Mapping[str, int], map_frame: str, now: float
) -> None:
    """Raise Rejected unless an array is well formed and fresh.

    *models* holds the length of each object vector table in the store, so a fake
    model's vector, which has no table, is refused. *now* is the node's clock.
    """
    problem = _array_problem(msg, map_frame) or next(
        (
            f"Detection {i}: {p}"
            for i, detection in enumerate(msg.detections)
            if (p := _detection_problem(detection, msg, models))
        ),
        None,
    )
    if problem is not None:
        raise Rejected("INVALID", problem)
    age = now - stamp_seconds(msg.header.stamp)
    if age > MAX_ARRAY_AGE_S:
        raise Rejected("STALE", f"The array is {age:.3f} s old, over the {MAX_ARRAY_AGE_S} s limit.")
    if -age > MAX_FUTURE_S:
        raise Rejected(
            "STALE",
            f"The array is stamped {-age:.3f} s ahead of the clock, over the {MAX_FUTURE_S} s limit.",
        )


def _array_problem(msg: ObjectDetectionArray, map_frame: str) -> str | None:
    if not _is_stream_id(msg.stream_id):
        return f"stream_id must be a UUID, not {msg.stream_id!r}."
    frame = msg.header.frame_id
    if not frame or frame.startswith("/"):
        return f"header.frame_id must name a frame with no leading slash, not {frame!r}."
    if frame == map_frame:
        return f"The array must be in the camera's optical frame, not the map frame {map_frame!r}."
    stamp = msg.header.stamp
    # TF reads a zero time as "the latest", which would break the exact-stamp rule.
    if stamp.sec < 0 or (stamp.sec, stamp.nanosec) == (0, 0) or stamp.nanosec >= 1_000_000_000:
        return f"header.stamp must be a valid time after zero, not {stamp.sec} s and {stamp.nanosec} ns."
    if msg.camera_info.header != msg.header:
        return "camera_info.header differs from the array's header."
    problem = _camera_problem(msg)
    if problem is not None:
        return problem
    classes = list(msg.classes)
    if not classes or not all(classes) or len(set(classes)) != len(classes):
        return f"classes must be unique names that are not empty, and at least one, not {classes}."
    return None


def _is_stream_id(stream_id: str) -> bool:
    try:
        return str(uuid.UUID(stream_id)) == stream_id
    except ValueError:
        return False


def _camera_problem(msg: ObjectDetectionArray) -> str | None:
    info = msg.camera_info
    if info.width == 0 or info.height == 0:
        return f"camera_info has no image size: {info.width} by {info.height}."
    # Rectified calibration has a focal length. k holds fx at 0 and fy at 4.
    fx, fy = info.k[0], info.k[4]
    if not (math.isfinite(fx) and math.isfinite(fy)) or fx == 0.0 or fy == 0.0:
        return f"camera_info must have a finite focal length, not {fx} and {fy}."
    return None


def _detection_problem(
    detection: ObjectDetection, msg: ObjectDetectionArray, models: Mapping[str, int]
) -> str | None:
    if detection.label not in msg.classes:
        return f"The label {detection.label!r} is not in classes: {list(msg.classes)}."
    # These comparisons are also false for NaN.
    if not 0.0 <= detection.score <= 1.0:
        return f"score must be finite and in [0, 1], not {detection.score}."
    if not 0.0 <= detection.view_quality <= 1.0:
        return f"view_quality must be finite and in [0, 1], not {detection.view_quality}."
    return (
        _bbox_problem(detection.bbox)
        or _embedding_problem(detection, models)
        or _cloud_problem(detection.points, msg.header)
        or _crop_problem(detection.crop, msg.header)
        or _color_shares_problem(detection.color_shares)
    )


def _bbox_problem(bbox: BoundingBox2D) -> str | None:
    values = (bbox.center.position.x, bbox.center.position.y, bbox.size_x, bbox.size_y)
    if not all(math.isfinite(value) for value in values) or bbox.size_x < 0 or bbox.size_y < 0:
        return f"bbox must be finite with a size that is not negative, not {values}."
    if bbox.center.theta != 0.0:
        return f"bbox.center.theta must be 0, not {bbox.center.theta}."
    return None


def _embedding_problem(detection: ObjectDetection, models: Mapping[str, int]) -> str | None:
    model = detection.embedding_model
    vector = np.asarray(detection.embedding, dtype=np.float64)
    if bool(model) != (vector.size > 0):
        return "embedding and embedding_model must be both set or both empty."
    if vector.size == 0:
        return None
    if model not in models:
        known = ", ".join(sorted(models)) or "none"
        return f"The store has no vector table for the model {model!r}. Known models: {known}."
    if vector.size != models[model]:
        return f"embedding: {model} has {models[model]} values, not {vector.size}."
    if not np.all(np.isfinite(vector)):
        return "embedding has a value that is not finite."
    norm = float(np.linalg.norm(vector))
    if abs(norm - 1.0) > _UNIT_NORM_TOLERANCE:
        return f"embedding must have unit length, not {norm}."
    return None


def _cloud_problem(cloud: PointCloud2, header: Header) -> str | None:
    if cloud.header != header:
        return "points.header differs from the array's header."
    fields = [(f.name, f.offset, f.datatype, f.count) for f in cloud.fields]
    if fields != [(name, offset, PointField.FLOAT32, 1) for name, offset in _CLOUD_FIELDS]:
        return f"points must have float32 fields x, y, z at offsets 0, 4, 8, not {fields}."
    if cloud.is_bigendian or not cloud.is_dense:
        return "points must be little-endian and dense."
    if cloud.height != 1 or cloud.point_step != POINT_STEP or cloud.row_step != POINT_STEP * cloud.width:
        return (
            f"points must have height 1, point_step {POINT_STEP} and row_step {POINT_STEP} * width, "
            f"not {cloud.height}, {cloud.point_step} and {cloud.row_step}."
        )
    if len(cloud.data) != cloud.row_step:
        return f"points has {len(cloud.data)} bytes for {cloud.width} points."
    if cloud.width == 0:
        return None
    if not MIN_CLOUD_POINTS <= cloud.width <= MAX_CLOUD_POINTS:
        return (
            f"points must be empty or hold {MIN_CLOUD_POINTS} to {MAX_CLOUD_POINTS} points, "
            f"not {cloud.width}."
        )
    xyz = cloud_points(cloud)
    if not np.all(np.isfinite(xyz)) or np.any(xyz[:, 2] <= 0):
        return "points has a point that is not finite or not in front of the camera."
    return None


def cloud_points(cloud: PointCloud2) -> np.ndarray:
    """A checked cloud's points as N rows of x, y, z. This does not copy the data."""
    return np.frombuffer(cloud.data, dtype="<f4").reshape(-1, 3)


def _crop_problem(crop: CompressedImage, header: Header) -> str | None:
    if len(crop.data) == 0:
        return None
    if crop.header != header:
        return "crop.header differs from the array's header."
    data = bytes(crop.data)
    if crop.format != JPEG_FORMAT or not data.startswith(_JPEG_START):
        return f"crop must be a JPEG with format {JPEG_FORMAT!r}, not format {crop.format!r}."
    if len(data) > MAX_CROP_BYTES:
        return f"crop has {len(data)} bytes, over the limit of {MAX_CROP_BYTES}."
    size = jpeg_size(data)
    if size is None:
        return "crop has no readable JPEG frame header."
    if not (0 < size[0] <= MAX_CROP_SIDE and 0 < size[1] <= MAX_CROP_SIDE):
        return f"crop is {size[0]} by {size[1]} pixels, and each side is at most {MAX_CROP_SIDE}."
    return None


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """A JPEG's width and height from its frame header, or None. This decodes no pixels."""
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            return None
        marker = data[position + 1]
        if marker == 0xFF:  # A marker may have fill bytes before it.
            position += 1
            continue
        length = int.from_bytes(data[position + 2 : position + 4], "big")
        if marker in _JPEG_FRAME_MARKERS:
            if length < 7 or position + 9 > len(data):
                return None
            height = int.from_bytes(data[position + 5 : position + 7], "big")
            width = int.from_bytes(data[position + 7 : position + 9], "big")
            return width, height
        if length < 2:
            return None
        position += 2 + length
    return None


def _color_shares_problem(shares: object) -> str | None:
    values = np.asarray(shares, dtype=np.float64)
    if values.size == 0:
        return None
    if values.size != COLOR_SHARES:
        return f"color_shares must be empty or hold {COLOR_SHARES} values, not {values.size}."
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        return "color_shares must be finite and not negative."
    if abs(float(values.sum()) - 1.0) > _SHARES_SUM_TOLERANCE:
        return f"color_shares must sum to 1, not {float(values.sum())}."
    return None


class ObservationSlot:
    """Counts the arrays that arrive, and keeps the newest valid one.

    Nothing accumulates: an array replaces the one before it, and the counters are
    all that stays. A refused array leaves the slot as it was.
    """

    def __init__(self, models: Mapping[str, int], map_frame: str) -> None:
        self._models = dict(models)
        self._map_frame = map_frame
        self.received = 0
        self.rejected = 0
        self.latest: ObjectDetectionArray | None = None

    def receive(self, msg: ObjectDetectionArray, now: float | None) -> str | None:
        """Count *msg*, and return why it was refused, or None.

        A *now* of None means that the node has no clock sample yet, so the array's
        age cannot be known.
        """
        self.received += 1
        try:
            if now is None:
                raise Rejected("STALE", "No clock sample has arrived, so the array's age is unknown.")
            check_observation(msg, models=self._models, map_frame=self._map_frame, now=now)
        except Rejected as e:
            self.rejected += 1
            return str(e)
        self.latest = msg
        return None
