#!/usr/bin/env python3
"""The peers that tools/pair_check.py starts, one role per process.

Each role runs in one repository's Pixi environment: the vision roles in
walkie-vision's, the database roles in walkie-database's. pair_check copies
this file into its run folder and runs it with `python -P`, so neither
repository's tools folder is on the import path.

A role writes one JSON file: its checks, and the import evidence of the process
that ran them. pair_check decides from that evidence whether the peer used its
own repository's builds. A check that saw no message fails: silence is never a pass.

The module itself imports only the standard library, so the helpers at the top
can be tested anywhere. ROS, NumPy and the repositories' packages are imported
inside the roles.

Fake vectors here are hash vectors under a fake_ model name. They are never
labelled as CLIP, and the database must refuse them.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import struct
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

NAMESPACE = "/pair_check"
CAMERA_FRAME = "pair_camera_optical"
UNKNOWN_FRAME = "pair_unknown_optical"
MAP_FRAME = "map"
VECTOR_MODEL = "fake_pair_vectors"
VECTOR_DIM = 512
# The stream ID of the malformed arrays, so the listener knows them.
MALFORMED_STREAM = "2f7c1d9e-4b3a-4c8e-9d61-5a0e7b3c2f18"
POINT_STEP = 12

IMAGE_WIDTH, IMAGE_HEIGHT = 64, 48
CAMERA_K = [50.0, 0.0, 32.0, 0.0, 50.0, 24.0, 0.0, 0.0, 1.0]
CROP_BOX = (20.0, 12.0, 44.0, 36.0)
STREAM_CLASSES = ["cup", "bottle", "bowl"]
ON_DEMAND_CLASSES = ["cup"]
TEXTS = ["red cup"]

IMAGE_TOPIC = f"{NAMESPACE}/camera/image"
CAMERA_INFO_TOPIC = f"{NAMESPACE}/camera/camera_info"
VISION_OBJECTS = f"{NAMESPACE}/vision/objects"
VISION_SERVICES = {
    name: f"{NAMESPACE}/vision/{name}"
    for name in ("embed_text", "embed_crop", "describe_crop", "detect_objects", "get_person_crop")
}

# What the tracked seed fixtures (tests/fixtures/pair) hold.
ROOM_NAMES = ["kitchen room", "living room"]
LIVING_ROOM_CONTENTS = ["table", "wardrobe"]
PRIOR = ("blue cup", "table", "living room")
SURFACE = ("table_top", 0.8)
EVENT_TEXT = "The pair gate checked the table."

# The camera's two poses in the map: a moving camera, so that a lookup at the
# older stamp can be told from the latest transform.
POSE_1 = ((1.0, 2.0, 0.5), (0.0, 0.0, 0.0, 1.0))
POSE_2 = ((5.0, 2.0, 0.5), (0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)))
BLOCK_A = (0.1, 0.2, 1.0)
BLOCK_B = (-0.3, 0.1, 2.0)
TOLERANCE_M = 1e-4

MODEL_MODULES = (
    "torch", "torchvision", "ultralytics", "onnxruntime", "open_clip", "clip", "transformers",
    "timm", "insightface", "huggingface_hub", "sentence_transformers",
)  # fmt: skip
SIDE_MODULES = {
    "vision": ["rclpy", "walkie_vision", "walkie_vision_msgs", "walkie_vision_msgs.msg", "walkie_vision_msgs.srv"],
    "database": [
        "rclpy", "walkie_database", "walkie_vision_msgs", "walkie_vision_msgs.msg", "walkie_vision_msgs.srv",
        "walkie_database_msgs", "walkie_database_msgs.msg", "walkie_database_msgs.srv",
    ],
}  # fmt: skip
DATABASE_TYPES = ["walkie_database_msgs", "walkie_database_msgs.msg", "walkie_database_msgs.srv"]
ENV_NAMES = (
    "PIXI_PROJECT_ROOT", "CONDA_PREFIX", "RMW_IMPLEMENTATION", "ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY",
    "ROS_AUTOMATIC_DISCOVERY_RANGE", "CUDA_VISIBLE_DEVICES", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
    "YOLO_AUTOINSTALL", "AMENT_PREFIX_PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "ROS_LOG_DIR",
)  # fmt: skip

LISTEN_COUNT = 5
# Arrays the database node itself must count in each phase.
NODE_COUNT = 3
LISTEN_TIMEOUT_S = 30.0
SERVICE_TIMEOUT_S = 20.0


class PeerError(Exception):
    """A role could not run its exchange, for the result file."""


# Pure helpers ----------------------------------------------------------------


def check(name: str, ok: bool, detail: str = "", **evidence) -> dict:
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail, "evidence": evidence}


def pattern_image(width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """A bgr8 image whose every pixel is known, so both peers can rebuild it."""
    return bytes((x * 7 + y * 13 + c * 61) % 256 for y in range(height) for x in range(width) for c in range(3))


def block_points(center: Sequence[float], count: int = 40) -> list[tuple[float, float, float]]:
    """*count* points in pairs placed symmetrically about *center*, so their mean is the centre."""
    if count % 2 or count < 2:
        raise ValueError("count must be even and positive")
    cx, cy, cz = center
    points = []
    for i in range(count // 2):
        dx, dy, dz = 0.002 * (i % 5 + 1), 0.003 * (i % 4 + 1), 0.001 * (i % 3 + 1)
        points += [(cx + dx, cy + dy, cz + dz), (cx - dx, cy - dy, cz - dz)]
    return points


def rotate(q: Sequence[float], v: Sequence[float]) -> tuple[float, float, float]:
    """Rotate *v* by the unit quaternion *q*, given as x, y, z, w."""
    x, y, z, w = q
    vx, vy, vz = v
    # t = 2 q_xyz × v; v' = v + w t + q_xyz × t
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def expected_centroid(pose, points: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    """The map-frame mean of camera-frame *points*, for a camera at *pose* (translation, rotation)."""
    n = len(points)
    mean = tuple(sum(p[i] for p in points) / n for i in range(3))
    translation, rotation = pose
    turned = rotate(rotation, mean)
    return tuple(turned[i] + translation[i] for i in range(3))


def near(a: Sequence[float], b: Sequence[float], tolerance: float = TOLERANCE_M) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def float32_bits(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def same_float32(a: Sequence[float], b: Sequence[float]) -> bool:
    """True when two vectors hold the same float32 values, bit for bit."""
    return len(a) == len(b) and float32_bits(a) == float32_bits(b)


def vector_digest(values: Sequence[float]) -> str:
    return hashlib.sha256(float32_bits(values)).hexdigest()[:16]


def phase_checks(
    phase: str,
    arrays: list[dict],
    before: dict | None,
    after: dict | None,
    expected: dict | None = None,
    minimum: int = LISTEN_COUNT,
) -> list[dict]:
    """The listener's verdict on one phase of observations.

    *arrays* are summaries of the arrays the listener got, each with "local": None
    when the database's own validator passed it, or [code, message] when it refused
    it. *before* and *after* are the node's store status around the phase.
    """
    checks = [check(f"{phase}.received", len(arrays) >= minimum, f"{len(arrays)} arrays reached the listener")]
    if before is None or after is None:
        return [*checks, check(f"{phase}.counters", False, "the store status could not be read")]
    received = after["received"] - before["received"]
    rejected = after["rejected"] - before["rejected"]
    counters = {"received": received, "rejected": rejected}
    refusals = [a["local"] for a in arrays]
    if phase in ("nonempty", "empty"):
        checks.append(check(f"{phase}.accepted", bool(arrays) and all(r is None for r in refusals),
                            "the database's validator passed every array", refusals=refusals[:3]))  # fmt: skip
        checks.append(check(f"{phase}.counters", received >= NODE_COUNT and rejected == 0,
                            f"the node counted {received} arrays and refused {rejected}", **counters))  # fmt: skip
    else:
        checks.append(check(f"{phase}.counters", received >= NODE_COUNT and rejected == received,
                            f"the node counted {received} arrays and refused {rejected}", **counters))  # fmt: skip
    detections = [d for a in arrays for d in a["detections"]]
    if phase == "nonempty":
        good = bool(detections) and all(a["detections"] for a in arrays) and all(
            d["points"] >= 30 and d["crop_bytes"] > 0 and not d["embedding_model"] and not d["embedding"]
            for d in detections
        )
        checks.append(check(f"{phase}.content", good, "every array has detections with points and a crop, and no vector",
                            detections=[{k: v for k, v in d.items() if k != "embedding"} for d in detections[:4]]))  # fmt: skip
    elif phase == "empty":
        good = bool(arrays) and all(not a["detections"] and a["classes"] for a in arrays)
        checks.append(check(f"{phase}.content", good, "every array is empty and still names its classes"))
    elif phase == "vector":
        expected = expected or {}
        wrong = [d["label"] for d in detections
                 if d["embedding_model"] != VECTOR_MODEL or not same_float32(d["embedding"], expected.get(d["label"], []))]  # fmt: skip
        checks.append(check(f"{phase}.decoded", bool(detections) and not wrong,
                            "every vector arrived bit for bit as the publisher's fake made it", wrong=wrong,
                            digests={d["label"]: vector_digest(d["embedding"]) for d in detections[:4]}))  # fmt: skip
        checks.append(check(f"{phase}.refused", bool(refusals) and all(
            r is not None and r[0] == "INVALID" and "no vector table" in r[1] for r in refusals),
            "the database refuses the fake model", refusal=refusals[0] if refusals else None))  # fmt: skip
        named = VECTOR_MODEL.startswith("fake_") and "clip" not in VECTOR_MODEL.lower()
        stored = VECTOR_MODEL not in after.get("models", [])
        checks.append(check(f"{phase}.not-clip", named and stored,
                            "the fake vectors carry a fake_ name, and the store has no table for it",
                            models=after.get("models")))  # fmt: skip
    elif phase == "malformed":
        good = bool(arrays) and all(a["stream_id"] == MALFORMED_STREAM and a["detections"] for a in arrays)
        checks.append(check(f"{phase}.content", good, "the malformed arrays arrived whole, not as empty arrays"))
        checks.append(check(f"{phase}.refused", bool(refusals) and all(
            r is not None and r[0] == "INVALID" and "bytes for" in r[1] for r in refusals),
            "the database refuses the cut cloud", refusal=refusals[0] if refusals else None))  # fmt: skip
    else:
        raise ValueError(f"no phase {phase!r}")
    return checks


# Evidence --------------------------------------------------------------------


def loaded_libraries() -> list[str]:
    """The walkie message libraries this process has mapped, from /proc/self/maps."""
    found = set()
    try:
        with open("/proc/self/maps") as maps:
            for line in maps:
                parts = line.split(maxsplit=5)
                if len(parts) == 6:
                    path = parts[5].strip()
                    name = os.path.basename(path)
                    if "walkie_" in name and ".so" in name:
                        found.add(path)
    except OSError:
        pass
    return sorted(found)


def _load_type_support(names: Sequence[str]) -> None:
    for name in names:
        if not name.endswith((".msg", ".srv")):
            continue
        module = sys.modules[name]
        for attribute in sorted(vars(module)):
            value = getattr(module, attribute)
            load = getattr(type(value), "__import_type_support__", None)
            if isinstance(value, type) and load is not None:
                load()


def evidence(side: str, *, database_types: bool = False, typesupport: bool = True) -> dict:
    """Where this process's Python, packages and message libraries come from."""
    names = list(SIDE_MODULES[side]) + (DATABASE_TYPES if side == "vision" and database_types else [])
    modules, errors = {}, {}
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - every failure is evidence
            errors[name] = f"{type(e).__name__}: {e}"
            continue
        path = getattr(module, "__file__", None)
        modules[name] = {"file": path, "real": os.path.realpath(path) if path else None}
    loaded: bool | str | None = None
    if typesupport:
        try:
            _load_type_support([n for n in names if n in modules])
            loaded = True
        except Exception as e:  # noqa: BLE001
            loaded = f"{type(e).__name__}: {e}"
    files = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if isinstance(path, str):
            files.update((path, os.path.realpath(path)))
    rmw = None
    if "rclpy" in sys.modules:
        try:
            rmw = sys.modules["rclpy"].get_rmw_implementation_identifier()
        except Exception as e:  # noqa: BLE001
            rmw = f"{type(e).__name__}: {e}"
    return {
        "side": side,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "python": sys.version,
        "pid": os.getpid(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "env": {name: os.environ.get(name) for name in ENV_NAMES},
        "rmw": rmw,
        "modules": modules,
        "import_errors": errors,
        "typesupport": loaded,
        "libraries": loaded_libraries(),
        "sys_path": list(sys.path),
        "module_files": sorted(files),
        "model_modules": sorted(set(MODEL_MODULES) & set(sys.modules)),
    }


def write(path: str | Path, data: dict) -> None:
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=1) + "\n")
    temporary.replace(path)


