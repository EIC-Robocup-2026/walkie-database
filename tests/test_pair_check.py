"""tools/pair_check.py and tools/contract_peer.py, without a second repository.

These tests run no build, no pixi, no model and no router. The gate itself, with
both worktrees and both environments, is run by hand (docs/setup/14-pair-check.md).
Here its parts are checked on throwaway Git repositories, small inventories,
host processes and the tracked seed fixtures in tests/fixtures/pair.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from rclpy.parameter import Parameter
from walkie_database_msgs.msg import ObjectQuery
from walkie_database_msgs.srv import GetObject, GetRoomContents, ListRooms, SearchObjects
from walkie_map_id import make_map_id

from walkie_database import seed_store
from walkie_database.map_config import digest_map_files
from walkie_database.node import DatabaseNode
from walkie_database.restore import restore

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "pair"
GIT = shutil.which("git") or shutil.which("git", path="/usr/bin:/bin")


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"tools_{name}", REPO / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pair = load("pair_check")
peer = load("contract_peer")


def git(*args: str, cwd: Path) -> str:
    # GIT_CONFIG_GLOBAL keeps a developer's own settings out of these repositories.
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run([GIT, *args], cwd=cwd, env=env, text=True, capture_output=True, check=True).stdout.strip()


def make_repo(path: Path) -> str:
    path.mkdir(parents=True)
    git("init", "--quiet", "--initial-branch=main", cwd=path)
    (path / "README.md").write_text("A test repository.\n")
    git("add", "README.md", cwd=path)
    git("commit", "--quiet", "-m", "start", cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


# Both tools run under the host's python3 or as copied scripts, so they import
# only the standard library at the top.


@pytest.mark.parametrize("name", ["pair_check", "contract_peer"])
def test_the_tools_import_only_the_standard_library_at_the_top(name):
    tree = ast.parse((REPO / "tools" / f"{name}.py").read_text())
    roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split(".")[0])
    assert roots - set(sys.stdlib_module_names) - {"__future__"} == set()


# Sources -----------------------------------------------------------------------


def test_a_clean_worktree_at_its_commit_has_no_problems(tmp_path):
    commit = make_repo(tmp_path / "repo")
    assert pair.source_problems(tmp_path / "repo", commit) == []


def test_a_short_or_other_commit_is_refused(tmp_path):
    commit = make_repo(tmp_path / "repo")
    assert "not a full" in pair.source_problems(tmp_path / "repo", commit[:12])[0]
    other = "0" * 40
    assert f"not {other}" in pair.source_problems(tmp_path / "repo", other)[0]


@pytest.mark.parametrize("change", ["modified", "untracked"])
def test_a_dirty_worktree_is_refused(tmp_path, change):
    repo = tmp_path / "repo"
    commit = make_repo(repo)
    (repo / ("README.md" if change == "modified" else "new.txt")).write_text("changed\n")
    [problem] = pair.source_problems(repo, commit)
    assert "changed or untracked" in problem


def test_an_ignored_file_does_not_make_a_worktree_dirty(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text("build/\n")
    git("add", ".gitignore", cwd=repo)
    git("commit", "--quiet", "-m", "ignore builds", cwd=repo)
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("x")
    assert pair.source_problems(repo, git("rev-parse", "HEAD", cwd=repo)) == []


def test_a_dirty_database_stops_the_gate_before_any_process(tmp_path, capsys):
    vision_commit = make_repo(tmp_path / "vision")
    database_commit = make_repo(tmp_path / "database")
    (tmp_path / "database" / "stray.txt").write_text("not committed\n")
    report = tmp_path / "reports" / "pair.json"

    code = pair.main([
        "--vision-root", str(tmp_path / "vision"), "--database-root", str(tmp_path / "database"),
        "--vision-commit", vision_commit, "--database-commit", database_commit, "--domain", "78",
        "--build", "--report", str(report), "--pixi", str(tmp_path / "no-pixi"),
    ])  # fmt: skip

    assert code == 1
    data = json.loads(report.read_text())
    statuses = {c["name"]: c["status"] for c in data["checks"]}
    assert statuses["sources"] == "fail"
    assert statuses["processes"] == "pass" and statuses["teardown"] == "pass"
    assert {statuses[name] for name in pair.PLANNED if name not in ("sources", "processes", "teardown")} == {"skip"}
    assert data["processes"] == []
    assert data["passed"] is False
    assert "FAILED" in capsys.readouterr().out


def test_the_run_folder_is_only_replaced_when_the_tool_made_it(tmp_path):
    mine = pair.fresh_dir(tmp_path / "run")
    (mine / "old.json").write_text("{}")
    assert not (pair.fresh_dir(tmp_path / "run") / "old.json").exists()
    (tmp_path / "theirs").mkdir()
    with pytest.raises(pair.GateError, match="did not make it"):
        pair.fresh_dir(tmp_path / "theirs")


# Type hash inventories -----------------------------------------------------------


def write_description(share: Path, name: str, hashes: dict[str, str]) -> None:
    path = share / name
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"type_name": t, "hash_string": h} for t, h in hashes.items()]
    path.write_text(json.dumps({"type_description_msg": {}, "type_hashes": entries}))


def test_an_inventory_reads_every_nested_hash(tmp_path):
    write_description(tmp_path, "srv/Find.json", {"pkg/srv/Find": "RIHS01_aa", "pkg/msg/Part": "RIHS01_bb"})
    write_description(tmp_path, "msg/Part.json", {"pkg/msg/Part": "RIHS01_bb"})
    assert pair.read_inventory(tmp_path) == {
        "msg/Part.json": {"pkg/msg/Part": "RIHS01_bb"},
        "srv/Find.json": {"pkg/srv/Find": "RIHS01_aa", "pkg/msg/Part": "RIHS01_bb"},
    }


def test_an_inventory_refuses_a_file_without_hashes_or_with_two_for_one_type(tmp_path):
    (tmp_path / "msg").mkdir()
    (tmp_path / "msg" / "Bad.json").write_text("{}")
    with pytest.raises(pair.GateError, match="no readable type_hashes"):
        pair.read_inventory(tmp_path)
    write_description(tmp_path, "msg/Bad.json", {"pkg/msg/Bad": "RIHS01_aa"})
    (tmp_path / "msg" / "Bad.json").write_text(json.dumps({"type_hashes": [
        {"type_name": "pkg/msg/Bad", "hash_string": "RIHS01_aa"},
        {"type_name": "pkg/msg/Bad", "hash_string": "RIHS01_ab"},
    ]}))  # fmt: skip
    with pytest.raises(pair.GateError, match="two hashes"):
        pair.read_inventory(tmp_path)


INVENTORY = {
    "msg/Part.json": {"pkg/msg/Part": "RIHS01_bb"},
    "srv/Find.json": {"pkg/srv/Find": "RIHS01_aa", "pkg/msg/Part": "RIHS01_bb"},
}
EXPECTED = {"pkg": {"msg/Part.json", "srv/Find.json"}}


def test_equal_inventories_agree():
    inventories = {"a": ("pkg", INVENTORY), "b": ("pkg", json.loads(json.dumps(INVENTORY)))}
    assert pair.inventory_problems(EXPECTED, inventories, [("a", "b")]) == []


def test_an_altered_nested_hash_is_named():
    inventories = {"a": ("pkg", INVENTORY), "b": ("pkg", pair.altered(INVENTORY, "srv/Find.json", "pkg/msg/Part"))}
    problems = pair.inventory_problems(EXPECTED, inventories, [("a", "b")])
    assert any("srv/Find.json differs" in p and "pkg/msg/Part" in p for p in problems)
    assert any(p.startswith("pkg/msg/Part has 2 hashes") for p in problems)


def test_one_type_has_one_hash_across_unpaired_inventories():
    other = {"msg/Part.json": {"pkg/msg/Part": "RIHS01_cc"}, "srv/Find.json": INVENTORY["srv/Find.json"]}
    [problem] = pair.inventory_problems(EXPECTED, {"a": ("pkg", INVENTORY), "c": ("pkg", other)}, [])
    assert problem.startswith("pkg/msg/Part has 2 hashes")
    assert "c:msg/Part.json" in problem


def test_a_missing_or_extra_description_is_named():
    fewer = {"msg/Part.json": INVENTORY["msg/Part.json"]}
    more = {**INVENTORY, "msg/Extra.json": {"pkg/msg/Extra": "RIHS01_ee"}}
    problems = pair.inventory_problems(EXPECTED, {"a": ("pkg", fewer), "b": ("pkg", more)}, [])
    assert "a lacks ['srv/Find.json']" in problems
    assert "b has files its source does not define: ['msg/Extra.json']" in problems


def test_the_installed_database_types_agree_with_their_source_and_their_vision_types():
    install = REPO / "ros2_ws" / "install"
    share = lambda package: install / package / "share" / package  # noqa: E731
    expected = {
        package: pair.interface_files(REPO / "ros2_ws" / "src" / folder / package)
        for package, folder in (("walkie_database_msgs", ""), ("walkie_vision_msgs", "walkie-vision/ros2_ws/src"))
    }
    database = pair.read_inventory(share("walkie_database_msgs"))
    vision = pair.read_inventory(share("walkie_vision_msgs"))
    inventories = {"database": ("walkie_database_msgs", database), "vision": ("walkie_vision_msgs", vision)}

    assert pair.inventory_problems(expected, inventories, []) == []
    nested = database["srv/LocalizeObjects.json"]
    assert nested["walkie_vision_msgs/msg/ObjectDetectionArray"] == vision["msg/ObjectDetectionArray.json"][
        "walkie_vision_msgs/msg/ObjectDetectionArray"
    ]
    # The gate's own control alters a hash that the inventories really hold.
    label, name, type_name = pair.Gate.CONTROL
    assert label == "database:walkie_database_msgs" and type_name in database[name]


# Import evidence -----------------------------------------------------------------


def good_evidence(root: Path) -> dict:
    site = root / ".pixi" / "envs" / "default" / "lib" / "python3.12" / "site-packages"
    msgs = root / "ros2_ws" / "install" / "walkie_vision_msgs" / "lib" / "python3.12" / "site-packages"
    return {
        "executable": str(root / ".pixi" / "envs" / "default" / "bin" / "python"),
        "env": {"PIXI_PROJECT_ROOT": str(root), **pair.MODEL_FREE_ENV, "ROS_DOMAIN_ID": "78",
                "PYTHONPATH": f"{msgs}:{site}"},
        "affinity": [0, 1],
        "modules": {
            "rclpy": {"file": str(site / "rclpy" / "__init__.py"), "real": str(site / "rclpy" / "__init__.py")},
            "walkie_vision_msgs": {"file": str(msgs / "walkie_vision_msgs" / "__init__.py"),
                                   "real": str(root / "ros2_ws" / "build" / "walkie_vision_msgs" / "__init__.py")},
        },
        "import_errors": {},
        "typesupport": True,
        "libraries": [str(root / "ros2_ws" / "install" / "walkie_vision_msgs" / "lib" / "libwalkie_vision_msgs__rosidl_typesupport_c.so")],
        "sys_path": [str(msgs), str(site)],
        "module_files": [str(site / "rclpy" / "__init__.py")],
        "model_modules": [],
    }  # fmt: skip


def problems_of(evidence: dict, root: Path, other: Path) -> list[str]:
    return pair.path_problems(
        evidence,
        project_root=root,
        module_roots={"rclpy": root / ".pixi", "walkie_vision_msgs": root / "ros2_ws",
                      "walkie_vision_msgs.so": root / "ros2_ws"},
        banned=[other],
        env={**pair.MODEL_FREE_ENV, "ROS_DOMAIN_ID": "78"},
        affinity=[0, 1],
    )  # fmt: skip


def test_evidence_from_the_peers_own_builds_has_no_problems(tmp_path):
    root = tmp_path / "vision"
    assert problems_of(good_evidence(root), root, tmp_path / "database") == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda e, o: e.update(executable="/usr/bin/python3"), "sys.executable"),
        (lambda e, o: e["env"].update(PIXI_PROJECT_ROOT=str(o)), "PIXI_PROJECT_ROOT"),
        (lambda e, o: e["env"].update(RMW_IMPLEMENTATION="rmw_zenoh_cpp"), "RMW_IMPLEMENTATION"),
        (lambda e, o: e["env"].update(CUDA_VISIBLE_DEVICES="0"), "CUDA_VISIBLE_DEVICES"),
        (lambda e, o: e.update(affinity=[0, 1, 2]), "CPU affinity"),
        (lambda e, o: e["import_errors"].update(walkie_vision_msgs="ImportError: no"), "did not import"),
        (lambda e, o: e["modules"].pop("walkie_vision_msgs"), "was not imported"),
        (lambda e, o: e["modules"]["walkie_vision_msgs"].update(real=str(o / "ros2_ws" / "build" / "x.py")), "real"),
        (lambda e, o: e["libraries"].append(str(o / "lib" / "libwalkie_vision_msgs__rosidl_generator_c.so")), "library"),
        (lambda e, o: e.update(typesupport="AttributeError: missing"), "type support"),
        (lambda e, o: e["sys_path"].append(str(o / "ros2_ws" / "install")), "sys.path paths are under"),
        (lambda e, o: e["env"].update(PYTHONPATH=str(o / "site-packages")), "PYTHONPATH paths are under"),
        (lambda e, o: e["module_files"].append(str(o / "src" / "walkie_database" / "node.py")), "module paths are under"),
        (lambda e, o: e.update(model_modules=["torch"]), "the model library torch"),
    ],
)  # fmt: skip
def test_each_kind_of_leak_is_named(tmp_path, change, expected):
    root, other = tmp_path / "vision", tmp_path / "database"
    evidence = good_evidence(root)
    change(evidence, other)
    problems = problems_of(evidence, root, other)
    assert any(expected in p for p in problems), problems


def test_a_shared_package_cache_path_is_not_a_leak(tmp_path):
    root = tmp_path / "vision"
    evidence = good_evidence(root)
    evidence["module_files"].append(str(Path.home() / ".cache" / "rattler" / "cache" / "pkgs" / "numpy" / "__init__.py"))
    assert problems_of(evidence, root, tmp_path / "database") == []


# Processes -----------------------------------------------------------------------

CHILD = """
import os, signal, sys, time
ready, ignore, fork = sys.argv[1], sys.argv[2], sys.argv[3] == "fork"
if fork and os.fork() == 0:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    open(ready + ".grandchild", "w").close()
    time.sleep(60)
    os._exit(0)
