"""Upgrades of a populated database from schema 3, the last one that shipped."""

import json
import sqlite3

import numpy as np
import pytest

from walkie_database.db import connect, transaction
from walkie_database.migrate import Migration, load_migrations, migrate, schema_version
from walkie_database.objects import object_id_highwater, reserve_object_ids

# Longer than 12 bytes, so that vec0 keeps it outside its fixed metadata slot.
MAP_ID = "chula-pat14-6-2026-11-05-abcd1234"
OTHER_MAP_ID = "demo"
KITCHEN = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
LIVING_ROOM = [[4, 0], [8, 0], [8, 4], [4, 4], [4, 0]]
TABLE, CUP, SOFA, LOST_MUG, FAR_LAMP = 10, 11, 12, 13, 40


def vectors() -> dict[int, bytes]:
    """Vectors with no round values, so that a rewrite would show in the bytes."""
    rng = np.random.default_rng(3)
    return {
        object_id: rng.standard_normal(512).astype(np.float32).tobytes()
        for object_id in (TABLE, CUP, SOFA, LOST_MUG, FAR_LAMP)
    }


def add_v3_object(conn, object_id, map_id, room_id, label, source, embedding):
    conn.execute(
        """INSERT INTO object (id, map_id, room_id, label, source, center_x, center_y, center_z,
                               size_x, size_y, size_z, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, 1, 2, 0.5, 0.2, 0.2, 0.2, 100, 200)""",
        (object_id, map_id, room_id, label, source),
    )
    conn.execute(
        """INSERT INTO object_box (id, min_x, max_x, min_y, max_y, min_z, max_z)
           VALUES (?, 0.9, 1.1, 1.9, 2.1, 0.4, 0.6)""",
        (object_id,),
    )
    conn.execute(
        "INSERT INTO object_clip_vit_b16 (object_id, embedding, map_id, room_id) VALUES (?, ?, ?, ?)",
        (object_id, embedding, map_id, room_id or 0),
    )


def populate_v3(conn: sqlite3.Connection) -> None:
    """A database at schema 3 with every kind of row the upgrades must keep."""
    assert migrate(conn, load_migrations()[:3]) == 3
    embeddings = vectors()
    points = np.arange(6, dtype="<f4").tobytes()
    with transaction(conn):
        for map_id in (MAP_ID, OTHER_MAP_ID):
            conn.execute("INSERT INTO map (id, created_at) VALUES (?, 1)", (map_id,))
        for room_id, name, outline in ((1, "kitchen", KITCHEN), (2, "living room", LIVING_ROOM)):
            conn.execute(
                "INSERT INTO room (id, map_id, name) VALUES (?, ?, ?)", (room_id, MAP_ID, name)
            )
            conn.execute(
                "INSERT INTO room_shape (rowid, _shape) VALUES (?, geopoly_ccw(?))",
                (room_id, json.dumps(outline)),
            )
            conn.execute(
                "INSERT INTO room_clip_vit_b16 (room_id, embedding, map_id) VALUES (?, ?, ?)",
                (room_id, embeddings[TABLE if room_id == 1 else CUP], MAP_ID),
            )
        add_v3_object(conn, TABLE, MAP_ID, 1, "table", "map", embeddings[TABLE])
        add_v3_object(conn, CUP, MAP_ID, 1, "cup", "perception", embeddings[CUP])
        add_v3_object(conn, SOFA, MAP_ID, 2, "sofa", "manual", embeddings[SOFA])
        add_v3_object(conn, LOST_MUG, MAP_ID, None, "mug", "perception", embeddings[LOST_MUG])
        add_v3_object(conn, FAR_LAMP, OTHER_MAP_ID, None, "lamp", "map", embeddings[FAR_LAMP])
        conn.execute("INSERT INTO object_cloud VALUES (?, 0.01, 2, ?, NULL)", (CUP, points))
        # The counters are above every id in the tables, as after a forgotten session.
        conn.execute("INSERT INTO session (id, started_at, ended_at) VALUES (9, 0, 1)")
        conn.execute("INSERT INTO person (id, first_seen, last_seen) VALUES (6, 0, 0)")
        conn.execute("DELETE FROM session WHERE id = 9")
        conn.execute("DELETE FROM person WHERE id = 6")
        conn.execute("INSERT INTO session (id, started_at) VALUES (3, 5)")
        conn.execute("INSERT INTO person (id, session_id, first_seen, last_seen) VALUES (2, 3, 5, 5)")
        conn.execute(
            """INSERT INTO object_observation (id, object_id, session_id, ts, label, score)
               VALUES (1, ?, 3, 150, 'cup', 0.9)""",
            (CUP,),
        )


