"""Small rotations by a unit quaternion, in plain Python values.

A quaternion is ``(x, y, z, w)`` everywhere, as in geometry_msgs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Vector = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]


def rotate(rotation: Sequence[float], point: Sequence[float]) -> Vector:
    """*point* turned by the unit quaternion *rotation*."""
    x, y, z, w = rotation
    px, py, pz = point
    # v' = v + 2w (u x v) + 2 u x (u x v), with u the vector part.
    tx = 2.0 * (y * pz - z * py)
    ty = 2.0 * (z * px - x * pz)
    tz = 2.0 * (x * py - y * px)
    return (
        px + w * tx + y * tz - z * ty,
        py + w * ty + z * tx - x * tz,
        pz + w * tz + x * ty - y * tx,
    )


def rotate_back(rotation: Sequence[float], point: Sequence[float]) -> Vector:
    """*point* turned by the inverse of the unit quaternion *rotation*."""
    x, y, z, w = rotation
    return rotate((-x, -y, -z, w), point)


def yaw_of(rotation: Sequence[float]) -> float:
    """The rotation about the z axis, in radians."""
    x, y, z, w = rotation
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def quaternion_of_yaw(yaw: float) -> Quaternion:
    """The unit quaternion of a rotation of *yaw* radians about the z axis."""
    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))
