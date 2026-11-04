"""Turn an observation's clouds into centroids in the map, with TF at the exact stamp.

Stateless: this keeps no history, writes nothing, allocates no id and runs no SQL.
It does not fall back to the latest transform or the latest depth.

The listener spins its own private node on its own thread, because a lookup that
waits blocks the node's only executor, and that executor could not then deliver the
transform it waits for. Handing the database node to the listener's thread would be
wrong: that thread would then run every service, and SQLite refuses a connection
from another thread. The TF thread touches only the buffer.
"""

from __future__ import annotations

import math

from geometry_msgs.msg import Point, PointStamped, TransformStamped
from rclpy.clock import Clock, JumpThreshold
from rclpy.duration import Duration
from rclpy.time import Time
from std_msgs.msg import Header
from tf2_py import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener
from walkie_database_msgs.msg import LocalizedDetection, OperationStatus
from walkie_vision_msgs.msg import ObjectDetection, ObjectDetectionArray

from walkie_database.geometry import rotate
from walkie_database.ingress import cloud_points

LOOKUP_TIMEOUT = Duration(seconds=0.05)
# A jump back of a second or more means that the sim restarted, or the wall clock was
# set back. Old transforms must not answer for the same times in the new run.
_BACKWARD_JUMP = JumpThreshold(min_forward=None, min_backward=Duration(seconds=-1), on_clock_change=True)


class FrameSource:
    """Transforms at a given time, from a TF buffer that its own thread fills."""

    def __init__(self, clock: Clock) -> None:
        self.buffer = Buffer()
        # With no node given, the listener makes a private one that uses no sim time.
        # The times it stores come from the messages, not from its clock.
        self._listener = TransformListener(self.buffer, None, spin_thread=True)
        self._jump = clock.create_jump_callback(_BACKWARD_JUMP, post_callback=self._on_jump)

    def _on_jump(self, _jump: object) -> None:
        self.buffer.clear()

    def lookup(self, target_frame: str, source_frame: str, time: Time) -> TransformStamped | None:
        """The transform at exactly this time, or None after at most LOOKUP_TIMEOUT.

        A time past the newest transform is not answered with the newest one.
        Between two transforms, TF interpolates.
        """
        try:
            found = self.buffer.lookup_transform(target_frame, source_frame, time, LOOKUP_TIMEOUT)
        except TransformException:
            return None
        return found if _is_finite(found) else None

    def close(self) -> None:
        self._jump.unregister()
        listener = self._listener
        listener.executor.shutdown()
        listener.dedicated_listener_thread.join()
        listener.unregister()
        listener.node.destroy_node()


def _is_finite(transform: TransformStamped) -> bool:
    t, r = transform.transform.translation, transform.transform.rotation
    return all(math.isfinite(value) for value in (t.x, t.y, t.z, r.x, r.y, r.z, r.w))


def localize(
    objects: ObjectDetectionArray, *, map_frame: str, frames: FrameSource
) -> list[LocalizedDetection]:
    """One result per detection, in order, for an array that check_observation passed.

    One lookup serves the whole array. With no transform, every entry says so, also
    an entry that has no points.
    """
    if not objects.detections:
        return []
    header = objects.header
    transform = frames.lookup(map_frame, header.frame_id, Time.from_msg(header.stamp))
    if transform is None:
        message = f"No transform from {header.frame_id} to {map_frame} at the array's stamp."
        return [
            LocalizedDetection(
                detection_index=index,
                status=OperationStatus(code=OperationStatus.TF_UNAVAILABLE, message=message),
            )
            for index in range(len(objects.detections))
        ]
    centroid_header = Header(stamp=header.stamp, frame_id=map_frame)
    return [
        _localized(index, detection, transform, centroid_header)
        for index, detection in enumerate(objects.detections)
    ]


def _localized(
    index: int, detection: ObjectDetection, transform: TransformStamped, header: Header
) -> LocalizedDetection:
    width = detection.points.width
    if width == 0:
        return LocalizedDetection(
            detection_index=index,
            status=OperationStatus(code=OperationStatus.NOT_FOUND, message="This detection has no points."),
        )
    # In float64, because a sum of float32 values loses the small ones.
    x, y, z = cloud_points(detection.points).astype(float).mean(axis=0)
    t, r = transform.transform.translation, transform.transform.rotation
    tx, ty, tz = rotate((r.x, r.y, r.z, r.w), (float(x), float(y), float(z)))
    return LocalizedDetection(
        detection_index=index,
        status=OperationStatus(code=OperationStatus.OK, message=""),
        centroid=PointStamped(header=header, point=Point(x=tx + t.x, y=ty + t.y, z=tz + t.z)),
        points_used=width,
    )
