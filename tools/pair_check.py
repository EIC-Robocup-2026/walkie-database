#!/usr/bin/env python3
"""Check a walkie-vision and walkie-database pair on one computer, before either is pushed.

This is the local pair gate (ADR-0024). It needs only the Python standard
library, so the host's python3 runs it. Every peer runs in its own
repository's Pixi environment, started with `pixi run --locked --clean-env`.

The gate, in order:

1. Both worktrees are clean and at the commits given. The database's pin and
   its imported clone are at the vision commit. Both pixi.lock files are hashed.
2. With --build, both message installations are rebuilt, and a client overlay
   is built. The overlay holds walkie_database_msgs, built by the vision
   environment's interpreter against the vision build of walkie_vision_msgs.
   The database's compiled environment is never sourced in the vision one.
3. A probe in each environment records where Python, the source package and
   every message module come from. Paths in the other worktree, in the owner's
   checkouts or in the other environment fail the gate.
4. The installed type hash inventories (share/<pkg>/{msg,srv}/*.json) must
   agree, nested types included.
5. Two negative controls must fail: an altered hash, and a vision probe that
   imports the database's own build of walkie_database_msgs.
6. Only then do peers meet on the wire, in one domain on localhost with Fast
   DDS: observations from the fake publisher, LocalizeObjects with a moving
   camera, the vision services with fake providers, and the seeded database
   services.

The report is JSON, with every check, the inventories, the import evidence and
each process's output. The exit status is 0 only when every check passed and
every process stopped. A process is stopped by its own PID: SIGINT to the
process that was started, then SIGTERM and SIGKILL to its process group. No
process is found or killed by name, and no Zenoh router is started.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
FULL_SHA = re.compile(r"[0-9a-f]{40}")
VISION_MSGS = "walkie_vision_msgs"
DATABASE_MSGS = "walkie_database_msgs"
PIN_MANIFEST = "ros2_ws.repos"
DEPENDENCY = "ros2_ws/src/walkie-vision"
PEER_SCRIPT = "tools/contract_peer.py"
SCENE = "config/fakes/boxes.json"
FIXTURES = "tests/fixtures/pair"
# The seed fixtures' map ID is made from these and the map files' digest.
ARENA = "pairlab"
DATE = "2026-11-05"
MARKER = ".pair-check"
NAMESPACE = "/pair_check"
OBJECTS_TOPIC = f"{NAMESPACE}/objects"
DATABASE_NODE = f"{NAMESPACE}/walkie_database"
VECTOR_MODEL = "fake_pair_vectors"

# What every peer must see after its environment's activation.
MODEL_FREE_ENV = {
    "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
    "ROS_LOCALHOST_ONLY": "1",
    "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
    "CUDA_VISIBLE_DEVICES": "",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "YOLO_AUTOINSTALL": "false",
}
# Variables of the shell that ran this tool that must not reach pixi itself.
_HOST_PREFIXES = (
    "ROS_", "RMW_", "AMENT_", "COLCON_", "CONDA_", "PIXI_", "PYTHON", "VIRTUAL_ENV",
    "CMAKE_", "LD_LIBRARY_PATH", "GIT_",
)  # fmt: skip

BUILD_TIMEOUT_S = 1800.0
ROLE_TIMEOUT_S = 180.0
READY_TIMEOUT_S = 60.0
STOP_GRACE_S = 15.0
TERM_GRACE_S = 5.0
KILL_GRACE_S = 5.0
OUTPUT_LIMIT = 64 * 1024

# Every check the gate makes, in order. A check that never ran is a skip.
PLANNED = (
    "sources", "pin", "locks", "build", "probe.vision", "probe.database", "hashes",
    "control.hash", "control.import", "seed", "observe.nonempty", "observe.empty",
    "observe.vector", "observe.malformed", "localize", "database-services",
    "vision-services", "store-rows", "processes", "teardown",
)  # fmt: skip


class GateError(Exception):
    """A failure that stops the gate, with the reason for the report."""


class Interrupted(Exception):
    """SIGTERM or SIGINT reached the gate."""


# Git and sources -------------------------------------------------------------


def _git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(root: Path, *args: str) -> str:
    """Run git in *root* and return its output. Raises GateError when it fails."""
    binary = shutil.which("git") or shutil.which("git", path="/usr/bin:/bin")
    if binary is None:
        raise GateError("git is not installed")
    result = subprocess.run(
        [binary, "-C", str(root), *args], env=_git_env(), text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise GateError(f"git -C {root} {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def source_problems(root: Path, commit: str) -> list[str]:
    """Why *root* is not a clean worktree at *commit*, as a list that is empty when it is.

    Untracked files count too: a file that is not committed must not run under the
    commit's name. Ignored files, such as builds and environments, do not.
    """
    if not FULL_SHA.fullmatch(commit):
        return [f"{commit!r} is not a full 40-character commit"]
    try:
        head = git(root, "rev-parse", "HEAD")
        status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    except GateError as e:
        return [str(e)]
    problems = []
    if head != commit:
        problems.append(f"{root} is at {head}, not {commit}")
    if status:
        lines = status.splitlines()
        problems.append(f"{root} has {len(lines)} changed or untracked paths: {lines[:10]}")
    return problems


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Type hash inventories -------------------------------------------------------


def read_inventory(share: Path) -> dict[str, dict[str, str]]:
    """Read every installed type description of one package.

    Returns {"msg/Name.json": {type_name: hash_string}}, nested types included.
    Raises GateError for a file without a type_hashes list, or with one type given
    two hashes.
    """
    inventory: dict[str, dict[str, str]] = {}
    for kind in ("msg", "srv"):
        for path in sorted((share / kind).glob("*.json")):
            try:
                entries = json.loads(path.read_text())["type_hashes"]
                hashes: dict[str, str] = {}
                for entry in entries:
                    name, value = entry["type_name"], entry["hash_string"]
                    if hashes.setdefault(name, value) != value:
                        raise GateError(f"{path} gives {name} two hashes")
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise GateError(f"{path} has no readable type_hashes: {e}") from None
            inventory[f"{kind}/{path.name}"] = hashes
    return inventory


def interface_files(package_source: Path) -> set[str]:
    """The description file each .msg and .srv in a package's source should install."""
    return {
        f"{kind}/{path.stem}.json"
        for kind in ("msg", "srv")
        for path in sorted((package_source / kind).glob(f"*.{kind}"))
    }


