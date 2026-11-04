"""The store's exclusive lock and its atomic replacement."""

import os
import signal
import subprocess
import sys

import pytest

from walkie_database.db import MEMORY, connect
from walkie_database.store_lock import (
    StoreLock,
    StoreLocked,
    checkpoint_closed_store,
    install_store,
    lock_path,
    real_path,
    temp_beside,
)


def test_a_second_open_in_the_same_process_conflicts(tmp_path):
    path = tmp_path / "walkie.db"
    with StoreLock(path):
        with pytest.raises(StoreLocked, match="in use"):
            StoreLock(path).acquire()


def test_the_lock_can_be_taken_again_after_release_and_its_file_stays(tmp_path):
    path = tmp_path / "walkie.db"
    with StoreLock(path):
        pass
    assert lock_path(path).exists()
    with StoreLock(path):
        assert lock_path(path).exists()


def test_a_symlink_to_a_store_shares_its_lock(tmp_path):
    real = tmp_path / "real.db"
    link = tmp_path / "link.db"
    real.touch()
    link.symlink_to(real)
    assert lock_path(link) == lock_path(real)
    with StoreLock(real), pytest.raises(StoreLocked):
        StoreLock(link).acquire()


def test_a_memory_store_has_nothing_to_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with StoreLock(MEMORY), StoreLock(MEMORY):
        assert list(tmp_path.iterdir()) == []


def test_the_kernel_drops_the_lock_when_the_holder_is_killed(tmp_path):
    path = tmp_path / "walkie.db"
    script = (
        "import sys, time\n"
        "from walkie_database.store_lock import StoreLock\n"
        "lock = StoreLock(sys.argv[1])\n"
        "lock.acquire()\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(StoreLocked):
            StoreLock(path).acquire()
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        holder.stdout.close()
    assert holder.returncode == -signal.SIGKILL

    with StoreLock(path):
        pass


def test_a_child_process_does_not_inherit_the_lock(tmp_path):
    path = tmp_path / "walkie.db"
    # A child that kept the open file would keep the lock after this process died.
    with StoreLock(path) as lock:
        assert os.get_inheritable(lock._fd) is False


def _store_with_wal(path):
    """A closed store whose last commit is only in its WAL, as a crash leaves it."""
    conn = connect(path)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("CREATE TABLE t (value INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    copy = path.with_name("crashed.db")
    copy.write_bytes(path.read_bytes())
    copy.with_name("crashed.db-wal").write_bytes(path.with_name(f"{path.name}-wal").read_bytes())
    conn.close()
    return copy


def test_a_checkpoint_moves_a_stopped_stores_wal_into_its_main_file(tmp_path):
    copy = _store_with_wal(tmp_path / "walkie.db")
    assert b"CREATE TABLE t" not in copy.read_bytes()

    checkpoint_closed_store(copy)

    assert b"CREATE TABLE t" in copy.read_bytes()
    conn = connect(copy, readonly=True)
    assert conn.execute("SELECT value FROM t").fetchone()[0] == 1
    conn.close()


def test_a_checkpoint_of_a_store_with_no_wal_does_nothing(tmp_path):
    path = tmp_path / "walkie.db"
    path.write_bytes(b"not a database")
    checkpoint_closed_store(path)
    assert path.read_bytes() == b"not a database"


def test_install_removes_the_old_side_files_and_replaces_the_store(tmp_path):
    target = real_path(tmp_path / "walkie.db")
    target.write_bytes(b"old")
    target.with_name("walkie.db-wal").write_bytes(b"old wal")
    target.with_name("walkie.db-shm").write_bytes(b"old shm")
    temp = temp_beside(target)
    temp.write_bytes(b"new")

    install_store(temp, target)

    assert target.read_bytes() == b"new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["walkie.db"]


def test_a_temporary_file_is_beside_its_target(tmp_path):
    target = real_path(tmp_path / "walkie.db")
    assert temp_beside(target).parent == target.parent
    assert temp_beside(target) != temp_beside(target)
