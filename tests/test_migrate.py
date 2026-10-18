import sqlite3

import pytest

from walkie_database.db import connect
from walkie_database.migrate import (
    SchemaTooNewError,
    load_migrations,
    migrate,
    schema_version,
)


def write_migrations(directory, files):
    directory.mkdir()
    for name, sql in files.items():
        (directory / name).write_text(sql)
    return directory


def test_new_database_reaches_newest_version(db):
    assert schema_version(db) == len(load_migrations())


def test_migrating_twice_changes_nothing(tmp_path):
    path = tmp_path / "walkie.db"
    first = migrate(connect(path))
    assert migrate(connect(path)) == first


def test_database_newer_than_code_is_refused(tmp_path):
    conn = connect(tmp_path / "walkie.db")
    conn.execute("PRAGMA user_version = 999")
    with pytest.raises(SchemaTooNewError):
        migrate(conn)
    assert schema_version(conn) == 999


def test_failed_migration_leaves_database_unchanged(tmp_path):
    directory = write_migrations(
        tmp_path / "migrations",
        {
            "0001_first.sql": "CREATE TABLE a (x INTEGER);",
            "0002_broken.sql": "CREATE TABLE b (x INTEGER);\nSELECT no_such_function();",
        },
    )
    conn = connect(tmp_path / "walkie.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, load_migrations(directory))
    assert schema_version(conn) == 1
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"a"}
    assert not conn.in_transaction


def test_migration_numbers_must_not_have_gaps(tmp_path):
    directory = write_migrations(
        tmp_path / "migrations",
        {"0001_first.sql": "", "0003_third.sql": ""},
    )
    with pytest.raises(ValueError, match="expected migration 0002"):
        load_migrations(directory)


def test_badly_named_migration_is_refused(tmp_path):
    directory = write_migrations(tmp_path / "migrations", {"1_first.sql": ""})
    with pytest.raises(ValueError, match="0001_name.sql"):
        load_migrations(directory)
