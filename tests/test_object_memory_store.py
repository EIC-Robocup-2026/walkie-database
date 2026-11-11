"""Saving the object memory to the store, and loading it back."""

import json
import sqlite3

import numpy as np
import pytest
from test_object_memory import blob, embedding_near, seen, unit

from walkie_database.db import connect, transaction
from walkie_database.migrate import migrate
from walkie_database.object_context import relations, set_room_shape
from walkie_database.object_memory import ANCHOR, MOVABLE, MergeConfig, ObjectMemory
from walkie_database.object_memory_store import load_tracks, save, surface_key
from walkie_database.objects import search_object_vectors

MAP = "m1"
CONFIG = MergeConfig(confirm_views=2)


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "walkie.db")
    migrate(conn)
    with transaction(conn):
        conn.execute("INSERT INTO map (id, created_at) VALUES (?, 0)", (MAP,))
        conn.execute("INSERT INTO room (id, map_id, name, key) VALUES (1, ?, 'kitchen', 'kitchen')", (MAP,))
        set_room_shape(conn, 1, [[0, -2], [4, -2], [4, 2], [0, 2]])
    yield conn
    conn.close()


@pytest.fixture
def cup_vector():
    return unit(np.random.default_rng(42).normal(size=512))


def table_points(seed=0):
    top = blob((2.0, 0.0, 0.74), (1.2, 0.8, 0.02), count=1500, seed=seed)
    legs = np.vstack([blob((2.0 + dx, dy, 0.37), (0.04, 0.04, 0.7), count=80, seed=seed + i)
                      for i, (dx, dy) in enumerate(((-0.55, -0.35), (0.55, -0.35), (-0.55, 0.35), (0.55, 0.35)))])
    return np.vstack((top, legs))


def run(memory, store, stamps, detections_at):
    """Add one array per stamp, then one round of upkeep and a save, as the node's timer does."""
    from walkie_database.object_memory import Maintenance

    for stamp in stamps:
        memory.add(detections_at(stamp))
    maintenance = Maintenance()
    maintenance.merged.extend(memory.maintain(now=stamps[-1]).merged)
    return save(store, memory, maintenance, map_id=MAP)


def test_a_confirmed_object_is_stored_with_its_box_cloud_vector_labels_and_sightings(store, cup_vector):
    memory = ObjectMemory(CONFIG)
    saved = run(memory, store, [1.0, 2.0], lambda t: [
        seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=int(t)), stamp=t,
             embedding=embedding_near(cup_vector, int(t)))
    ])
    [object_id] = saved.inserted
    row = store.execute("SELECT * FROM object WHERE id = ?", (object_id,)).fetchone()
    assert (row["label"], row["state"], row["layer"], row["source"]) == ("cup", "present", MOVABLE, "perception")
    assert (row["n_detections"], row["viewpoints"], row["last_verified"]) == (2, 2, 2.0)
    assert row["room_id"] == 1
    assert (row["center_x"], row["center_y"]) == pytest.approx((1.0, 0.5), abs=0.02)
    assert store.execute("SELECT count(*) FROM object_box WHERE id = ?", (object_id,)).fetchone()[0] == 1
    cloud = store.execute("SELECT n_points, length(points) FROM object_cloud WHERE object_id = ?",
                          (object_id,)).fetchone()
    assert cloud[0] > 0 and cloud[1] == cloud[0] * 12
    assert store.execute("SELECT count(*) FROM object_observation WHERE object_id = ?",
                         (object_id,)).fetchone()[0] == 2
    assert dict(store.execute("SELECT label, count FROM object_label WHERE object_id = ?", (object_id,))) == {"cup": 2}
    [match] = search_object_vectors(store, "clip_vit_b16", cup_vector, map_id=MAP, states=("present",))
    assert match.object_id == object_id and match.distance < 0.1


def test_an_unconfirmed_track_is_not_stored(store):
    memory = ObjectMemory(CONFIG)
    saved = run(memory, store, [1.0], lambda t: [seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1)), stamp=t)])
    assert saved.inserted == []
    assert store.execute("SELECT count(*) FROM object").fetchone()[0] == 0


def test_a_later_view_updates_the_same_row(store):
    memory = ObjectMemory(CONFIG)
    [object_id] = run(memory, store, [1.0, 2.0], lambda t: [
        seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=int(t)), stamp=t)]).inserted
    revision = store.execute("SELECT revision FROM object WHERE id = ?", (object_id,)).fetchone()[0]
    saved = run(memory, store, [3.0], lambda t: [seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=3), stamp=t)])
    assert saved.inserted == [] and saved.updated == [object_id]
    row = store.execute("SELECT n_detections, last_seen, revision FROM object WHERE id = ?", (object_id,)).fetchone()
    assert (row[0], row[1]) == (3, 3.0) and row[2] > revision


def test_nothing_changed_writes_nothing(store):
    memory = ObjectMemory(CONFIG)
    run(memory, store, [1.0, 2.0], lambda t: [seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=int(t)), stamp=t)])
    changes = store.total_changes
    from walkie_database.object_memory import Maintenance

    assert save(store, memory, Maintenance(), map_id=MAP).updated == []
    assert store.total_changes == changes