def inventory_problems(
    expected: dict[str, set[str]],
    inventories: dict[str, tuple[str, dict[str, dict[str, str]]]],
    pairs: list[tuple[str, str]],
) -> list[str]:
    """Compare inventories. Empty means they agree.

    *expected* maps a package to the files its source should install.
    *inventories* maps a label to (package, inventory). Each pair of labels must
    hold equal inventories, and across every inventory one type name has one hash.
    """
    problems = []
    for label, (package, inventory) in inventories.items():
        files = set(inventory)
        if missing := sorted(expected[package] - files):
            problems.append(f"{label} lacks {missing}")
        if extra := sorted(files - expected[package]):
            problems.append(f"{label} has files its source does not define: {extra}")
    for a, b in pairs:
        left, right = inventories[a][1], inventories[b][1]
        for name in sorted(set(left) & set(right)):
            if left[name] != right[name]:
                differing = sorted(
                    t for t in set(left[name]) | set(right[name]) if left[name].get(t) != right[name].get(t)
                )
                problems.append(f"{name} differs between {a} and {b} for {differing}")
    seen: dict[str, dict[str, list[str]]] = {}
    for label, (_, inventory) in inventories.items():
        for name, hashes in inventory.items():
            for type_name, value in hashes.items():
                seen.setdefault(type_name, {}).setdefault(value, []).append(f"{label}:{name}")
    for type_name, values in sorted(seen.items()):
        if len(values) > 1:
            where = "; ".join(f"{value} in {sorted(found)[:3]}" for value, found in sorted(values.items()))
            problems.append(f"{type_name} has {len(values)} hashes: {where}")
    return problems


def altered(inventory: dict[str, dict[str, str]], name: str, type_name: str) -> dict[str, dict[str, str]]:
    """A copy of *inventory* with one hash changed in its last character."""
    changed = copy.deepcopy(inventory)
    value = changed[name][type_name]
    changed[name][type_name] = value[:-1] + ("0" if value[-1] != "0" else "1")
    return changed


# Import evidence -------------------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def is_under(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    path, root = _norm(os.fspath(path)), _norm(os.fspath(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def path_problems(
    evidence: dict,
    *,
    project_root: Path,
    module_roots: dict[str, Path],
    banned: list[Path],
    env: dict[str, str],
    affinity: list[int] | None = None,
) -> list[str]:
    """Why a peer's imports or environment break the pair's isolation. Empty means none.

    *module_roots* maps a module, or a message package's shared library, to the
    folder its file and real path must be under. Nothing the peer loaded, nothing
    on its search paths and no shared library may be under a *banned* folder. A
    package cache path of an ordinary dependency is not checked: it is shared.
    """
    problems = []
    executable = evidence.get("executable") or ""
    if not is_under(executable, project_root / ".pixi"):
        problems.append(f"sys.executable {executable!r} is not in {project_root / '.pixi'}")
    if evidence.get("env", {}).get("PIXI_PROJECT_ROOT") != str(project_root):
        problems.append(f"PIXI_PROJECT_ROOT is {evidence.get('env', {}).get('PIXI_PROJECT_ROOT')!r}, not {project_root}")
    for name, value in env.items():
        got = evidence.get("env", {}).get(name)
        if got != value:
            problems.append(f"{name} is {got!r}, not {value!r}")
    if affinity is not None and evidence.get("affinity") != affinity:
        problems.append(f"CPU affinity is {evidence.get('affinity')}, not {affinity}")
    for name, error in sorted(evidence.get("import_errors", {}).items()):
        problems.append(f"{name} did not import: {error}")
    modules = evidence.get("modules", {})
    for name, root in module_roots.items():
        if name.endswith(".so"):
            continue
        entry = modules.get(name)
        if entry is None:
            problems.append(f"{name} was not imported")
            continue
        for kind in ("file", "real"):
            if not entry.get(kind) or not is_under(entry[kind], root):
                problems.append(f"{name} {kind} {entry.get(kind)!r} is not under {root}")
    for library in evidence.get("libraries", []):
        package = next((p for p in (VISION_MSGS, DATABASE_MSGS) if p in os.path.basename(library)), None)
        root = module_roots.get(f"{package}.so") if package else None
        if root is not None and not is_under(library, root):
            problems.append(f"the library {library} is not under {root}")
    if evidence.get("typesupport") is not None and evidence.get("typesupport") is not True:
        problems.append(f"type support did not load: {evidence.get('typesupport')}")
    searched = [
        *(("module", f) for f in evidence.get("module_files", [])),
        *(("sys.path", p) for p in evidence.get("sys_path", [])),
        *(("library", p) for p in evidence.get("libraries", [])),
        *((name, p) for name in ("AMENT_PREFIX_PATH", "PYTHONPATH", "LD_LIBRARY_PATH")
          for p in (evidence.get("env", {}).get(name) or "").split(os.pathsep) if p),
    ]  # fmt: skip
    found: dict[tuple[str, Path], list[str]] = {}
    for kind, path in searched:
        for root in banned:
            if is_under(path, root):
                found.setdefault((kind, root), []).append(path)
    for (kind, root), paths in found.items():
        unique = sorted(set(paths))
        problems.append(f"{len(unique)} {kind} paths are under {root}, such as {unique[:3]}")
    for name in evidence.get("model_modules", []):
        problems.append(f"the model library {name} was imported")
    return problems


