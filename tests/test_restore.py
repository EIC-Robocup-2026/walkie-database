"""The offline restore: a fresh run on a store's map, with its ID lineage kept."""

import os
import shutil
import uuid

import pytest
from seed_helpers import (
    anchor,
    anchors_document,
    dump_rows,
    map_id_of,
    rooms_document,
    seed_argv,
    write_map_folder,
)

from walkie_database import restore as restore_tool
from walkie_database import seed_store
from walkie_database.db import connect, transaction
from walkie_database.migrate import migrate
from walkie_database.people import Refused
from walkie_database.restore import main, restore
from walkie_database.store_lock import StoreLock


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


@pytest.fixture
def seed(tmp_path, folder):
    path = tmp_path / "seeds" / "seed.db"
    assert seed_store.main(seed_argv(tmp_path, folder, path)) == 0
    return path


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "run" / "walkie.db"
    path.parent.mkdir()
    return path


def counters(path):
    conn = connect(path, readonly=True)
    try:
        return {r["table_name"]: r["id"] for r in conn.execute("SELECT * FROM last_id")}
    finally:
        conn.close()


def scalar(path, sql):
    conn = connect(path, readonly=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def set_counters(path, **values):
    conn = connect(path)
    with transaction(conn):
        for name, value in values.items():
            conn.execute("UPDATE last_id SET id = ? WHERE table_name = ?", (value, name))
    conn.close()


def temporary_files(db):
    return [p.name for p in db.parent.iterdir() if ".tmp" in p.name]


def run_rows(path):
    """Every row except what a restore must change."""
    conn = connect(path, readonly=True)
    rows = dump_rows(conn)
    conn.close()
    del rows["store_meta"]
    del rows["last_id"]
    return rows


# The restored store ----------------------------------------------------------------


def test_a_restored_store_has_the_seeds_rows_and_a_fresh_store_id(seed, db, folder):
    restored = restore(seed, db, folder, "wall")

    assert run_rows(db) == run_rows(seed)
    store_id = scalar(db, "SELECT store_id FROM store_meta")
    assert store_id == restored.store_id
    assert store_id != scalar(seed, "SELECT store_id FROM store_meta")
    assert uuid.UUID(store_id).version == 4
    assert scalar(db, "SELECT refresh_anchors_pending FROM store_meta") == 1
    assert scalar(db, "PRAGMA quick_check") == "ok"


def test_every_restore_gets_its_own_store_id(seed, db, folder):
    first = restore(seed, db, folder, "wall").store_id
    second = restore(seed, db, folder, "wall").store_id
    assert first != second


def test_a_restore_is_closed_and_leaves_only_the_lock_beside_the_store(seed, db, folder):
    restore(seed, db, folder, "wall")
    assert sorted(p.name for p in db.parent.iterdir()) == ["walkie.db", "walkie.db.lock"]


def test_the_seed_is_not_changed(seed, db, folder):
    before = seed.read_bytes()
    restore(seed, db, folder, "wall")
    assert seed.read_bytes() == before


def test_a_restored_store_opens_and_writes(seed, db, folder):
    restore(seed, db, folder, "wall")
    conn = connect(db)
    migrate(conn)
    with transaction(conn):
        conn.execute("UPDATE store_meta SET refresh_anchors_pending = 0")
    conn.close()
    assert scalar(db, "SELECT refresh_anchors_pending FROM store_meta") == 0


def test_the_cli_restores_and_reports(seed, db, folder, capsys):
    argv = ["--seed", str(seed), "--db", str(db), "--map-folder", str(folder), "--clock-kind", "wall", "--replace"]
    assert main(argv) == 0
    assert "Restored" in capsys.readouterr().out
    assert db.exists()


# The ID lineage --------------------------------------------------------------------


def test_each_counter_takes_the_larger_of_the_store_and_the_seed(seed, db, folder):
    restore(seed, db, folder, "wall")
    set_counters(seed, object=500, person=3)
    set_counters(db, object=20, person=40, session=70)

    restored = restore(seed, db, folder, "wall")

    expected = {"object": 500, "person": 40, "session": 70}
    assert restored.counters == expected
    assert {name: counters(db)[name] for name in expected} == expected


def _crash_with_a_wal_counter(db):
    """Leave a copy of *db* as a crash would: its newest counter only in the WAL."""
    conn = connect(db)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    with transaction(conn):
        conn.execute("UPDATE last_id SET id = 900 WHERE table_name = 'object'")
    crashed = db.parent / "crashed"
    crashed.mkdir()
    shutil.copy(db, crashed / "walkie.db")
    shutil.copy(f"{db}-wal", crashed / "walkie.db-wal")
    conn.close()
    return crashed / "walkie.db"


def test_a_counter_only_in_the_outgoing_wal_is_recovered(seed, db, folder, tmp_path):
    restore(seed, db, folder, "wall")
    crashed = _crash_with_a_wal_counter(db)
    # The main file alone does not have it, so a plain copy would lose it.
    alone = tmp_path / "alone.db"
    shutil.copy(crashed, alone)
    assert counters(alone)["object"] < 900

    restored = restore(seed, crashed, folder, "wall")

    # Checked first: a read-only open of a WAL store leaves empty side files.
    assert not os.path.exists(f"{crashed}-wal")
    assert restored.counters["object"] == 900
    assert counters(crashed)["object"] == 900
    assert scalar(crashed, "PRAGMA quick_check") == "ok"


def test_people_events_and_the_ledger_do_not_survive_a_restore(seed, db, folder):
    restore(seed, db, folder, "wall")
    conn = connect(db)
    with transaction(conn):
        conn.execute("INSERT INTO session (id, started_at) VALUES (9, 1.0)")
        conn.execute("UPDATE last_id SET id = 9 WHERE table_name = 'session'")
        conn.execute(
            """INSERT INTO write_request (request_id, task_id, operation, payload_hash, outcome_json, committed_at)
               VALUES ('r', 't', 'op', 'h', '{}', 1.0)"""
        )
    conn.close()
    assert scalar(db, "SELECT count(*) FROM write_request") == 1

    restored = restore(seed, db, folder, "wall")

    assert scalar(db, "SELECT count(*) FROM write_request") == 0
    assert scalar(db, "SELECT count(*) FROM session") == 0
    assert restored.counters["session"] == 9


# A crash ---------------------------------------------------------------------------


def test_a_crash_before_the_replacement_leaves_the_old_store_usable(seed, db, folder, monkeypatch, capsys):
    first = restore(seed, db, folder, "wall")
    set_counters(db, object=321)
    argv = ["--seed", str(seed), "--db", str(db), "--map-folder", str(folder), "--clock-kind", "wall", "--replace"]

    def crash(*args):
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)
    assert main(argv) == 1
    monkeypatch.undo()

    assert "power cut" in capsys.readouterr().err
    assert temporary_files(db) == []
    assert scalar(db, "SELECT store_id FROM store_meta") == first.store_id
    assert counters(db)["object"] == 321
    assert scalar(db, "PRAGMA quick_check") == "ok"
    with StoreLock(db):
        pass