# ROS helpers -----------------------------------------------------------------


def to_time(seconds: float):
    from builtin_interfaces.msg import Time

    whole = math.floor(seconds)
    return Time(sec=int(whole), nanosec=int(round((seconds - whole) * 1e9)) % 1_000_000_000)


def seconds_of(stamp) -> float:
    return stamp.sec + stamp.nanosec / 1e9


def wall_now(node) -> float:
    return node.get_clock().now().nanoseconds / 1e9


def spin_for(node, seconds: float) -> None:
    import rclpy

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())))


def call(node, client, request, timeout: float = SERVICE_TIMEOUT_S):
    """One service call, waited for while this node spins. Raises PeerError on no answer."""
    import rclpy

    if not client.wait_for_service(timeout_sec=timeout):
        raise PeerError(f"{client.srv_name} is not available after {timeout:g} s")
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
    if not future.done():
        client.remove_pending_request(future)
        raise PeerError(f"{client.srv_name} gave no answer in {timeout:g} s")
    return future.result()


def guarded(name: str, checks: list[dict], body: Callable[[], None]) -> None:
    """Run one exchange. A PeerError or a crash is a failed check, and the role goes on."""
    try:
        body()
    except Exception as e:  # noqa: BLE001 - a failure is recorded, never hidden
        checks.append(check(name, False, f"{type(e).__name__}: {e}"))


