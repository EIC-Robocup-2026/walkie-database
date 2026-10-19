"""Write a sample house, its people and a conversation to ``data/demo.db``.

The data is made up, so it never goes into the robot's memory: :func:`main`
writes only ``data/demo.db``, and recreates it on every run.

The vectors are random, but objects with the same label, and the faces of the
same person, get vectors close to each other. Vector searches therefore give
sensible answers: the nearest object to a cup is the other cup.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np

from walkie_database.db import connect, transaction
from walkie_database.migrate import migrate

DEMO_PATH = Path("data/demo.db")
MAP_ID = "demo-house"
DIMENSIONS = 512
DAY = 24 * 3600.0
MAX_CLOUD_POINTS = 2000


class Room(NamedTuple):
    name: str
    description: str
    outline: list[list[float]]  # closed: the first point is repeated at the end
    stand: tuple[float, float, float]  # x, y, yaw where the robot stands


class DemoObject(NamedTuple):
    label: str
    room: str
    caption: str
    source: str  # 'perception': merged from detections; 'map': drawn on the map
    center: tuple[float, float, float]
    size: tuple[float, float, float]
    yaw: float
    color: tuple[int, int, int]
    detections: int


class DemoPerson(NamedTuple):
    name: str | None  # None: seen, but has not said their name yet
    guest: bool  # guests belong to the session; others are kept across sessions
    attributes: dict
    clothes: str
    faces: int


ROOMS = [
    Room("kitchen", "Fridge, counter and a dining table",
         [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]], (2.0, 2.0, 0.0)),
    Room("living room", "Sofa, coffee table and TV",
         [[4, 0], [9, 0], [9, 4], [4, 4], [4, 0]], (6.5, 1.5, math.pi / 2)),
    Room("bedroom", "A double bed",
         [[0, 4], [4, 4], [4, 8], [0, 8], [0, 4]], (2.0, 5.0, math.pi / 2)),
]

OBJECTS = [
    DemoObject("dining table", "kitchen", "a wooden dining table", "map",
               (1.5, 1.5, 0.375), (1.2, 0.8, 0.75), 0.0, (140, 100, 60), 0),
    DemoObject("fridge", "kitchen", "a tall white fridge", "map",
               (3.6, 3.6, 0.9), (0.7, 0.7, 1.8), 0.0, (235, 235, 235), 0),
    DemoObject("cup", "kitchen", "a white ceramic cup on the table", "perception",
               (1.3, 1.4, 0.8), (0.08, 0.08, 0.1), 0.0, (245, 245, 240), 12),
    DemoObject("cup", "kitchen", "a red mug with a handle on the counter", "perception",
               (3.6, 0.4, 0.95), (0.11, 0.08, 0.1), 0.6, (200, 30, 30), 5),
    DemoObject("plate", "kitchen", "an empty white plate", "perception",
               (1.7, 1.6, 0.76), (0.25, 0.25, 0.02), 0.0, (250, 250, 250), 8),
    DemoObject("sofa", "living room", "a grey three-seat sofa", "map",
               (6.5, 3.4, 0.4), (2.0, 0.9, 0.8), 0.0, (120, 120, 125), 0),
    DemoObject("coffee table", "living room", "a low black coffee table", "map",
               (6.5, 2.0, 0.2), (1.0, 0.5, 0.4), 0.0, (30, 30, 30), 0),
    DemoObject("tv", "living room", "a flat TV on the wall", "perception",
               (6.5, 0.1, 1.2), (1.2, 0.08, 0.7), 0.0, (15, 15, 20), 20),
    DemoObject("coke can", "living room", "a red can of coke", "perception",
               (6.3, 2.0, 0.46), (0.066, 0.066, 0.12), 0.0, (190, 20, 25), 7),
    DemoObject("remote", "living room", "a black TV remote", "perception",
               (6.7, 2.1, 0.41), (0.05, 0.17, 0.02), 0.3, (25, 25, 25), 3),
    DemoObject("bed", "bedroom", "a double bed with a blue cover", "map",
               (2.0, 6.8, 0.25), (1.6, 2.0, 0.5), 0.0, (60, 90, 160), 0),
    DemoObject("book", "bedroom", "a paperback book on the bed", "perception",
               (2.3, 6.4, 0.515), (0.13, 0.2, 0.03), 1.1, (220, 180, 60), 2),
]

PEOPLE = [
    DemoPerson("Robin", False, {"role": "team member"}, "black team t-shirt", 2),
    DemoPerson("Charlie", True, {"drink": "coke", "interest": "football", "glasses": True},
               "red jacket, blue jeans", 3),
    DemoPerson("Jane", True, {"drink": "milk", "interest": "painting"}, "green dress", 3),
    DemoPerson(None, True, {}, "grey hoodie", 1),
]

# role, name of the person speaking (or None), text
CONVERSATION = [
    ("system", None, "Task: welcome each guest, learn their name and favorite drink, "
                     "and introduce them to Robin in the living room."),
    ("robot", None, "Hello, I'm Walkie. What's your name?"),
    ("human", "Charlie", "Hi, I'm Charlie."),
    ("robot", None, "Nice to meet you, Charlie. What's your favorite drink?"),
    ("human", "Charlie", "I'd like a coke, please."),
    ("tool", None, "navigate_to_location(living room): succeeded"),
    ("robot", None, "Robin, this is Charlie. Charlie likes coke and football."),
    ("robot", None, "Hello again. What's your name?"),
    ("human", "Jane", "I'm Jane."),
    ("robot", None, "What would you like to drink, Jane?"),
    ("human", "Jane", "Some milk, if you have any."),
]


def _unit(vector: np.ndarray) -> np.ndarray:
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def _near(rng: np.random.Generator, center: np.ndarray, spread: float = 0.3) -> bytes:
    """A unit vector close to *center*, as vec0 expects it."""
    return _unit(center + spread * rng.standard_normal(DIMENSIONS)).tobytes()


def _box_surface(rng: np.random.Generator, obj: DemoObject) -> tuple[np.ndarray, np.ndarray, float]:
    """Points on the surface of *obj*'s box in the map frame, their colors, and the voxel size."""
    size = np.asarray(obj.size)
    areas = np.array([size[1] * size[2], size[0] * size[2], size[0] * size[1]])
    n = int(min(MAX_CLOUD_POINTS, max(100, 2 * areas.sum() / 0.01**2)))
    half = size / 2
    points = rng.uniform(-half, half, (n, 3))
    # Push each point onto one face, choosing faces by their area.
    axis = rng.choice(3, n, p=areas / areas.sum())
    points[np.arange(n), axis] = rng.choice([-1.0, 1.0], n) * half[axis]
    cos, sin = math.cos(obj.yaw), math.sin(obj.yaw)
    rotated = points @ np.array([[cos, sin, 0], [-sin, cos, 0], [0, 0, 1]])
    shade = rng.integers(-15, 16, (n, 1))
    colors = np.clip(np.asarray(obj.color) + shade, 0, 255).astype(np.uint8)
    voxel_size = round(math.sqrt(2 * areas.sum() / n), 3)
    return (rotated + obj.center).astype("<f4"), colors, voxel_size


