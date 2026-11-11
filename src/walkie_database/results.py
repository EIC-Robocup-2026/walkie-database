"""Between the repository's plain records and the object services' messages.

A value a message cannot hold raises ValueError here. rclpy does not check integer
ranges when it builds a message. A value out of range fails only when the response is
sent, after the callback, and that stops the node.
"""

from __future__ import annotations

import array
import json
import math

from builtin_interfaces.msg import Time
from geometry_msgs.msg import Point, Point32, Polygon, Pose, PoseStamped, Quaternion, Vector3
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import Header
from walkie_database_msgs.msg import (
    Event,
    Fact,
    ObjectMatch,
    ObjectQuery,
    ObjectSummary,
    RoomSummary,
    SearchPrior,
    Surface,
)

from walkie_database.events import EventRecord
from walkie_database.geometry import quaternion_of_yaw
from walkie_database.queries import (
    CloudRecord,
    Match,
    ObjectFilter,
    ObjectRecord,
    Prior,
    RoomRecord,
    Snapshot,
    SurfaceRecord,
)

_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
_UINT32_MAX = 2**32 - 1
_UINT64_MAX = 2**64 - 1
_FLOAT32_MAX = 3.4028234663852886e38
SNAPSHOT_SCHEMA_VERSION = 1


def to_time(seconds: float, what: str) -> Time:
    """Seconds since the Unix epoch as a builtin_interfaces/Time.

    *what* names the value for the error, for example "object 4 has last_seen".
    """
    if not math.isfinite(seconds) or not _INT32_MIN <= math.floor(seconds) <= _INT32_MAX:
        raise ValueError(f"{what} {seconds!r}, which builtin_interfaces/Time cannot hold")
    # floor, not int: Time.nanosec is never negative, also before 1970.
    sec = math.floor(seconds)
    # Rounding can reach a whole second, and nanosec must stay below one.
    return Time(sec=sec, nanosec=min(round((seconds - sec) * 1e9), 999_999_999))


def to_query(msg: ObjectQuery) -> ObjectFilter:
    """The filter of an ObjectQuery. A handler checks its map_id first."""
    return ObjectFilter(
        room=msg.room,
        states=tuple(msg.states),
        layer=msg.layer,
        thing=msg.thing,
        category=msg.category,
        attributes=tuple((fact.key, fact.value) for fact in msg.attributes),
        relation=msg.relation,
        reference_id=msg.reference_id,
        reference_key=msg.reference_key,
        near_distance_m=msg.near_distance_m,
        superlative=msg.superlative,
        reference_frame=msg.reference_frame,
    )


def _in_range(value: int, low: int, high: int, what: str) -> int:
    if not low <= value <= high:
        raise ValueError(f"{what} {value}, which this message field cannot hold")
    return value


def object_summary(record: ObjectRecord) -> ObjectSummary:
    has_last_verified = record.last_verified is not None
    has_weight = record.weight_g is not None
    who = f"object {record.id} has"
    if has_weight and not (math.isfinite(record.weight_g) and abs(record.weight_g) <= _FLOAT32_MAX):
        raise ValueError(f"{who} weight_g {record.weight_g}, which this message field cannot hold")
    position, rotation, size = record.center, record.rotation, record.size
    pose = PoseStamped(
        pose=Pose(
            position=Point(x=position[0], y=position[1], z=position[2]),
            orientation=Quaternion(x=rotation[0], y=rotation[1], z=rotation[2], w=rotation[3]),
        )
    )
    if record.geometry_valid:
        pose.header.frame_id = record.frame_id
        if record.geometry_stamp is not None:
            pose.header.stamp = to_time(record.geometry_stamp, f"{who} geometry_stamp")
    return ObjectSummary(
        id=record.id,
        map_id=record.map_id,
        revision=_in_range(record.revision, 0, _UINT64_MAX, f"{who} revision"),
        key=record.key,
        label=record.label,
        labels=list(record.labels),
        official_name=record.official_name,
        category=record.category,
        layer=record.layer,
        state=record.state,
        source=record.source,
        room_id=record.room_id,
        room=record.room,
        place=record.place,
        place_anchor_id=record.place_anchor_id,
        attributes=[Fact(key=key, value=value) for key, value in sorted(record.attributes.items())],
        caption=record.caption,
        printed_text=record.printed_text,
        caption_model=record.caption_model,
        prompt_version=record.prompt_version,
        p_exist=record.p_exist,
        p_now=record.p_now,
        has_last_verified=has_last_verified,
        last_verified=to_time(record.last_verified, f"{who} last_verified") if has_last_verified else Time(),
        last_verified_age_s=record.age_s,
        first_seen=to_time(record.first_seen, f"{who} first_seen"),
        last_seen=to_time(record.last_seen, f"{who} last_seen"),
        n_detections=_in_range(record.n_detections, 0, _UINT32_MAX, f"{who} n_detections"),
        geometry_valid=record.geometry_valid,
        pose=pose,
        size=Vector3(x=size[0], y=size[1], z=size[2]),
        has_weight=has_weight,
        weight_g=record.weight_g if has_weight else 0.0,
        in_baseline=record.in_baseline,
        depth_seen=record.depth_seen,
        merged_into=record.merged_into,
        split_from=record.split_from,
    )


