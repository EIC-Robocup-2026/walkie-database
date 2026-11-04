"""tools/import_interfaces.py, on throwaway Git repositories.

Each test copies the script into a temporary repository root and runs it as a
real command, because the script finds its manifest from its own location.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "import_interfaces.py"
PACKAGE_XML = "ros2_ws/src/walkie_vision_msgs/package.xml"
GIT = shutil.which("git") or shutil.which("git", path="/usr/bin:/bin")


def git(*args: str, cwd: Path) -> str:
    # GIT_CONFIG_GLOBAL keeps a developer's own settings, such as commit
    # signing, out of these repositories.
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    done = subprocess.run(
        [GIT, *args], cwd=cwd, env=env, text=True, capture_output=True, check=True
    )
    return done.stdout.strip()


def commit_file(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git("add", path, cwd=repo)
    git("commit", "--quiet", "-m", f"change {path}", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def vision(tmp_path) -> Path:
    repo = tmp_path / "vision"
    repo.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=repo)
    commit_file(repo, PACKAGE_XML, "<package>one</package>\n")
    return repo


class Database:
    """A repository root that holds a copy of the script and a pin."""

    def __init__(self, root: Path, url: Path | str, pin: str) -> None:
        self.root = root
        (root / "tools").mkdir(parents=True)
        shutil.copy(SCRIPT, root / "tools" / "import_interfaces.py")
        self.write_manifest(url, pin)

    @property
    def clone(self) -> Path:
        return self.root / "ros2_ws" / "src" / "walkie-vision"

    def write_manifest(self, url: Path | str, version: str) -> None:
        (self.root / "ros2_ws.repos").write_text(
            "repositories:\n"
            "  ros2_ws/src/walkie-vision:\n"
            "    type: git\n"
            f"    url: {url}\n"
            f"    version: {version}\n"
        )

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.root / "tools" / "import_interfaces.py"), *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )


@pytest.fixture
def database(tmp_path, vision) -> Database:
    return Database(tmp_path / "database", vision, git("rev-parse", "HEAD", cwd=vision))


def pin_of(vision: Path) -> str:
    return git("rev-parse", "HEAD", cwd=vision)


def test_no_arguments_use_the_url_and_commit_in_the_manifest(database, vision):
    done = database.run()

    assert done.returncode == 0, done.stderr
    assert git("rev-parse", "HEAD", cwd=database.clone) == pin_of(vision)
    # A detached HEAD: no branch can move the build to another commit.
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=database.clone) == "HEAD"
    assert (database.clone / PACKAGE_XML).read_text() == "<package>one</package>\n"


def test_the_clone_is_a_real_clone_and_not_a_link(database, vision):
    pin = pin_of(vision)

    assert database.run("--vision-source", str(vision), "--vision-ref", pin).returncode == 0

    assert not database.clone.is_symlink()
    assert (database.clone / ".git").is_dir()
    assert not (database.clone / ".git" / "objects" / "info" / "alternates").exists()
    assert not (database.clone / PACKAGE_XML).is_symlink()


def test_local_source_keeps_its_checkout_and_changes(database, vision):
    pin = pin_of(vision)
    (vision / "uncommitted.txt").write_text("not committed\n")
    (vision / PACKAGE_XML).write_text("<package>edited</package>\n")
    before = git("status", "--porcelain", cwd=vision)

    done = database.run("--vision-source", str(vision), "--vision-ref", pin)

    assert done.returncode == 0, done.stderr
    assert pin_of(vision) == pin
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=vision) == "main"
    assert git("status", "--porcelain", cwd=vision) == before
    assert (vision / PACKAGE_XML).read_text() == "<package>edited</package>\n"
    # Only committed work reaches the clone.
    assert (database.clone / PACKAGE_XML).read_text() == "<package>one</package>\n"
    assert not (database.clone / "uncommitted.txt").exists()


def test_a_linked_worktree_is_a_valid_source(tmp_path, database, vision):
    worktree = tmp_path / "vision-worktree"
    git("worktree", "add", "--quiet", "-b", "prep", str(worktree), cwd=vision)
    pin = commit_file(worktree, "extra.txt", "from the worktree\n")
    database.write_manifest("https://example.invalid/not-used.git", pin)

    done = database.run("--vision-source", str(worktree), "--vision-ref", pin)

    assert done.returncode == 0, done.stderr
    assert git("rev-parse", "HEAD", cwd=database.clone) == pin
    assert (database.clone / "extra.txt").is_file()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree) == "prep"


def test_a_commit_on_no_branch_is_fetched_by_its_hash(tmp_path, database, vision):
    # A plain clone copies only what branches and tags reach.
    worktree = tmp_path / "detached"
    git("worktree", "add", "--quiet", "--detach", str(worktree), cwd=vision)
    pin = commit_file(worktree, "detached.txt", "on no branch\n")
    database.write_manifest("https://example.invalid/not-used.git", pin)

    done = database.run("--vision-source", str(worktree), "--vision-ref", pin)

    assert done.returncode == 0, done.stderr
    assert git("rev-parse", "HEAD", cwd=database.clone) == pin


def test_a_second_run_changes_nothing(database, vision):
    pin = pin_of(vision)
    assert database.run("--vision-source", str(vision), "--vision-ref", pin).returncode == 0

    done = database.run("--vision-source", str(vision), "--vision-ref", pin)

    assert done.returncode == 0, done.stderr
    assert git("rev-parse", "HEAD", cwd=database.clone) == pin
    assert git("status", "--porcelain", cwd=database.clone) == ""


def test_a_clean_clone_moves_to_a_new_pin(database, vision):
    assert database.run().returncode == 0
    new_pin = commit_file(vision, "second.txt", "second\n")
    database.write_manifest(vision, new_pin)

    done = database.run()

    assert done.returncode == 0, done.stderr
    assert git("rev-parse", "HEAD", cwd=database.clone) == new_pin


def test_no_fetch_happens_when_the_clone_already_has_the_commit(database, vision):
    pin = pin_of(vision)
    assert database.run().returncode == 0
    # Nothing can answer a fetch from this URL.
    database.write_manifest("https://example.invalid/not-used.git", pin)

    done = database.run()

    assert done.returncode == 0, done.stderr
    assert git("rev-parse", "HEAD", cwd=database.clone) == pin


@pytest.mark.parametrize(
    "ref",
    ["0" * 40, "main", "HEAD", "abc1234", "A" * 40, "0" * 39],
    ids=["other commit", "branch", "head", "short", "upper case", "too short"],
)
def test_a_ref_that_is_not_the_pin_is_refused(database, vision, ref):
    done = database.run("--vision-source", str(vision), "--vision-ref", ref)

    assert done.returncode == 1
    assert "--vision-ref" in done.stderr
    assert not database.clone.exists()


@pytest.mark.parametrize("arg", ["--vision-source", "--vision-ref"])
def test_the_two_arguments_go_together(database, arg):
    done = database.run(arg, "x")

    assert done.returncode == 2
    assert "go together" in done.stderr


def test_a_source_that_is_not_a_directory_is_refused(tmp_path, database, vision):
    done = database.run("--vision-source", str(tmp_path / "missing"), "--vision-ref", pin_of(vision))

    assert done.returncode == 1
    assert "not a directory" in done.stderr
    assert not database.clone.exists()


@pytest.mark.parametrize("version", ["main", "abc1234", "", "null"])
def test_a_manifest_without_a_full_commit_is_refused(database, vision, version):
    database.write_manifest(vision, version)

    done = database.run()

    assert done.returncode == 1
    assert "full commit" in done.stderr
    assert not database.clone.exists()


def test_a_manifest_without_the_dependency_is_refused(database):
    (database.root / "ros2_ws.repos").write_text("repositories: {}\n")

    done = database.run()

    assert done.returncode == 1
    assert "does not pin" in done.stderr


def test_a_changed_tracked_file_in_the_clone_is_refused(database):
    assert database.run().returncode == 0
    (database.clone / PACKAGE_XML).write_text("<package>edited</package>\n")

    done = database.run()

    assert done.returncode == 1
    assert "has changes" in done.stderr
    assert (database.clone / PACKAGE_XML).read_text() == "<package>edited</package>\n"


def test_an_untracked_file_in_the_clone_is_refused(database):
    assert database.run().returncode == 0
    (database.clone / "new.txt").write_text("mine\n")

    done = database.run()

    assert done.returncode == 1
    assert "has changes" in done.stderr
    assert (database.clone / "new.txt").is_file()


def test_a_directory_that_is_not_a_clone_is_refused(database):
    # Inside another repository, git would otherwise report on that repository
    # and call this directory clean.
    git("init", "--quiet", cwd=database.root)
    database.clone.mkdir(parents=True)
    (database.clone / "mine.txt").write_text("mine\n")

    done = database.run()

    assert done.returncode == 1
    assert "not a clone" in done.stderr
    assert (database.clone / "mine.txt").is_file()


def test_a_symlink_at_the_dependency_path_is_refused(database, vision):
    database.clone.parent.mkdir(parents=True)
    database.clone.symlink_to(vision)

    done = database.run()

    assert done.returncode == 1
    assert "symlink" in done.stderr
    assert database.clone.is_symlink()


def test_a_commit_the_source_lacks_leaves_no_clone(database, vision):
    pin = "1" * 40
    database.write_manifest(vision, pin)

    done = database.run("--vision-source", str(vision), "--vision-ref", pin)

    assert done.returncode == 1
    assert not database.clone.exists()


def test_a_commit_without_the_message_package_leaves_no_clone(database, vision):
    git("rm", "--quiet", PACKAGE_XML, cwd=vision)
    git("commit", "--quiet", "-m", "remove the package", cwd=vision)
    pin = pin_of(vision)
    database.write_manifest(vision, pin)

    done = database.run("--vision-source", str(vision), "--vision-ref", pin)

    assert done.returncode == 1
    assert "has no" in done.stderr
    assert not database.clone.exists()


def test_this_repository_pins_a_full_commit_and_ignores_the_clone():
    repos = yaml.safe_load((REPO / "ros2_ws.repos").read_text())
    pin = repos["repositories"]["ros2_ws/src/walkie-vision"]["version"]

    assert len(pin) == 40
    assert set(pin) <= set("0123456789abcdef")
    ignored = subprocess.run(
        [GIT, "check-ignore", "--quiet", "ros2_ws/src/walkie-vision/ros2_ws/src/x"],
        cwd=REPO,
        check=False,
    )
    assert ignored.returncode == 0
