#!/usr/bin/env python3
"""Clone the pinned walkie-vision commit into ros2_ws/src/walkie-vision.

The database's interfaces nest walkie_vision_msgs, so colcon must build it from
the commit that ros2_ws.repos pins. Run this before `pixi run build-msgs`.

With no arguments, the URL and the commit come from ros2_ws.repos. With
--vision-source and --vision-ref, the commit comes from a local Git repository,
and it must still equal the pin. Only committed objects are read from that
repository: its working tree and HEAD are never touched.

The clone is a real clone at a detached HEAD, in a path Git ignores. An existing
clone with changes is refused, never overwritten.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "ros2_ws.repos"
DEPENDENCY = "ros2_ws/src/walkie-vision"
MESSAGE_PACKAGE = "ros2_ws/src/walkie_vision_msgs/package.xml"
FULL_SHA = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    """A refusal, with the reason to show the user."""


def find_git() -> str:
    # Under `pixi run --clean-env`, PATH holds only the environment's bin, which
    # has no git.
    git = shutil.which("git") or shutil.which("git", path="/usr/bin:/bin")
    if git is None:
        raise Refused("git is not installed")
    return git


def run_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    # A variable such as GIT_DIR, set by a hook or a wrapper, would redirect
    # these commands to another repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        [find_git(), *args], cwd=cwd, env=env, text=True, capture_output=True, check=False
    )


def checked_git(*args: str, cwd: Path | None = None) -> str:
    result = run_git(*args, cwd=cwd)
    if result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def read_pin(root: Path) -> tuple[str, str]:
    """Return the URL and the full commit of the vision dependency."""
    path = root / MANIFEST
    try:
        repos = yaml.safe_load(path.read_text())
        entry = repos["repositories"][DEPENDENCY]
        kind, url, version = entry["type"], entry["url"], entry["version"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as e:
        raise Refused(f"{path} does not pin {DEPENDENCY}: {e}") from e
    if kind != "git" or not isinstance(url, str) or not url:
        raise Refused(f"{path} must give {DEPENDENCY} a git url")
    if not isinstance(version, str) or not FULL_SHA.fullmatch(version):
        raise Refused(f"{path} must pin {DEPENDENCY} to a full commit, not {version!r}")
    return url, version


def check_existing_clone(dependency: Path) -> None:
    """Refuse anything at the dependency path except a clean clone of its own."""
    if dependency.is_symlink():
        raise Refused(f"{dependency} is a symlink. Remove it: the dependency must be a clone.")
    # Without this check, git would find the repository around this path and
    # report on that one.
    if not (dependency / ".git").is_dir():
        raise Refused(f"{dependency} exists and is not a clone. Remove it first.")
    status = checked_git("status", "--porcelain", "--untracked-files=all", cwd=dependency)
    if status:
        raise Refused(f"{dependency} has changes. Commit or remove them first:\n{status}")


def has_commit(dependency: Path, commit: str) -> bool:
    return run_git("cat-file", "-e", f"{commit}^{{commit}}", cwd=dependency).returncode == 0


def import_vision(root: Path, source: str, commit: str) -> Path:
    """Leave the dependency path at a detached, clean checkout of commit."""
    dependency = root / DEPENDENCY
    created = False
    if dependency.exists() or dependency.is_symlink():
        check_existing_clone(dependency)
    else:
        dependency.parent.mkdir(parents=True, exist_ok=True)
        # --no-local copies objects through Git's transport, as a remote clone
        # does, so the clone shares no hard-linked files with the source.
        checked_git("clone", "--quiet", "--no-local", "--no-checkout", source, str(dependency))
        created = True
    try:
        if not has_commit(dependency, commit):
            checked_git("fetch", "--quiet", "--no-tags", source, commit, cwd=dependency)
        if not has_commit(dependency, commit):
            raise Refused(f"{source} does not have commit {commit}")
        checked_git("checkout", "--quiet", "--detach", commit, cwd=dependency)
        head = checked_git("rev-parse", "HEAD", cwd=dependency)
        if head != commit:
            raise Refused(f"{dependency} is at {head}, not {commit}")
        if not (dependency / MESSAGE_PACKAGE).is_file():
            raise Refused(f"Commit {commit} has no {MESSAGE_PACKAGE}")
    except Refused:
        if created:
            shutil.rmtree(dependency)
        raise
    return dependency


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--vision-source", metavar="PATH", help="a local Git repository that holds the pinned commit"
    )
    parser.add_argument(
        "--vision-ref", metavar="FULL_SHA", help="the commit to import, which must equal the pin"
    )
    args = parser.parse_args(argv)
    if (args.vision_source is None) != (args.vision_ref is None):
        parser.error("--vision-source and --vision-ref go together")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        url, pin = read_pin(ROOT)
        source = url
        if args.vision_source is not None:
            if not FULL_SHA.fullmatch(args.vision_ref):
                raise Refused(f"--vision-ref must be a full commit, not {args.vision_ref!r}")
            if args.vision_ref != pin:
                raise Refused(
                    f"--vision-ref {args.vision_ref} is not the commit {MANIFEST} pins, {pin}"
                )
            source_path = Path(args.vision_source).resolve()
            if not source_path.is_dir():
                raise Refused(f"--vision-source {source_path} is not a directory")
            source = str(source_path)
        dependency = import_vision(ROOT, source, pin)
    except Refused as e:
        print(f"import_interfaces: {e}", file=sys.stderr)
        return 1
    print(f"{dependency} is at {pin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
