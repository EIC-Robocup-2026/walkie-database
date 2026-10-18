"""Open the Walkie database with the extension and settings it needs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

MEMORY = ":memory:"


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database at *path*, or a throwaway one for ``":memory:"``.

    sqlite-vec is loaded, foreign keys are enforced and rows come back as
    :class:`sqlite3.Row`. The connection is in autocommit mode, so nothing is
    in a transaction unless :func:`transaction` opens one. Python's default
    mode opens transactions implicitly, which hides where one ends.
    """
    path = str(path)
    if readonly:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, autocommit=True)
    else:
        if path != MEMORY:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, autocommit=True)

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row

    # Wait up to 5 s for another process's lock instead of failing at once.
    conn.execute("PRAGMA busy_timeout = 5000")
    # Off by default in SQLite, and set per connection, not per file.
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly and path != MEMORY:
        # Readers in other processes keep working while the node writes (ADR-0004).
        conn.execute("PRAGMA journal_mode = WAL")
        # In WAL mode, NORMAL may lose the last commits on a power cut but
        # never corrupts the file.
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the block as one write transaction, rolled back on any error.

    BEGIN IMMEDIATE takes the write lock at the start, so a second writer
    waits for it (busy_timeout) instead of failing halfway through.

    Every write to a vec0 table belongs in one: when a vec0 insert fails, it
    can leave a half-written row behind, and only a rollback removes it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
