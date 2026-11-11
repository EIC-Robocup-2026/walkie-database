"""The object memory as RViz and Foxglove see it: a MarkerArray and one PointCloud2.

Each message holds the whole memory, so with transient local durability a late
subscriber gets the current picture at once. The marker array starts with a
DELETEALL, so objects that merged away disappear.

Namespaces of the marker array:

``objects``
    One translucent box per confirmed object, coloured by label.
``labels``
    One text per object above its box: ``#<id> <label> (<detections>)``.
``relations``
    A line from each object's bottom down to the top surface of the anchor it rests
    on, and the word ``on``.
"""

from __future__ import annotations

import colorsys
import hashlib
from collections.abc import Mapping, Sequence

import numpy as np
from builtin_interfaces.msg import Time
from geometry_msgs.msg import Point, Pose, Quaternion, Vector3
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import ColorRGBA, Header
from visualization_msgs.msg import Marker, MarkerArray

from walkie_database.object_memory import ANCHOR, Track

_TEXT_HEIGHT = 0.08
_BOX_ALPHA = {ANCHOR: 0.15, "movable": 0.45}


def label_color(label: str) -> tuple[float, float, float]:
    """A stable bright colour for a label, the same in every run."""
    hue = int.from_bytes(hashlib.sha1(label.encode()).digest()[:2], "big") / 65535.0
    return colorsys.hsv_to_rgb(hue, 0.75, 0.95)


def marker_array(
    tracks: Sequence[Track], relations: Mapping[int, int], *, frame_id: str, stamp: Time
) -> MarkerArray:
    """Markers for each stored track. *relations* maps an object id to its anchor's id."""
    header = Header(stamp=stamp, frame_id=frame_id)
    markers = [Marker(header=header, action=Marker.DELETEALL)]
    shown = {}
    for track in tracks:
        if track.object_id is None or track.box is None:
            continue
        box = track.box
        r, g, b = label_color(track.label)
        qx, qy, qz, qw = box.rotation
        shown[track.object_id] = track
        markers.append(
            Marker(
                header=header,
                ns="objects",
                id=track.object_id,
                type=Marker.CUBE,
                action=Marker.ADD,
                pose=Pose(position=Point(x=box.center[0], y=box.center[1], z=box.center[2]),
                          orientation=Quaternion(x=qx, y=qy, z=qz, w=qw)),
                scale=Vector3(x=box.size[0], y=box.size[1], z=box.size[2]),
                color=ColorRGBA(r=r, g=g, b=b, a=_BOX_ALPHA.get(track.layer, 0.45)),
            )
        )
        markers.append(
            Marker(
                header=header,
                ns="labels",
                id=track.object_id,
                type=Marker.TEXT_VIEW_FACING,
                action=Marker.ADD,
                pose=Pose(position=Point(x=box.center[0], y=box.center[1], z=box.top + _TEXT_HEIGHT),
                          orientation=Quaternion(w=1.0)),
                scale=Vector3(z=_TEXT_HEIGHT),
                color=ColorRGBA(r=1.0, g=1.0, b=1.0, a=1.0),
                text=f"#{track.object_id} {track.label} ({track.n_detections})",
            )
        )
    lines = Marker(
        header=header, ns="relations", id=0, type=Marker.LINE_LIST, action=Marker.ADD,
        pose=Pose(orientation=Quaternion(w=1.0)), scale=Vector3(x=0.01),
        color=ColorRGBA(r=1.0, g=0.85, b=0.2, a=0.9),
    )
    for object_id, anchor_id in sorted(relations.items()):
        child, parent = shown.get(object_id), shown.get(anchor_id)
        if child is None or parent is None:
            continue
        # The line ends on the anchor's top surface, which lies below the box top when
        # something taller stands on the anchor.
        surface = parent.box.top if parent.surface_z is None else parent.surface_z
        start = Point(x=child.box.center[0], y=child.box.center[1], z=child.box.bottom)
        end = Point(x=parent.box.center[0], y=parent.box.center[1], z=surface)
        lines.points.extend([start, end])
        markers.append(
            Marker(
                header=header, ns="relations", id=object_id, type=Marker.TEXT_VIEW_FACING,
                action=Marker.ADD,
                pose=Pose(position=Point(x=(start.x + end.x) / 2, y=(start.y + end.y) / 2,
                                         z=(start.z + end.z) / 2 + 0.03),
                          orientation=Quaternion(w=1.0)),
                scale=Vector3(z=0.05), color=ColorRGBA(r=1.0, g=0.85, b=0.2, a=1.0),
                text="on",
            )
        )
    if lines.points:
        markers.append(lines)
    return MarkerArray(markers=markers)


def cloud_message(tracks: Sequence[Track], *, frame_id: str, stamp: Time) -> PointCloud2:
    """Every stored track's points, with a packed rgb by label and the object's id."""
    rows = []
    for track in tracks:
        if track.object_id is None or len(track.points) == 0:
            continue
        r, g, b = (int(round(255 * c)) for c in label_color(track.label))
        rgb = np.array([(r << 16) | (g << 8) | b], dtype="<u4").view("<f4")[0]
        block = np.zeros(len(track.points), dtype=_CLOUD_DTYPE)
        block["x"], block["y"], block["z"] = track.points.T.astype("<f4")
        block["rgb"] = rgb
        block["object_id"] = track.object_id
        rows.append(block)
    data = np.concatenate(rows) if rows else np.zeros(0, dtype=_CLOUD_DTYPE)
    return PointCloud2(
        header=Header(stamp=stamp, frame_id=frame_id),
        height=1,
        width=len(data),
        fields=_CLOUD_FIELDS,
        is_bigendian=False,
        point_step=_CLOUD_DTYPE.itemsize,
        row_step=_CLOUD_DTYPE.itemsize * len(data),
        data=data.tobytes(),
        is_dense=True,
    )


_CLOUD_DTYPE = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "<f4"), ("object_id", "<u4")])
_CLOUD_FIELDS = [
    PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
    PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
    PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
    PointField(name="rgb", offset=12, datatype=PointField.FLOAT32, count=1),
    PointField(name="object_id", offset=16, datatype=PointField.UINT32, count=1),
]