def _run_ros(body: Callable[[], int]) -> int:
    """Run *body* between rclpy.init and shutdown. SIGINT ends it without an error."""
    import rclpy
    from rclpy.executors import ExternalShutdownException

    rclpy.init(args=["contract_peer"])
    try:
        return body()
    except (KeyboardInterrupt, ExternalShutdownException):
        return 0
    finally:
        rclpy.try_shutdown()


# Roles -----------------------------------------------------------------------

ROLES: dict[str, Callable[[argparse.Namespace], int]] = {}


def role(name: str):
    def register(function):
        ROLES[name] = function
        return function

    return register


@role("pin")
def pin_role(args: argparse.Namespace) -> int:
    """Read the vision pin with the import tool's own reader."""
    root = Path(args.database_root)
    spec = importlib.util.spec_from_file_location("import_interfaces", root / "tools" / "import_interfaces.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    url, version = module.read_pin(root)
    write(args.out, {"url": url, "version": version, "evidence": evidence("database", typesupport=False)})
    return 0


@role("probe")
def probe_role(args: argparse.Namespace) -> int:
    data = evidence(args.side, database_types=args.side == "vision", typesupport=not args.no_typesupport)
    write(args.out, data)
    return 0


@role("store-rows")
def store_rows_role(args: argparse.Namespace) -> int:
    """Read a store's map, object, event and request rows, and every table's count. Writes nothing."""
    from walkie_database.db import connect

    conn = connect(args.db, readonly=True)
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name"
            )
        ]
        rows = {
            "map": [dict(r) for r in conn.execute("SELECT * FROM map ORDER BY id")],
            "store_meta": [dict(r) for r in conn.execute("SELECT * FROM store_meta")],
            "object": [dict(r) for r in conn.execute("SELECT * FROM object ORDER BY id")],
            "event": [dict(r) for r in conn.execute("SELECT * FROM event ORDER BY id")],
            "write_request": [
                dict(r) for r in conn.execute("SELECT request_id, task_id, operation FROM write_request ORDER BY request_id")
            ],
            "counts": {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables},
        }
    finally:
        conn.close()
    write(args.out, {**rows, "evidence": evidence("database", typesupport=False)})
    return 0


@role("database-node")
def database_node_role(args: argparse.Namespace) -> int:
    """Run the database node's own main() with these ROS arguments, then record evidence."""
    from walkie_database import node

    sys.argv = ["walkie_database.node", *args.ros_args]
    code = 0
    try:
        node.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    write(args.out, {"returncode": code, "evidence": evidence("database")})
    return code


@role("vision-publisher")
def vision_publisher_role(args: argparse.Namespace) -> int:
    """Run the fake publisher's own main() with these ROS arguments, then record evidence."""
    from walkie_vision import fake_publisher

    code = fake_publisher.main(["fake_publisher", *args.ros_args])
    write(args.out, {"returncode": code, "evidence": evidence("vision")})
    return code


@role("expect-vectors")
def expect_vectors_role(args: argparse.Namespace) -> int:
    """Compute, with the vision repository's own fakes, every vector the database side must receive."""
    import numpy as np
    from builtin_interfaces.msg import Time
    from std_msgs.msg import Header
    from walkie_vision import convert, requests
    from walkie_vision.fake_publisher import FakeStream, load_scene
    from walkie_vision.fakes import FakeEmbedder
    from walkie_vision_msgs.srv import EmbedText

    stream = FakeStream(load_scene(args.scene), embedding_model=VECTOR_MODEL, embedding_dim=VECTOR_DIM)
    array = stream.array(0, Time(sec=1))
    vectors = {d.label: [float(v) for v in d.embedding] for d in array.detections}
    text = convert.fill_embed_text_response(EmbedText.Response(), FakeEmbedder(), TEXTS)
    image = np.frombuffer(pattern_image(), dtype=np.uint8).reshape(IMAGE_HEIGHT, IMAGE_WIDTH, 3).copy()
    box = convert.bbox_to_box(convert.box_to_bbox(CROP_BOX))
    crop = requests.embed_crop_answer(FakeEmbedder(), image, Header(), box, None, 0.2, 3.0)
    write(args.out, {
        "stream": vectors,
        "text": {"success": text.success, "model": text.embedding_model, "dim": text.dim,
                 "vectors": [float(v) for v in text.vectors]},
        "crop": {"model": crop.embedding_model, "embedding": [float(v) for v in crop.embedding]},
        "evidence": evidence("vision"),
    })  # fmt: skip
    return 0


