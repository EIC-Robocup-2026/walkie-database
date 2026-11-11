"""Saving the object memory to the store, and loading it back."""

import json
import sqlite3
from dataclasses import replace

import numpy as np
import pytest
from test_object_memory import MODEL, blob, embedding_near, seen, unit

from walkie_database.db import connect, transaction
from walkie_database.migrate import migrate
from walkie_database.object_context import relations, set_room_shape
from walkie_database.object_memory import ANCHOR, MOVABLE, Maintenance, MergeConfig, ObjectMemory
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


def stored_views(store, object_id):
    return store.execute("SELECT viewpoints FROM object WHERE id = ?", (object_id,)).fetchone()[0]


def test_a_save_whose_commit_fails_is_rolled_back_and_the_retry_writes_once(store):
    memory = ObjectMemory(CONFIG)
    cup = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    [object_id] = run(memory, store, [1.0, 2.0], lambda t: [seen(cup, stamp=t)]).inserted
    for stamp in (3.0, 4.0):
        memory.add([seen(cup, stamp=stamp)])
    maintenance = Maintenance(merged=memory.maintain(now=4.0).merged)
    [track] = memory.tracks

    def deny_commit(action, operation, *args):
        # SQLite keeps the transaction open after a failed COMMIT, as after SQLITE_BUSY.
        if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.set_authorizer(deny_commit)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        save(store, memory, maintenance, map_id=MAP)
    store.set_authorizer(None)
    assert not store.in_transaction
    assert (track.dirty, len(track.sightings), track.n_views) == (True, 2, 4)
    assert store.execute("SELECT count(*) FROM object_observation").fetchone()[0] == 2
    assert stored_views(store, object_id) == 2

    assert save(store, memory, maintenance, map_id=MAP).updated == [object_id]
    assert store.execute("SELECT count(*) FROM object_observation").fetchone()[0] == 4
    assert stored_views(store, object_id) == track.n_views == 4


def test_a_model_change_keeps_only_the_vector_of_the_new_model(store, cup_vector):
    """After a restart the object has the new model's vector and weight, not the old vector."""
    # A second model whose name sorts first, so a vector left behind would load first.
    sql = store.execute("SELECT sql FROM sqlite_master WHERE name = 'object_clip_vit_b16'").fetchone()[0]
    store.execute(sql.replace("object_clip_vit_b16", "object_aaa_b16", 1))
    old_vector = unit(np.random.default_rng(7).normal(size=512))
    memory = ObjectMemory(CONFIG)
    cup = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    [object_id] = run(memory, store, [1.0, 2.0], lambda t: [
        replace(seen(cup, stamp=t, embedding=embedding_near(old_vector, int(t))), model="aaa_b16")]).inserted
    assert store.execute("SELECT count(*) FROM object_aaa_b16 WHERE object_id = ?", (object_id,)).fetchone()[0] == 1

    run(memory, store, [3.0, 4.0], lambda t: [
        seen(cup, stamp=t, score=0.4, embedding=embedding_near(cup_vector, int(t)))])
    [track] = memory.tracks
    assert track.model == MODEL
    assert store.execute("SELECT count(*) FROM object_aaa_b16 WHERE object_id = ?", (object_id,)).fetchone()[0] == 0
    [loaded] = load_tracks(store, MAP, CONFIG)
    assert loaded.model == MODEL
    assert loaded.embedding_sum == pytest.approx(track.embedding_sum, abs=1e-5)


def test_stored_objects_that_saw_the_same_arrays_count_them_once_after_a_restart(store):
    """Two objects stored apart, seen in arrays 1 to 3 and 2 to 4, merge into 4 views, not 6."""
    memory = ObjectMemory(CONFIG)
    left = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    run(memory, store, [1.0, 2.0, 3.0, 4.0], lambda t: (
        ([seen(left, stamp=t)] if t <= 3.0 else []) + ([seen(left + [0.5, 0.0, 0.0], stamp=t)] if t >= 2.0 else [])))
    assert [stored_views(store, t.object_id) for t in memory.tracks] == [3, 3]

    restarted = ObjectMemory(CONFIG, load_tracks(store, MAP, CONFIG))
    first, second = sorted(restarted.tracks, key=lambda t: t.object_id)
    second.set_points(first.points + 0.003)  # a better localization shows one cup
    maintenance = Maintenance(merged=restarted.maintain(now=5.0).merged)
    assert restarted.tracks == [first]
    assert first.n_views == 6  # in memory the two histories are summed

    assert save(store, restarted, maintenance, map_id=MAP).merged == [(second.object_id, first.object_id)]
    assert stored_views(store, first.object_id) == first.n_views == 4
    [loaded] = load_tracks(store, MAP, CONFIG)
    assert loaded.n_views == 4


def test_the_first_save_after_a_restart_counts_the_stored_views_again(store):
    """A count an older build stored too high is replaced by the count of the stored arrays."""
    memory = ObjectMemory(CONFIG)
    cup = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    [object_id] = run(memory, store, [1.0, 2.0], lambda t: [seen(cup, stamp=t)]).inserted
    with transaction(store):
        store.execute("UPDATE object SET viewpoints = 9 WHERE id = ?", (object_id,))
    restarted = ObjectMemory(CONFIG, load_tracks(store, MAP, CONFIG))
    run(restarted, store, [3.0], lambda t: [seen(cup, stamp=t)])
    assert stored_views(store, object_id) == restarted.tracks[0].n_views == 3


def test_an_array_the_store_already_holds_is_not_counted_again(store):
    """A part split off in array 2 merges after the save that stored array 2 for the object."""
    memory = ObjectMemory(CONFIG)
    cup = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    stray = blob((1.6, 0.5, 0.8), (0.08, 0.08, 0.1), seed=5)  # too far to join the cup
    run(memory, store, [1.0, 2.0], lambda t: [seen(cup, stamp=t)] + ([seen(stray, stamp=t)] if t == 2.0 else []))
    stored = next(t for t in memory.tracks if t.object_id is not None)
    part = next(t for t in memory.tracks if t.object_id is None)
    assert (stored.n_views, stored.unsaved_views, part.unsaved_views) == (2, set(), {2.0})

    part.set_points(stored.points + 0.003)
    maintenance = Maintenance(merged=memory.maintain(now=3.0).merged)
    assert memory.tracks == [stored]
    assert stored.n_views == 3  # memory no longer holds array 2 for the stored cup

    save(store, memory, maintenance, map_id=MAP)
    assert stored_views(store, stored.object_id) == stored.n_views == 2


def test_memory_holds_only_the_views_the_store_lacks(store):
    memory = ObjectMemory(CONFIG)
    cup = blob((1.0, 0.5, 0.8), (0.08, 0.08, 0.1))
    for first in range(1, 50, 5):
        run(memory, store, [float(s) for s in range(first, first + 5)], lambda t: [seen(cup, stamp=t)])
        [track] = memory.tracks
        assert track.unsaved_views == set()
    assert stored_views(store, track.object_id) == track.n_views == 50
    [loaded] = load_tracks(store, MAP, CONFIG)
    assert loaded.n_views == 50