# Processes -------------------------------------------------------------------


def host_env() -> dict[str, str]:
    """The environment for pixi itself: the shell's, without ROS, Conda, Pixi or Git variables."""
    return {key: value for key, value in os.environ.items() if not key.startswith(_HOST_PREFIXES)}


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The group is gone and its number was reused by another user's process.
        return False
    return True


class Child:
    """One process this tool started, in its own session and process group."""

    def __init__(self, name: str, argv: list[str], cwd: Path, log_dir: Path, index: int) -> None:
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.stdout_path = log_dir / f"{index:02d}-{name}.stdout"
        self.stderr_path = log_dir / f"{index:02d}-{name}.stderr"
        self.popen: subprocess.Popen | None = None
        self.started = 0.0
        self.ended: float | None = None
        self.stopped_by: str | None = None
        self.group_left = False

    @property
    def pid(self) -> int:
        return self.popen.pid

    @property
    def returncode(self) -> int | None:
        return self.popen.poll() if self.popen else None

    def finished(self) -> bool:
        """True when the process has exited and its process group is empty."""
        if self.popen is None:
            return True
        if self.popen.poll() is None or group_alive(self.pid):
            return False
        if self.ended is None:
            self.ended = time.monotonic()
        return True

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.finished():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def record(self) -> dict:
        return {
            "name": self.name,
            "argv": self.argv,
            "cwd": str(self.cwd),
            "pid": self.popen.pid if self.popen else None,
            "returncode": self.returncode,
            "stopped_by": self.stopped_by,
            "group_left": self.group_left,
            "seconds": round((self.ended or time.monotonic()) - self.started, 3) if self.popen else None,
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "stdout": _tail(self.stdout_path),
            "stderr": _tail(self.stderr_path),
        }


def _tail(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > OUTPUT_LIMIT:
        data = b"[earlier output cut; the full file is at the path above]\n" + data[-OUTPUT_LIMIT:]
    return data.decode(errors="replace")


class Runner:
    """Starts processes and stops them by the PID it got, with bounded waits."""

    def __init__(self, log_dir: Path, env: dict[str, str] | None = None) -> None:
        self.log_dir = log_dir
        self.env = host_env() if env is None else env
        self.children: list[Child] = []

    def start(self, name: str, argv: list[str], cwd: Path) -> Child:
        child = Child(name, argv, cwd, self.log_dir, len(self.children) + 1)
        self.children.append(child)
        with open(child.stdout_path, "wb") as out, open(child.stderr_path, "wb") as err:
            child.popen = subprocess.Popen(
                argv, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True,
            )  # fmt: skip
        child.started = time.monotonic()
        return child

    def run(self, name: str, argv: list[str], cwd: Path, timeout: float) -> Child:
        """Start a process and wait for it. One that runs past *timeout* is stopped."""
        child = self.start(name, argv, cwd)
        if not child.wait(timeout):
            child.stopped_by = "timeout"
            self.stop(child)
        return child

    def stop(self, child: Child, grace: float = STOP_GRACE_S) -> None:
        """SIGINT to the process, then SIGTERM and SIGKILL to its group, each with a bounded wait.

        pixi passes SIGINT on to its child once and waits for it, so the peer shuts
        down cleanly. Signalling the whole group at once would interrupt it twice.
        """
        if child.popen is None or child.finished():
            return
        steps = (
            ("SIGINT", lambda: os.kill(child.pid, signal.SIGINT), grace),
            ("SIGTERM", lambda: os.killpg(child.pid, signal.SIGTERM), TERM_GRACE_S),
            ("SIGKILL", lambda: os.killpg(child.pid, signal.SIGKILL), KILL_GRACE_S),
        )
        used = []
        for name, send, wait_s in steps:
            if name == "SIGINT" and child.popen.poll() is not None:
                continue  # the leader is gone; only its group is left
            try:
                send()
            except ProcessLookupError:
                pass
            used.append(name)
            if child.wait(wait_s):
                break
        child.stopped_by = ", ".join(([child.stopped_by] if child.stopped_by else []) + used)
        child.group_left = group_alive(child.pid)

    def stop_all(self) -> None:
        for child in reversed(self.children):
            self.stop(child)


# The gate ----------------------------------------------------------------------


def _check(name: str, ok: bool, detail: str = "", **evidence) -> dict:
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail, "evidence": evidence}


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def fresh_dir(path: Path) -> Path:
    """Empty a folder this tool made, or make it. A folder it did not make is refused."""
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not (path / MARKER).is_file():
            raise GateError(f"{path} exists and this tool did not make it, so it is left alone")
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / MARKER).write_text("Made by tools/pair_check.py. The next run deletes it.\n")
    return path