for name in filter(None, ignore.split(",")):
    signal.signal(getattr(signal, name), signal.SIG_IGN)
open(ready, "w").close()
time.sleep(60)
"""


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(pair, "TERM_GRACE_S", 1.0)
    monkeypatch.setattr(pair, "KILL_GRACE_S", 2.0)
    (tmp_path / "logs").mkdir()
    runner = pair.Runner(tmp_path / "logs", env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    yield runner
    for child in runner.children:
        if child.popen and not child.finished():
            os.killpg(child.pid, signal.SIGKILL)


def start(runner, tmp_path, name, ignore="", fork=False):
    ready = tmp_path / f"{name}.ready"
    child = runner.start(name, [sys.executable, "-c", CHILD, str(ready), ignore, "fork" if fork else "-"], tmp_path)
    deadline = time.monotonic() + 20
    while not (ready.exists() and (not fork or Path(f"{ready}.grandchild").exists())):
        assert time.monotonic() < deadline, child.stderr_path.read_text()
        time.sleep(0.02)
    return child


@pytest.mark.parametrize(
    ("ignore", "fork", "stopped_by"),
    [
        ("", False, "SIGINT"),
        ("SIGINT", False, "SIGINT, SIGTERM"),
        ("SIGINT,SIGTERM", False, "SIGINT, SIGTERM, SIGKILL"),
        ("", True, "SIGINT, SIGTERM"),
    ],
)
def test_a_child_and_its_group_are_stopped_by_pid_with_bounded_steps(runner, tmp_path, ignore, fork, stopped_by):
    child = start(runner, tmp_path, "peer", ignore, fork)

    runner.stop(child, grace=1.0)

    assert child.stopped_by == stopped_by
    assert child.finished() and not child.group_left
    assert not pair.group_alive(child.pid)
    record = child.record()
    assert record["pid"] == child.pid and record["stopped_by"] == stopped_by


def test_a_child_past_its_limit_is_stopped_and_marked(runner, tmp_path):
    child = runner.run("slow", [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, timeout=0.5)
    assert child.stopped_by == "timeout, SIGINT"
    assert child.finished()


def test_a_finished_child_is_not_signalled(runner, tmp_path):
    child = runner.run("quick", [sys.executable, "-c", "print('done')"], tmp_path, timeout=20)
    runner.stop(child)
    assert child.returncode == 0 and child.stopped_by is None
    assert child.record()["stdout"] == "done\n"


# The store after the wire tests ----------------------------------------------------

ANCHOR = {"id": 1, "layer": "anchor", "source": "map", "label": "table",
          "first_seen": 0.0, "last_seen": 0.0, "last_verified": None}  # fmt: skip
MOVABLE = {"id": 2, "layer": "movable", "source": "perception", "label": "cup",
           "first_seen": 5.0, "last_seen": 5.0, "last_verified": 5.0}  # fmt: skip
COUNTS = {"object": 2, "event": 0, "event_fts_data": 2, "write_request": 0, "surface": 1}


def rows(objects, events=(), requests=(), **counts) -> dict:
    return {"object": [dict(o) for o in objects], "event": list(events), "write_request": list(requests),
            "counts": {**COUNTS, **counts}}  # fmt: skip


EVENT = {"id": 1, "text": "The pair gate checked the table."}
REQUEST = {"request_id": "pair-check-event-1", "task_id": "pair-check", "operation": "remember_event"}


def test_the_store_may_gain_one_event_and_the_anchor_stamp():
    stamped = {**ANCHOR, "first_seen": 9.0, "last_seen": 9.0, "last_verified": 9.0}
    after = rows([stamped, MOVABLE], [EVENT], [REQUEST], event=1, write_request=1, event_fts_data=3)
    check = pair.rows_check(rows([ANCHOR, MOVABLE]), after, True)
    assert check["status"] == "pass", check["detail"]


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (rows([ANCHOR, MOVABLE, {**MOVABLE, "id": 3}], [EVENT], [REQUEST], object=3), "made or removed"),
        (rows([ANCHOR, {**MOVABLE, "last_seen": 6.0}], [EVENT], [REQUEST]), "object 2 changed"),
        (rows([{**ANCHOR, "label": "desk"}, MOVABLE], [EVENT], [REQUEST]), "object 1 changed"),
        (rows([{**ANCHOR, "first_seen": 9.0, "last_seen": 8.0, "last_verified": 9.0}, MOVABLE], [EVENT], [REQUEST]),
         "object 1 changed"),
        (rows([ANCHOR, MOVABLE], [EVENT], [REQUEST], surface=2), "row counts changed"),
        (rows([ANCHOR, MOVABLE], [], []), "one new event"),
        (rows([ANCHOR, MOVABLE], [EVENT, {**EVENT, "id": 2}], [REQUEST]), "one new event"),
        (rows([ANCHOR, MOVABLE], [EVENT], [REQUEST, {**REQUEST, "operation": "set_object_state"}]), "write request"),
    ],
)  # fmt: skip
def test_any_other_store_change_fails(after, expected):
    check = pair.rows_check(rows([ANCHOR, MOVABLE]), after, True)
    assert check["status"] == "fail" and expected in check["detail"], check["detail"]


def test_a_node_that_did_not_stop_cleanly_fails_the_store_check():
    after = rows([ANCHOR, MOVABLE], [EVENT], [REQUEST])
    assert "did not stop cleanly" in pair.rows_check(rows([ANCHOR, MOVABLE]), after, False)["detail"]
    assert pair.rows_check(None, after, True)["status"] == "fail"


def test_a_failed_check_or_a_negative_control_never_passes(tmp_path):
    args = pair.parse_args(["--vision-root", "v", "--database-root", "d", "--vision-commit", "a" * 40,
                            "--database-commit", "b" * 40, "--domain", "78", "--report", str(tmp_path / "r.json")])  # fmt: skip
    gate = pair.Gate(args)
    gate.checks.extend(pair._check(name, True) for name in pair.PLANNED)
    assert gate.passed()
    gate.checks.append({"name": "harness", "status": "fail"})
    assert not gate.passed()
    gate.checks.pop()
    gate.checks[3]["status"] = "skip"
    assert not gate.passed()
    gate.checks[3]["status"] = "pass"
    gate.args.negative_control = "hash"
    assert not gate.passed()


def test_the_domain_is_bounded(tmp_path):
    with pytest.raises(SystemExit):
        pair.parse_args(["--vision-root", "v", "--database-root", "d", "--vision-commit", "a", "--database-commit",
                         "b", "--domain", "300", "--report", "r"])  # fmt: skip


# The peers' own helpers --------------------------------------------------------------


def test_a_quarter_turn_about_z_turns_x_into_y():
    assert peer.near(peer.rotate(peer.POSE_2[1], (1.0, 0.0, 0.0)), (0.0, 1.0, 0.0), 1e-12)
    assert peer.near(peer.rotate((0.0, 0.0, 0.0, 1.0), (1.0, 2.0, 3.0)), (1.0, 2.0, 3.0), 1e-12)


def test_the_two_camera_poses_put_a_block_in_different_places():
    points = peer.block_points(peer.BLOCK_A)
    assert peer.near(tuple(sum(p[i] for p in points) / len(points) for i in range(3)), peer.BLOCK_A, 1e-12)
    first, second = peer.expected_centroid(peer.POSE_1, points), peer.expected_centroid(peer.POSE_2, points)
    assert peer.near(first, (1.1, 2.2, 1.5), 1e-9)
    assert peer.near(second, (4.8, 2.1, 1.5), 1e-9)
    # The latest transform instead of the stamped one moves a centroid by metres.
    assert abs(first[0] - second[0]) > 1.0


def test_float32_equality_is_bit_for_bit():
    assert peer.same_float32([0.1, 0.2], [0.1, 0.2])
    assert not peer.same_float32([0.1, 0.2], [0.1, 0.2000001])
    assert not peer.same_float32([0.1], [0.1, 0.2])


def test_the_pattern_image_is_bgr8_of_its_size():
    image = peer.pattern_image()
    assert len(image) == peer.IMAGE_WIDTH * peer.IMAGE_HEIGHT * 3
    assert image[:6] == bytes([0, 61, 122, 7, 68, 129])


def summary(*, stream="7f3a9c1e-52b4-4d10-a8e6-0c9b7d2e4f61", detections=(), local=None, classes=("cup",)):
    return {"stream_id": stream, "frame_id": "camera", "classes": list(classes), "detections": list(detections),
            "local": local}  # fmt: skip


def detection(label="cup", points=40, crop=100, model="", embedding=()):
    return {"label": label, "points": points, "crop_bytes": crop, "embedding_model": model, "embedding": list(embedding)}


def status(received=0, rejected=0, models=("clip_vit_b16",)):
    return {"received": received, "rejected": rejected, "models": list(models)}


def failed(checks):
    return [c["name"] for c in checks if c["status"] != "pass"]


def test_a_nonempty_phase_passes_only_with_the_nodes_own_count():
    arrays = [summary(detections=[detection()])] * 5
    assert failed(peer.phase_checks("nonempty", arrays, status(), status(4))) == []
    # Silence at the node is a failure, not a pass.
    assert failed(peer.phase_checks("nonempty", arrays, status(), status(0))) == ["nonempty.counters"]
    assert failed(peer.phase_checks("nonempty", arrays[:2], status(), status(4))) == ["nonempty.received"]
    no_crop = [summary(detections=[detection(crop=0)])] * 5
    assert failed(peer.phase_checks("nonempty", no_crop, status(), status(4))) == ["nonempty.content"]
    refused = [summary(detections=[detection()], local=["INVALID", "bad"])] * 5
    assert "nonempty.accepted" in failed(peer.phase_checks("nonempty", refused, status(), status(4, 4)))


def test_an_empty_phase_needs_empty_arrays_that_name_their_classes():
    arrays = [summary()] * 5
    assert failed(peer.phase_checks("empty", arrays, status(10), status(14))) == []
    assert failed(peer.phase_checks("empty", [summary(classes=())] * 5, status(), status(4))) == ["empty.content"]


def test_a_vector_phase_needs_exact_vectors_and_a_refusal():
    vector = [0.6, 0.8]
    refusal = ["INVALID", "Detection 0: The store has no vector table for the model 'fake_pair_vectors'."]
    arrays = [summary(detections=[detection(model=peer.VECTOR_MODEL, embedding=vector)], local=refusal)] * 5
    expected = {"cup": vector}
    assert failed(peer.phase_checks("vector", arrays, status(), status(5, 5), expected)) == []
    assert failed(peer.phase_checks("vector", arrays, status(), status(5, 5), {"cup": [0.6, 0.80001]})) == ["vector.decoded"]
    assert failed(peer.phase_checks("vector", arrays, status(), status(5, 4), expected)) == ["vector.counters"]
    stored = status(5, 5, models=("clip_vit_b16", peer.VECTOR_MODEL))
    assert failed(peer.phase_checks("vector", arrays, status(), stored, expected)) == ["vector.not-clip"]


def test_a_malformed_phase_must_not_look_like_an_empty_success():
    refusal = ["INVALID", "Detection 0: points has 21048 bytes for 1755 points."]
    arrays = [summary(stream=peer.MALFORMED_STREAM, detections=[detection()], local=refusal)] * 5
    assert failed(peer.phase_checks("malformed", arrays, status(), status(5, 5))) == []
    emptied = [summary(stream=peer.MALFORMED_STREAM, local=None)] * 5
    assert failed(peer.phase_checks("malformed", emptied, status(), status(5, 0))) == [
        "malformed.counters", "malformed.content", "malformed.refused"]  # fmt: skip


def test_ros_arguments_pass_through_only_to_the_wrapped_nodes():
    args = peer.parse_args(["database-node", "--out", "o.json", "--ros-args", "-r", "__ns:=/pair_check", "-p", "a:=1"])
    assert args.ros_args == ["--ros-args", "-r", "__ns:=/pair_check", "-p", "a:=1"]
    with pytest.raises(SystemExit):
        peer.parse_args(["probe", "--side", "vision", "--out", "o.json", "--ros-args", "-p", "a:=1"])


def test_the_two_tools_agree_on_their_shared_names():
    assert peer.VECTOR_MODEL == pair.VECTOR_MODEL
    assert peer.VECTOR_MODEL.startswith("fake_") and "clip" not in peer.VECTOR_MODEL
    assert peer.NAMESPACE == pair.NAMESPACE


# The seed fixtures --------------------------------------------------------------------


def test_the_fixtures_name_the_map_their_files_make():
    map_id = make_map_id(pair.ARENA, pair.DATE, digest_map_files(FIXTURES / "map" / "map.yaml"))
    for name in ("rooms.yaml", "anchors.yaml"):
        assert yaml.safe_load((FIXTURES / name).read_text())["map_id"] == map_id


@pytest.fixture
def pair_store(tmp_path):
    seed = tmp_path / "seed.db"
    argv = [
        "--map-folder", str(FIXTURES / "map"), "--arena", pair.ARENA, "--date", pair.DATE, "--clock-kind", "wall",
        "--rooms", str(FIXTURES / "rooms.yaml"), "--anchors", str(FIXTURES / "anchors.yaml"),
        "--catalogue", str(FIXTURES / "catalogue.yaml"), "--output", str(seed),
    ]  # fmt: skip
    assert seed_store.main(argv) == 0
    store = tmp_path / "store.db"
    restore(seed, store, FIXTURES / "map", "wall")
    return store


def read_rows(store: Path, out: Path) -> dict:
    assert peer.main(["store-rows", "--db", str(store), "--out", str(out)]) == 0
    return json.loads(out.read_text())


def test_the_fixtures_serve_what_the_vision_client_expects(ros, pair_store, tmp_path):
    before = read_rows(pair_store, tmp_path / "before.json")
    [row] = before["map"]
    node = DatabaseNode(parameter_overrides=[
        Parameter("memory_enabled", value=True), Parameter("db_path", value=str(pair_store)),
        Parameter("map_folder", value=str(FIXTURES / "map")), Parameter("map_id", value=row["id"]),
    ])  # fmt: skip
    try:
        rooms = node.list_rooms(ListRooms.Request(), ListRooms.Response())
        contents = node.get_room_contents(
            GetRoomContents.Request(query=ObjectQuery(room="living room")), GetRoomContents.Response()
        )
        prior = node.search_objects(SearchObjects.Request(query=ObjectQuery(thing="mug")), SearchObjects.Response())
        table = {m.object.label: m.object.id for m in contents.objects}["table"]
        found = node.get_object(GetObject.Request(object_id=table), GetObject.Response())
    finally:
        node.destroy_node()

    assert [r.name for r in rooms.rooms] == peer.ROOM_NAMES
    assert sorted(m.object.label for m in contents.objects) == peer.LIVING_ROOM_CONTENTS
    assert [(p.official_name, p.place, p.room) for p in prior.priors] == [peer.PRIOR]
    assert [(s.key, round(s.z, 6)) for s in found.surfaces] == [peer.SURFACE]
    # The node's first start stamps the anchors and changes nothing else.
    after = read_rows(pair_store, tmp_path / "after.json")
    assert pair.object_problems(before["object"], after["object"]) == []
    assert before["object"] != after["object"]
    assert before["counts"] == after["counts"]
