"""tools/replay_memory.py's merging, from an empty store or a seed, with no bag."""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from seed_helpers import anchor, anchors_document, map_id_of, rooms_document, seed_argv, write_map_folder

from walkie_database import seed_store
from walkie_database.db import connect
from walkie_database.object_memory import ANCHOR, MapDetection, MergeConfig

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tools_replay_memory", REPO / "tools" / "replay_memory.py")
replay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = replay  # its dataclasses resolve their annotations through it
spec.loader.exec_module(replay)

# A seeded sofa, and a strip of it that the detector calls a table in three arrays.
SOFA = anchor(
    "sofa", center=[2.0, 2.0, 0.4], size=[0.6, 1.8, 0.8],
    surfaces=[{"key": "sofa_top", "z": 0.45, "outline": [[1.75, 1.35], [2.25, 1.35], [2.25, 2.65], [1.75, 2.65]]}],
)  # fmt: skip


def strip(stamp: float) -> MapDetection:
    rng = np.random.default_rng(int(stamp))
    points = np.array([1.95, 2.0, 0.6]) + (rng.random((1500, 3)) - 0.5) * np.array([0.3, 1.6, 0.3])
    return MapDetection(label="table", score=0.9, points=points, stamp=stamp, view_quality=0.5)


def intake() -> "replay.Intake":
    steps = [replay.Step(now=float(t), detections=[strip(float(t))]) for t in (1, 2, 3)]
    return replay.Intake(steps=[*steps, replay.Step(now=4.0)])


def build_seed(tmp_path: Path) -> Path:
    folder = write_map_folder(tmp_path / "map")
    output = tmp_path / "seed" / "seed.db"
    map_id = map_id_of(folder)
    argv = seed_argv(tmp_path, folder, output, rooms=rooms_document(map_id),
                     anchors=anchors_document(map_id, anchors=[SOFA]))
    assert seed_store.main(argv) == 0
    return output


def anchors(result) -> list:
    return [t for t in result.memory.confirmed() if t.layer == ANCHOR]


def test_without_a_seed_the_strip_is_an_anchor_of_its_own():
    result = replay.run(intake(), MergeConfig())
    (table,) = anchors(result)
    assert table.label == "table"
    assert not table.fixed


def test_a_seed_starts_the_memory_from_its_anchors_as_the_node_does(tmp_path):
    seed = build_seed(tmp_path)

    result = replay.run(intake(), MergeConfig(), str(seed))

    (sofa,) = anchors(result)
    assert (sofa.fixed, sofa.label, sofa.labels) == (True, "sofa", {"sofa": 4})
    assert result.saves >= 1


def test_the_seed_is_only_read(tmp_path):
    seed = build_seed(tmp_path)
    before = seed.read_bytes()

    replay.run(intake(), MergeConfig(), str(seed))

    assert seed.read_bytes() == before
    conn = connect(seed, readonly=True)
    assert conn.execute("SELECT count(*) FROM object").fetchone()[0] == 1
    conn.close()


def test_a_missing_seed_stops_the_replay(tmp_path):
    with pytest.raises(SystemExit, match="is not a file"):
        replay.run(intake(), MergeConfig(), str(tmp_path / "none.db"))
