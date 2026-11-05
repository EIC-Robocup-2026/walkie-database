"""Reset a stopped store to a seed, keeping its ID lineage.

    python -m walkie_database.restore --seed PATH --db PATH --map-folder PATH \\
        --clock-kind wall|ros_sim --replace

This is the one write to a store outside the node, and it runs only while the node
is stopped (ADR-0023). A restored store is the seed's rows plus new run metadata. It
is not a copy of the seed's bytes:

* It gets a fresh ``store_id``, so a client can tell this run from the last one.
* It carries the largest object, person, session and event counters of the outgoing
  store and the seed, so no ID is ever given twice on this map (ADR-0020). A store
  from before migration 0007 has no event counter, so the largest event ID in its log
  or its ledger stands in for it.
* It sets ``refresh_anchors_pending``, so the node stamps the anchors once, on its
  first clock sample.
* It has no request ledger, because a seed has none. A retry from before the restore
  starts over.

A crash before the replacement leaves the old store usable, and a crash after it
leaves a complete new one. Only a store of the same map is ever replaced. A store
with no map row, such as an old people-only file, stays as it is.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from walkie_database.db import connect, transaction
from walkie_database.map_config import CLOCK_KINDS, MapFolder, check_store, read_map_folder
from walkie_database.migrate import load_migrations, schema_version
from walkie_database.people import Refused
from walkie_database.seed_store import COUNTERS, REFUSALS, check_seed_contents, read_counters
from walkie_database.store_lock import (
    StoreLock,
    checkpoint_closed_store,
    discard_temp,
    install_store,
    real_path,
    side_file,
    temp_beside,
)


def _quick_check(conn: sqlite3.Connection, what: object) -> None:
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise Refused(f"{what} is damaged: {result}")


def check_seed(conn: sqlite3.Connection, path: Path, folder: MapFolder, clock_kind: str) -> sqlite3.Row:
    """Return the map row of a seed fit for this map folder and clock, or raise."""
    _quick_check(conn, f"The seed {path}")
    version, newest = schema_version(conn), load_migrations()[-1].version
    if version != newest:
        raise Refused(f"The seed {path} is at schema {version}, not {newest}. Build it again.")
    row = check_store(conn, folder, clock_kind)
    if conn.execute("SELECT count(*) FROM map").fetchone()[0] != 1:
        raise Refused(f"The seed {path} has more than one map.")
    check_seed_contents(conn, f"The file {path}")
    return row


def _read_outgoing(db: Path, map_id: str, clock_kind: str) -> dict[str, int]:
    """The counters of the store being replaced, or Refused when it must stay.

    The caller holds the lock, and has already moved any WAL into the main file,
    so a counter committed just before a crash is seen here.
    """
    with closing(connect(db, readonly=True)) as conn:
        _quick_check(conn, f"The store {db}")
        version, newest = schema_version(conn), load_migrations()[-1].version
        if version > newest:
            raise Refused(f"The store {db} is at schema {version}, newer than this code.")
        meta = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'store_meta'").fetchone():
            meta = conn.execute("SELECT map_id, clock_kind FROM store_meta WHERE id = 1").fetchone()
        if meta is None:
            raise Refused(f"The store {db} is registered to no map, so this tool will not replace it.")
        if meta["map_id"] != map_id:
            raise Refused(f"The store {db} holds the map {meta['map_id']!r}, not {map_id!r}.")
        if meta["clock_kind"] != clock_kind:
            raise Refused(f"The store {db} runs on the clock {meta['clock_kind']!r}, not {clock_kind!r}.")
        return read_counters(conn)


@dataclass(frozen=True)
class Restored:
    db: Path
    map_id: str
    store_id: str
    counters: dict[str, int]


def restore(seed: str | Path, db: str | Path, map_folder: str | Path, clock_kind: str) -> Restored:
    """Replace the stopped store at *db* with *seed*, or raise and leave it as it was."""
    if clock_kind not in CLOCK_KINDS:
        raise Refused(f"The clock kind {clock_kind!r} is not one of {', '.join(CLOCK_KINDS)}.")
    seed_path, target = real_path(seed), real_path(db)
    if seed_path == target:
        raise Refused(f"The seed and the store are the same file, {target}.")
    if not seed_path.is_file():
        raise Refused(f"The seed {seed_path} is not a file.")
    folder = read_map_folder(map_folder)

    # Read-only, and VACUUM INTO below copies through SQLite, never the open file.
    with closing(connect(seed_path, readonly=True)) as seed_conn:
        # Everything about the seed is checked first, because a lock file is a write.
        map_id = check_seed(seed_conn, seed_path, folder, clock_kind)["id"]
        counters = read_counters(seed_conn)
        with StoreLock(target):
            if target.exists():
                checkpoint_closed_store(target)
                outgoing = _read_outgoing(target, map_id, clock_kind)
                counters = {name: max(counters[name], outgoing[name]) for name in COUNTERS}
            elif side_file(target, "-wal").exists():
                raise Refused(f"{target}-wal exists with no store. Look at it, then remove it.")
            store_id = str(uuid.uuid4())
            temp = temp_beside(target)
            try:
                seed_conn.execute("VACUUM INTO ?", (str(temp),))
                with closing(connect(temp)) as conn:
                    with transaction(conn):
                        conn.execute(
                            "UPDATE store_meta SET store_id = ?, refresh_anchors_pending = 1",
                            (store_id,),
                        )
                        conn.executemany(
                            "UPDATE last_id SET id = ? WHERE table_name = ?",
                            [(value, name) for name, value in counters.items()],
                        )
                    busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                if busy:
                    raise sqlite3.OperationalError(f"{temp} could not be checkpointed")
                install_store(temp, target)
            finally:
                discard_temp(temp)
    return Restored(target, map_id, store_id, counters)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m walkie_database.restore",
        description="Reset a stopped store to a seed. Run this only while the node is stopped.",
    )
    parser.add_argument("--seed", required=True, help="the seed file")
    parser.add_argument("--db", required=True, help="the store to replace, or a new path")
    parser.add_argument("--map-folder", required=True, help="the folder with map.yaml and its image")
    parser.add_argument("--clock-kind", required=True, choices=CLOCK_KINDS,
                        help="wall on the robot, ros_sim in a simulation")
    parser.add_argument("--replace", required=True, action="store_true",
                        help="required: this replaces --db")
    args = parser.parse_args(argv)
    try:
        restored = restore(args.seed, args.db, args.map_folder, args.clock_kind)
    except REFUSALS as error:
        print(f"Refused, and changed nothing: {error}", file=sys.stderr)
        return 1
    print(
        f"Restored {restored.db}: map {restored.map_id}, store_id {restored.store_id}, "
        f"counters {restored.counters}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
