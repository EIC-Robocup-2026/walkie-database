import sqlite3

import numpy as np
import pytest

from walkie_database.db import connect, transaction


def test_extensions_are_available():
    conn = connect(":memory:")
    assert conn.execute("SELECT vec_version()").fetchone()[0].startswith("v0.1.")
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    assert {"ENABLE_FTS5", "ENABLE_RTREE", "ENABLE_GEOPOLY"} <= options


def test_file_database_uses_wal_and_foreign_keys(tmp_path):
    path = tmp_path / "nested" / "walkie.db"
    conn = connect(path)
    assert path.exists()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_readonly_connection_cannot_write(tmp_path):
    path = tmp_path / "walkie.db"
    connect(path).execute("CREATE TABLE t (x INTEGER)")
    reader = connect(path, readonly=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("INSERT INTO t VALUES (1)")


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with transaction(db):
            db.execute("INSERT INTO map (id, created_at) VALUES ('m1', 0)")
            raise RuntimeError("boom")
    assert db.execute("SELECT count(*) FROM map").fetchone()[0] == 0
    assert not db.in_transaction


def test_transaction_removes_half_written_vec0_row(db):
    # vec0 rejects NULL metadata, but only after it has written part of the
    # row. Without the rollback, object_id 1 would stay taken.
    embedding = np.ones(512, dtype=np.float32).tobytes()
    with pytest.raises(sqlite3.OperationalError, match="NULL"):
        with transaction(db):
            db.execute(
                "INSERT INTO object_clip_vit_b16 VALUES (1, ?, 'm1', NULL)", (embedding,)
            )
    assert db.execute("SELECT count(*) FROM object_clip_vit_b16").fetchone()[0] == 0