def _add_object(conn, rng, obj, room_id, embedding, first_seen, last_seen, session):
    session_id, session_start = session
    sx, sy, sz = obj.size
    object_id = conn.execute(
        """INSERT INTO object (map_id, room_id, label, caption, source,
                               center_x, center_y, center_z, size_x, size_y, size_z,
                               qz, qw, n_detections, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (MAP_ID, room_id, obj.label, obj.caption, obj.source, *obj.center, sx, sy, sz,
         math.sin(obj.yaw / 2), math.cos(obj.yaw / 2), obj.detections, first_seen, last_seen),
    ).lastrowid

    # The box aligned with the map's axes around the turned box.
    cos, sin = abs(math.cos(obj.yaw)), abs(math.sin(obj.yaw))
    hx, hy, hz = (cos * sx + sin * sy) / 2, (sin * sx + cos * sy) / 2, sz / 2
    x, y, z = obj.center
    conn.execute(
        "INSERT INTO object_box VALUES (?, ?, ?, ?, ?, ?, ?)",
        (object_id, x - hx, x + hx, y - hy, y + hy, z - hz, z + hz),
    )
    conn.execute(
        "INSERT INTO object_clip_vit_b16 (object_id, embedding, map_id, room_id) VALUES (?, ?, ?, ?)",
        (object_id, embedding, MAP_ID, room_id),
    )
    if obj.source != "perception":
        return

    points, colors, voxel_size = _box_surface(rng, obj)
    conn.execute(
        "INSERT INTO object_cloud VALUES (?, ?, ?, ?, ?)",
        (object_id, voxel_size, len(points), points.tobytes(), colors.tobytes()),
    )
    for ts in np.linspace(first_seen, last_seen, obj.detections):
        jitter = rng.normal(0, 0.02, 3)
        conn.execute(
            """INSERT INTO object_observation (object_id, session_id, ts, label, score,
                                               n_points, center_x, center_y, center_z)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (object_id, session_id if ts >= session_start else None, float(ts), obj.label,
             float(rng.uniform(0.5, 0.95)), int(rng.integers(200, 3000)),
             *(float(c) for c in np.asarray(obj.center) + jitter)),
        )


