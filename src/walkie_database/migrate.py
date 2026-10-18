"""Bring a database up to the newest schema in ``migrations/``.

Each migration is a numbered SQL file, ``NNNN_short_name.sql``. The database
stores the number of the last one applied in ``PRAGMA user_version``, which
is 0 in a new file (ADR-0009). A merged migration is never edited: a schema
change is a new file.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILE_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer version of this package."""


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every migration in *directory*, in order.

    Raises ValueError for a badly named file, or when the numbers do not run
    1, 2, 3, ... without a gap.
    """
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_NAME.match(path.name)
        if match is None:
            raise ValueError(f"migration file name must look like 0001_name.sql: {path.name}")
        migrations.append(Migration(int(match.group(1)), path))
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise ValueError(f"expected migration {expected:04d}, found {migration.path.name}")
    return migrations


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> int:
    """Apply every migration newer than the database, and return the new version.

    *conn* must be in autocommit mode, as :func:`walkie_database.db.connect`
    opens it. Raises SchemaTooNewError when the database is newer than the
    newest migration.
    """
    if migrations is None:
        migrations = load_migrations()
    current = schema_version(conn)
    newest = migrations[-1].version if migrations else 0
    if current > newest:
        raise SchemaTooNewError(
            f"database is at schema {current}, but this code only knows up to {newest}"
        )
    # load_migrations() guarantees the numbers run 1, 2, 3, ... so the
    # missing ones start at index `current`.
    for migration in migrations[current:]:
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{migration.sql}\n"
                f"PRAGMA user_version = {migration.version};\nCOMMIT;"
            )
        except BaseException:
            # executescript() stops at the failing statement but leaves the
            # transaction open, so undo the partial migration here.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)