@role("vision-services")
def vision_services_role(args: argparse.Namespace) -> int:
    """Serve the vision node with fake providers until SIGINT, then record its counters."""
    import rclpy
    from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
    from rclpy.parameter import Parameter
    from walkie_vision.fakes import (
        FakeEmbedder, FakeObjectDetector, FakePersonFaceRecognizer, FakePoseEstimator, FakeReIdentifier,
    )  # fmt: skip
    from walkie_vision.node import EXECUTOR_THREADS, VisionNode
    from walkie_vision.pipeline import Providers, TrackIdBase

    providers = Providers(
        objects=FakeObjectDetector([], classes=STREAM_CLASSES),
        pose=FakePoseEstimator([]),
        faces=FakePersonFaceRecognizer([]),
        reid=FakeReIdentifier(),
        embedder=FakeEmbedder(),
        on_demand=FakeObjectDetector([]),
    )
    overrides = [
        Parameter("classes", value=STREAM_CLASSES),
        Parameter("image_topic", value=IMAGE_TOPIC),
        Parameter("camera_info_topic", value=CAMERA_INFO_TOPIC),
        Parameter("people_topic", value=f"{NAMESPACE}/vision/people"),
        Parameter("objects_topic", value=VISION_OBJECTS),
        Parameter("person_crop_service", value=VISION_SERVICES["get_person_crop"]),
        Parameter("embed_text_service", value=VISION_SERVICES["embed_text"]),
        Parameter("embed_crop_service", value=VISION_SERVICES["embed_crop"]),
        Parameter("describe_crop_service", value=VISION_SERVICES["describe_crop"]),
        Parameter("detect_objects_service", value=VISION_SERVICES["detect_objects"]),
        Parameter("device", value="cpu"),
        Parameter("object_embeddings_enabled", value=True),
        Parameter("on_demand_enabled", value=True),
        Parameter("max_rate_hz", value=0.0),
    ]
    rclpy.init(args=["contract_peer"])
    node = executor = None
    counters: dict = {}
    try:
        node = VisionNode(providers, track_ids=TrackIdBase(start_ns=0), parameter_overrides=overrides)
        executor = MultiThreadedExecutor(num_threads=EXECUTOR_THREADS)
        executor.add_node(node)
        while rclpy.ok() and node.fatal is None:
            executor.spin_once(timeout_sec=0.5)
            node.check_health()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            counters = {
                "frames_received": node.frames_received,
                "frames_processed": node.frames_processed,
                "service_jobs": node.service_jobs,
                "service_busy": node.service_busy,
                "classes_prepared": node.classes_prepared,
                "late_results": node.late_results,
                "fatal": node.fatal,
            }
            counters["closed"] = node.close()
        if executor is not None:
            executor.shutdown()
        rclpy.try_shutdown()
    write(args.out, {**counters, "evidence": evidence("vision")})
    return 0 if counters.get("closed") and not counters.get("fatal") else 1


@role("vision-malformed")
def vision_malformed_role(args: argparse.Namespace) -> int:
    """Publish the fake scene's array with one cloud cut short, at 10 Hz until SIGINT."""
    import array as array_module

    import rclpy
    from rclpy.node import Node
    from walkie_vision.fake_publisher import OBSERVATION_QOS, FakeStream, load_scene
    from walkie_vision_msgs.msg import ObjectDetectionArray

    stream = FakeStream(load_scene(args.scene), stream_id=MALFORMED_STREAM)
    state = {"published": 0, "cut": None}

    def body() -> int:
        node = Node("pair_check_malformed")
        publisher = node.create_publisher(ObjectDetectionArray, args.topic, OBSERVATION_QOS)

        def tick() -> None:
            msg = stream.array(0, node.get_clock().now().to_msg())
            points = msg.detections[0].points
            data = bytes(points.data)
            # The cloud keeps its width but loses one point's bytes.
            points.data = array_module.array("B", data[:-POINT_STEP])
            state["cut"] = {"width": points.width, "bytes": len(data) - POINT_STEP}
            publisher.publish(msg)
            state["published"] += 1

        node.create_timer(0.1, tick)
        try:
            rclpy.spin(node)
        finally:
            node.destroy_node()
        return 0

    code = _run_ros(body)
    write(args.out, {**state, "evidence": evidence("vision")})
    return code if state["published"] else 1


def _summary(msg, refusal) -> dict:
    return {
        "stream_id": msg.stream_id,
        "frame_id": msg.header.frame_id,
        "classes": list(msg.classes),
        "detections": [
            {
                "label": d.label,
                "points": d.points.width,
                "crop_bytes": len(d.crop.data),
                "embedding_model": d.embedding_model,
                "embedding": [float(v) for v in d.embedding],
            }
            for d in msg.detections
        ],
        "local": refusal,
    }


def _status(response) -> dict:
    return {
        "code": response.status.code,
        "map_id": response.map_id,
        "map_valid": response.map_valid,
        "clock_kind": response.clock_kind,
        "ingest_enabled": response.ingest_enabled,
        "captions_enabled": response.captions_enabled,
        "models": list(response.object_embedding_models),
        "received": response.arrays_received,
        "rejected": response.arrays_rejected,
    }


