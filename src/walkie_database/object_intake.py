"""Turn a checked ObjectDetectionArray into map-frame detections for the object memory.

The node and ``tools/replay_memory.py`` both call :func:`map_detections`, so a
recording replayed offline is merged exactly as the node merges it live. The array
must already have passed ``ingress.check_observation``, and *transform* is the
camera's pose in the map frame at the array's stamp.
"""

from __future__ import annotations

import numpy as np
from geometry_msgs.msg import TransformStamped
from walkie_vision_msgs.msg import ObjectDetectionArray

from walkie_database.ingress import cloud_points, stamp_seconds
from walkie_database.object_memory import MapDetection, rotation_matrix


def map_detections(message: ObjectDetectionArray, transform: TransformStamped) -> list[MapDetection]:
    """The detections that have points, with their points moved into the map frame."""
    t, r = transform.transform.translation, transform.transform.rotation
    position, rotation = (t.x, t.y, t.z), (r.x, r.y, r.z, r.w)
    turn, offset = rotation_matrix(rotation), np.array(position)
    stamp = stamp_seconds(message.header.stamp)
    detections = []
    for detection in message.detections:
        if detection.points.width == 0:
            continue
        points = cloud_points(detection.points).astype(np.float64) @ turn.T + offset
        has_vector = bool(detection.embedding_model)
        detections.append(
            MapDetection(
                label=detection.label,
                score=detection.score,
                points=points,
                stamp=stamp,
                view_quality=detection.view_quality,
                model=detection.embedding_model,
                embedding=np.asarray(detection.embedding, dtype=np.float64) if has_vector else None,
                camera_position=position,
                camera_rotation=rotation,
                color_shares=tuple(detection.color_shares) or None,
            )
        )
    return detections