class Gate:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.vision = Path(args.vision_root).resolve()
        self.database = Path(args.database_root).resolve()
        self.report_path = Path(args.report).resolve()
        self.report_dir = self.report_path.parent
        # One run folder per report, so a control run keeps the main run's logs.
        self.run_dir = self.report_dir / f"{self.report_path.stem}-run"
        self.overlay = self.report_dir / "client-overlay"
        self.forbidden = [Path(p) for p in args.forbid]
        self.pixi = args.pixi
        self.checks: list[dict] = []
        self.report: dict = {
            "schema_version": SCHEMA_VERSION,
            "tool": "tools/pair_check.py",
            "started": _now(),
            "argv": sys.argv,
            "negative_control": args.negative_control,
            "build": args.build,
            "domain": args.domain,
            "host": {"python": sys.version, "affinity": sorted(os.sched_getaffinity(0))},
            "repositories": {},
            "evidence": {},
            "inventories": {},
            "checks": self.checks,
            "processes": [],
        }
        self.runner: Runner | None = None
        self.affinity = sorted(os.sched_getaffinity(0))
        self.map_id = ""

    # -- helpers --

    def add(self, check: dict) -> bool:
        self.checks.append(check)
        return check["status"] == "pass"

    def env_pairs(self, extra: dict[str, str] | None = None) -> list[str]:
        values = {
            **MODEL_FREE_ENV,
            "ROS_DOMAIN_ID": str(self.args.domain),
            "ROS_PYTHON_CHECK_FIELDS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "ROS_LOG_DIR": str(self.run_dir / "ros-log"),
            **(extra or {}),
        }
        return [f"{key}={value}" for key, value in values.items()]

    def in_env(self, root: Path, argv: list[str], extra: dict[str, str] | None = None) -> list[str]:
        """argv run in *root*'s default Pixi environment, with the gate's variables set after activation."""
        manifest = str(root / "pixi.toml")
        return [self.pixi, "run", "--locked", "--clean-env", "--manifest-path", manifest,
                "/usr/bin/env", *self.env_pairs(extra), *argv]  # fmt: skip

    def peer(self, side: str, role: str, *args: str, overlay: bool = False, prepend: str = "") -> list[str]:
        """The command of one contract_peer role, in the vision or the database environment.

        With *overlay*, the client overlay is sourced on top of the vision
        environment. *prepend* puts a folder first on PYTHONPATH, for a negative control.
        """
        script = str(self.run_dir / "contract_peer.py")
        python = ["python", "-P", script, role, *args]
        root = self.vision if side == "vision" else self.database
        if not overlay and not prepend:
            return self.in_env(root, python)
        steps = []
        if overlay:
            steps.append('source "$1"')
        if prepend:
            steps.append('export PYTHONPATH="$2${PYTHONPATH:+:$PYTHONPATH}"')
        command = " && ".join([*steps, "shift 2", 'exec "$@"'])
        setup = str(self.overlay / "install" / "local_setup.sh")
        return self.in_env(root, ["/usr/bin/bash", "-c", command, "bash", setup, prepend or "-", *python])

    def run_peer(self, name: str, argv: list[str], out: Path, timeout: float = ROLE_TIMEOUT_S) -> tuple[Child, dict | None]:
        child = self.runner.run(name, argv, self.run_dir, timeout)
        return child, _read_json(out)

    def peer_paths(self, side: str, evidence: dict | None, *, overlay: bool) -> list[str]:
        if evidence is None:
            return ["the peer wrote no evidence"]
        own, other = (self.vision, self.database) if side == "vision" else (self.database, self.vision)
        roots = {
            "rclpy": own / ".pixi",
            VISION_MSGS: own / "ros2_ws",
            f"{VISION_MSGS}.msg": own / "ros2_ws",
            f"{VISION_MSGS}.srv": own / "ros2_ws",
            f"{VISION_MSGS}.so": own / "ros2_ws",
        }
        source = "walkie_vision" if side == "vision" else "walkie_database"
        if source in evidence.get("modules", {}):
            roots[source] = own / "src"
        if side == "database" or overlay:
            database_root = own / "ros2_ws" if side == "database" else self.overlay / "install"
            for name in (DATABASE_MSGS, f"{DATABASE_MSGS}.msg", f"{DATABASE_MSGS}.srv", f"{DATABASE_MSGS}.so"):
                roots[name] = database_root
        return path_problems(
            evidence,
            project_root=own,
            module_roots=roots,
            banned=[*self.forbidden, other],
            env={**MODEL_FREE_ENV, "ROS_DOMAIN_ID": str(self.args.domain)},
            affinity=self.affinity,
        )

    # -- stages --

    def run(self) -> None:
        fresh_dir(self.run_dir)
        (self.run_dir / "logs").mkdir()
        (self.run_dir / "ros-log").mkdir()
        self.runner = Runner(self.run_dir / "logs")
        if not self.stage_sources():
            return
        # Copied only after the sources check, so the peers run the committed file.
        shutil.copyfile(self.database / PEER_SCRIPT, self.run_dir / "contract_peer.py")
        if not self.stage_pin():
            return
        self.stage_locks()
        if not self.stage_build():
            return
        probes = self.stage_probes()
        hashes = self.stage_hashes()
        controls = self.stage_controls()
        if probes and hashes and controls:
            self.stage_wire()

    def stage_sources(self) -> bool:
        problems = {}
        for label, root, commit in (
            ("vision", self.vision, self.args.vision_commit),
            ("database", self.database, self.args.database_commit),
        ):
            problems[label] = source_problems(root, commit)
            self.report["repositories"][label] = {"root": str(root), "commit": commit}
        flat = [p for found in problems.values() for p in found]
        return self.add(_check("sources", not flat, "; ".join(flat) or "both worktrees are clean at the given commits"))

    def stage_pin(self) -> bool:
        out = self.run_dir / "pin.json"
        child, result = self.run_peer(
            "pin", self.peer("database", "pin", "--database-root", str(self.database), "--out", str(out)), out
        )
        problems = []
        if result is None:
            problems.append(f"the pin reader wrote nothing (exit {child.returncode})")
        elif result.get("version") != self.args.vision_commit:
            problems.append(f"{PIN_MANIFEST} pins {result.get('version')!r}, not {self.args.vision_commit}")
        clone = self.database / DEPENDENCY
        clone_head = ""
        try:
            clone_head = git(clone, "rev-parse", "HEAD")
            if clone_head != self.args.vision_commit:
                problems.append(f"{clone} is at {clone_head}, not {self.args.vision_commit}")
            if git(clone, "status", "--porcelain=v1", "--untracked-files=all"):
                problems.append(f"{clone} has changes")
        except GateError as e:
            problems.append(str(e))
        self.report["repositories"]["database"]["pin"] = result
        self.report["repositories"]["database"]["imported_commit"] = clone_head
        return self.add(_check("pin", not problems, "; ".join(problems) or "the pin and the clone are at the vision commit",
                               pin=result, imported_commit=clone_head))  # fmt: skip

    def stage_locks(self) -> bool:
        locks = {}
        for label, root in (("vision", self.vision), ("database", self.database)):
            locks[label] = file_sha256(root / "pixi.lock")
            self.report["repositories"][label]["pixi_lock_sha256"] = locks[label]
        return self.add(_check("locks", True, "sha256 of each pixi.lock", **locks))

    def stage_build(self) -> bool:
        if not self.args.build:
            self.add({"name": "build", "status": "skip", "detail": "--build was not given, so the run cannot pass",
                      "evidence": {}})  # fmt: skip
            missing = [str(p) for p in (self.overlay / MARKER, self.overlay / "install" / "local_setup.sh") if not p.exists()]
            if missing:
                raise GateError(f"there is no client overlay to test without --build: {missing}")
            return True
        defaults = self.run_dir / "colcon-defaults.yaml"
        defaults.write_text("{}\n")
        build_env = {
            "COLCON_DEFAULTS_FILE": str(defaults),
            "CMAKE_BUILD_PARALLEL_LEVEL": "2",
            "MAKEFLAGS": "-j2",
        }
        python = '-DPython3_EXECUTABLE="$CONDA_PREFIX/bin/python"'
        path = 'export PATH="$PATH:/usr/bin:/bin"'
        builds = [
            ("build-vision", self.vision,
             f'{path} && cd "$1" && exec colcon build --symlink-install --parallel-workers 2 '
             f"--packages-select {VISION_MSGS} --cmake-args {python}",
             [str(self.vision / "ros2_ws")]),
            ("build-database", self.database,
             f'{path} && cd "$1" && exec colcon build --symlink-install --parallel-workers 2 '
             f"--packages-select {VISION_MSGS} {DATABASE_MSGS} --cmake-args {python}",
             [str(self.database / "ros2_ws")]),
        ]  # fmt: skip
        records = {}
        for name, root, script, script_args in builds:
            argv = self.in_env(root, ["/usr/bin/bash", "-c", script, "bash", *script_args], build_env)
            child = self.runner.run(name, argv, root / "ros2_ws", BUILD_TIMEOUT_S)
            records[name] = child.returncode
            if child.returncode != 0:
                return self.add(_check("build", False, f"{name} exited {child.returncode}", **records))
        fresh_dir(self.overlay)
        script = (
            f'{path} && cd "$1" && exec colcon --log-base "$1/log" build --base-paths "$2" '
            f'--build-base "$1/build" --install-base "$1/install" --parallel-workers 2 '
            f"--packages-select {DATABASE_MSGS} --cmake-args {python}"
        )
        source = self.database / "ros2_ws" / "src" / DATABASE_MSGS
        argv = self.in_env(self.vision, ["/usr/bin/bash", "-c", script, "bash", str(self.overlay), str(source)], build_env)
        child = self.runner.run("build-overlay", argv, self.overlay, BUILD_TIMEOUT_S)
        records["build-overlay"] = child.returncode
        detail = "both installations and the client overlay were built"
        if child.returncode != 0:
            detail = f"build-overlay exited {child.returncode}"
        return self.add(_check("build", child.returncode == 0, detail, overlay=str(self.overlay), **records))

    def stage_probes(self) -> bool:
        results = []
        for side, overlay in (("vision", True), ("database", False)):
            out = self.run_dir / f"probe-{side}.json"
            argv = self.peer(side, "probe", "--side", side, "--out", str(out), overlay=overlay)
            child, evidence = self.run_peer(f"probe-{side}", argv, out)
            self.report["evidence"][f"probe-{side}"] = evidence
            problems = self.peer_paths(side, evidence, overlay=overlay)
            if child.returncode != 0:
                problems.insert(0, f"the probe exited {child.returncode}")
            results.append(self.add(_check(f"probe.{side}", not problems, "; ".join(problems) or "imports are from this worktree's builds")))
        return all(results)

    def inventories(self) -> tuple[dict[str, set[str]], dict[str, tuple[str, dict]]]:
        expected = {
            VISION_MSGS: interface_files(self.vision / "ros2_ws" / "src" / VISION_MSGS),
            DATABASE_MSGS: interface_files(self.database / "ros2_ws" / "src" / DATABASE_MSGS),
        }
        share = lambda prefix, package: prefix / package / "share" / package  # noqa: E731
        inventories = {
            "vision:walkie_vision_msgs": (VISION_MSGS, read_inventory(share(self.vision / "ros2_ws" / "install", VISION_MSGS))),
            "database:walkie_vision_msgs": (VISION_MSGS, read_inventory(share(self.database / "ros2_ws" / "install", VISION_MSGS))),
            "database:walkie_database_msgs": (DATABASE_MSGS, read_inventory(share(self.database / "ros2_ws" / "install", DATABASE_MSGS))),
            "overlay:walkie_database_msgs": (DATABASE_MSGS, read_inventory(share(self.overlay / "install", DATABASE_MSGS))),
        }  # fmt: skip
        return expected, inventories

    PAIRS = [
        ("vision:walkie_vision_msgs", "database:walkie_vision_msgs"),
        ("database:walkie_database_msgs", "overlay:walkie_database_msgs"),
    ]
    CONTROL = ("database:walkie_database_msgs", "srv/LocalizeObjects.json", f"{VISION_MSGS}/msg/ObjectDetectionArray")

    def stage_hashes(self) -> bool:
        expected, inventories = self.inventories()
        self.report["inventories"] = {label: inventory for label, (_, inventory) in inventories.items()}
        if self.args.negative_control == "hash":
            label, name, type_name = self.CONTROL
            package, inventory = inventories[label]
            inventories[label] = (package, altered(inventory, name, type_name))
        problems = inventory_problems(expected, inventories, self.PAIRS)
        counts = {label: sum(len(h) for h in inv.values()) for label, (_, inv) in inventories.items()}
        files = {label: len(inv) for label, (_, inv) in inventories.items()}
        return self.add(_check("hashes", not problems, "; ".join(problems) or "every type hash agrees, nested types included",
                               files=files, entries=counts))  # fmt: skip

    def stage_controls(self) -> bool:
        expected, inventories = self.inventories()
        label, name, type_name = self.CONTROL
        package, inventory = inventories[label]
        inventories[label] = (package, altered(inventory, name, type_name))
        found = inventory_problems(expected, inventories, self.PAIRS)
        caught = any(type_name in p for p in found)
        hash_ok = self.add(_check("control.hash", caught, f"an altered {type_name} in {name} was caught" if caught
                                  else "an altered hash was not caught", problems=found))  # fmt: skip

        sites = sorted((self.database / "ros2_ws" / "install" / DATABASE_MSGS / "lib").glob("python3*/site-packages"))
        problems: list[str] = []
        if not sites:
            problems = ["the database install has no site-packages to import from"]
            evidence = None
        else:
            out = self.run_dir / "probe-wrong-import.json"
            argv = self.peer("vision", "probe", "--side", "vision", "--no-typesupport", "--out", str(out),
                             overlay=True, prepend=str(sites[0]))  # fmt: skip
            _, evidence = self.run_peer("probe-wrong-import", argv, out)
            self.report["evidence"]["probe-wrong-import"] = evidence
            problems = self.peer_paths("vision", evidence, overlay=True)
        caught = any(DATABASE_MSGS in p and str(self.database) in p for p in problems)
        import_ok = self.add(_check("control.import", caught, "a vision peer importing the database's own build was refused"
                                    if caught else "a vision peer importing the database's own build was not refused",
                                    problems=problems))  # fmt: skip
        if self.args.negative_control == "import":
            # The control replaces the real probe, so the gate must stop here.
            self.add(_check("probe.vision.import-control", not problems, "; ".join(problems)))
            return False
        return hash_ok and import_ok

    # -- the wire --

    def stage_wire(self) -> None:
        work = self.run_dir / "work"
        work.mkdir()
        fixtures = self.database / FIXTURES
        map_folder = fixtures / "map"
        seed, store = work / "seed.db", work / "store.db"
        seeded = self.runner.run("seed", self.in_env(self.database, [
            "python", "-m", "walkie_database.seed_store", "--map-folder", str(map_folder), "--arena", ARENA,
            "--date", DATE, "--clock-kind", "wall", "--rooms", str(fixtures / "rooms.yaml"),
            "--anchors", str(fixtures / "anchors.yaml"), "--catalogue", str(fixtures / "catalogue.yaml"),
            "--output", str(seed)]), work, ROLE_TIMEOUT_S)  # fmt: skip
        restored = None
        if seeded.returncode == 0:
            restored = self.runner.run("restore", self.in_env(self.database, [
                "python", "-m", "walkie_database.restore", "--seed", str(seed), "--db", str(store),
                "--map-folder", str(map_folder), "--clock-kind", "wall", "--replace"]), work, ROLE_TIMEOUT_S)  # fmt: skip
        before_out = work / "rows-before.json"
        before = None
        if restored is not None and restored.returncode == 0:
            _, before = self.run_peer("rows-before", self.peer("database", "store-rows", "--db", str(store),
                                                               "--out", str(before_out)), before_out)  # fmt: skip
        maps = (before or {}).get("map", [])
        self.map_id = maps[0]["id"] if len(maps) == 1 else ""
        ok = self.add(_check("seed", bool(self.map_id) and self.map_id.startswith(f"{ARENA}-{DATE}-"),
                             f"seeded and restored map {self.map_id}" if self.map_id else "seeding or restoring failed",
                             rows=before))  # fmt: skip
        if not ok:
            return

        node_out = work / "database-node.json"
        node = self.runner.start("database-node", self.peer(
            "database", "database-node", "--out", str(node_out), "--ros-args", "-r", f"__ns:={NAMESPACE}",
            "-p", "memory_enabled:=true", "-p", f"db_path:={store}", "-p", f"map_folder:={map_folder}",
            "-p", f"map_id:={self.map_id}", "-p", f"objects_topic:={OBJECTS_TOPIC}"), work)  # fmt: skip
        services_out = work / "vision-services.json"
        services = self.runner.start("vision-services", self.peer("vision", "vision-services", "--out", str(services_out)), work)
        vectors_out = work / "vectors.json"
        scene = json.loads((self.vision / SCENE).read_text())
        boxes = [box["name"] for box in scene["boxes"]]
        scenes = {}
        for phase, visible in (("nonempty", boxes), ("empty", [])):
            scenes[phase] = work / f"scene-{phase}.json"
            scenes[phase].write_text(json.dumps({**scene, "sequence": [{"frames": 1, "visible": visible}]}, indent=1))
        expect, vectors = self.run_peer("expect-vectors", self.peer(
            "vision", "expect-vectors", "--scene", str(scenes["nonempty"]), "--out", str(vectors_out)), vectors_out)  # fmt: skip
        if vectors is None:
            self.add(_check("observe.vector", False, f"expect-vectors wrote nothing (exit {expect.returncode})"))
        for phase in ("nonempty", "empty", "vector", "malformed"):
            if phase == "vector" and vectors is None:
                continue
            self.observe(phase, scenes["empty" if phase == "empty" else "nonempty"], store, vectors_out, work, node)

        localize_out = work / "vision-client.json"
        client, result = self.run_peer("vision-client", self.peer(
            "vision", "vision-client", "--node", DATABASE_NODE, "--map-id", self.map_id, "--out", str(localize_out),
            overlay=True), localize_out)  # fmt: skip
        self.role_checks(client, result, "vision", overlay=True, groups={"localize": "localize.", "database-services": "db."})

        client_out = work / "database-client.json"
        client, result = self.run_peer("database-client", self.peer(
            "database", "database-client", "--vectors", str(vectors_out), "--out", str(client_out)), client_out)  # fmt: skip
        stopped_services = self.stop_server(services)
        served = _read_json(services_out)
        if result is not None:
            result.setdefault("checks", []).append(_check(
                "vision.node", stopped_services and served is not None and not served.get("fatal")
                and served.get("closed") is True and served.get("frames_received", 0) > 0
                and served.get("service_jobs", 0) > 0,
                "the vision node took frames, served jobs and stopped cleanly", served=served))  # fmt: skip
            if served is not None:
                result.setdefault("checks", []).append(_check(
                    "vision.node.paths", not (p := self.peer_paths("vision", served.get("evidence"), overlay=False)),
                    "; ".join(p) or "the vision node's imports are from its own build"))  # fmt: skip
        self.role_checks(client, result, "database", overlay=False, groups={"vision-services": ""})

        stopped_node = self.stop_server(node)
        ran = _read_json(node_out)
        node_problems = [] if ran is not None else ["the database node wrote no evidence"]
        if ran is not None:
            self.report["evidence"]["database-node"] = ran.get("evidence")
            node_problems = self.peer_paths("database", ran.get("evidence"), overlay=False)
        after_out = work / "rows-after.json"
        _, after = self.run_peer("rows-after", self.peer("database", "store-rows", "--db", str(store),
                                                         "--out", str(after_out)), after_out)  # fmt: skip
        self.add(rows_check(before, after, stopped_node, node_problems))

    def observe(self, phase: str, scene: Path, store: Path, vectors: Path, work: Path, node: Child) -> None:
        out, ready = work / f"listen-{phase}.json", work / f"listen-{phase}.ready"
        listener = self.runner.start(f"listen-{phase}", self.peer(
            "database", "database-listener", "--phase", phase, "--topic", OBJECTS_TOPIC,
            "--node", DATABASE_NODE, "--db", str(store), "--vectors", str(vectors),
            "--ready", str(ready), "--out", str(out)), work)  # fmt: skip
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not ready.exists() and listener.returncode is None and time.monotonic() < deadline:
            time.sleep(0.05)
        publisher = None
        published_out = work / f"publish-{phase}.json"
        if ready.exists():
            if phase == "malformed":
                argv = self.peer("vision", "vision-malformed", "--scene", str(scene), "--topic", OBJECTS_TOPIC,
                                 "--out", str(published_out))  # fmt: skip
            else:
                params = ["-p", f"objects_topic:={OBJECTS_TOPIC}", "-p", f"scene_file:={scene}", "-p", "rate_hz:=10.0",
                          "-p", "loop:=true", "-p", "publish_tf:=false", "-p", "crops_enabled:=true"]  # fmt: skip
                if phase == "vector":
                    params += ["-p", f"embedding_model:={VECTOR_MODEL}", "-p", "embedding_dim:=512"]
                argv = self.peer("vision", "vision-publisher", "--out", str(published_out), "--ros-args", *params)
            publisher = self.runner.start(f"publish-{phase}", argv, work)
        if not listener.wait(ROLE_TIMEOUT_S):
            listener.stopped_by = "timeout"
            self.runner.stop(listener)
        published = False
        if publisher is not None:
            published = self.stop_server(publisher)
        sent = _read_json(published_out)
        result = _read_json(out)
        if result is not None:
            paths = ["the publisher wrote no evidence"] if sent is None else self.peer_paths(
                "vision", sent.get("evidence"), overlay=False)  # fmt: skip
            if sent is not None:
                self.report["evidence"][f"publish-{phase}"] = sent.get("evidence")
            result.setdefault("checks", []).append(_check(
                f"{phase}.publisher", publisher is not None and published and not paths,
                "; ".join(paths) or "the publisher ran from its own build until the listener finished, and stopped cleanly",
                published=sent and {k: v for k, v in sent.items() if k != "evidence"}))  # fmt: skip
            result["checks"].append(_check(
                f"{phase}.database-node", node.returncode is None, "the database node was still running"))
        self.role_checks(listener, result, "database", overlay=False, groups={f"observe.{phase}": ""})

    def stop_server(self, child: Child) -> bool:
        """Stop a peer that runs until SIGINT. True when SIGINT alone stopped it with exit 0."""
        early = child.returncode
        self.runner.stop(child)
        return early is None and child.returncode == 0 and child.stopped_by == "SIGINT"

    def role_checks(self, child: Child, result: dict | None, side: str, *, overlay: bool, groups: dict[str, str]) -> None:
        """Turn a role's own checks into gate checks, grouped by the prefix of their names."""
        problems = []
        if child.returncode != 0:
            problems.append(f"{child.name} exited {child.returncode}")
        if result is None:
            problems.append(f"{child.name} wrote no result")
            sub = []
        else:
            sub = result.get("checks", [])
            problems += self.peer_paths(side, result.get("evidence"), overlay=overlay)
            self.report["evidence"][child.name] = result.get("evidence")
        for group, prefix in groups.items():
            mine = [c for c in sub if c["name"].startswith(prefix)] if prefix else sub
            failed = [c["name"] for c in mine if c["status"] != "pass"]
            ok = not problems and not failed and bool(mine)
            detail = "; ".join(problems + [f"failed: {failed}"] if failed else problems) or f"{len(mine)} checks passed"
            if not mine and not problems:
                detail = "the role made no checks"
            self.add(_check(group, ok, detail, checks=mine))

    # -- teardown and report --

    def finish(self) -> None:
        if self.runner is not None:
            self.runner.stop_all()
            records = [child.record() for child in self.runner.children]
            self.report["processes"] = records
            left = [c.name for c in self.runner.children if c.group_left or not c.finished()]
            killed = [c.name for c in self.runner.children if c.stopped_by and "SIGKILL" in c.stopped_by]
            timed_out = [c.name for c in self.runner.children if c.stopped_by and "timeout" in c.stopped_by]
            self.add(_check("processes", not timed_out, f"timed out: {timed_out}" if timed_out else
                            "no process ran past its limit"))  # fmt: skip
            self.add(_check("teardown", not left and not killed,
                            f"left running: {left}; needed SIGKILL: {killed}" if left or killed
                            else "every process and its group stopped"))  # fmt: skip
        names = {c["name"] for c in self.checks}
        for name in PLANNED:
            if name not in names:
                self.checks.append({"name": name, "status": "skip", "detail": "an earlier check failed, so this did not run",
                                    "evidence": {}})  # fmt: skip
        self.report["ended"] = _now()
        self.report["passed"] = self.passed()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, indent=1, sort_keys=False) + "\n")
        temporary.replace(self.report_path)

    def passed(self) -> bool:
        statuses = {c["name"]: c["status"] for c in self.checks}
        return (
            self.args.negative_control is None
            and all(statuses.get(name) == "pass" for name in PLANNED)
            and all(c["status"] == "pass" for c in self.checks)
        )