@role("database-listener")
def database_listener_role(args: argparse.Namespace) -> int:
    """Listen to one phase of arrays as the database does, and judge them with its own validator."""
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from walkie_database import people
    from walkie_database.db import connect
    from walkie_database.ingress import check_observation
    from walkie_database.object_context import Rejected
    from walkie_database.objects import OBJECT
    from walkie_database_msgs.srv import GetStoreStatus
    from walkie_vision_msgs.msg import ObjectDetectionArray

    result: dict = {"phase": args.phase, "checks": []}
    conn = connect(args.db, readonly=True)
    try:
        models = dict(people.vector_tables(conn, OBJECT).lengths)
    finally:
        conn.close()
    expected = json.loads(Path(args.vectors).read_text())["stream"] if args.phase == "vector" else None

    def body() -> int:
        node = Node("pair_check_listener")
        try:
            received: list = []
            qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.VOLATILE)  # fmt: skip
            node.create_subscription(ObjectDetectionArray, args.topic, lambda m: received.append((m, wall_now(node))), qos)
            status = node.create_client(GetStoreStatus, f"{args.node}/get_store_status")
            before = _status(call(node, status, GetStoreStatus.Request()))
            Path(args.ready).write_text("ready\n")
            deadline = time.monotonic() + LISTEN_TIMEOUT_S
            while len(received) < LISTEN_COUNT and time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=0.1)
            # The node matches a new publisher on its own schedule, which can be a
            # second or two after this listener did. Wait until it has counted
            # arrays of its own, or until the deadline, which then fails the phase.
            while True:
                after = _status(call(node, status, GetStoreStatus.Request()))
                if after["received"] - before["received"] >= NODE_COUNT or time.monotonic() >= deadline:
                    break
                spin_for(node, 0.2)
            arrays = []
            for msg, now in received:
                try:
                    check_observation(msg, models=models, map_frame=MAP_FRAME, now=now)
                    refusal = None
                except Rejected as e:
                    refusal = [e.code, str(e)]
                arrays.append(_summary(msg, refusal))
            result["checks"] = phase_checks(args.phase, arrays, before, after, expected)
            result.update(before=before, after=after, models=models, arrays=len(arrays),
                          first=_strip(arrays[0]) if arrays else None)  # fmt: skip
        finally:
            node.destroy_node()
        return 0

    try:
        code = _run_ros(body)
    except PeerError as e:
        result["checks"].append(check(f"{args.phase}.exchange", False, str(e)))
        code = 1
    write(args.out, {**result, "evidence": evidence("database")})
    return code


def _strip(summary: dict) -> dict:
    """A summary without its vectors, for the report."""
    out = dict(summary)
    out["detections"] = [
        {**{k: v for k, v in d.items() if k != "embedding"}, "embedding_len": len(d["embedding"])} for d in summary["detections"]
    ]
    return out