def test_a_wal_is_recovered_before_a_crash_too(seed, db, folder, monkeypatch):
    restore(seed, db, folder, "wall")
    crashed = _crash_with_a_wal_counter(db)

    def crash(*args):
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="power cut"):
        restore(seed, crashed, folder, "wall")
    monkeypatch.undo()

    assert counters(crashed)["object"] == 900


def test_a_crash_while_copying_leaves_the_old_store_and_no_temporary_file(seed, db, folder, monkeypatch):
    first = restore(seed, db, folder, "wall")

    def crash(*args):
        raise OSError("disk full")

    monkeypatch.setattr(restore_tool, "install_store", crash)
    with pytest.raises(OSError, match="disk full"):
        restore(seed, db, folder, "wall")

    assert temporary_files(db) == []
    assert scalar(db, "SELECT store_id FROM store_meta") == first.store_id


# Refusals --------------------------------------------------------------------------


def test_replace_is_required(seed, db, folder):
    argv = ["--seed", str(seed), "--db", str(db), "--map-folder", str(folder), "--clock-kind", "wall"]
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2
    assert not db.exists()


@pytest.mark.parametrize("flag", ["--seed", "--db", "--map-folder", "--clock-kind"])
def test_every_path_and_the_clock_are_required(seed, db, folder, flag):
    argv = ["--seed", str(seed), "--db", str(db), "--map-folder", str(folder), "--clock-kind", "wall", "--replace"]
    at = argv.index(flag)
    del argv[at : at + 2]
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2
    assert not db.exists()


def test_a_held_lock_refuses_and_changes_nothing(seed, db, folder):
    first = restore(seed, db, folder, "wall")
    before = db.read_bytes()
    with StoreLock(db), pytest.raises(Exception, match="in use"):
        restore(seed, db, folder, "wall")
    assert db.read_bytes() == before
    assert scalar(db, "SELECT store_id FROM store_meta") == first.store_id


def test_a_seed_of_another_map_folder_is_refused(seed, db, tmp_path, capsys):
    other = write_map_folder(tmp_path / "other_map", pixel=7)
    argv = ["--seed", str(seed), "--db", str(db), "--map-folder", str(other), "--clock-kind", "wall", "--replace"]

    assert main(argv) == 1

    assert "digest" in capsys.readouterr().err
    assert not db.exists()
    assert sorted(p.name for p in db.parent.iterdir()) == []