def table_rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every row of every ordinary table, shadow tables included, in a fixed order."""
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY 1, 2')]
        for name in names
    }


def snapshot(conn: sqlite3.Connection) -> tuple[list[tuple], dict[str, list[tuple]]]:
    """The whole schema and all the data, to compare a database with itself."""
    schema = [
        tuple(row) for row in conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2")
    ]
    return schema, table_rows(conn)


@pytest.fixture
def v3(tmp_path):
    """A populated file at schema 3, so that a restart can really reopen it."""
    conn = connect(tmp_path / "walkie.db")
    populate_v3(conn)
    yield conn
    conn.close()


def upgraded(conn: sqlite3.Connection, version: int) -> sqlite3.Connection:
    assert migrate(conn, load_migrations()[:version]) == version
    return conn


def object_field(conn, object_id, column):
    return conn.execute(f"SELECT {column} FROM object WHERE id = ?", (object_id,)).fetchone()[0]


# Migration 0004 --------------------------------------------------------------


def test_0004_backfills_map_objects_as_present_anchors(v3):
    upgraded(v3, 4)
    for object_id in (TABLE, FAR_LAMP):
        assert object_field(v3, object_id, "layer") == "anchor"
        assert object_field(v3, object_id, "state") == "present"
        assert object_field(v3, object_id, "p_exist") == 0.95


def test_0004_leaves_other_objects_as_candidates_to_review(v3):
    upgraded(v3, 4)
    for object_id in (CUP, SOFA, LOST_MUG):
        assert object_field(v3, object_id, "layer") == "movable"
        assert object_field(v3, object_id, "state") == "candidate"
        assert object_field(v3, object_id, "p_exist") == 0.5


def test_0004_does_not_invent_verification_or_a_baseline(v3):
    upgraded(v3, 4)
    for object_id in (TABLE, CUP, SOFA, LOST_MUG, FAR_LAMP):
        assert object_field(v3, object_id, "last_verified") is None
        assert object_field(v3, object_id, "in_baseline") == 0
        assert object_field(v3, object_id, "geometry_valid") == 1
        assert object_field(v3, object_id, "revision") == 0
    assert v3.execute("SELECT count(*) FROM object_label").fetchone()[0] == 0


def test_0004_gives_old_maps_a_blank_identity(v3):
    upgraded(v3, 4)
    rows = v3.execute("SELECT map_hash, baseline_rev, clock_kind FROM map").fetchall()
    assert [tuple(row) for row in rows] == [("", "", "wall"), ("", "", "wall")]
    assert v3.execute("SELECT count(*) FROM store_meta").fetchone()[0] == 0


def test_0004_keeps_every_old_row_and_value(v3):
    before = table_rows(v3)
    upgraded(v3, 4)
    after = table_rows(v3)
    # Every old column comes first in its row, because ALTER TABLE appends.
    for table in ("object", "object_observation", "map"):
        width = len(before[table][0])
        assert [row[:width] for row in after[table]] == before[table], table
    for table in ("room", "session", "person", "object_cloud", "room_clip_vit_b16_chunks"):
        assert after[table] == before[table], table


def test_0004_keeps_both_counters_and_starts_the_object_counter_at_the_largest_id(v3):
    upgraded(v3, 4)
    rows = v3.execute("SELECT table_name, id FROM last_id ORDER BY table_name")
    assert [tuple(row) for row in rows] == [("object", FAR_LAMP), ("person", 6), ("session", 9)]


def test_0004_keeps_the_counter_triggers_working(v3):
    upgraded(v3, 4)
    with transaction(v3):
        v3.execute("INSERT INTO session (id, started_at, ended_at) VALUES (20, 0, 1)")
        v3.execute("INSERT INTO person (id, first_seen, last_seen) VALUES (21, 0, 0)")
        v3.execute(
            """INSERT INTO object (id, map_id, label, center_x, center_y, center_z,
                                   size_x, size_y, size_z, first_seen, last_seen)
               VALUES (50, ?, 'bottle', 0, 0, 0, 0, 0, 0, 0, 0)""",
            (MAP_ID,),
        )
    rows = v3.execute("SELECT table_name, id FROM last_id ORDER BY table_name")
    assert [tuple(row) for row in rows] == [("object", 50), ("person", 21), ("session", 20)]


def test_0004_leaves_no_temporary_table_behind(v3):
    upgraded(v3, 4)
    assert v3.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0] == 0


def test_0004_twice_changes_nothing(tmp_path, v3):
    upgraded(v3, 4)
    before = snapshot(v3)
    assert upgraded(v3, 4) is v3
    assert snapshot(v3) == before
    v3.close()
    reopened = connect(tmp_path / "walkie.db")
    assert upgraded(reopened, 4) is reopened
    assert snapshot(reopened) == before
    reopened.close()


def failing_copy(tmp_path, migration: Migration) -> Migration:
    """*migration* with a statement at the end that fails, after all its real work."""
    path = tmp_path / migration.path.name
    path.write_text(migration.path.read_text() + "\nINSERT INTO no_such_table VALUES (1);\n")
    return Migration(migration.version, path)


def test_0004_that_fails_at_the_end_leaves_schema_3_unchanged(tmp_path, v3):
    before = snapshot(v3)
    migrations = load_migrations()
    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        migrate(v3, [*migrations[:3], failing_copy(tmp_path, migrations[3])])

    assert schema_version(v3) == 3
    assert snapshot(v3) == before
    assert not v3.in_transaction
    assert v3.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        v3.execute("INSERT INTO last_id (table_name, id) VALUES ('object', 0)")

    # The real migration still runs afterwards.
    upgraded(v3, 4)
    assert object_id_highwater(v3) == FAR_LAMP


def test_ids_reserved_after_the_upgrade_are_above_every_old_id(v3):
    upgraded(v3, 4)
    assert reserve_object_ids(v3).first == FAR_LAMP + 1
