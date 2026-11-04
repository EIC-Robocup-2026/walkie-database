"""The node's start-up: the store lock, the map gate, the refresh and the clock."""

import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from builtin_interfaces.msg import Time
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rosgraph_msgs.msg import Clock
from seed_helpers import (
    anchor,
    anchors_document,
    bare_anchor,
    digest_of,
    map_id_of,
    seed_argv,
    write_manifest,
    write_map_folder,
)
from walkie_database_msgs.msg import ModelVector
from walkie_database_msgs.srv import FindObjects, RememberPerson, StartSession

from walkie_database import seed_store
from walkie_database.db import connect, transaction
from walkie_database.map_config import MapGateError
from walkie_database.node import DatabaseNode
from walkie_database.restore import restore
from walkie_database.store_lock import StoreLock, StoreLocked, lock_path

ROOT = Path(__file__).resolve().parents[1]
SIM_TIME = 1234.5


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


def make_store(tmp_path, folder, clock_kind="wall", *, anchors=None, name="run/walkie.db"):
    """A restored store, as a node finds it. Returns its path."""
    seed = tmp_path / "seeds" / f"{clock_kind}-{folder.name}.db"
    if not seed.exists():
        argv = seed_argv(tmp_path, folder, seed, clock_kind=clock_kind, anchors=anchors)
        assert seed_store.main(argv) == 0
    db = tmp_path / name
    db.parent.mkdir(exist_ok=True)
    restore(seed, db, folder, clock_kind)
    return db


def memory_node(db, folder, *, map_id=None, sim=False, **extra):
    overrides = [
        Parameter("memory_enabled", value=True),
        Parameter("db_path", value=str(db)),
        Parameter("map_folder", value=str(folder)),
        Parameter("map_id", value=map_id_of(folder) if map_id is None else map_id),
    ]
    if sim:
        overrides.append(Parameter("use_sim_time", value=True))
    overrides.extend(Parameter(name, value=value) for name, value in extra.items())
    return DatabaseNode(parameter_overrides=overrides)


def scalar(path, sql):
    conn = connect(path, readonly=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def anchor_times(path):
    conn = connect(path, readonly=True)
    try:
        return {
            r["key"]: (r["first_seen"], r["last_seen"], r["last_verified"], r["revision"])
            for r in conn.execute("SELECT * FROM object")
        }
    finally:
        conn.close()


def assert_refused(db, folder, message, **kwargs):
    """The gate refuses, the main file stays as it was, and the lock is free again."""
    before = db.read_bytes() if db.exists() else None
    with pytest.raises(MapGateError, match=message):
        memory_node(db, folder, **kwargs)
    assert (db.read_bytes() if db.exists() else None) == before
    with StoreLock(db):
        pass


def start(node):
    return node.start_session(StartSession.Request(label="", resume=False), StartSession.Response())


def started_at(response):
    return response.started_at.sec + response.started_at.nanosec * 1e-9


# Passing the gate -----------------------------------------------------------------


def test_a_matching_store_starts_and_serves(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    node = memory_node(db, folder)
    try:
        assert ("/walkie_database/find_objects", FindObjects) in {
            (s.service_name, s.srv_type) for s in node.services
        }
        assert start(node).success
    finally:
        node.destroy_node()


def test_a_folder_with_a_manifest_that_matches_passes(ros, tmp_path):
    folder = write_map_folder(tmp_path / "map", baseline_rev="r1")
    db = make_store(tmp_path, folder, anchors=anchors_document(map_id_of(folder), baseline_rev="r1"))
    memory_node(db, folder).destroy_node()


def test_empty_baselines_on_both_sides_are_valid(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    assert scalar(db, "SELECT baseline_rev FROM map") == ""
    memory_node(db, folder).destroy_node()


# Refusals -------------------------------------------------------------------------


def test_another_map_id_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    assert_refused(db, folder, "holds the map", map_id="other-2026-01-01-00000000")


def test_a_map_id_that_differs_only_in_its_date_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    wrong = map_id_of(folder).replace("2026-10-16", "2026-10-17")
    assert_refused(db, folder, "holds the map", map_id=wrong)


def test_another_full_digest_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    other = write_map_folder(tmp_path / "other_map", pixel=7)
    assert_refused(db, other, "digest", map_id=map_id_of(folder))


def test_a_digest_that_shares_the_ids_8_characters_is_still_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    conn = connect(db)
    with transaction(conn):
        conn.execute("UPDATE map SET map_hash = ?", (digest_of(folder)[:8] + "f" * 56,))
    conn.close()
    assert_refused(db, folder, "digest")


def test_a_blank_hash_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    conn = connect(db)
    with transaction(conn):
        conn.execute("UPDATE map SET map_hash = ''")
    conn.close()
    assert_refused(db, folder, "blank hash")


def test_a_wall_store_with_use_sim_time_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder, "wall")
    assert_refused(db, folder, "clock", sim=True)


def test_a_sim_store_without_use_sim_time_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder, "ros_sim")
    assert_refused(db, folder, "clock")


def test_a_clock_mismatch_in_only_one_row_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder, "wall")
    conn = connect(db)
    with transaction(conn):
        conn.execute("UPDATE map SET clock_kind = 'ros_sim'")
    conn.close()
    assert_refused(db, folder, "clock")