def test_a_seed_with_only_its_digest_changed_is_refused(seed, db, folder, capsys):
    # The 8 characters in the map ID still match. Only the full digest says no.
    conn = connect(seed)
    with transaction(conn):
        conn.execute("UPDATE map SET map_hash = substr(map_hash, 1, 8) || ?", ("f" * 56,))
    conn.close()

    with pytest.raises(Exception, match="digest"):
        restore(seed, db, folder, "wall")
    assert not db.exists()


def test_a_seed_on_another_clock_is_refused(seed, db, folder):
    with pytest.raises(Exception, match="clock"):
        restore(seed, db, folder, "ros_sim")
    assert not db.exists()


def test_an_unknown_clock_kind_is_refused(seed, db, folder):
    with pytest.raises(Refused, match="clock kind"):
        restore(seed, db, folder, "sundial")


def test_an_outgoing_store_of_another_map_stays(seed, db, folder, tmp_path, capsys):
    other = write_map_folder(tmp_path / "other_map", pixel=7)
    other_seed = tmp_path / "seeds" / "other.db"
    map_id = map_id_of(other)
    assert seed_store.main(
        seed_argv(tmp_path, other, other_seed, rooms=rooms_document(map_id),
                  anchors=anchors_document(map_id, anchors=[anchor()]))  # fmt: skip
    ) == 0
    restore(other_seed, db, other, "wall")
    before = db.read_bytes()

    with pytest.raises(Refused, match="holds the map"):
        restore(seed, db, folder, "wall")

    assert db.read_bytes() == before


def test_an_outgoing_store_on_another_clock_stays(seed, db, folder, tmp_path):
    restore(seed, db, folder, "wall")
    sim_seed = tmp_path / "seeds" / "sim.db"
    assert seed_store.main(seed_argv(tmp_path, folder, sim_seed, clock_kind="ros_sim")) == 0
    before = db.read_bytes()

    with pytest.raises(Refused, match="runs on the clock"):
        restore(sim_seed, db, folder, "ros_sim")

    assert db.read_bytes() == before


def test_a_people_only_store_stays(seed, db, folder):
    conn = connect(db)
    migrate(conn)
    with transaction(conn):
        conn.execute("INSERT INTO session (id, started_at) VALUES (1, 1.0)")
    conn.close()

    with pytest.raises(Refused, match="registered to no map"):
        restore(seed, db, folder, "wall")

    assert scalar(db, "SELECT count(*) FROM session") == 1


def test_a_file_that_is_not_a_store_stays(seed, db, folder, capsys):
    db.write_bytes(b"not a database" * 1000)
    argv = ["--seed", str(seed), "--db", str(db), "--map-folder", str(folder), "--clock-kind", "wall", "--replace"]

    assert main(argv) == 1

    assert db.read_bytes() == b"not a database" * 1000


def test_a_lone_wal_with_no_store_is_refused(seed, db, folder):
    wal = db.with_name("walkie.db-wal")
    wal.write_bytes(b"something")
    with pytest.raises(Refused, match="no store"):
        restore(seed, db, folder, "wall")
    assert wal.read_bytes() == b"something"
    assert not db.exists()


def test_a_seed_with_a_person_in_it_is_refused(seed, db, folder):
    conn = connect(seed)
    with transaction(conn):
        conn.execute("INSERT INTO session (id, started_at) VALUES (1, 1.0)")
    conn.close()

    with pytest.raises(Refused, match="not a seed"):
        restore(seed, db, folder, "wall")
    assert not db.exists()


def test_a_seed_at_an_older_schema_is_refused(seed, db, folder):
    conn = connect(seed)
    conn.execute("PRAGMA user_version = 5")
    conn.close()

    with pytest.raises(Refused, match="schema"):
        restore(seed, db, folder, "wall")
    assert not db.exists()


def test_a_seed_that_is_not_a_database_is_refused(db, folder, tmp_path, capsys):
    fake = tmp_path / "fake.db"
    fake.write_bytes(b"not a database" * 1000)
    argv = ["--seed", str(fake), "--db", str(db), "--map-folder", str(folder), "--clock-kind", "wall", "--replace"]

    assert main(argv) == 1

    assert "Refused" in capsys.readouterr().err
    assert not db.exists()


def test_a_missing_seed_is_refused(db, folder, tmp_path):
    with pytest.raises(Refused, match="not a file"):
        restore(tmp_path / "absent.db", db, folder, "wall")
    assert not db.exists()


def test_a_seed_cannot_restore_onto_itself(seed, folder):
    with pytest.raises(Refused, match="same file"):
        restore(seed, seed, folder, "wall")


def test_the_tool_writes_only_beside_the_store_it_is_given(seed, db, folder, tmp_path, monkeypatch):
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    restore(seed, db, folder, "wall")
    assert list(work.iterdir()) == []
