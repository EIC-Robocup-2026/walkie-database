"""Upgrades of a populated database from schema 3, the last one that shipped."""

import json
import sqlite3

import numpy as np
import pytest

from walkie_database.db import connect, transaction
from walkie_database.events import REMEMBER_EVENT
from walkie_database.migrate import Migration, load_migrations, migrate, schema_version
from walkie_database.object_context import (
    add_event,
    add_surface,
    find_events,
    record_write,
    relations,
    room_at,
    room_by_key,
    room_outline,
    set_relation,
)
from walkie_database.objects import object_id_highwater, reserve_object_ids
from walkie_database.people import end_session

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


# Migration 0005 --------------------------------------------------------------


def vector_rows(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            """SELECT object_id, embedding, map_id, room_id, state, layer
               FROM object_clip_vit_b16 ORDER BY object_id"""
        )
    ]


def test_0005_keeps_the_exact_bytes_of_every_vector(v3):
    upgraded(v3, 5)
    stored = {row[0]: row[1] for row in vector_rows(v3)}
    assert stored == vectors()


def test_0005_takes_metadata_from_the_object_rows(v3):
    upgraded(v3, 5)
    assert [(row[0], *row[2:]) for row in vector_rows(v3)] == [
        (TABLE, MAP_ID, 1, "present", "anchor"),
        (CUP, MAP_ID, 1, "candidate", "movable"),
        (SOFA, MAP_ID, 2, "candidate", "movable"),
        (LOST_MUG, MAP_ID, 0, "candidate", "movable"),
        (FAR_LAMP, OTHER_MAP_ID, 0, "present", "anchor"),
    ]


def test_0005_corrects_metadata_that_had_drifted_at_version_3(v3):
    with transaction(v3):
        v3.execute("UPDATE object_clip_vit_b16 SET room_id = 2 WHERE object_id = ?", (CUP,))
    upgraded(v3, 5)
    assert v3.execute(
        "SELECT room_id FROM object_clip_vit_b16 WHERE object_id = ?", (CUP,)
    ).fetchone()[0] == 1


def test_0005_leaves_a_vector_with_no_object_behind(v3):
    with transaction(v3):
        v3.execute(
            "INSERT INTO object_clip_vit_b16 (object_id, embedding, map_id, room_id) VALUES (99, ?, ?, 0)",
            (vectors()[CUP], MAP_ID),
        )
    upgraded(v3, 5)
    assert [row[0] for row in vector_rows(v3)] == [TABLE, CUP, SOFA, LOST_MUG, FAR_LAMP]


def test_0005_does_not_touch_the_room_vectors(v3):
    def room_vectors():
        rows = v3.execute(
            "SELECT room_id, embedding, map_id FROM room_clip_vit_b16 ORDER BY room_id"
        ).fetchall()
        sql = v3.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'room_clip_vit_b16'"
        ).fetchone()[0]
        return [tuple(row) for row in rows], sql

    upgraded(v3, 4)
    before = room_vectors()
    upgraded(v3, 5)
    assert room_vectors() == before


def test_0005_leaves_nothing_of_the_old_table_or_its_stage(v3):
    def names():
        rows = v3.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        return {row[0] for row in rows}

    upgraded(v3, 4)
    before = names()
    upgraded(v3, 5)
    after = names()
    assert before - after == set()
    # Only the two new triggers, and the shadow tables of the two new columns.
    assert after - before == {
        "object_state_layer_update",
        "object_map_fixed",
        "object_clip_vit_b16_metadatachunks02",
        "object_clip_vit_b16_metadatachunks03",
        "object_clip_vit_b16_metadatatext02",
        "object_clip_vit_b16_metadatatext03",
    }
    assert v3.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0] == 0


def test_0005_recreates_the_table_under_the_same_name_and_declares_it_fully(v3):
    upgraded(v3, 5)
    sql = v3.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'object_clip_vit_b16'"
    ).fetchone()[0]
    for declaration in ("object_id INTEGER PRIMARY KEY", "FLOAT[512] distance_metric=cosine",
                        "map_id    TEXT", "room_id   INTEGER", "state     TEXT", "layer     TEXT"):
        assert declaration in sql