def test_a_missing_store_is_not_created(ros, tmp_path, folder):
    db = tmp_path / "run" / "walkie.db"
    with pytest.raises(MapGateError, match="never creates"):
        memory_node(db, folder)
    assert not db.parent.exists()


def test_a_missing_map_folder_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    missing = tmp_path / "no_such_folder"
    assert_refused(db, missing, "cannot be used", map_id=map_id_of(folder))
    assert not missing.exists()


def test_a_store_with_no_registration_is_refused(ros, tmp_path, folder):
    db = tmp_path / "people.db"
    legacy = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(db))])
    legacy.destroy_node()
    assert_refused(db, folder, "no store_meta row")


def test_a_file_that_is_not_a_store_is_refused(ros, tmp_path, folder):
    db = tmp_path / "junk.db"
    db.write_bytes(b"not a database" * 1000)
    assert_refused(db, folder, "cannot be read")


@pytest.mark.parametrize("missing", ["db_path", "map_folder", "map_id"])
def test_every_required_parameter_is_required(ros, tmp_path, folder, missing):
    db = make_store(tmp_path, folder)
    values = {"db_path": str(db), "map_folder": str(folder), "map_id": map_id_of(folder)}
    del values[missing]
    overrides = [Parameter("memory_enabled", value=True)]
    overrides.extend(Parameter(name, value=value) for name, value in values.items())
    with pytest.raises(MapGateError, match=missing):
        DatabaseNode(parameter_overrides=overrides)