def test_a_merge_of_two_stored_objects_leaves_a_tombstone(store):
    memory = ObjectMemory(CONFIG)
    points = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    run(memory, store, [1.0, 2.0], lambda t: [seen(points, stamp=t)])
    run(memory, store, [3.0, 4.0], lambda t: [seen(points + [0.5, 0.0, 0.0], stamp=t)])
    first, second = sorted(memory.tracks, key=lambda t: t.object_id)
    # The second cup was a misplaced view of the first: move it back, as a better pose would.
    second.set_points(first.points + 0.003)
    second.n_detections = 1
    saved = run(memory, store, [5.0], lambda t: [])
    assert saved.merged == [(second.object_id, first.object_id)]
    row = store.execute("SELECT state, merged_into FROM object WHERE id = ?", (second.object_id,)).fetchone()
    assert (row["state"], row["merged_into"]) == ("merged", first.object_id)


def test_an_anchor_gets_a_top_surface_and_a_cup_on_it_a_relation(store, cup_vector):
    memory = ObjectMemory(CONFIG)
    saved = run(memory, store, [1.0, 2.0], lambda t: [
        seen(table_points(int(t)), label="table", stamp=t),
        seen(blob((2.1, 0.1, 0.80), (0.07, 0.07, 0.1), seed=int(t)), label="cup", stamp=t,
             embedding=embedding_near(cup_vector, int(t))),
    ])
    tracks = {t.label: t for t in memory.tracks}
    table, cup = tracks["table"], tracks["cup"]
    assert table.layer == ANCHOR
    surface = store.execute("SELECT * FROM surface WHERE key = ?", (surface_key(table.object_id),)).fetchone()
    assert surface["z"] == pytest.approx(0.75, abs=0.02)
    assert len(json.loads(surface["outline"])) == 4
    [row] = relations(store, src_id=cup.object_id)
    assert (row["dst_id"], row["predicate"], row["surface_id"]) == (table.object_id, "on", surface["id"])
    assert saved.relations == {cup.object_id: (table.object_id, surface["id"])}


def test_a_table_seen_with_a_bottle_on_it_keeps_its_surface_under_the_bottle(store):
    """The table's mask takes in the bottle, so its box top is the bottle's top."""
    memory = ObjectMemory(CONFIG)

    def bottle(stamp):
        return blob((2.3, -0.1, 0.86), (0.07, 0.07, 0.22), count=400, seed=int(stamp) + 10)

    saved = run(memory, store, [1.0, 2.0], lambda t: [
        seen(np.vstack((table_points(int(t)), bottle(t))), label="table", stamp=t),
        seen(bottle(t), label="bottle", stamp=t),
    ])
    tracks = {t.label: t for t in memory.tracks}
    table, bottle_track = tracks["table"], tracks["bottle"]
    assert table.box.top > 0.85
    surface = store.execute("SELECT * FROM surface WHERE key = ?", (surface_key(table.object_id),)).fetchone()
    assert surface["z"] == pytest.approx(0.75, abs=0.02)
    assert saved.relations == {bottle_track.object_id: (table.object_id, surface["id"])}


def test_a_cup_lifted_off_the_table_loses_its_relation(store):
    memory = ObjectMemory(CONFIG)
    run(memory, store, [1.0, 2.0], lambda t: [
        seen(table_points(int(t)), label="table", stamp=t),
        seen(blob((2.1, 0.1, 0.80), (0.07, 0.07, 0.1), seed=int(t)), label="cup", stamp=t),
    ])
    cup = next(t for t in memory.tracks if t.label == "cup")
    cup.set_points(cup.points + [0.0, 0.0, 0.5])
    cup.needs_cleanup = cup.dirty = True
    saved = run(memory, store, [3.0], lambda t: [])
    assert saved.relations == {}
    assert relations(store, src_id=cup.object_id) == []


def test_a_restart_loads_the_stored_objects_and_merges_into_them(store, cup_vector):
    memory = ObjectMemory(CONFIG)
    [object_id] = run(memory, store, [1.0, 2.0], lambda t: [
        seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=int(t)), stamp=t,
             embedding=embedding_near(cup_vector, int(t)))]).inserted
    restarted = ObjectMemory(CONFIG, load_tracks(store, MAP, CONFIG))
    [track] = restarted.tracks
    assert (track.object_id, track.label, track.n_views, track.fixed) == (object_id, "cup", 2, False)
    assert float(np.dot(track.embedding, cup_vector)) > 0.9
    saved = run(restarted, store, [3.0], lambda t: [
        seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=3), stamp=t, embedding=embedding_near(cup_vector, 3))])
    assert saved.inserted == [] and saved.updated == [object_id]