def seed(conn: sqlite3.Connection, *, now: float | None = None, rng_seed: int = 0) -> None:
    """Add the demo house, people and conversation to *conn*, a migrated database.

    The map was built a day before *now*. The session, a Receptionist run,
    ended a minute before it.
    """
    now = time.time() if now is None else now
    rng = np.random.default_rng(rng_seed)
    mapped_at = now - DAY
    session_start, session_end = now - 900, now - 60

    with transaction(conn):
        conn.execute(
            "INSERT INTO map (id, created_at, description) VALUES (?, ?, ?)",
            (MAP_ID, mapped_at, "A sample house made by pixi run db-seed"),
        )
        session_id = conn.execute(
            "INSERT INTO session (label, started_at, ended_at) VALUES (?, ?, ?)",
            ("Receptionist demo", session_start, session_end),
        ).lastrowid

        room_ids = {}
        for room in ROOMS:
            x, y, yaw = room.stand
            room_id = conn.execute(
                "INSERT INTO room (map_id, name, description, x, y, yaw) VALUES (?, ?, ?, ?, ?, ?)",
                (MAP_ID, room.name, room.description, x, y, yaw),
            ).lastrowid
            conn.execute(
                "INSERT INTO room_shape (rowid, _shape) VALUES (?, geopoly_ccw(?))",
                (room_id, json.dumps(room.outline)),
            )
            conn.execute(
                "INSERT INTO room_clip_vit_b16 (room_id, embedding, map_id) VALUES (?, ?, ?)",
                (room_id, _near(rng, rng.standard_normal(DIMENSIONS)), MAP_ID),
            )
            room_ids[room.name] = room_id

        label_vectors = {}
        for i, obj in enumerate(OBJECTS):
            center = label_vectors.setdefault(obj.label, rng.standard_normal(DIMENSIONS))
            first_seen = mapped_at + 60 * i
            last_seen = session_start + 30 * i if obj.detections else first_seen
            _add_object(conn, rng, obj, room_ids[obj.room], _near(rng, center),
                        first_seen, last_seen, (session_id, session_start))

        person_ids = {}
        for i, person in enumerate(PEOPLE):
            first_seen = session_start + 120 * i if person.guest else mapped_at
            person_id = conn.execute(
                """INSERT INTO person (session_id, name, attributes, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id if person.guest else None, person.name,
                 json.dumps(person.attributes), first_seen, session_end),
            ).lastrowid
            person_ids[person.name] = person_id

            face_center = rng.standard_normal(DIMENSIONS)
            for _ in range(person.faces):
                face_id = conn.execute(
                    "INSERT INTO face (person_id, ts) VALUES (?, ?)", (person_id, first_seen)
                ).lastrowid
                conn.execute(
                    """INSERT INTO face_arcface_w600k_r50 (face_id, embedding, person_id)
                       VALUES (?, ?, ?)""",
                    (face_id, _near(rng, face_center, spread=0.25), person_id),
                )
            appearance_id = conn.execute(
                "INSERT INTO appearance (person_id, session_id, ts, caption) VALUES (?, ?, ?, ?)",
                (person_id, session_id, max(first_seen, session_start), person.clothes),
            ).lastrowid
            conn.execute(
                """INSERT INTO appearance_osnet_x1_0_msmt17
                       (appearance_id, embedding, person_id, session_id)
                   VALUES (?, ?, ?, ?)""",
                (appearance_id, _near(rng, rng.standard_normal(DIMENSIONS)), person_id, session_id),
            )

        conversation_id = conn.execute(
            "INSERT INTO conversation (session_id, started_at) VALUES (?, ?)",
            (session_id, session_start),
        ).lastrowid
        for i, (role, speaker, text) in enumerate(CONVERSATION):
            conn.execute(
                """INSERT INTO message (conversation_id, ts, role, person_id, text)
                   VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, session_start + 20 * i, role,
                 person_ids[speaker] if speaker else None, text),
            )


def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{DEMO_PATH}{suffix}").unlink(missing_ok=True)
    conn = connect(DEMO_PATH)
    try:
        migrate(conn)
        seed(conn)
    finally:
        conn.close()
    print(
        f"Wrote {DEMO_PATH}: {len(ROOMS)} rooms, {len(OBJECTS)} objects, "
        f"{len(PEOPLE)} people and {len(CONVERSATION)} messages. "
        "Browse it with: pixi run db-browse"
    )


if __name__ == "__main__":
    main()