def test_memory_mode_has_no_default_store(ros, tmp_path, folder, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(MapGateError, match="db_path"):
        DatabaseNode(
            parameter_overrides=[
                Parameter("memory_enabled", value=True),
                Parameter("map_folder", value=str(folder)),
                Parameter("map_id", value=map_id_of(folder)),
            ]
        )
    assert not (tmp_path / "data").exists()


def test_a_store_baseline_with_no_manifest_is_refused(ros, tmp_path):
    with_manifest = write_map_folder(tmp_path / "map", baseline_rev="r1")
    db = make_store(tmp_path, with_manifest, anchors=anchors_document(map_id_of(with_manifest), baseline_rev="r1"))
    (with_manifest / "manifest.yaml").unlink()
    assert_refused(db, with_manifest, "no manifest.yaml")


def test_a_manifest_baseline_with_an_empty_store_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    write_manifest(folder, "r1")
    assert_refused(db, folder, "baseline")


def test_a_different_baseline_is_refused(ros, tmp_path):
    folder = write_map_folder(tmp_path / "map", baseline_rev="r1")
    db = make_store(tmp_path, folder, anchors=anchors_document(map_id_of(folder), baseline_rev="r1"))
    write_manifest(folder, "r2")
    assert_refused(db, folder, "baseline")


def test_a_manifest_that_disagrees_with_the_folder_is_refused(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    write_manifest(folder, "", map_hash="b" * 64)
    assert_refused(db, folder, "records the map hash")


def test_a_refused_store_with_a_crash_left_wal_stays_as_it_was(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    conn = connect(db)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    with transaction(conn):
        conn.execute("UPDATE last_id SET id = 900 WHERE table_name = 'object'")
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    (crashed / "walkie.db").write_bytes(db.read_bytes())
    (crashed / "walkie.db-wal").write_bytes(Path(f"{db}-wal").read_bytes())
    conn.close()
    target = crashed / "walkie.db"
    before = target.read_bytes()

    with pytest.raises(MapGateError, match="holds the map"):
        memory_node(target, folder, map_id="other-2026-01-01-00000000")

    assert target.read_bytes() == before
    # The committed counter is still recoverable.
    assert scalar(target, "SELECT id FROM last_id WHERE table_name = 'object'") == 900


# The lock -------------------------------------------------------------------------


def test_a_node_holds_the_lock_for_its_whole_life(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    node = memory_node(db, folder)
    try:
        with pytest.raises(StoreLocked):
            StoreLock(db).acquire()
    finally:
        node.destroy_node()
    with StoreLock(db):
        pass


def test_a_second_node_refuses(ros, tmp_path):
    path = tmp_path / "walkie.db"
    first = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(path))])
    try:
        with pytest.raises(StoreLocked):
            DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(path))])
    finally:
        first.destroy_node()


def test_the_tools_refuse_while_a_node_runs(ros, tmp_path, folder, capsys):
    db = make_store(tmp_path, folder)
    before_seed = tmp_path / "seeds" / "wall-map.db"
    node = memory_node(db, folder)
    try:
        with pytest.raises(StoreLocked):
            restore(before_seed, db, folder, "wall")
        argv = seed_argv(tmp_path, folder, db, extra=("--replace", "--previous-seed", str(before_seed)))
        assert seed_store.main(argv) == 1
        assert "in use" in capsys.readouterr().err
    finally:
        node.destroy_node()