@role("vision-client")
def vision_client_role(args: argparse.Namespace) -> int:
    """Call the database node from the vision side: LocalizeObjects, the seeded reads, events and the stubs."""
    import numpy as np
    import rclpy
    from geometry_msgs.msg import Quaternion, Transform, TransformStamped, Vector3
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CameraInfo
    from std_msgs.msg import Header
    from tf2_msgs.msg import TFMessage
    from walkie_database_msgs.msg import ModelVector, ObjectQuery, OperationStatus
    from walkie_database_msgs.srv import (
        ArenaReset, DescribeObject, FindEvents, GetObject, GetRoomContents, GetStoreStatus, ListRooms,
        LocalizeObjects, RememberEvent, ReportSighting, SearchObjects, SetObjectState, SubmitDetections,
    )  # fmt: skip
    from walkie_vision import convert
    from walkie_vision.providers import Detection

    checks: list[dict] = []
    stream_id = convert.new_stream_id()
    unit = list(np.full(VECTOR_DIM, 1.0 / math.sqrt(VECTOR_DIM)))
    OK, NOT_FOUND, TF_UNAVAILABLE = OperationStatus.OK, OperationStatus.NOT_FOUND, OperationStatus.TF_UNAVAILABLE

    def array(stamp: float, clouds: list, *, frame: str = CAMERA_FRAME, vector: bool = False):
        header = Header(stamp=to_time(stamp), frame_id=frame)
        info = CameraInfo(header=header, width=IMAGE_WIDTH, height=IMAGE_HEIGHT, k=CAMERA_K)
        detections = []
        for i, points in enumerate(clouds):
            cloud = convert.to_point_cloud(header, np.array(points)) if points else convert.empty_cloud(header)
            detections.append(convert.to_object_detection(
                Detection(box=np.array([2.0 + 20 * i, 4.0, 16.0 + 20 * i, 20.0]), label="cup", score=0.9, track_id=i + 1),
                points=cloud, embedding=np.array(unit) if vector else None,
                embedding_model=VECTOR_MODEL if vector else ""))  # fmt: skip
        return convert.to_object_detection_array(header, info, ["cup"], detections, stream_id=stream_id)

    def body() -> int:
        node = Node("pair_check_vision_client")
        try:
            run(node)
        finally:
            node.destroy_node()
        return 0

    def run(node) -> None:
        name = lambda service: f"{args.node}/{service}"  # noqa: E731
        clients = {
            service: node.create_client(kind, name(service))
            for service, kind in (
                ("localize_objects", LocalizeObjects), ("get_store_status", GetStoreStatus), ("list_rooms", ListRooms),
                ("get_room_contents", GetRoomContents), ("search_objects", SearchObjects), ("get_object", GetObject),
                ("remember_event", RememberEvent), ("find_events", FindEvents), ("set_object_state", SetObjectState),
                ("report_sighting", ReportSighting), ("arena_reset", ArenaReset),
                ("submit_detections", SubmitDetections), ("describe_object", DescribeObject),
            )
        }  # fmt: skip
        tf = node.create_publisher(TFMessage, "/tf", QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=100, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE))  # fmt: skip
        deadline = time.monotonic() + SERVICE_TIMEOUT_S
        while node.count_subscribers("/tf") < 1 and time.monotonic() < deadline:
            spin_for(node, 0.05)
        if node.count_subscribers("/tf") < 1:
            raise PeerError("nothing subscribes to /tf, so the database node's TF listener is not there")

        def transform(stamp: float, pose) -> TransformStamped:
            (x, y, z), (qx, qy, qz, qw) = pose
            return TransformStamped(
                header=Header(stamp=to_time(stamp), frame_id=MAP_FRAME), child_frame_id=CAMERA_FRAME,
                transform=Transform(translation=Vector3(x=x, y=y, z=z), rotation=Quaternion(x=qx, y=qy, z=qz, w=qw)))  # fmt: skip

        def moving_camera() -> tuple[float, float]:
            """Publish the camera at POSE_1 and then POSE_2, 0.1 s apart and just in the past."""
            now = wall_now(node)
            t1, t2 = now - 0.3, now - 0.2
            tf.publish(TFMessage(transforms=[transform(t1, POSE_1), transform(t2, POSE_2)]))
            spin_for(node, 0.15)
            return t1, t2

        def localize(objects, map_id: str = ""):
            return call(node, clients["localize_objects"], LocalizeObjects.Request(map_id=map_id, objects=objects))

        a, b = block_points(BLOCK_A), block_points(BLOCK_B)

        def results(response) -> list:
            return [{"index": r.detection_index, "code": r.status.code, "points": r.points_used,
                     "frame": r.centroid.header.frame_id, "stamp": seconds_of(r.centroid.header.stamp),
                     "centroid": [r.centroid.point.x, r.centroid.point.y, r.centroid.point.z]}
                    for r in response.results]  # fmt: skip

        def stamped() -> None:
            # The camera moves after t1, so the latest transform would put these elsewhere.
            for attempt in range(1, 6):
                t1, _ = moving_camera()
                response = localize(array(t1, [a, [], b]))
                found = results(response)
                if not (response.status.code == OK and all(r["code"] == TF_UNAVAILABLE for r in found)):
                    break
            want = [expected_centroid(POSE_1, a), None, expected_centroid(POSE_1, b)]
            ok = (
                response.status.code == OK
                and [r["index"] for r in found] == [0, 1, 2]
                and [r["code"] for r in found] == [OK, NOT_FOUND, OK]
                and [r["points"] for r in found] == [40, 0, 40]
                and all(near(r["centroid"], w) and r["frame"] == MAP_FRAME and abs(r["stamp"] - t1) < 1e-6
                        for r, w in zip(found, want, strict=True) if w is not None)
            )  # fmt: skip
            checks.append(check("localize.stamped-not-latest", ok,
                                "an older stamp uses the transform at that stamp, in order, with a failed entry kept",
                                attempts=attempt, results=found, expected=want, status=response.status.message))  # fmt: skip

        def moving() -> None:
            _, t2 = moving_camera()
            response = localize(array(t2, [a, b]))
            found = results(response)
            want = [expected_centroid(POSE_2, a), expected_centroid(POSE_2, b)]
            ok = response.status.code == OK and [r["code"] for r in found] == [OK, OK] and all(
                near(r["centroid"], w) for r, w in zip(found, want, strict=True))
            checks.append(check("localize.moving-camera", ok, "the newer stamp uses the turned and moved camera",
                                results=found, expected=want))  # fmt: skip

        def past_newest() -> None:
            _, t2 = moving_camera()
            response = localize(array(t2 + 0.1, [a, b]))
            found = results(response)
            ok = response.status.code == OK and [r["code"] for r in found] == [TF_UNAVAILABLE] * 2
            checks.append(check("localize.past-newest", ok, "a stamp after the newest transform is not answered with it",
                                results=found))  # fmt: skip

        def unknown_frame() -> None:
            t1, _ = moving_camera()
            response = localize(array(t1, [a, []], frame=UNKNOWN_FRAME))
            found = results(response)
            ok = response.status.code == OK and [(r["index"], r["code"]) for r in found] == [(0, TF_UNAVAILABLE), (1, TF_UNAVAILABLE)]
            checks.append(check("localize.unavailable-transform", ok, "a frame with no transform fails every entry, in order",
                                results=found))  # fmt: skip

        def no_depth() -> None:
            t1, _ = moving_camera()
            response = localize(array(t1, [[], []]))
            found = results(response)
            ok = response.status.code == OK and [r["code"] for r in found] == [NOT_FOUND, NOT_FOUND]
            checks.append(check("localize.no-depth", ok, "detections without points are NOT_FOUND", results=found))

        def empty() -> None:
            t1, _ = moving_camera()
            response = localize(array(t1, []))
            checks.append(check("localize.empty-array", response.status.code == OK and len(response.results) == 0,
                                "an empty array succeeds with no results"))  # fmt: skip

        def stale() -> None:
            response = localize(array(wall_now(node) - 1.5, [a]))
            checks.append(check("localize.stale", response.status.code == OperationStatus.STALE and not response.results,
                                "an array 1.5 s old is STALE", status=response.status.message))  # fmt: skip

        def wrong_map() -> None:
            response = localize(array(wall_now(node) - 0.05, [a]), map_id="elsewhere-2026-01-01-00000000")
            checks.append(check("localize.wrong-map", response.status.code == OperationStatus.WRONG_MAP,
                                "another map is WRONG_MAP", status=response.status.message))  # fmt: skip

        def fake_vector() -> None:
            response = localize(array(wall_now(node) - 0.05, [a], vector=True))
            ok = response.status.code == OperationStatus.INVALID and "no vector table" in response.status.message
            checks.append(check("localize.fake-vector", ok, "a fake model's vector is refused", status=response.status.message))

        for case in (stamped, moving, past_newest, unknown_frame, no_depth, empty, stale, wrong_map, fake_vector):
            guarded(f"localize.{case.__name__}", checks, case)

        reads = {}

        def status() -> None:
            r = _status(call(node, clients["get_store_status"], GetStoreStatus.Request()))
            ok = (r["code"] == OK and r["map_id"] == args.map_id and r["map_valid"] and r["clock_kind"] == "wall"
                  and not r["ingest_enabled"] and not r["captions_enabled"] and r["models"]
                  and not any(m.startswith("fake_") for m in r["models"]))  # fmt: skip
            checks.append(check("db.store-status", ok, "memory mode on the seeded map, with no ingest, captions or fake model", **r))

        def rooms() -> None:
            r = call(node, clients["list_rooms"], ListRooms.Request())
            names = [room.name for room in r.rooms]
            checks.append(check("db.list-rooms", r.status.code == OK and names == ROOM_NAMES, f"rooms {names}"))

        def contents() -> None:
            r = call(node, clients["get_room_contents"], GetRoomContents.Request(query=ObjectQuery(room="living room")))
            labels = sorted(m.object.label for m in r.objects)
            reads["anchors"] = {m.object.key: m.object.id for m in r.objects}
            ok = r.status.code == OK and r.room.name == "living room" and labels == LIVING_ROOM_CONTENTS
            checks.append(check("db.room-contents", ok, f"living room holds {labels}"))

        def search() -> None:
            r = call(node, clients["search_objects"], SearchObjects.Request(query=ObjectQuery(thing="table")))
            keys = [m.object.key for m in r.matches]
            ok = r.status.code == OK and keys == ["table"] and r.lexical_only and not r.priors
            checks.append(check("db.search-objects", ok, f"a search for table finds {keys}", lexical_only=r.lexical_only))
            r = call(node, clients["search_objects"], SearchObjects.Request(query=ObjectQuery(thing="mug")))
            priors = [(p.official_name, p.place, p.room) for p in r.priors]
            ok = r.status.code == OK and not r.matches and priors == [PRIOR]
            checks.append(check("db.search-prior", ok, f"an unseen alias gives the catalogue prior {priors}"))
            r = call(node, clients["search_objects"], SearchObjects.Request(
                query=ObjectQuery(thing="table"), use_embedding=True, vector=ModelVector(model=VECTOR_MODEL, embedding=unit)))  # fmt: skip
            checks.append(check("db.search-fake-vector", r.status.code == OperationStatus.UNSUPPORTED,
                                "a fake model's query vector is UNSUPPORTED", status=r.status.message))  # fmt: skip

        def get_object() -> None:
            table = reads.get("anchors", {}).get("table")
            if table is None:
                raise PeerError("the room contents gave no table to read")
            r = call(node, clients["get_object"], GetObject.Request(object_id=table))
            surfaces = [(s.key, round(s.z, 6)) for s in r.surfaces]
            ok = (r.status.code == OK and r.requested_id == table and not r.merged and r.object.key == "table"
                  and surfaces == [SURFACE] and r.cloud.width == 0)  # fmt: skip
            checks.append(check("db.get-object", ok, f"the table has surfaces {surfaces}"))

        def events() -> None:
            request = RememberEvent.Request(request_id="pair-check-event-1", task_id="pair-check", cause="pair gate",
                                            kind="note", text=EVENT_TEXT)  # fmt: skip
            first = call(node, clients["remember_event"], request)
            again = call(node, clients["remember_event"], request)
            request.text = "Another line under the same request."
            other = call(node, clients["remember_event"], request)
            ok = (first.status.code == OK and first.event.id > 0 and first.event.text == EVENT_TEXT
                  and again.status.code == OK and again.event.id == first.event.id
                  and other.status.code == OperationStatus.CONFLICT)  # fmt: skip
            checks.append(check("db.remember-event", ok, "an event is written once, a retry answers it, a changed retry conflicts",
                                codes=[first.status.code, again.status.code, other.status.code]))  # fmt: skip
            found = call(node, clients["find_events"], FindEvents.Request(text="table"))
            texts = [e.text for e in found.events]
            checks.append(check("db.find-events", found.status.code == OK and texts == [EVENT_TEXT], f"found {texts}"))

        def stubs() -> None:
            ids = {"request_id": "pair-check-write", "task_id": "pair-check", "cause": "pair gate"}
            answers = {
                "set_object_state": call(node, clients["set_object_state"], SetObjectState.Request(**ids, object_id=1, operation="observed")),
                "report_sighting": call(node, clients["report_sighting"], ReportSighting.Request(**ids, object_id=1)),
                "arena_reset": call(node, clients["arena_reset"], ArenaReset.Request(**ids)),
                "submit_detections": call(node, clients["submit_detections"], SubmitDetections.Request(**ids)),
                "describe_object": call(node, clients["describe_object"], DescribeObject.Request(**ids, object_id=1)),
            }  # fmt: skip
            for service, answer in answers.items():
                admitted = getattr(answer, "queued", False) or getattr(answer, "applied", False) or getattr(answer, "objects_changed", 0)
                checks.append(check(f"db.not-ready.{service}", answer.status.code == OperationStatus.NOT_READY and not admitted,
                                    "NOT_READY, and nothing queued or applied", message=answer.status.message))  # fmt: skip

        for case in (status, rooms, contents, search, get_object, events, stubs):
            guarded(f"db.{case.__name__}", checks, case)

    code = 0
    try:
        code = _run_ros(body)
    except PeerError as e:
        checks.append(check("localize.exchange", False, str(e)))
        code = 1
    write(args.out, {"checks": checks, "evidence": evidence("vision", database_types=True)})
    return code