def test_0005_twice_changes_nothing(v3):
    upgraded(v3, 5)
    before = snapshot(v3)
    upgraded(v3, 5)
    assert snapshot(v3) == before


def test_0005_that_fails_at_the_end_rolls_everything_back(tmp_path, v3):
    upgraded(v3, 4)
    before = snapshot(v3)
    migrations = load_migrations()

    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        migrate(v3, [*migrations[:4], failing_copy(tmp_path, migrations[4])])

    assert schema_version(v3) == 4
    assert snapshot(v3) == before
    assert not v3.in_transaction
    assert v3.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError, match="state"):
        v3.execute("SELECT state FROM object_clip_vit_b16")

    # The old table and its triggers work on the same connection, with the old bytes.
    embeddings = vectors()
    rows = v3.execute(
        "SELECT object_id FROM object_clip_vit_b16 WHERE embedding MATCH ? AND k = 1 AND room_id = 1",
        (embeddings[CUP],),
    ).fetchall()
    assert [row[0] for row in rows] == [CUP]
    with transaction(v3):
        v3.execute("DELETE FROM object WHERE id = ?", (SOFA,))
    assert v3.execute(
        "SELECT count(*) FROM object_clip_vit_b16 WHERE object_id = ?", (SOFA,)
    ).fetchone()[0] == 0

    # The real migration still runs afterwards.
    upgraded(v3, 5)
    assert {row[0]: row[1] for row in vector_rows(v3)} == {
        key: value for key, value in embeddings.items() if key != SOFA
    }


def test_0005_that_fails_is_also_undone_after_a_restart(tmp_path, v3):
    upgraded(v3, 4)
    before = snapshot(v3)
    migrations = load_migrations()
    with pytest.raises(sqlite3.OperationalError):
        migrate(v3, [*migrations[:4], failing_copy(tmp_path, migrations[4])])
    v3.close()
    reopened = connect(tmp_path / "walkie.db")
    assert snapshot(reopened) == before
    assert {row[0]: row[1] for row in reopened.execute(
        "SELECT object_id, embedding FROM object_clip_vit_b16")} == vectors()
    reopened.close()


# Migration 0006 --------------------------------------------------------------

CONTEXT_TABLES = ("surface", "object_catalogue", "catalogue_alias", "object_relation", "object_event",
                  "event", "write_request")


def test_0006_keeps_every_old_row_and_only_adds_columns_at_the_end(v3):
    upgraded(v3, 5)
    old = table_rows(v3)
    upgraded(v3, 6)
    new = table_rows(v3)
    for table, rows in old.items():
        width = len(rows[0]) if rows else 0
        assert [row[:width] for row in new[table]] == rows, table


def test_0006_starts_with_no_keys_links_or_new_rows(v3):
    upgraded(v3, 6)
    assert [tuple(row) for row in v3.execute("SELECT name, key FROM room ORDER BY id")] == [
        ("kitchen", None), ("living room", None),
    ]
    assert v3.execute(
        "SELECT count(*) FROM object WHERE enclosed != 0 OR catalogue_id IS NOT NULL"
    ).fetchone()[0] == 0
    for table in CONTEXT_TABLES:
        assert v3.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_0006_leaves_old_room_outlines_and_vectors_working(v3):
    upgraded(v3, 6)
    assert room_at(v3, MAP_ID, 1, 1) == 1
    assert room_at(v3, MAP_ID, 6, 1) == 2
    assert room_outline(v3, 2) == [[4, 0], [8, 0], [8, 4], [4, 4]]
    assert {row[0]: row[1] for row in vector_rows(v3)} == vectors()


