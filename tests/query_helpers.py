"""A small store for the query tests, built with the store's own helpers.

Rooms, anchors, surfaces, catalogue items, objects, labels, relations and vectors are
all synthetic. A vector is a made-up direction, never a real model's output.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence

import numpy as np

from walkie_database.db import transaction
from walkie_database.geometry import FramePose
from walkie_database.object_context import (
    add_catalogue_item,
    add_surface,
    link_catalogue,
    set_relation,
    set_room_shape,
)
from walkie_database.objects import NewObject, ObjectBox, insert_object, reserve_object_ids, write_object_vector
from walkie_database.queries import ObjectRepository

MAP_ID = "testlab-2026-10-16-0123abcd"
OTHER_MAP = "otherlab-2026-10-16-4567cdef"
FRAME = "map"
MODEL = "clip_vit_b16"
DIMENSIONS = 512
NOW = 1_000_000.0
LIVING = [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]
KITCHEN = [[5.0, 0.0], [9.0, 0.0], [9.0, 4.0], [5.0, 4.0]]


def direction(*values: float) -> list[float]:
    """A vector of DIMENSIONS values that starts with *values* and is 0 after them."""
    return [*values, *([0.0] * (DIMENSIONS - len(values)))]


def with_cosine(cosine: float) -> list[float]:
    """A unit vector whose cosine with direction(1) is *cosine*."""
    return direction(cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine)))


def yaw_rotation(yaw: float) -> tuple[float, float, float, float]:
    return (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))


class Store:
    """One map, and whatever a test adds to it. Ids come from a real reservation."""

    def __init__(self, conn: sqlite3.Connection, map_id: str = MAP_ID, *, ids: int = 2000) -> None:
        self.conn = conn
        self.map_id = map_id
        with transaction(conn):
            conn.execute(
                """INSERT INTO map (id, frame_id, created_at, map_hash, baseline_rev, clock_kind)
                   VALUES (?, ?, 0, ?, '', 'wall')""",
                (map_id, FRAME, "ab" * 32),
            )
        self._reservation = reserve_object_ids(conn, ids)
        self._ids = iter(self._reservation)

    @classmethod
    def existing(cls, conn: sqlite3.Connection, map_id: str, *, ids: int = 50) -> Store:
        """A store whose map is already there, such as a restored seed."""
        store = cls.__new__(cls)
        store.conn, store.map_id = conn, map_id
        store._reservation = reserve_object_ids(conn, ids)
        store._ids = iter(store._reservation)
        return store

    def other_map(self, map_id: str = OTHER_MAP) -> Store:
        """A second map in the same file. It shares this store's ids."""
        other = Store.__new__(Store)
        other.conn, other.map_id = self.conn, map_id
        other._reservation, other._ids = self._reservation, self._ids
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO map (id, frame_id, created_at, clock_kind) VALUES (?, ?, 0, 'wall')",
                (map_id, FRAME),
            )
        return other

    def room(self, name: str, outline=LIVING, *, stand=(1.0, 1.0, 0.0), key: str | None = None) -> int:
        with transaction(self.conn):
            x, y, yaw = stand if stand is not None else (None, None, None)
            room_id = self.conn.execute(
                """INSERT INTO room (map_id, key, name, description, floor, x, y, yaw)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (self.map_id, key or name.replace(" ", "_"), name, f"The {name}.", x, y, yaw),
            ).lastrowid
            set_room_shape(self.conn, room_id, outline)
        return room_id

    def add(
        self,
        label: str,
        *,
        center: Sequence[float] | None = (1.0, 1.0, 0.5),
        size: Sequence[float] = (0.1, 0.1, 0.1),
        rotation: Sequence[float] = (0.0, 0.0, 0.0, 1.0),
        layer: str = "movable",
        state: str = "present",
        room: int | None = None,
        key: str | None = None,
        p_exist: float = 0.9,
        last_verified: float | None = NOW,
        caption: str | None = None,
        printed_text: str | None = None,
        official_name: str | None = None,
        category: str | None = None,
        attributes: Mapping[str, object] | None = None,
        catalogue: int | None = None,
        enclosed: bool = False,
        labels: Sequence[str] = (),
        vector: Sequence[float] | None = None,
    ) -> int:
        """One object. A *center* of None gives it no measured box."""
        object_id = next(self._ids)
        box = None if center is None else ObjectBox(tuple(center), tuple(size), tuple(rotation), stamp=NOW - 5)
        with transaction(self.conn):
            insert_object(
                self.conn,
                NewObject(
                    self.map_id, label, NOW - 100, NOW - 10, room_id=room, caption=caption, key=key,
                    layer=layer, state="present", p_exist=p_exist, box=box,
                ),
                object_id,
                reservation=self._reservation,
            )
            self.conn.execute(
                """UPDATE object SET last_verified = ?, printed_text = ?, official_name = ?, category = ?,
                       attributes = ?, enclosed = ?, state = ? WHERE id = ?""",
                (last_verified, printed_text, official_name, category,
                 json.dumps(dict(attributes or {}), sort_keys=True), int(enclosed), state, object_id),
            )
            if catalogue is not None:
                link_catalogue(self.conn, object_id, catalogue)
            for extra in labels:
                self.conn.execute(
                    "INSERT INTO object_label (object_id, label, count) VALUES (?, ?, 1)", (object_id, extra)
                )
            if vector is not None:
                write_object_vector(self.conn, object_id, MODEL, vector)
        return object_id

    def surface(self, anchor_id: int, key: str, z: float = 0.8, outline=None, *, valid: bool = True) -> int:
        outline = outline if outline is not None else [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        with transaction(self.conn):
            return add_surface(self.conn, anchor_id, key, z, outline if valid else [], geometry_valid=valid)

    def on(self, src_id: int, dst_id: int, surface_id: int, weight: float = 0.9, now: float = NOW) -> None:
        with transaction(self.conn):
            set_relation(self.conn, src_id, dst_id, "on", weight, now, surface_id=surface_id)

    def inside(self, src_id: int, dst_id: int, weight: float = 0.9, now: float = NOW) -> None:
        with transaction(self.conn):
            set_relation(self.conn, src_id, dst_id, "inside", weight, now)

    def item(self, official_name: str, **fields: object) -> int:
        with transaction(self.conn):
            return add_catalogue_item(self.conn, self.map_id, official_name, **fields)

    def update(self, object_id: int, **columns: object) -> None:
        """Set columns directly, as a later core would. Names come from the test, never a request."""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with transaction(self.conn):
            self.conn.execute(f"UPDATE object SET {assignments} WHERE id = ?", (*columns.values(), object_id))

    def repository(
        self,
        *,
        locate=None,
        frames: Sequence[str] = ("base_footprint",),
        robot_frame: str = "base_footprint",
        thing_cosine: Mapping[str, float] | None = None,
    ) -> ObjectRepository:
        return ObjectRepository(
            self.conn,
            map_id=self.map_id,
            frame_id=FRAME,
            locate=locate,
            reference_frames=frames,
            robot_frame=robot_frame,
            thing_cosine=thing_cosine,
        )


def at(x: float, y: float, z: float = 0.0, yaw: float = 0.0):
    """A locate function that puts every frame at one pose, at any time."""
    pose = FramePose((x, y, z), yaw_rotation(yaw))

    def locate(frame: str, now: float) -> FramePose:
        return pose

    return locate


def nowhere(frame: str, now: float) -> None:
    return None


def random_unit(rng: np.random.Generator) -> list[float]:
    values = rng.standard_normal(DIMENSIONS)
    return list(values / np.linalg.norm(values))