# The node's one write to object rows: on its first start after a seed or a
# restore, it stamps the seeded anchors with its clock (ADR-0023).
ANCHOR_STAMP = ("first_seen", "last_seen", "last_verified")
# The tables a RememberEvent may add to. Every other table keeps its row count.
EVENT_TABLES = ("event", "write_request")
EVENT_INDEX_PREFIX = "event_fts_"


def object_problems(before: list[dict], after: list[dict]) -> list[str]:
    """Why the object rows after the wire tests are not the seeded ones. Empty means they are.

    No row may be made or removed. The only change allowed is the anchors' first
    start stamp: one time in all three columns of a seeded anchor.
    """
    old, new = {r["id"]: r for r in before}, {r["id"]: r for r in after}
    if set(old) != set(new):
        return [f"object rows were made or removed: ids {sorted(old)} before, {sorted(new)} after"]
    problems = []
    for key in sorted(old):
        changed = sorted(c for c in old[key] if old[key][c] != new[key].get(c))
        if not changed:
            continue
        anchor = new[key].get("layer") == "anchor" and new[key].get("source") == "map"
        stamps = {new[key][c] for c in ANCHOR_STAMP}
        if not (anchor and set(changed) <= set(ANCHOR_STAMP) and len(stamps) == 1):
            problems.append(f"object {key} changed in {changed}")
    return problems


