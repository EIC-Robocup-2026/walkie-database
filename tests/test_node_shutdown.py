"""Ctrl-C stops the node cleanly, with the object memory's final save, also under pixi.

``pixi run`` forwards SIGINT to its child, and a terminal's Ctrl-C reaches the child
directly too, so one Ctrl-C arrives twice. These tests run the node's real ``main()``
in its own process group through ``shutdown_harness.py`` and signal the group, as a
terminal does.
"""

import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from memory_helpers import memory_store
from seed_helpers import map_id_of, write_map_folder

HARNESS = Path(__file__).with_name("shutdown_harness.py")


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


def start(tmp_path, folder, mode):
    """The harness running the node in memory mode, once it spins. Returns the process and its markers."""
    db = memory_store(tmp_path, folder)
    markers = tmp_path / "markers"
    markers.mkdir()
    env = dict(os.environ, ROS_DOMAIN_ID=str(random.randint(100, 200)))
    process = subprocess.Popen(
        [
            sys.executable, str(HARNESS), str(markers), mode, "--", "--ros-args",
            "-p", "memory_enabled:=true", "-p", f"db_path:={db}",
            "-p", f"map_folder:={folder}", "-p", f"map_id:={map_id_of(folder)}",
            # No timer round before the shutdown: only the final save writes.
            "-p", "merge_period_s:=3600.0", "-p", "confirm_views:=2",
        ],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )  # fmt: skip
    wait_for(process, markers / "spinning")
    return process, markers, db


def wait_for(process, marker, timeout=60.0):
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail(f"the node stopped before {marker.name}: {stderr}")
        if time.monotonic() > deadline:
            process.kill()
            pytest.fail(f"no {marker.name} in {timeout} s")
        time.sleep(0.02)


def ctrl_c(process):
    """SIGINT to the whole process group, as a terminal sends it."""
    os.killpg(process.pid, signal.SIGINT)


def finish(process):
    try:
        _, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        raise
    return process.returncode, stderr


def stored_cups(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT count(*) FROM object WHERE source = 'perception' AND label = 'cup'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_a_second_ctrl_c_does_not_cut_the_final_save_short(tmp_path, folder):
    process, markers, db = start(tmp_path, folder, "save")
    ctrl_c(process)
    wait_for(process, markers / "saving")
    ctrl_c(process)  # The copy that pixi forwards, while the save runs.

    code, stderr = finish(process)
    assert code == 0, stderr
    assert "Traceback" not in stderr
    assert stored_cups(db) == 1


def test_ctrl_c_exits_0_when_the_signal_handler_shut_rclpy_down_first(tmp_path, folder):
    process, _, _ = start(tmp_path, folder, "race")
    ctrl_c(process)

    code, stderr = finish(process)
    assert code == 0, stderr
    assert "Traceback" not in stderr