@role("database-client")
def database_client_role(args: argparse.Namespace) -> int:
    """Call the vision node's services from the database side, with images it publishes."""
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import CameraInfo, Image
    from std_msgs.msg import Header
    from vision_msgs.msg import BoundingBox2D
    from walkie_vision_msgs.msg import ObjectDetectionArray
    from walkie_vision_msgs.srv import DescribeCrop, DetectObjects, EmbedCrop, EmbedText

    expected = json.loads(Path(args.vectors).read_text())
    checks: list[dict] = []
    pixels = pattern_image()

    def body() -> int:
        node = Node("pair_check_database_client")
        try:
            run(node)
        finally:
            node.destroy_node()
        return 0

    def run(node) -> None:
        images = node.create_publisher(Image, IMAGE_TOPIC, 10)
        infos = node.create_publisher(CameraInfo, CAMERA_INFO_TOPIC, 10)
        stream: list = []
        node.create_subscription(ObjectDetectionArray, VISION_OBJECTS, stream.append, 10)
        clients = {name: node.create_client(kind, VISION_SERVICES[name]) for name, kind in (
            ("embed_text", EmbedText), ("embed_crop", EmbedCrop), ("describe_crop", DescribeCrop),
            ("detect_objects", DetectObjects))}  # fmt: skip

        def send(header: Header) -> None:
            infos.publish(CameraInfo(header=header, width=IMAGE_WIDTH, height=IMAGE_HEIGHT, k=CAMERA_K))
            images.publish(Image(header=header, height=IMAGE_HEIGHT, width=IMAGE_WIDTH, encoding="bgr8",
                                 step=IMAGE_WIDTH * 3, data=pixels))  # fmt: skip

        def fresh() -> Header:
            return Header(stamp=node.get_clock().now().to_msg(), frame_id=CAMERA_FRAME)

        timer = node.create_timer(0.05, lambda: send(fresh()))

        def wait_stream(count: int) -> list:
            start = len(stream)
            deadline = time.monotonic() + SERVICE_TIMEOUT_S
            while len(stream) - start < count and time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=0.05)
            return stream[start:]

        def stream_check(name: str, arrays: list, ids: set) -> None:
            classes = [list(a.classes) for a in arrays]
            ok = len(arrays) >= 3 and all(c == STREAM_CLASSES for c in classes) and len({a.stream_id for a in arrays} | ids) == 1
            checks.append(check(name, ok, f"{len(arrays)} stream arrays with classes {classes[:1]}",
                                stream_ids=sorted({a.stream_id for a in arrays})))  # fmt: skip

        before = wait_stream(3)
        stream_check("vision.stream-before", before, set())
        stream_ids = {a.stream_id for a in before}

        def embed_text() -> None:
            r = call(node, clients["embed_text"], EmbedText.Request(texts=TEXTS))
            want = expected["text"]
            ok = r.success and r.embedding_model == want["model"] and r.dim == want["dim"] and same_float32(r.vectors, want["vectors"])
            checks.append(check("vision.embed-text", ok, "the text vectors arrived bit for bit", model=r.embedding_model,
                                digest=vector_digest(list(r.vectors))))  # fmt: skip
            r = call(node, clients["embed_text"], EmbedText.Request(texts=[]))
            checks.append(check("vision.embed-text-invalid", not r.success and r.message.startswith("INVALID"),
                                "no texts is INVALID", message=r.message))  # fmt: skip

        def detect() -> None:
            r = call(node, clients["detect_objects"], DetectObjects.Request(classes=ON_DEMAND_CLASSES))
            o = r.objects
            ok = (r.success and not r.error_code and len(o.detections) == 0 and list(o.classes) == ON_DEMAND_CLASSES
                  and o.header.frame_id == CAMERA_FRAME and o.stream_id in stream_ids)  # fmt: skip
            checks.append(check("vision.detect-empty", ok, "an on-demand detection that finds nothing succeeds",
                                error=r.error_code, message=r.message, classes=list(o.classes)))  # fmt: skip
            r = call(node, clients["detect_objects"], DetectObjects.Request(classes=[]))
            checks.append(check("vision.detect-invalid", not r.success and r.error_code == "INVALID",
                                "no classes is INVALID", error=r.error_code))  # fmt: skip
            r = call(node, clients["describe_crop"], DescribeCrop.Request())
            checks.append(check("vision.describe-disabled", not r.success and r.error_code == "DISABLED",
                                "captions are off in this build", error=r.error_code))  # fmt: skip

        guarded("vision.embed-text", checks, embed_text)
        guarded("vision.detect", checks, detect)
        stream_check("vision.stream-after", wait_stream(3), stream_ids)

        def embed_crop() -> None:
            timer.cancel()
            spin_for(node, 0.2)
            header = fresh()
            for _ in range(3):
                send(header)
                spin_for(node, 0.03)
            x1, y1, x2, y2 = CROP_BOX
            box = BoundingBox2D(size_x=x2 - x1, size_y=y2 - y1)
            box.center.position.x, box.center.position.y = (x1 + x2) / 2, (y1 + y2) / 2
            request = EmbedCrop.Request(header=header, image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT, bbox=box)
            r = call(node, clients["embed_crop"], request)
            want = expected["crop"]
            ok = (r.success and r.header == header and r.embedding_model == want["model"]
                  and same_float32(r.embedding, want["embedding"]) and len(r.crop.data) > 0)  # fmt: skip
            checks.append(check("vision.embed-crop", ok, "the crop's vector arrived bit for bit, with its image's header",
                                error=r.error_code, message=r.message, digest=vector_digest(list(r.embedding))))  # fmt: skip
            old = Header(stamp=to_time(seconds_of(header.stamp) - 0.1), frame_id=CAMERA_FRAME)
            request.header = old
            r = call(node, clients["embed_crop"], request)
            checks.append(check("vision.embed-crop-stale", not r.success and r.error_code == "STALE_IMAGE",
                                "a box for an older image is STALE_IMAGE", error=r.error_code))  # fmt: skip

        guarded("vision.embed-crop", checks, embed_crop)

    code = 0
    try:
        code = _run_ros(body)
    except PeerError as e:
        checks.append(check("vision.exchange", False, str(e)))
        code = 1
    write(args.out, {"checks": checks, "evidence": evidence("database")})
    return code


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    roles = parser.add_subparsers(dest="role", required=True)
    for name in ROLES:
        sub = roles.add_parser(name)
        sub.add_argument("--out", required=True)
        if name == "pin":
            sub.add_argument("--database-root", required=True)
        if name == "probe":
            sub.add_argument("--side", choices=("vision", "database"), required=True)
            sub.add_argument("--no-typesupport", action="store_true")
        if name in ("store-rows", "database-listener"):
            sub.add_argument("--db", required=True)
        if name in ("expect-vectors", "vision-malformed"):
            sub.add_argument("--scene", required=True)
        if name in ("vision-malformed", "database-listener"):
            sub.add_argument("--topic", required=True)
        if name in ("database-listener", "vision-client"):
            sub.add_argument("--node", required=True)
        if name in ("database-listener", "database-client"):
            sub.add_argument("--vectors", required=True)
        if name == "database-listener":
            sub.add_argument("--phase", choices=("nonempty", "empty", "vector", "malformed"), required=True)
            sub.add_argument("--ready", required=True)
        if name == "vision-client":
            sub.add_argument("--map-id", required=True)
    # Everything from --ros-args on goes to the wrapped node unchanged.
    argv = list(sys.argv[1:] if argv is None else argv)
    ros_args = argv[argv.index("--ros-args") :] if "--ros-args" in argv else []
    args = parser.parse_args(argv[: len(argv) - len(ros_args)])
    if ros_args and args.role not in ("database-node", "vision-publisher"):
        parser.error(f"the {args.role} role takes no ROS arguments")
    args.ros_args = ros_args
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return ROLES[args.role](args)


if __name__ == "__main__":
    sys.exit(main())