def test_a_restart_keeps_the_weight_of_the_embeddings_and_the_measured_surface(store, cup_vector):
    """A restarted node merges as an uninterrupted one: same embedding sum, same tabletop."""
    memory = ObjectMemory(CONFIG)

    def bottle(stamp):
        return blob((2.3, -0.1, 0.86), (0.07, 0.07, 0.22), count=400, seed=int(stamp) + 10)

    run(memory, store, [1.0, 2.0], lambda t: [
        seen(np.vstack((table_points(int(t)), bottle(t))), label="table", stamp=t),
        seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=int(t)), stamp=t, score=0.4,
             embedding=embedding_near(cup_vector, int(t))),
    ])
    tracks = {t.label: t for t in memory.tracks}
    loaded = {t.object_id: t for t in load_tracks(store, MAP, CONFIG)}
    table, cup = loaded[tracks["table"].object_id], loaded[tracks["cup"].object_id]
    assert table.box.top > 0.85
    assert table.surface_z == pytest.approx(tracks["table"].surface_z)
    assert table.surface_z == pytest.approx(0.75, abs=0.02)
    assert cup.surface_z is None
    assert cup.embedding_sum == pytest.approx(tracks["cup"].embedding_sum, abs=1e-5)


def test_a_vector_stored_without_its_weight_loads_as_one_detection(store, cup_vector):
    memory = ObjectMemory(CONFIG)
    [object_id] = run(memory, store, [1.0, 2.0], lambda t: [
        seen(blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1), seed=int(t)), stamp=t,
             embedding=embedding_near(cup_vector, int(t)))]).inserted
    with transaction(store):
        store.execute("UPDATE object SET fingerprint_weight = 0 WHERE id = ?", (object_id,))
    [track] = load_tracks(store, MAP, CONFIG)
    assert float(np.linalg.norm(track.embedding_sum)) == pytest.approx(1.0)


def test_a_failed_save_changes_nothing_and_the_retry_writes_everything_once(store, monkeypatch):
    import walkie_database.object_memory_store as memory_store
    from walkie_database.object_memory import Maintenance

    memory = ObjectMemory(CONFIG)
    points = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    run(memory, store, [1.0, 2.0], lambda t: [seen(points, stamp=t)])
    run(memory, store, [3.0, 4.0], lambda t: [seen(points + [0.5, 0.0, 0.0], stamp=t)])
    first, second = sorted(memory.tracks, key=lambda t: t.object_id)
    # The second cup was a misplaced view of the first, and a third cup is new.
    second.set_points(first.points + 0.003)
    second.n_detections = 1
    for stamp in (5.0, 6.0):
        memory.add([seen(blob((3.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=int(stamp)), stamp=stamp)])
    maintenance = Maintenance()
    maintenance.merged.extend(memory.maintain(now=6.0).merged)
    new = next(t for t in memory.tracks if t.object_id is None)
    sightings = len(new.sightings)
    observations = store.execute("SELECT count(*) FROM object_observation").fetchone()[0]
    objects = store.execute("SELECT count(*) FROM object").fetchone()[0]

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(memory_store, "_write_relations", fail)
    with pytest.raises(sqlite3.OperationalError):
        save(store, memory, maintenance, map_id=MAP)
    assert (new.object_id, new.dirty, len(new.sightings)) == (None, True, sightings)
    assert first.dirty
    assert maintenance.merged == [(second.object_id, first.object_id)]
    assert store.execute("SELECT state FROM object WHERE id = ?", (second.object_id,)).fetchone()[0] == "present"
    assert store.execute("SELECT count(*) FROM object").fetchone()[0] == objects
    assert store.execute("SELECT count(*) FROM object_observation").fetchone()[0] == observations

    monkeypatch.undo()
    saved = save(store, memory, maintenance, map_id=MAP)
    assert saved.inserted == [new.object_id]
    assert saved.merged == [(second.object_id, first.object_id)]
    assert store.execute("SELECT count(*) FROM object_observation WHERE object_id = ?",
                         (new.object_id,)).fetchone()[0] == sightings
    assert store.execute("SELECT count(*) FROM object_observation").fetchone()[0] == observations + sightings
    assert (maintenance.merged, new.dirty, new.sightings) == ([], False, [])


def test_a_seeded_anchor_loads_as_a_fixed_track_and_keeps_its_geometry(store):
    with transaction(store):
        store.execute(
            """INSERT INTO object (id, map_id, label, source, layer, state, p_exist,
                                   center_x, center_y, center_z, size_x, size_y, size_z,
                                   first_seen, last_seen)
               VALUES (5, ?, 'table', 'map', 'anchor', 'present', 0.95, 2.0, 0.0, 0.37, 1.2, 0.8, 0.75, 0, 0)""",
            (MAP,),
        )
    memory = ObjectMemory(CONFIG, load_tracks(store, MAP, CONFIG))
    [anchor] = memory.tracks
    assert anchor.fixed and anchor.object_id == 5
    saved = run(memory, store, [1.0], lambda t: [seen(table_points(1), label="table", stamp=t)])
    assert saved.inserted == [] and saved.updated == [5]
    row = store.execute("SELECT center_x, size_x, n_detections, last_verified FROM object WHERE id = 5").fetchone()
    # A seeded row starts with one detection, the survey.
    assert tuple(row) == (2.0, 1.2, 2, 1.0)