def test_0006_lets_old_rows_take_keys_surfaces_relations_and_events(v3):
    upgraded(v3, 6)
    with transaction(v3):
        v3.execute("UPDATE room SET key = 'living_room' WHERE name = 'living room'")
        surface_id = add_surface(v3, TABLE, "table_top", 0.8, [[0, 0], [1, 0], [1, 1]])
        set_relation(v3, CUP, TABLE, "on", 0.5, 10.0, surface_id=surface_id)
        add_event(v3, MAP_ID, "note", "cup on table", 11.0, object_id=CUP, room_id=1)
    assert room_by_key(v3, MAP_ID, "living_room")["id"] == 2
    assert [row["surface_key"] for row in relations(v3, src_id=CUP)] == ["table_top"]
    assert find_events(v3, MAP_ID, text="cup")[0][0]["object_id"] == CUP


def test_0006_forgets_the_events_of_a_session_that_is_older_than_it(v3):
    upgraded(v3, 6)
    with transaction(v3):
        add_event(v3, MAP_ID, "note", "guest waved", 11.0, person_id=2, session_id=3)
        add_event(v3, MAP_ID, "note", "robot moved", 12.0, session_id=3)
    assert end_session(v3, 3) == (3, 1)
    assert [row["text"] for row in find_events(v3, MAP_ID)[0]] == ["robot moved"]
    assert find_events(v3, MAP_ID, text="waved") == ([], False)
    assert v3.execute("SELECT session_id FROM event").fetchone()[0] is None
    # Forgetting keeps its other effects on old rows.
    assert v3.execute("SELECT session_id FROM object_observation").fetchone()[0] is None
    assert v3.execute("SELECT count(*) FROM person").fetchone()[0] == 0


def test_0006_twice_changes_nothing(tmp_path, v3):
    upgraded(v3, 6)
    with transaction(v3):
        add_event(v3, MAP_ID, "note", "robot moved", 12.0)
    before = snapshot(v3)
    upgraded(v3, 6)
    assert snapshot(v3) == before
    v3.close()
    reopened = connect(tmp_path / "walkie.db")
    upgraded(reopened, 6)
    assert snapshot(reopened) == before
    reopened.close()


def test_0006_that_fails_at_the_end_rolls_everything_back(tmp_path, v3):
    upgraded(v3, 5)
    before = snapshot(v3)
    migrations = load_migrations()

    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        migrate(v3, [*migrations[:5], failing_copy(tmp_path, migrations[5])])

    assert schema_version(v3) == 5
    assert snapshot(v3) == before
    assert not v3.in_transaction
    names = {row[0] for row in v3.execute("SELECT name FROM sqlite_master")}
    assert names.isdisjoint({*CONTEXT_TABLES, "event_fts", "event_fts_data",
                             "session_forget_events", "object_relation_check", "room_map_key"})
    with pytest.raises(sqlite3.OperationalError, match="key"):
        v3.execute("SELECT key FROM room")

    # A session still goes as it did at schema 5.
    assert end_session(v3, 3) == (3, 1)

    # The real migration still runs afterwards.
    upgraded(v3, 6)
    with transaction(v3):
        event_id = add_event(v3, MAP_ID, "note", "robot moved", 12.0)
    assert [row["id"] for row in find_events(v3, MAP_ID, text="moved")[0]] == [event_id]


def test_0006_that_fails_is_also_undone_after_a_restart(tmp_path, v3):
    upgraded(v3, 5)
    before = snapshot(v3)
    migrations = load_migrations()
    with pytest.raises(sqlite3.OperationalError):
        migrate(v3, [*migrations[:5], failing_copy(tmp_path, migrations[5])])
    v3.close()
    reopened = connect(tmp_path / "walkie.db")
    assert snapshot(reopened) == before
    reopened.close()


# Migration 0007 --------------------------------------------------------------


def counters(conn) -> list[tuple]:
    return [tuple(row) for row in conn.execute("SELECT table_name, id FROM last_id ORDER BY table_name")]


def forget_the_newest_line(conn) -> int:
    """At schema 6: a kept line, then a guest's line that the ledger names and a forget deletes."""
    with transaction(conn):
        add_event(conn, MAP_ID, "note", "robot moved", 11.0)
        guest = add_event(conn, MAP_ID, "note", "guest waved", 12.0, person_id=2, session_id=3)
        outcome = {"event_id": guest, "ts": 12.0}
        record_write(conn, "r1", "t1", REMEMBER_EVENT, {"line": "guest"}, outcome, 12.0)
    end_session(conn, 3)
    return guest


