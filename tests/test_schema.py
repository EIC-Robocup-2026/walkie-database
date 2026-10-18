"""The schema's promises: what each index finds, and what a delete removes."""

import json
import sqlite3

import numpy as np
import pytest

from walkie_database.db import transaction


def unit(index: int) -> bytes:
    """A 512-dimension unit vector along one axis, as vec0 expects it."""
    vector = np.zeros(512, dtype=np.float32)
    vector[index] = 1.0
    return vector.tobytes()


def count(db, table: str) -> int:
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def add_map(db, map_id="m1"):
    db.execute("INSERT INTO map (id, created_at) VALUES (?, 0)", (map_id,))


def add_room(db, room_id, name, outline, map_id="m1"):
    db.execute("INSERT INTO room (id, map_id, name) VALUES (?, ?, ?)", (room_id, map_id, name))
    db.execute(
        "INSERT INTO room_shape (rowid, _shape) VALUES (?, geopoly_ccw(?))",
        (room_id, json.dumps(outline)),
    )


def add_object(db, object_id, label, center, embedding, room_id=None, map_id="m1", half=0.05):
    x, y, z = center
    db.execute(
        """INSERT INTO object (id, map_id, room_id, label, center_x, center_y, center_z,
                               size_x, size_y, size_z, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
        (object_id, map_id, room_id, label, x, y, z, 2 * half, 2 * half, 2 * half),
    )
    db.execute(
        "INSERT INTO object_box VALUES (?, ?, ?, ?, ?, ?, ?)",
        (object_id, x - half, x + half, y - half, y + half, z - half, z + half),
    )
    db.execute(
        "INSERT INTO object_clip_vit_b16 VALUES (?, ?, ?, ?)",
        (object_id, embedding, map_id, room_id or 0),
    )


KITCHEN = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
LIVING_ROOM = [[4, 0], [8, 0], [8, 4], [4, 4], [4, 0]]


@pytest.fixture
def house(db):
    with transaction(db):
        add_map(db)
        add_room(db, 1, "kitchen", KITCHEN)
        add_room(db, 2, "living room", LIVING_ROOM)
        add_object(db, 10, "cup", (1, 1, 0.8), unit(0), room_id=1)
        add_object(db, 11, "cup", (5, 1, 0.5), unit(0), room_id=2)
        add_object(db, 12, "sofa", (6, 3, 0.4), unit(1), room_id=2, half=0.8)
    return db


def test_vector_search_filters_by_room_inside_the_search(house):
    rows = house.execute(
        """SELECT object_id FROM object_clip_vit_b16
           WHERE embedding MATCH ? AND k = 5 AND room_id = 1""",
        (unit(0),),
    ).fetchall()
    assert [row["object_id"] for row in rows] == [10]


def test_deleting_a_room_removes_it_from_object_search(house):
    with transaction(house):
        house.execute("DELETE FROM room WHERE id = 1")
    assert house.execute("SELECT room_id FROM object WHERE id = 10").fetchone()[0] is None
    assert (
        house.execute("SELECT room_id FROM object_clip_vit_b16 WHERE object_id = 10").fetchone()[0]
        == 0
    )
    assert count(house, "room_shape") == 1


def test_deleting_a_map_removes_its_objects_and_their_indexes(house):
    with transaction(house):
        house.execute("DELETE FROM map WHERE id = 'm1'")
    for table in ("object", "object_box", "object_clip_vit_b16", "room", "room_shape"):
        assert count(house, table) == 0, table


def test_point_finds_its_room(house):
    def rooms_at(x, y):
        rows = house.execute(
            "SELECT rowid FROM room_shape WHERE geopoly_contains_point(_shape, ?, ?)", (x, y)
        )
        return sorted(row[0] for row in rows)

    assert rooms_at(1, 1) == [1]
    assert rooms_at(5, 1) == [2]
    assert rooms_at(9, 9) == []
    # A point on a shared wall is in both rooms. Callers must pick one.
    assert rooms_at(4, 2) == [1, 2]


def test_box_search_finds_objects_near_a_point(house):
    x, y, z, radius = 5.0, 1.0, 0.5, 0.5
    rows = house.execute(
        """SELECT id FROM object_box
           WHERE max_x >= ? AND min_x <= ? AND max_y >= ? AND min_y <= ?
             AND max_z >= ? AND min_z <= ?""",
        (x - radius, x + radius, y - radius, y + radius, z - radius, z + radius),
    )
    assert [row[0] for row in rows] == [11]


def test_point_cloud_blob_must_match_its_point_count(house):
    points = np.zeros((5, 3), dtype="<f4").tobytes()
    with transaction(house):
        house.execute("INSERT INTO object_cloud VALUES (10, 0.01, 5, ?, NULL)", (points,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with transaction(house):
            house.execute("INSERT INTO object_cloud VALUES (11, 0.01, 4, ?, NULL)", (points,))


def test_deleting_a_session_forgets_its_people_and_conversations(db):
    with transaction(db):
        db.execute("INSERT INTO session (id, started_at) VALUES (1, 0)")
        # A guest met in session 1, and a team member kept across sessions.
        db.execute("INSERT INTO person (id, session_id, first_seen, last_seen) VALUES (1, 1, 0, 0)")
        db.execute("INSERT INTO person (id, session_id, first_seen, last_seen) VALUES (2, NULL, 0, 0)")
        for face_id, person_id in ((1, 1), (2, 2)):
            db.execute("INSERT INTO face (id, person_id, ts) VALUES (?, ?, 0)", (face_id, person_id))
            db.execute("INSERT INTO face_arcface VALUES (?, ?, ?)", (face_id, unit(face_id), person_id))
        # The team member's clothes today belong to session 1.
        db.execute("INSERT INTO appearance (id, person_id, session_id, ts) VALUES (1, 2, 1, 0)")
        db.execute("INSERT INTO appearance_osnet VALUES (1, ?, 2, 1)", (unit(3),))
        db.execute("INSERT INTO conversation (id, session_id, started_at) VALUES (1, 1, 0)")
        db.execute(
            "INSERT INTO message (conversation_id, ts, role, person_id, text) "
            "VALUES (1, 0, 'human', 1, 'I am Alice')"
        )

    with transaction(db):
        db.execute("DELETE FROM session WHERE id = 1")

    assert [row[0] for row in db.execute("SELECT id FROM person")] == [2]
    assert [row[0] for row in db.execute("SELECT person_id FROM face_arcface")] == [2]
    for table in ("appearance", "appearance_osnet", "conversation", "message"):
        assert count(db, table) == 0, table
    assert db.execute("SELECT count(*) FROM message_fts WHERE message_fts MATCH 'alice'").fetchone()[0] == 0


def test_message_search_matches_word_forms(db):
    with transaction(db):
        db.execute("INSERT INTO conversation (id, started_at) VALUES (1, 0)")
        db.execute(
            "INSERT INTO message (id, conversation_id, ts, role, text) "
            "VALUES (1, 1, 0, 'human', 'I would like a drink, maybe a coke')"
        )
        db.execute(
            "INSERT INTO message (id, conversation_id, ts, role, text) "
            "VALUES (2, 1, 1, 'robot', 'Please sit on the sofa')"
        )
    rows = db.execute("SELECT rowid FROM message_fts WHERE message_fts MATCH 'drinks'").fetchall()
    assert [row[0] for row in rows] == [1]

    with transaction(db):
        db.execute("UPDATE message SET text = 'Water, please' WHERE id = 1")
    assert db.execute("SELECT count(*) FROM message_fts WHERE message_fts MATCH 'coke'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM message_fts WHERE message_fts MATCH 'water'").fetchone()[0] == 1


def test_person_attributes_are_json(db):
    with transaction(db):
        db.execute(
            "INSERT INTO person (id, first_seen, last_seen, attributes) "
            "VALUES (1, 0, 0, json_object('drink', 'coke'))"
        )
    drink = db.execute("SELECT json_extract(attributes, '$.drink') FROM person").fetchone()[0]
    assert drink == "coke"
    with pytest.raises(sqlite3.IntegrityError, match="json_valid"):
        with transaction(db):
            db.execute("INSERT INTO person (first_seen, last_seen, attributes) VALUES (0, 0, 'coke')")


def test_strict_tables_reject_wrong_types(db):
    with pytest.raises(sqlite3.IntegrityError, match="cannot store TEXT value in REAL column"):
        with transaction(db):
            add_map(db)
            db.execute(
                """INSERT INTO object (map_id, label, center_x, center_y, center_z,
                                       size_x, size_y, size_z, first_seen, last_seen)
                   VALUES ('m1', 'cup', 'one', 0, 0, 0, 0, 0, 0, 0)"""
            )