def test_a_killed_node_process_leaves_the_store_usable(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    env = dict(os.environ, ROS_DOMAIN_ID=str(random.randint(100, 200)))
    process = subprocess.Popen(
        [
            sys.executable, "-m", "walkie_database.node", "--ros-args",
            "-p", "memory_enabled:=true", "-p", f"db_path:={db}",
            "-p", f"map_folder:={folder}", "-p", f"map_id:={map_id_of(folder)}",
        ],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert process.poll() is None, "the node stopped by itself"
            if scalar(db, "SELECT refresh_anchors_pending FROM store_meta") == 0:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the node did not stamp its anchors in time")
        with pytest.raises(StoreLocked):
            StoreLock(db).acquire()
    finally:
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)

    with StoreLock(db):
        pass
    # Restart the same store: a new start after a crash.
    node = memory_node(db, folder)
    node.destroy_node()


@pytest.mark.parametrize(
    "make_args, code",
    [
        (lambda db, folder: ["-p", f"map_id:={map_id_of(folder)}x"], 1),
        (lambda db, folder: ["-p", f"map_id:={map_id_of(folder)}", "-p", "use_sim_time:=true"], 1),
    ],
)
def test_the_node_process_exits_with_a_message_on_a_mismatch(tmp_path, folder, make_args, code):
    db = make_store(tmp_path, folder)
    before = db.read_bytes()
    env = dict(os.environ, ROS_DOMAIN_ID=str(random.randint(100, 200)))

    result = subprocess.run(
        [
            sys.executable, "-m", "walkie_database.node", "--ros-args",
            "-p", "memory_enabled:=true", "-p", f"db_path:={db}", "-p", f"map_folder:={folder}",
            *make_args(db, folder),
        ],
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip

    assert result.returncode == code, result.stderr
    assert "Not starting" in result.stderr
    assert "Traceback" not in result.stderr
    assert db.read_bytes() == before


def test_db_memory_is_the_documented_task():
    tasks = tomllib.loads((ROOT / "pixi.toml").read_text())["tasks"]
    assert tasks["db-memory"]["cmd"] == "python -m walkie_database.node --ros-args -p memory_enabled:=true"
    assert tasks["db-node"]["cmd"] == "python -m walkie_database.node"


# Read-only parameters -------------------------------------------------------------


@pytest.mark.parametrize(
    "name, value", [("db_path", "x.db"), ("map_folder", "elsewhere"), ("map_id", "m"), ("memory_enabled", False)]
)
def test_the_gates_parameters_are_read_only_in_memory_mode(ros, tmp_path, folder, name, value):
    db = make_store(tmp_path, folder)
    node = memory_node(db, folder)
    try:
        result = node.set_parameters([Parameter(name, value=value)])[0]
        assert not result.successful
    finally:
        node.destroy_node()


def test_use_sim_time_cannot_change_while_a_node_runs(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    node = memory_node(db, folder)
    try:
        result = node.set_parameters([Parameter("use_sim_time", value=True)])[0]
        assert not result.successful
        assert not node.get_parameter("use_sim_time").value
        assert start(node).success
    finally:
        node.destroy_node()


def test_use_sim_time_cannot_change_without_memory_mode_either(node):
    result = node.set_parameters([Parameter("use_sim_time", value=True)])[0]
    assert not result.successful


# The store ID and the refresh -----------------------------------------------------


def test_every_start_gets_a_new_store_id(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    seen = {scalar(db, "SELECT store_id FROM store_meta")}
    for _ in range(2):
        memory_node(db, folder).destroy_node()
        seen.add(scalar(db, "SELECT store_id FROM store_meta"))
    assert len(seen) == 3


def test_the_sequence_starts_at_zero_on_every_start(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    node = memory_node(db, folder)
    try:
        assert node.publication_sequence == 0
    finally:
        node.destroy_node()


def test_the_refresh_stamps_anchors_once(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    assert scalar(db, "SELECT refresh_anchors_pending FROM store_meta") == 1
    assert anchor_times(db)["table"][:3] == (0.0, 0.0, None)

    before = time.time()
    memory_node(db, folder).destroy_node()
    after = time.time()

    assert scalar(db, "SELECT refresh_anchors_pending FROM store_meta") == 0
    times = anchor_times(db)
    for key in ("table", "wardrobe"):
        first_seen, last_seen, last_verified, _ = times[key]
        assert before <= first_seen <= after
        assert first_seen == last_seen == last_verified


def test_the_refresh_leaves_the_revision_alone(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    before = anchor_times(db)["table"][3]
    memory_node(db, folder).destroy_node()
    assert anchor_times(db)["table"][3] == before


def test_a_later_start_leaves_the_times_alone(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    memory_node(db, folder).destroy_node()
    stamped = anchor_times(db)

    memory_node(db, folder).destroy_node()

    assert anchor_times(db) == stamped


def test_a_tombstone_is_not_stamped(ros, tmp_path, folder):
    map_id = map_id_of(folder)
    first = tmp_path / "seeds" / "first.db"
    assert seed_store.main(seed_argv(tmp_path, folder, first)) == 0
    second = tmp_path / "seeds" / "wall-map.db"
    argv = seed_argv(
        tmp_path, folder, second, anchors=anchors_document(map_id, anchors=[anchor()]),
        extra=("--previous-seed", str(first)),
    )  # fmt: skip
    assert seed_store.main(argv) == 0
    db = make_store(tmp_path, folder)

    memory_node(db, folder).destroy_node()

    times = anchor_times(db)
    assert times["wardrobe"][:2] == (0.0, 0.0)
    assert times["table"][0] > 0


def test_a_new_restore_sets_the_refresh_again(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    memory_node(db, folder).destroy_node()
    stamped = anchor_times(db)["table"][0]
    make_store(tmp_path, folder)

    assert scalar(db, "SELECT refresh_anchors_pending FROM store_meta") == 1
    memory_node(db, folder).destroy_node()
    assert anchor_times(db)["table"][0] >= stamped


# The sim clock --------------------------------------------------------------------


class ClockSource:
    """A real /clock publisher, and a spin that also runs the node under test."""

    def __init__(self, node):
        self.helper = Node("fake_sim_clock")
        self.publisher = self.helper.create_publisher(
            Clock, "/clock", QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        )
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.helper)
        self.executor.add_node(node)

    def publish(self, seconds):
        sec = int(seconds)
        self.publisher.publish(Clock(clock=Time(sec=sec, nanosec=int((seconds - sec) * 1e9))))

    def publish_until(self, seconds, done, timeout=30.0):
        """Keep publishing, because a first sample may go out before discovery ends."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.publish(seconds)
            self.executor.spin_once(timeout_sec=0.05)
            if done():
                return
        pytest.fail("the node did not react to /clock in time")

    def close(self):
        self.executor.shutdown()
        self.helper.destroy_node()


@pytest.fixture
def sim_store(tmp_path, folder):
    return make_store(tmp_path, folder, "ros_sim")


def test_a_sim_store_waits_for_the_first_clock_sample(ros, folder, sim_store):
    node = memory_node(sim_store, folder, sim=True)
    source = ClockSource(node)
    try:
        for _ in range(5):
            source.executor.spin_once(timeout_sec=0.01)
        assert scalar(sim_store, "SELECT refresh_anchors_pending FROM store_meta") == 1

        source.publish_until(SIM_TIME, lambda: not node._waiting_for_clock)

        assert scalar(sim_store, "SELECT refresh_anchors_pending FROM store_meta") == 0
        assert anchor_times(sim_store)["table"][:3] == (SIM_TIME, SIM_TIME, SIM_TIME)
    finally:
        source.close()
        node.destroy_node()


def test_a_sample_at_time_zero_counts(ros, folder, sim_store):
    node = memory_node(sim_store, folder, sim=True)
    source = ClockSource(node)
    try:
        source.publish_until(0.0, lambda: not node._waiting_for_clock)

        assert scalar(sim_store, "SELECT refresh_anchors_pending FROM store_meta") == 0
        assert anchor_times(sim_store)["table"][:3] == (0.0, 0.0, 0.0)
        assert started_at(start(node)) == 0.0
    finally:
        source.close()
        node.destroy_node()


def test_a_failed_stamp_is_tried_again_on_the_next_sample(ros, folder, sim_store):
    node = memory_node(sim_store, folder, sim=True)
    real = node._stamp_seeded_anchors
    calls = []

    def fail_once(now):
        calls.append(now)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(now)

    node._stamp_seeded_anchors = fail_once
    source = ClockSource(node)
    try:
        source.publish_until(SIM_TIME, lambda: not node._waiting_for_clock)

        assert len(calls) == 2
        assert scalar(sim_store, "SELECT refresh_anchors_pending FROM store_meta") == 0
    finally:
        source.close()
        node.destroy_node()


def test_the_people_services_refuse_before_the_first_sample(ros, folder, sim_store):
    node = memory_node(sim_store, folder, sim=True)
    try:
        response = start(node)
        assert not response.success
        assert "no /clock sample" in response.message

        response = node.remember_person(RememberPerson.Request(name="Sam"), RememberPerson.Response())
        assert not response.success
        assert "no /clock sample" in response.message
        assert scalar(sim_store, "SELECT count(*) FROM session") == 0
    finally:
        node.destroy_node()


def test_people_timestamps_follow_the_sim_clock(ros, folder, sim_store):
    node = memory_node(sim_store, folder, sim=True)
    source = ClockSource(node)
    try:
        source.publish_until(SIM_TIME, lambda: not node._waiting_for_clock)
        response = start(node)
        assert response.success
        assert started_at(response) == pytest.approx(SIM_TIME, abs=1.0)

        later = SIM_TIME + 100.0
        source.publish_until(later, lambda: node.get_clock().now().nanoseconds / 1e9 == later)
        person = node.remember_person(
            RememberPerson.Request(name="Sam", faces=[_face()]), RememberPerson.Response()
        )
        assert person.success, person.message
        conn = connect(sim_store, readonly=True)
        row = conn.execute("SELECT first_seen, last_seen FROM person").fetchone()
        conn.close()
        assert tuple(row) == (later, later)
    finally:
        source.close()
        node.destroy_node()


def test_a_later_sim_start_keeps_the_times_and_still_waits(ros, folder, sim_store):
    node = memory_node(sim_store, folder, sim=True)
    source = ClockSource(node)
    try:
        source.publish_until(SIM_TIME, lambda: not node._waiting_for_clock)
    finally:
        source.close()
        node.destroy_node()
    stamped = anchor_times(sim_store)

    node = memory_node(sim_store, folder, sim=True)
    source = ClockSource(node)
    try:
        assert not start(node).success
        source.publish_until(SIM_TIME + 50.0, lambda: not node._waiting_for_clock)
        assert anchor_times(sim_store) == stamped
        assert start(node).success
    finally:
        source.close()
        node.destroy_node()


def _face():
    import numpy as np

    values = np.zeros(512, dtype=np.float32)
    values[0] = 1.0
    return ModelVector(model="arcface_w600k_r50", embedding=values.tolist())


# Without memory mode ---------------------------------------------------------------


def test_the_default_node_needs_no_map_and_creates_its_store(ros, tmp_path):
    path = tmp_path / "new" / "walkie.db"
    node = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(path))])
    try:
        assert path.exists()
        assert lock_path(path).exists()
        assert not node.memory_enabled
        assert start(node).success
    finally:
        node.destroy_node()


def test_the_default_node_keeps_its_old_default_path(ros, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    node = DatabaseNode()
    try:
        assert node.db_path == "data/walkie.db"
        assert (tmp_path / "data" / "walkie.db").exists()
    finally:
        node.destroy_node()


def test_the_default_node_can_set_its_map_id_while_it_runs(node):
    assert node.set_parameters([Parameter("map_id", value="m")])[0].successful
    assert node.get_parameter("map_id").value == "m"


def test_the_default_node_leaves_a_seeded_stores_times_alone(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    before = anchor_times(db)
    node = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(db))])
    node.destroy_node()
    assert anchor_times(db) == before
    assert scalar(db, "SELECT refresh_anchors_pending FROM store_meta") == 1


def test_the_default_node_refreshes_a_store_id_when_there_is_one(ros, tmp_path, folder):
    db = make_store(tmp_path, folder)
    before = scalar(db, "SELECT store_id FROM store_meta")
    DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(db))]).destroy_node()
    assert scalar(db, "SELECT store_id FROM store_meta") != before


# What the old FindObjects can see --------------------------------------------------


def find_keys(node, map_id):
    response = node.find_objects(
        FindObjects.Request(map_id=map_id, label="", max_results=0), FindObjects.Response()
    )
    assert response.success, response.message
    return sorted(result.label for result in response.objects)


def test_find_objects_shows_a_measured_anchor_and_hides_what_has_no_place(ros, tmp_path, folder):
    map_id = map_id_of(folder)
    first = tmp_path / "seeds" / "first.db"
    assert seed_store.main(
        seed_argv(tmp_path, folder, first, anchors=anchors_document(map_id, anchors=[anchor(), anchor("desk", surfaces=[]), bare_anchor()]))
    ) == 0  # fmt: skip
    second = tmp_path / "seeds" / "wall-map.db"
    assert seed_store.main(
        seed_argv(
            tmp_path, folder, second,
            anchors=anchors_document(map_id, anchors=[anchor(), bare_anchor()]),
            extra=("--previous-seed", str(first)),
        )  # fmt: skip
    ) == 0
    db = make_store(tmp_path, folder)
    node = memory_node(db, folder)
    try:
        # table has a box. wardrobe has none. desk is a tombstone.
        assert find_keys(node, map_id) == ["table"]
    finally:
        node.destroy_node()
