"""Keep one writer on a store file, and replace a stopped store safely.

The node and the offline seed and restore tools take an exclusive advisory lock
at ``<db_path>.lock`` before they open a store for write. The node holds it for
its whole life (ADR-0023).

The lock is a kernel ``flock``, not a process id written to a file. The kernel
drops it when the holder dies, so a crash leaves nothing to clean up. The lock
file stays on disk. Deleting it could give two processes the lock at once: one
holds the old file while the other locks a new one at the same path.

``flock`` belongs to one open of the file, so a second open in the same process
conflicts too. It does not work across NFS as it does on a local disk, so keep
stores on a local disk. A reader such as ``pixi run db-shell`` takes no lock.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from types import TracebackType

from walkie_database.db import MEMORY, connect


class StoreLocked(RuntimeError):
    """Another process holds the store's lock."""


def real_path(path: str | Path) -> Path:
    """*path* with symlinks resolved, so that every name of a store shares one lock."""
    return Path(os.path.realpath(path))


def lock_path(db_path: str | Path) -> Path:
    """The lock file of a store: ``<db_path>.lock``, next to the real file."""
    return Path(f"{real_path(db_path)}.lock")


class StoreLock:
    """The exclusive lock of one store. Use it as a context manager, or call acquire and release."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock now, or raise StoreLocked. An in-memory store has nothing to lock."""
        if self.db_path == MEMORY or self._fd is not None:
            return
        path = lock_path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise StoreLocked(
                f"{self.db_path} is in use: another process holds {path}. Stop it first."
            ) from None
        self._fd = fd

    def release(self) -> None:
        """Drop the lock. The file stays."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> StoreLock:
        self.acquire()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def side_file(path: str | Path, suffix: str) -> Path:
    """A store's ``-wal`` or ``-shm`` file."""
    return Path(f"{path}{suffix}")


def checkpoint_closed_store(path: Path) -> None:
    """Move a stopped store's WAL into its main file, if it has one.

    After this the main file alone holds every committed change, so its ``-wal``
    can go. The caller holds the store's lock. Raises sqlite3.Error when a
    reader keeps the WAL busy or the store is damaged, and the WAL is then left
    in place.
    """
    wal = side_file(path, "-wal")
    if not wal.exists():
        return
    with closing(connect(path)) as conn:
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    if busy:
        raise sqlite3.OperationalError(f"another process is reading {path}, so its WAL stays")


def temp_beside(target: Path) -> Path:
    """A new path in *target*'s directory, so that os.replace onto *target* is atomic.

    A crash can leave this file behind. It is harmless and safe to delete.
    """
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


def discard_temp(temp: Path) -> None:
    """Remove a temporary store and anything SQLite left next to it."""
    for suffix in ("", "-wal", "-shm"):
        side_file(temp, suffix).unlink(missing_ok=True)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install_store(temp: Path, target: Path) -> None:
    """Replace *target* with the closed store at *temp*, atomically.

    The caller holds *target*'s lock and has checkpointed *target*, so the old
    main file is complete. A crash before os.replace leaves the old store usable,
    and a crash after it leaves the new one complete.

    The old ``-wal`` and ``-shm`` go first, with a directory sync in between. SQLite
    would apply a leftover ``-wal`` to the new file and corrupt it, so the rename
    must never reach the disk ahead of the delete.
    """
    _fsync(temp)
    for suffix in ("-wal", "-shm"):
        side_file(target, suffix).unlink(missing_ok=True)
    _fsync(target.parent)
    os.replace(temp, target)
    _fsync(target.parent)