def test_0007_starts_the_event_counter_above_every_event_and_every_id_the_ledger_gave(v3):
    upgraded(v3, 6)
    guest = forget_the_newest_line(v3)
    with transaction(v3):
        # Only an integer event_id of remember_event counts.
        record_write(v3, "r2", "t1", "set_object_state", {}, {"event_id": 900}, 13.0)
        record_write(v3, "r3", "t1", REMEMBER_EVENT, {"line": "odd"}, {"event_id": "800", "ts": 13.0}, 13.0)
    # The ledger names an id that no row has any more.
    assert v3.execute("SELECT max(id) FROM event").fetchone()[0] == guest - 1

    upgraded(v3, 7)

    assert counters(v3) == [("event", guest), ("object", FAR_LAMP), ("person", 6), ("session", 9)]
    with transaction(v3):
        assert add_event(v3, MAP_ID, "note", "opened a door", 12.0) == guest + 1


def test_0007_keeps_every_counter_trigger_working(v3):
    upgraded(v3, 7)
    with transaction(v3):
        v3.execute("INSERT INTO session (id, started_at, ended_at) VALUES (20, 0, 1)")
        v3.execute("INSERT INTO person (id, first_seen, last_seen) VALUES (21, 0, 0)")
        v3.execute(
            """INSERT INTO object (id, map_id, label, center_x, center_y, center_z,
                                   size_x, size_y, size_z, first_seen, last_seen)
               VALUES (50, ?, 'bottle', 0, 0, 0, 0, 0, 0, 0, 0)""",
            (MAP_ID,),
        )
        v3.execute("INSERT INTO event (id, ts, kind, text, map_id) VALUES (22, 0, 'note', 'x', ?)", (MAP_ID,))
    assert counters(v3) == [("event", 22), ("object", 50), ("person", 21), ("session", 20)]


def test_0007_twice_changes_nothing(tmp_path, v3):
    upgraded(v3, 6)
    forget_the_newest_line(v3)
    upgraded(v3, 7)
    before = snapshot(v3)
    assert upgraded(v3, 7) is v3
    assert snapshot(v3) == before
    v3.close()
    reopened = connect(tmp_path / "walkie.db")
    assert upgraded(reopened, 7) is reopened
    assert snapshot(reopened) == before
    reopened.close()


def test_0007_that_fails_at_the_end_leaves_schema_6_unchanged(tmp_path, v3):
    upgraded(v3, 6)
    guest = forget_the_newest_line(v3)
    before = snapshot(v3)
    migrations = load_migrations()

    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        migrate(v3, [*migrations[:6], failing_copy(tmp_path, migrations[6])])

    assert schema_version(v3) == 6
    assert snapshot(v3) == before
    assert not v3.in_transaction
    assert v3.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        v3.execute("INSERT INTO last_id (table_name, id) VALUES ('event', 0)")
    v3.close()
    reopened = connect(tmp_path / "walkie.db")
    assert snapshot(reopened) == before

    # The real migration still runs afterwards.
    upgraded(reopened, 7)
    assert ("event", guest) in counters(reopened)
    reopened.close()


@pytest.mark.parametrize("version", [4, 5, 6, 7])
def test_an_upgrade_and_a_new_database_end_with_the_same_schema(v3, version):
    upgraded(v3, version)
    fresh = connect(":memory:")
    upgraded(fresh, version)
    assert snapshot(v3)[0] == snapshot(fresh)[0]
    fresh.close()


def test_an_upgrade_from_3_to_the_newest_schema_runs_in_one_go(tmp_path, v3):
    assert migrate(v3) == len(load_migrations())
    assert {row[0]: row[1] for row in vector_rows(v3)} == vectors()
    assert object_id_highwater(v3) == FAR_LAMP
    v3.execute("INSERT INTO event_fts (event_fts, rank) VALUES ('integrity-check', 1)")
    assert v3.execute("PRAGMA foreign_key_check").fetchall() == []
    assert v3.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