def object_match(match: Match) -> ObjectMatch:
    return ObjectMatch(
        object=object_summary(match.record),
        score=match.score,
        has_similarity=match.similarity is not None,
        similarity=match.similarity if match.similarity is not None else 0.0,
        matched_attributes=list(match.matched),
        unknown_attributes=list(match.unknown),
        mismatched_attributes=list(match.mismatched),
        measure_unknown=match.measure_unknown,
    )


def prior_message(prior: Prior) -> SearchPrior:
    return SearchPrior(
        official_name=prior.official_name, category=prior.category, place=prior.place, room=prior.room
    )


def _polygon(outline: list[list[float]], z: float) -> Polygon:
    return Polygon(points=[Point32(x=float(x), y=float(y), z=float(z)) for x, y in outline])


def _flat_pose(x: float, y: float, yaw: float, header: Header) -> PoseStamped:
    qx, qy, qz, qw = quaternion_of_yaw(yaw)
    return PoseStamped(
        header=header,
        pose=Pose(position=Point(x=x, y=y, z=0.0), orientation=Quaternion(x=qx, y=qy, z=qz, w=qw)),
    )


def room_summary(room: RoomRecord, frame_id: str) -> RoomSummary:
    message = RoomSummary(
        id=room.id,
        map_id=room.map_id,
        name=room.name,
        description=room.description,
        floor=_in_range(room.floor, _INT32_MIN, _INT32_MAX, f"room {room.id} has floor"),
        outline=_polygon(room.outline, 0.0),
    )
    if room.stand_pose is not None:
        message.has_stand_pose = True
        message.stand_pose = _flat_pose(*room.stand_pose, Header(frame_id=frame_id))
    return message


def surface_message(surface: SurfaceRecord) -> Surface:
    return Surface(
        key=surface.key,
        anchor_id=surface.anchor_id,
        map_id=surface.map_id,
        z=surface.z,
        outline=_polygon(surface.outline, surface.z),
        geometry_valid=surface.geometry_valid,
    )


def event_message(event: EventRecord, frame_id: str) -> Event:
    message = Event(
        id=event.id,
        stamp=to_time(event.ts, f"event {event.id} has ts"),
        kind=event.kind,
        text=event.text,
        map_id=event.map_id,
        room_id=event.room_id,
        object_id=event.object_id,
        person_id=event.person_id,
        session_id=event.session_id,
        task_id=event.task_id,
        cause=event.cause,
    )
    if event.pose is not None:
        message.has_robot_pose = True
        message.robot_pose = _flat_pose(*event.pose, Header(stamp=message.stamp, frame_id=frame_id))
    return message


def cloud_message(cloud: CloudRecord, frame_id: str) -> PointCloud2:
    header = Header(frame_id=frame_id)
    if cloud.stamp is not None:
        header.stamp = to_time(cloud.stamp, "an object cloud has geometry_stamp")
    return PointCloud2(
        header=header,
        height=1,
        width=cloud.n_points,
        fields=[
            PointField(name=name, offset=offset, datatype=PointField.FLOAT32, count=1)
            for name, offset in (("x", 0), ("y", 4), ("z", 8))
        ],
        is_bigendian=False,
        point_step=12,
        row_step=12 * cloud.n_points,
        data=array.array("B", cloud.points),
        is_dense=True,
    )


def snapshot_text(snapshot: Snapshot, *, sequence: int, now: float) -> str:
    """The snapshot's JSON, schema version 1. It holds no cloud, vector, crop or person."""
    stamp = to_time(now, "the snapshot has the time")
    objects = []
    for record in snapshot.records:
        verified = None
        if record.last_verified is not None:
            time = to_time(record.last_verified, f"object {record.id} has last_verified")
            verified = {"sec": time.sec, "nanosec": time.nanosec}
        objects.append(
            {
                "id": record.id,
                "key": record.key,
                "label": record.label,
                "layer": record.layer,
                "state": record.state,
                "geometry_valid": record.geometry_valid,
                "frame_id": record.frame_id,
                "position": list(record.center),
                "orientation": list(record.rotation),
                "size": list(record.size),
                "p_exist": record.p_exist,
                "last_verified": verified,
            }
        )
    return json.dumps(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "map_id": snapshot.map_id,
            "store_id": snapshot.store_id,
            "sequence": sequence,
            "stamp": {"sec": stamp.sec, "nanosec": stamp.nanosec},
            "objects": objects,
        },
        allow_nan=False,
    )
