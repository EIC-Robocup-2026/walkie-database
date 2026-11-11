#!/usr/bin/env python3
"""Replay a recorded detection topic through the object memory offline, and score it.

    pixi run memory-replay <bag folder> [--truth TRUTH.json] [--set NAME=VALUE ...]

The bag needs the detection topic, /tf, /tf_static and /clock. The tool reads it in
order and does what the node does in memory mode, without ROS discovery or timing:

- Each array is checked by ``ingress.check_observation`` against the latest /clock
  sample, then looked up in TF at its exact stamp once 50 ms more of the bag has
  been read (the node's lookup timeout), and merged through
  ``object_intake.map_detections``.
- Every ``--merge-period`` seconds of /clock, the memory runs one round of upkeep
  and saves into a scratch store, as the node's timer does. So the boxes, surfaces
  and ``on`` relations are the node's own.

The node can drop arrays when it falls behind (its subscription keeps one), and this
tool never does, so its result is the best the node could reach on the recording.

``--set`` overrides a ``MergeConfig`` field, for example ``--set match_distance=0.1``
or ``--set anchor_labels=table,shelf``. ``--truth`` scores the confirmed objects
against a JSON file of known boxes in the map frame, made by hand for each world
(``tools/truth/``): ``{"name": {"kind": "furniture" | "object", "min": [x, y, z],
"max": [x, y, z]}}``. See docs/setup/15-scene-graph-demo.md and ADR-0027.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import struct
import sys
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import rclpy.time
import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
from tf2_ros import Buffer, TransformException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from walkie_database.db import connect, transaction  # noqa: E402
from walkie_database.ingress import check_observation, stamp_seconds  # noqa: E402
from walkie_database.migrate import migrate  # noqa: E402
from walkie_database.object_context import Rejected  # noqa: E402
from walkie_database.object_intake import map_detections  # noqa: E402
from walkie_database.object_memory import ANCHOR, MOVABLE, Maintenance, MapDetection, MergeConfig, ObjectMemory  # noqa: E402
from walkie_database.object_memory_store import save  # noqa: E402
from walkie_database.objects import OBJECT  # noqa: E402
from walkie_database.people import vector_tables  # noqa: E402

LOOKUP_DELAY_S = 0.05  # the node's TF lookup timeout
REPLAY_MAP = "replay"


@dataclass
class Step:
    """One thing the node would do, in order: merge an array, or run its timer."""

    now: float
    detections: list[MapDetection] | None = None  # None for a timer tick


@dataclass
class Intake:
    """The steps of a recording, and what was refused on the way."""

    steps: list[Step] = field(default_factory=list)
    arrays: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    tf_misses: int = 0
    detections: int = 0
    with_embedding: int = 0


def _clock_seconds(data: bytes, clock_type) -> float:
    # A rosgraph_msgs/Clock in little-endian CDR: a 4-byte header, then sec and nanosec.
    if len(data) >= 12 and data[1] == 1:
        sec, nanosec = struct.unpack_from("<iI", data, 4)
        return sec + nanosec / 1e9
    return stamp_seconds(deserialize_message(data, clock_type).clock)


def read_intake(bag: str, *, topic: str, map_frame: str, merge_period: float) -> Intake:
    """Read a bag once into the steps the node would take. Independent of MergeConfig."""
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=bag, storage_id=""), rosbag2_py.ConverterOptions("cdr", "cdr"))
    types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    missing = [name for name in (topic, "/tf", "/clock") if name not in types]
    if missing:
        raise SystemExit(f"The bag has no {', '.join(missing)}.")
    wanted = [name for name in (topic, "/tf", "/tf_static", "/clock") if name in types]
    reader.set_filter(rosbag2_py.StorageFilter(topics=wanted))
    classes = {name: get_message(types[name]) for name in wanted}

    with tempfile.TemporaryDirectory() as scratch:
        conn = connect(Path(scratch) / "models.db")
        migrate(conn)
        models = dict(vector_tables(conn, OBJECT).lengths)
        conn.close()

    buffer = Buffer()
    intake = Intake()
    now = None
    next_tick = None
    pending: deque = deque()  # (receive time, array) waiting for the lookup delay

    def look_up(message) -> None:
        try:
            found = buffer.lookup_transform(map_frame, message.header.frame_id,
                                            rclpy.time.Time.from_msg(message.header.stamp))
        except TransformException:
            intake.tf_misses += 1
            return
        detections = map_detections(message, found)
        intake.detections += len(detections)
        intake.with_embedding += sum(d.embedding is not None for d in detections)
        intake.steps.append(Step(now=now, detections=detections))

    while reader.has_next():
        name, data, received_ns = reader.read_next()
        received = received_ns / 1e9
        while pending and pending[0][0] + LOOKUP_DELAY_S <= received:
            look_up(pending.popleft()[1])
        if name == "/clock":
            now = _clock_seconds(data, classes[name])
            if next_tick is None:
                next_tick = now + merge_period
            while now >= next_tick:
                intake.steps.append(Step(now=next_tick))
                next_tick += merge_period
        elif name == "/tf":
            for transform in deserialize_message(data, classes[name]).transforms:
                buffer.set_transform(transform, "bag")
        elif name == "/tf_static":
            for transform in deserialize_message(data, classes[name]).transforms:
                buffer.set_transform_static(transform, "bag")
        else:
            intake.arrays += 1
            message = deserialize_message(data, classes[name])
            if now is None:
                intake.rejected["NO_CLOCK"] = intake.rejected.get("NO_CLOCK", 0) + 1
                continue
            try:
                check_observation(message, models=models, map_frame=map_frame, now=now)
            except Rejected as e:
                intake.rejected[e.code] = intake.rejected.get(e.code, 0) + 1
                continue
            if message.detections:
                pending.append((received, message))
    while pending:
        look_up(pending.popleft()[1])
    if now is not None:
        intake.steps.append(Step(now=now))  # the node's save on shutdown
    return intake


@dataclass
class Run:
    memory: ObjectMemory
    relations: dict[int, int]  # object id to the id of the anchor it is on
    saves: int


def run(intake: Intake, config: MergeConfig) -> Run:
    """Merge the steps with one configuration, saving into a scratch store as the node does."""
    memory = ObjectMemory(config)
    maintenance = Maintenance()
    relations: dict[int, int] = {}
    saves = 0
    with tempfile.TemporaryDirectory() as scratch:
        conn = connect(Path(scratch) / "replay.db")
        migrate(conn)
        with transaction(conn):
            conn.execute("INSERT INTO map (id, created_at) VALUES (?, 0)", (REPLAY_MAP,))
        for step in intake.steps:
            if step.detections is not None:
                memory.add(step.detections)
                continue
            maintenance.merged.extend(memory.maintain(step.now).merged)
            saved = save(conn, memory, maintenance, map_id=REPLAY_MAP)
            if saved.inserted or saved.updated or saved.merged:
                relations = {src: dst for src, (dst, _) in saved.relations.items()}
                saves += 1
        conn.close()
    return Run(memory, relations, saves)


# Scoring ---------------------------------------------------------------------


def _overlap_xy(low_a, high_a, low_b, high_b) -> float:
    """The share of the smaller footprint that both boxes cover, seen from above."""
    dx = min(high_a[0], high_b[0]) - max(low_a[0], low_b[0])
    dy = min(high_a[1], high_b[1]) - max(low_a[1], low_b[1])
    if dx <= 0 or dy <= 0:
        return 0.0
    smaller = min((high_a[0] - low_a[0]) * (high_a[1] - low_a[1]), (high_b[0] - low_b[0]) * (high_b[1] - low_b[1]))
    return dx * dy / smaller if smaller > 0 else 0.0


def score(result: Run, truth: dict, *, object_radius: float, furniture_overlap: float) -> dict:
    """Match the confirmed tracks to the known boxes, and count what is right and wrong."""
    furniture = {k: v for k, v in truth.items() if v["kind"] == "furniture"}
    objects = {k: v for k, v in truth.items() if v["kind"] == "object"}
    owner: dict[int, str | None] = {}  # object id to the known box it stands for
    rows = []
    for track in result.memory.confirmed():
        if track.box is None or track.object_id is None:
            continue
        low, high = track.box.bounds()
        center = (low + high) / 2
        best, best_value = None, None
        if track.layer == ANCHOR:
            for name, known in furniture.items():
                value = _overlap_xy(low, high, known["min"], known["max"])
                if value >= furniture_overlap and (best_value is None or value > best_value):
                    best, best_value = name, value
        else:
            for name, known in objects.items():
                known_center = [(a + b) / 2 for a, b in zip(known["min"], known["max"])]
                distance = float(((center[0] - known_center[0]) ** 2 + (center[1] - known_center[1]) ** 2) ** 0.5)
                stands = low[2] <= known["max"][2] + 0.05 and high[2] >= known["min"][2] - 0.05
                if distance <= object_radius and stands and (best_value is None or distance < best_value):
                    best, best_value = name, distance
        owner[track.object_id] = best
        rows.append({
            "id": track.object_id, "label": track.label, "layer": track.layer, "labels": dict(track.labels),
            "detections": track.n_detections, "views": track.n_views,
            "center": [round(float(v), 3) for v in center], "size": [round(float(v), 3) for v in high - low],
            "surface_z": None if track.surface_z is None else round(track.surface_z, 3),
            "truth": best, "on": result.relations.get(track.object_id),
        })

    def claims(layer: str) -> dict[str, list[int]]:
        found: dict[str, list[int]] = {}
        for row in rows:
            if row["layer"] == layer and row["truth"] is not None:
                found.setdefault(row["truth"], []).append(row["id"])
        return found

    object_claims, furniture_claims = claims(MOVABLE), claims(ANCHOR)
    # An object rests on the furniture whose top is just below its bottom.
    expected = {}
    for name, known in objects.items():
        middle = [(a + b) / 2 for a, b in zip(known["min"], known["max"])]
        for base, under in furniture.items():
            inside = under["min"][0] - 0.02 <= middle[0] <= under["max"][0] + 0.02 and \
                under["min"][1] - 0.02 <= middle[1] <= under["max"][1] + 0.02
            if inside and -0.02 <= known["min"][2] - under["max"][2] <= 0.05:
                expected[name] = base
    right_relations = sum(
        1 for row in rows
        if row["layer"] == MOVABLE and row["on"] is not None and row["truth"] in expected
        and owner.get(row["on"]) == expected[row["truth"]]
    )
    stated_relations = sum(1 for row in rows if row["layer"] == MOVABLE and row["on"] is not None)
    found_expected = [name for name in expected if name in object_claims]
    related = {
        name for name in found_expected
        if any(owner.get(r["on"]) == expected[name] for r in rows if r["truth"] == name and r["on"] is not None)
    }
    movable = [r for r in rows if r["layer"] == MOVABLE]
    anchors = [r for r in rows if r["layer"] == ANCHOR]
    return {
        "objects_found": f"{len(object_claims)}/{len(objects)}",
        "tracks_per_found_object": round(sum(map(len, object_claims.values())) / max(1, len(object_claims)), 2),
        "movable_tracks": len(movable),
        "movable_unmatched": sum(1 for r in movable if r["truth"] is None),
        "furniture_found": f"{len(furniture_claims)}/{len(furniture)}",
        "anchors_per_found_furniture": round(sum(map(len, furniture_claims.values())) / max(1, len(furniture_claims)), 2),
        "anchor_tracks": len(anchors),
        "anchors_unmatched": sum(1 for r in anchors if r["truth"] is None),
        "on_expected_and_found": len(found_expected),
        "on_recalled": len(related),
        "on_stated": stated_relations,
        "on_right": right_relations,
        "object_claims": object_claims,
        "furniture_claims": furniture_claims,
        "expected_on": expected,
        "tracks": rows,
    }


def parse_overrides(pairs: list[str]) -> dict:
    fields = {f.name: f for f in dataclasses.fields(MergeConfig)}
    defaults = MergeConfig()
    overrides = {}
    for pair in pairs:
        name, _, text = pair.partition("=")
        if name not in fields:
            raise SystemExit(f"MergeConfig has no field {name!r}. Fields: {', '.join(fields)}")
        current = getattr(defaults, name)
        if isinstance(current, tuple):
            overrides[name] = tuple(v.strip() for v in text.split(",") if v.strip())
        elif isinstance(current, bool):
            overrides[name] = text.lower() in ("1", "true", "yes")
        elif isinstance(current, int):
            overrides[name] = int(text)
        else:
            overrides[name] = float(text)
    return overrides


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bag", help="the bag folder")
    parser.add_argument("--topic", default="/walkie_vision/sim/objects", help="the ObjectDetectionArray topic")
    parser.add_argument("--map-frame", default="map")
    parser.add_argument("--merge-period", type=float, default=2.0, help="seconds of /clock between upkeep rounds")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="override a MergeConfig field")
    parser.add_argument("--truth", help="a JSON file of known boxes to score against")
    parser.add_argument("--object-radius", type=float, default=0.15,
                        help="metres from a known object's centre, seen from above, that a track may stand for it")
    parser.add_argument("--furniture-overlap", type=float, default=0.3,
                        help="share of the smaller footprint an anchor must share with known furniture")
    parser.add_argument("--json", help="write the summary and every track to this file")
    args = parser.parse_args()

    config = dataclasses.replace(MergeConfig(), **parse_overrides(args.set))
    intake = read_intake(args.bag, topic=args.topic, map_frame=args.map_frame, merge_period=args.merge_period)
    result = run(intake, config)
    summary = {
        "arrays": intake.arrays, "rejected": intake.rejected, "tf_misses": intake.tf_misses,
        "detections": intake.detections, "with_embedding": intake.with_embedding,
        "confirmed": len(result.memory.confirmed()), "relations": len(result.relations),
        "config": dataclasses.asdict(config),
    }
    if args.truth:
        summary["score"] = score(result, json.loads(Path(args.truth).read_text()),
                                 object_radius=args.object_radius, furniture_overlap=args.furniture_overlap)
    brief = {k: v for k, v in summary.items() if k != "config"}
    if "score" in brief:
        brief["score"] = {k: v for k, v in brief["score"].items() if k != "tracks"}
    print(json.dumps(brief, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