def rows_check(before: dict | None, after: dict | None, stopped: bool, node_problems: list[str] = ()) -> dict:
    """The store after the wire tests: the same objects, and only the one event the client wrote."""
    problems = list(node_problems)
    if not stopped:
        problems.append("the database node did not stop cleanly on SIGINT")
    if before is None or after is None:
        problems.append("the store's rows could not be read before and after")
    else:
        problems += object_problems(before["object"], after["object"])
        counts = {
            table: (before["counts"].get(table), after["counts"].get(table))
            for table in sorted(set(before["counts"]) | set(after["counts"]))
            if table not in EVENT_TABLES and not table.startswith(EVENT_INDEX_PREFIX)
            and before["counts"].get(table) != after["counts"].get(table)
        }  # fmt: skip
        if counts:
            problems.append(f"row counts changed outside the event log: {counts}")
        new_events = [e for e in after["event"] if e not in before["event"]]
        if len(new_events) != 1 or len(after["event"]) != len(before["event"]) + 1:
            problems.append(f"expected one new event, found {new_events}")
        new_requests = [r for r in after["write_request"] if r not in before["write_request"]]
        if [r["operation"] for r in new_requests] != ["remember_event"]:
            problems.append(f"expected only the event's write request, found {new_requests}")
    return _check("store-rows", not problems, "; ".join(problems) or "no object row was made, and only the "
                  "seeded anchors' first start stamp changed",
                  before=before, after=after)  # fmt: skip


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vision-root", required=True, help="the walkie-vision worktree")
    parser.add_argument("--database-root", required=True, help="the walkie-database worktree")
    parser.add_argument("--vision-commit", required=True, help="the full commit the vision worktree must be at")
    parser.add_argument("--database-commit", required=True, help="the full commit the database worktree must be at")
    parser.add_argument("--domain", required=True, type=int, help="the ROS domain of every peer")
    parser.add_argument("--build", action="store_true", help="rebuild both message installations and the client overlay")
    parser.add_argument("--report", required=True, help="the JSON report to write; its folder holds the run's files")
    parser.add_argument("--pixi", default=shutil.which("pixi") or str(Path.home() / ".pixi" / "bin" / "pixi"),
                        help="the pixi executable")  # fmt: skip
    parser.add_argument("--forbid", action="append", metavar="PATH",
                        help="a folder no peer may import from; the default is the owner's two checkouts")  # fmt: skip
    parser.add_argument("--negative-control", choices=("hash", "import"),
                        help="break one input on purpose; the gate must then fail before the wire tests")  # fmt: skip
    args = parser.parse_args(argv)
    if args.forbid is None:
        args.forbid = [str(Path.home() / "walkie-vision"), str(Path.home() / "walkie-database")]
    if not 0 <= args.domain <= 232:
        parser.error("--domain must be from 0 to 232")
    return args


def _interrupt(signum: int, _frame: object) -> None:
    raise Interrupted(signal.Signals(signum).name)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    gate = Gate(args)
    gate.report["argv"] = list(sys.argv if argv is None else argv)
    previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        gate.run()
    except (GateError, Interrupted) as e:
        gate.add(_check("harness", False, f"{type(e).__name__}: {e}"))
    finally:
        # A second signal must not cut the teardown short.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        gate.finish()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    for check in gate.checks:
        print(f"{check['status']:4}  {check['name']}: {check['detail']}"[:400])
    print(f"{'PASSED' if gate.passed() else 'FAILED'}; report {gate.report_path}")
    return 0 if gate.passed() else 1


if __name__ == "__main__":
    sys.exit(main())
