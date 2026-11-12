"""The object memory's merge rules, with no ROS and no store."""

import math

import numpy as np
import pytest

from walkie_database.object_memory import (
    ANCHOR,
    MOVABLE,
    Box,
    MapDetection,
    MergeConfig,
    ObjectMemory,
    Track,
    largest_cluster,
    surface_height,
    upright_box,
    voxel_downsample,
)

MODEL = "clip_vit_b16"


def blob(center, size, count=400, seed=0) -> np.ndarray:
    """Points spread evenly through a box."""
    rng = np.random.default_rng(seed)
    return np.asarray(center) + (rng.random((count, 3)) - 0.5) * np.asarray(size)


def unit(vector) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float64)
    return vector / np.linalg.norm(vector)


def embedding_near(base, seed, spread=0.2) -> np.ndarray:
    """A unit vector close to *base*: another view of the same thing."""
    rng = np.random.default_rng(seed)
    return unit(base + spread * unit(rng.normal(size=base.size)))


def seen(points, label="cup", stamp=1.0, embedding=None, score=0.9) -> MapDetection:
    return MapDetection(
        label=label, score=score, points=points, stamp=stamp, view_quality=0.5,
        model=MODEL if embedding is not None else "", embedding=embedding,
    )


@pytest.fixture
def cup_vector():
    return unit(np.random.default_rng(42).normal(size=512))


# Building blocks ---------------------------------------------------------------


def test_downsampling_keeps_one_mean_point_per_voxel():
    points = np.array([[0.001, 0.001, 0.001], [0.003, 0.002, 0.004], [0.5, 0.5, 0.5]])
    kept = voxel_downsample(points, 0.025)
    assert len(kept) == 2
    assert np.allclose(sorted(kept[:, 0]), [0.002, 0.5])


def test_the_box_follows_the_main_horizontal_direction():
    yaw = math.radians(30)
    rng = np.random.default_rng(1)
    local = (rng.random((2000, 3)) - 0.5) * [1.2, 0.4, 0.1]
    c, s = math.cos(yaw), math.sin(yaw)
    points = np.column_stack(
        (c * local[:, 0] - s * local[:, 1] + 2.0, s * local[:, 0] + c * local[:, 1] - 1.0, local[:, 2] + 0.7)
    )
    box = upright_box(points, percentile=0.0, minimum=0.01)
    assert box.yaw == pytest.approx(yaw, abs=0.03)
    assert box.size == pytest.approx((1.2, 0.4, 0.1), abs=0.03)
    assert box.center == pytest.approx((2.0, -1.0, 0.7), abs=0.02)


def test_a_box_with_few_points_is_aligned_with_the_map_and_never_flat():
    box = upright_box(np.array([[1.0, 1.0, 1.0]]), percentile=2.0, minimum=0.025)
    assert box.yaw == 0.0
    assert box.size == (0.025, 0.025, 0.025)


def test_the_largest_cluster_drops_stray_points():
    cup = blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), count=300)
    stray = blob((1.6, 1.0, 0.8), (0.02, 0.02, 0.02), count=12, seed=3)
    kept = largest_cluster(np.vstack((cup, stray)), eps=0.08, min_points=8)
    assert len(kept) == len(cup)


def test_a_box_knows_which_points_it_holds():
    box = Box((0.0, 0.0, 0.5), (2.0, 1.0, 1.0), yaw=math.pi / 2)
    inside = box.contains(np.array([[0.0, 0.9, 0.5], [0.9, 0.0, 0.5]]))
    assert inside.tolist() == [True, False]


# Matching ----------------------------------------------------------------------


def test_two_views_of_one_cup_make_one_object(cup_vector):
    memory = ObjectMemory()
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), embedding=embedding_near(cup_vector, 1))])
    memory.add([seen(blob((1.01, 1.0, 0.8), (0.08, 0.08, 0.1), seed=5), stamp=2.0,
                     embedding=embedding_near(cup_vector, 2))])
    [track] = memory.tracks
    assert track.n_detections == 2
    assert track.n_views == 2


def test_two_cups_apart_stay_two_objects(cup_vector):
    memory = ObjectMemory()
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), embedding=embedding_near(cup_vector, 1))])
    memory.add([seen(blob((1.3, 1.0, 0.8), (0.08, 0.08, 0.1), seed=5), stamp=2.0,
                     embedding=embedding_near(cup_vector, 2))])
    assert len(memory.tracks) == 2


def test_without_embeddings_a_small_object_named_otherwise_joins_where_it_overlaps():
    memory = ObjectMemory()
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), label="cup")])
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=5), label="bowl", stamp=2.0)])
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=6), label="cup", stamp=3.0)])
    [track] = memory.tracks
    assert track.labels == {"cup": 2, "bowl": 1}


def test_without_embeddings_another_label_needs_more_overlap_than_the_same_label():
    """A view 0.15 m off overlaps the cup a little: enough for a cup, not for a bowl."""
    cup = blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1))
    beside = blob((1.15, 1.0, 0.8), (0.08, 0.08, 0.1), seed=5)
    named_alike, named_otherwise = ObjectMemory(), ObjectMemory()
    for memory, label in ((named_alike, "cup"), (named_otherwise, "bowl")):
        memory.add([seen(cup, label="cup")])
        memory.add([seen(beside, label=label, stamp=2.0)])
    assert len(named_alike.tracks) == 1
    assert len(named_otherwise.tracks) == 2


def test_with_no_other_label_similarity_only_the_same_label_joins():
    memory = ObjectMemory(MergeConfig(other_label_similarity=0.0))
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), label="cup")])
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=5), label="bowl", stamp=2.0)])
    assert sorted(t.label for t in memory.tracks) == ["bowl", "cup"]


def test_an_anchor_answers_only_to_its_main_label():
    """A table once seen as a chair must not pull in the stool against it."""
    table = Track(key=1, layer=ANCHOR, points=voxel_downsample(blob((1.0, 0.0, 0.75), (1.0, 0.8, 0.03), 800), 0.025),
                  labels={"table": 3, "chair": 1}, first_seen=1.0, last_seen=1.0)
    memory = ObjectMemory(tracks=[table])
    # The stool's mask takes in a strip of the table's edge, as YOLOE masks do.
    stool = blob((1.0, 0.55, 0.45), (0.3, 0.3, 0.1), count=400, seed=3)
    edge = blob((1.0, 0.42, 0.74), (0.3, 0.05, 0.02), count=100, seed=4)
    memory.add([seen(np.vstack((stool, edge)), label="chair", stamp=2.0)])
    assert sorted((t.label, t.n_detections) for t in memory.tracks) == [("chair", 1), ("table", 0)]
    memory.add([seen(blob((1.2, 0.0, 0.75), (0.6, 0.6, 0.03), 400, seed=6), label="table", stamp=3.0)])
    assert table.n_detections == 1


def test_parts_of_the_building_are_dropped():
    memory = ObjectMemory()
    memory.add([seen(blob((1.0, 1.0, 1.2), (2.0, 0.05, 2.4), count=800), label="wall")])
    assert memory.tracks == []


def test_an_alike_embedding_lets_another_label_join_and_the_majority_names_it(cup_vector):
    memory = ObjectMemory()
    for stamp, label in ((1.0, "cup"), (2.0, "bowl"), (3.0, "cup")):
        points = blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=int(stamp))
        memory.add([seen(points, label=label, stamp=stamp, embedding=embedding_near(cup_vector, int(stamp)))])
    [track] = memory.tracks
    assert track.labels == {"cup": 2, "bowl": 1}
    assert track.label == "cup"


def test_a_cup_in_a_shelf_never_joins_the_shelf(cup_vector):
    """The cup lies inside the shelf's box. Only the layers keep them apart."""
    memory = ObjectMemory()
    shelf_vector = embedding_near(cup_vector, 9, spread=0.5)
    shelf = np.vstack([blob((2.0, 0.0, z), (0.8, 0.4, 0.02), count=300, seed=int(z * 10)) for z in (0.3, 0.7, 1.1)])
    memory.add([seen(shelf, label="shelf", embedding=shelf_vector)])
    memory.add([seen(blob((2.0, 0.0, 0.76), (0.06, 0.06, 0.1)), label="cup", stamp=2.0, embedding=cup_vector)])
    assert sorted((t.label, t.layer) for t in memory.tracks) == [("cup", MOVABLE), ("shelf", ANCHOR)]


def test_partial_views_of_a_table_join():
    memory = ObjectMemory()
    memory.add([seen(blob((1.0, 0.0, 0.75), (1.0, 0.8, 0.03), count=800), label="table")])
    memory.add([seen(blob((1.6, 0.0, 0.75), (1.0, 0.8, 0.03), count=800, seed=2), label="table", stamp=2.0)])
    [table] = memory.tracks
    assert table.layer == ANCHOR
    assert table.n_views == 2


def test_two_detections_in_one_array_are_one_view():
    memory = ObjectMemory()
    points = blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1))
    memory.add([seen(points, label="cup"), seen(points, label="cup")])
    [track] = memory.tracks
    assert (track.n_detections, track.n_views) == (2, 1)


# Upkeep ------------------------------------------------------------------------


def test_a_track_is_confirmed_after_enough_views():
    memory = ObjectMemory(MergeConfig(confirm_views=3))
    for stamp in (1.0, 2.0):
        memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=int(stamp)), stamp=stamp)])
    assert memory.confirmed() == []
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=3), stamp=3.0)])
    assert len(memory.confirmed()) == 1


def test_an_unconfirmed_ghost_is_dropped_and_a_confirmed_object_is_kept():
    memory = ObjectMemory(MergeConfig(confirm_views=2, ghost_age_s=10.0))
    memory.add([seen(blob((5.0, 5.0, 0.8), (0.08, 0.08, 0.1)), stamp=1.0)])
    for stamp in (1.0, 2.0):
        memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1), seed=int(stamp)), stamp=stamp)])
    result = memory.maintain(now=12.0)
    assert result.dropped == 1
    [kept] = memory.tracks
    assert kept.box.center == pytest.approx((1.0, 1.0, 0.8), abs=0.02)


def test_an_anchors_top_surface_lies_under_the_things_on_it():
    table = blob((1.0, 0.0, 0.75), (1.0, 0.8, 0.01), count=4000)
    bottle = blob((1.2, 0.1, 0.86), (0.07, 0.07, 0.22), count=600, seed=2)
    memory = ObjectMemory()
    memory.add([seen(np.vstack((table, bottle)), label="table")])
    memory.maintain(now=1.0)
    [track] = memory.tracks
    assert track.box.top > 0.85
    assert track.surface_z == pytest.approx(0.75, abs=0.01)


def test_the_surface_of_a_flat_anchor_is_its_only_layer():
    """All points in one or two height bins: no neighbours to count."""
    assert surface_height(np.array([[0.0, 0.0, 0.4]]), bin_size=0.02, min_share=0.3) == pytest.approx(0.4)
    two = np.array([[0.0, 0.0, 0.40], [0.1, 0.0, 0.425], [0.2, 0.0, 0.425]])
    assert surface_height(two, bin_size=0.02, min_share=0.3) == pytest.approx(0.425)
    assert surface_height(np.zeros((0, 3)), bin_size=0.02, min_share=0.3) is None


def test_a_movable_tracks_surface_is_not_measured():
    memory = ObjectMemory()
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)))])
    memory.maintain(now=1.0)
    [track] = memory.tracks
    assert track.surface_z is None


def test_upkeep_boxes_a_changed_track():
    memory = ObjectMemory()
    memory.add([seen(blob((1.0, 1.0, 0.8), (0.3, 0.1, 0.2), count=2000))])
    memory.maintain(now=1.0)
    [track] = memory.tracks
    assert sorted(track.box.size[:2]) == pytest.approx([0.1, 0.3], abs=0.03)
    assert track.box.size[2] == pytest.approx(0.2, abs=0.03)


def test_duplicates_merge_into_the_stored_object_seen_most():
    points = blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1))
    first = Track(key=1, layer=MOVABLE, points=voxel_downsample(points, 0.025), labels={"cup": 2},
                  first_seen=1.0, last_seen=2.0, n_detections=2, n_views=2, object_id=7)
    second = Track(key=2, layer=MOVABLE, points=voxel_downsample(points + 0.005, 0.025), labels={"cup": 5},
                   first_seen=3.0, last_seen=4.0, n_detections=5, n_views=5, object_id=9)
    memory = ObjectMemory(tracks=[first, second])
    result = memory.maintain(now=5.0)
    assert result.merged == [(7, 9)]
    [winner] = memory.tracks
    assert (winner.object_id, winner.n_detections, winner.first_seen) == (9, 7, 1.0)
    assert winner.dirty


def test_a_merged_track_is_measured_again_in_the_same_round():
    """The save after the round must see the box of the merged cloud, not the winner's old one."""
    first = Track(key=1, layer=MOVABLE, points=voxel_downsample(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), 0.025),
                  labels={"cup": 5}, first_seen=1.0, last_seen=2.0, n_detections=5, n_views=5)
    second = Track(key=2, layer=MOVABLE,
                   points=voxel_downsample(blob((1.06, 1.0, 0.8), (0.08, 0.08, 0.1), seed=1), 0.025),
                   labels={"cup": 2}, first_seen=1.0, last_seen=2.0, n_detections=2, n_views=2)
    memory = ObjectMemory(tracks=[first, second])
    memory.maintain(now=3.0)
    [winner] = memory.tracks
    assert winner is first
    assert winner.box.bounds()[1][0] > 1.07
    assert winner.dirty and not winner.needs_cleanup
    winner.dirty = False  # as a save leaves it
    memory.maintain(now=4.0)
    assert not winner.dirty


def test_merged_tracks_count_an_array_they_both_saw_once():
    points = voxel_downsample(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), 0.025)

    def track(key, stamps):
        return Track(key=key, layer=MOVABLE, points=points + 0.005 * key, labels={"cup": len(stamps)},
                     first_seen=1.0, last_seen=max(stamps), n_detections=len(stamps), n_views=len(stamps),
                     unsaved_views=set(stamps))

    memory = ObjectMemory(MergeConfig(confirm_views=3), tracks=[track(1, {1.0, 2.0}), track(2, {1.0, 2.0})])
    memory.maintain(now=2.0)
    [merged] = memory.tracks
    assert merged.n_views == 2
    assert memory.confirmed() == []
    memory.add([seen(points, stamp=3.0)])
    assert merged.n_views == 3
    assert len(memory.confirmed()) == 1


def test_views_loaded_from_the_store_still_count_after_a_merge():
    """A loaded track has views but no unsaved stamps: they are kept, and only shared stamps are removed."""
    points = voxel_downsample(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), 0.025)
    stored = Track(key=1, layer=MOVABLE, points=points, labels={"cup": 4}, first_seen=1.0, last_seen=2.0,
                   n_detections=4, n_views=4, object_id=3)
    fresh = Track(key=2, layer=MOVABLE, points=points + 0.005, labels={"cup": 1}, first_seen=5.0,
                  last_seen=5.0, n_detections=1, n_views=1, unsaved_views={5.0})
    memory = ObjectMemory(tracks=[stored, fresh])
    memory.maintain(now=6.0)
    [merged] = memory.tracks
    assert (merged.object_id, merged.n_views) == (3, 5)
    # The fresh track was never stored, so the store holds no history to count again.
    assert not merged.views_counted and merged.unsaved_views == {5.0}


def test_absorbing_a_stored_track_asks_the_store_to_count_the_views_again():
    points = voxel_downsample(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), 0.025)
    a, b = (Track(key=key, layer=MOVABLE, points=points + 0.005 * key, labels={"cup": 3}, first_seen=1.0,
                  last_seen=3.0, n_detections=3, n_views=3, object_id=key, views_counted=True, dirty=False,
                  needs_cleanup=False)
            for key in (1, 2))
    memory = ObjectMemory(tracks=[a, b])
    memory.maintain(now=4.0)
    [merged] = memory.tracks
    # Until the save counts the shared arrays in the store, both histories are summed.
    assert merged.n_views == 6
    assert not merged.views_counted


def test_overlapping_movable_tracks_named_otherwise_merge_and_anchors_do_not():
    points = voxel_downsample(blob((1.0, 1.0, 0.8), (0.08, 0.08, 0.1)), 0.025)

    def pair(layer, first, second):
        return [Track(key=1, layer=layer, points=points, labels={first: 3}, first_seen=1.0, last_seen=1.0,
                      n_detections=3, n_views=3),
                Track(key=2, layer=layer, points=points + 0.005, labels={second: 2}, first_seen=1.0,
                      last_seen=1.0, n_detections=2, n_views=2)]

    movable = ObjectMemory(tracks=pair(MOVABLE, "cup", "bowl"))
    movable.maintain(now=2.0)
    [cup] = movable.tracks
    assert cup.labels == {"cup": 3, "bowl": 2}
    anchors = ObjectMemory(tracks=pair(ANCHOR, "table", "chair"))
    anchors.maintain(now=2.0)
    assert len(anchors.tracks) == 2


def test_a_seeded_anchor_takes_its_own_label_and_keeps_its_box():
    box = Box((2.0, 0.0, 0.4), (1.0, 0.6, 0.8))
    anchor = Track(key=1, layer=ANCHOR, points=np.zeros((0, 3)), labels={"table": 1}, first_seen=0.0,
                   last_seen=0.0, object_id=3, fixed=True, box=box, dirty=False, needs_cleanup=False)
    memory = ObjectMemory(tracks=[anchor])
    memory.add([seen(blob((2.0, 0.0, 0.78), (0.8, 0.5, 0.03)), label="table", stamp=5.0)])
    memory.add([seen(blob((2.0, 0.0, 0.5), (0.4, 0.4, 0.4), seed=2), label="chair", stamp=6.0)])
    assert len(memory.tracks) == 2
    assert anchor.box == box and anchor.n_detections == 1 and anchor.last_seen == 5.0
    memory.maintain(now=7.0)
    assert anchor.box == box


def half_out_chair():
    """A chair pulled half out from under a table at (2, 0) that is 0.6 m deep."""
    return blob((2.0, 0.4, 0.45), (0.4, 0.4, 0.9), count=3000)


def seeded(label, center, size, object_id=3):
    return Track(key=object_id, layer=ANCHOR, points=np.zeros((0, 3)), labels={label: 1}, first_seen=0.0,
                 last_seen=0.0, object_id=object_id, fixed=True, box=Box(center, size), dirty=False,
                 needs_cleanup=False)


def test_a_seeded_anchor_absorbs_an_anchor_track_inside_its_box_and_keeps_its_name():
    sofa = seeded("sofa", (3.17, -1.65, 0.42), (0.58, 1.88, 0.84))
    memory = ObjectMemory(tracks=[sofa])
    for stamp in (1.0, 2.0, 3.0):
        strip = blob((3.12, -1.68, 0.66), (0.3, 1.6, 0.38), count=1500, seed=int(stamp))
        memory.add([seen(strip, label="table", stamp=stamp)])
    assert len(memory.tracks) == 2  # another name never joins a seeded anchor as it is seen
    memory.maintain(now=4.0)
    assert len(memory.tracks) == 1 and memory.tracks[0] is sofa
    assert sofa.label == "sofa" and sofa.labels == {"sofa": 4}
    assert sofa.n_detections == 3 and sofa.n_views == 3 and sofa.last_seen == 3.0
    assert sofa.box == Box((3.17, -1.65, 0.42), (0.58, 1.88, 0.84))


def test_a_seeded_anchor_leaves_an_anchor_track_that_reaches_well_outside_its_box():
    table = seeded("table", (2.0, 0.0, 0.4), (1.0, 0.6, 0.8))
    memory = ObjectMemory(tracks=[table])
    memory.add([seen(half_out_chair(), label="chair", stamp=1.0)])
    memory.maintain(now=2.0)
    assert len(memory.tracks) == 2 and table.labels == {"table": 1}
    assert len(memory.tracks[1].points) > 1000  # the chair kept its cloud through the cleanup


def test_containment_needs_a_seeded_anchor_and_never_takes_a_movable_object():
    table = seeded("table", (2.0, 0.0, 0.4), (1.0, 0.6, 0.8))
    cup = blob((2.0, 0.0, 0.7), (0.08, 0.08, 0.1))
    with_seed = ObjectMemory(tracks=[table])
    with_seed.add([seen(cup, label="cup", stamp=1.0)])
    with_seed.maintain(now=2.0)
    assert len(with_seed.tracks) == 2

    # The same tracks made by the memory: a measured box is not trusted to own its space.
    big = Track(key=1, layer=ANCHOR, points=voxel_downsample(blob((2.0, 0.0, 0.4), (1.0, 0.6, 0.8), 6000), 0.025),
                labels={"sofa": 5}, first_seen=0.0, last_seen=0.0, n_detections=5, n_views=5)
    strip = Track(key=2, layer=ANCHOR, points=voxel_downsample(blob((2.0, 0.0, 0.6), (0.3, 0.5, 0.3), 1500), 0.025),
                  labels={"table": 3}, first_seen=0.0, last_seen=0.0, n_detections=3, n_views=3)
    without_seed = ObjectMemory(tracks=[strip, big])
    without_seed.maintain(now=1.0)
    assert len(without_seed.tracks) == 2


def partition(seed, across=0.0):
    """One view of a partition 1.6 m long and 0.8 m high: a face 1 cm deep, *across* metres off."""
    return blob((1.0, 1.0 + across, 0.4), (1.6, 0.01, 0.8), count=3000, seed=seed)


def test_furniture_shaped_like_a_wall_is_kept_unconfirmed_until_it_is_a_ghost():
    memory = ObjectMemory(MergeConfig(confirm_views=2, ghost_age_s=10.0))
    for stamp in (1.0, 2.0, 3.0):
        memory.add([seen(partition(int(stamp)), label="chair", stamp=stamp)])
    memory.maintain(now=4.0)
    [wall] = memory.tracks
    assert (wall.n_views, wall.measured_detections, wall.thin_detections) == (3, 3, 3)
    assert wall.held_back(memory.config) and memory.confirmed() == []
    assert memory.maintain(now=20.0).dropped == 1 and memory.tracks == []


def test_a_wall_smeared_by_drift_is_still_a_slab():
    """Each view lands 6 cm further off, so the merged box is 0.3 m deep and only 2.7 times as long."""
    memory = ObjectMemory(MergeConfig(confirm_views=2))
    for step in range(6):
        view = blob((1.0, 1.0 + 0.06 * step, 0.4), (0.8, 0.01, 0.8), count=2000, seed=step)
        memory.add([seen(view, label="chair", stamp=float(step))])
    memory.maintain(now=6.0)
    [wall] = memory.tracks
    assert min(wall.box.size[:2]) > 0.25
    assert wall.held_back(memory.config) and memory.confirmed() == []


@pytest.mark.parametrize(
    ("label", "size", "slab"),
    [
        ("cabinet", (0.8, 0.01, 1.9), True),  # an open door
        ("table", (1.6, 0.21, 0.4), False),  # a strip of a sofa
        ("chair", (0.35, 0.10, 0.45), False),  # part of a desk
        ("book", (0.3, 0.01, 0.2), False),  # a movable object is never a slab
    ],
)
def test_only_a_thin_anchor_is_held_back(label, size, slab):
    memory = ObjectMemory(MergeConfig(confirm_views=1))
    memory.add([seen(blob((1.0, 1.0, 1.0), size, count=4000), label=label)])
    memory.maintain(now=1.0)
    [track] = memory.tracks
    assert track.held_back(memory.config) is slab
    assert len(memory.confirmed()) == (0 if slab else 1)


def test_sparse_fragments_cannot_confirm_an_anchor_and_dense_views_can():
    """A fragment too sparse to measure says nothing of the shape, as a partition's end seen from afar."""
    def view(count, seed):
        return blob((1.0, 1.0, 0.4), (0.12, 0.12, 0.12), count=count, seed=seed)

    memory = ObjectMemory(MergeConfig(confirm_views=3))
    for stamp in (1.0, 2.0, 3.0):
        memory.add([seen(view(30, int(stamp)), label="chair", stamp=stamp)])
    memory.maintain(now=4.0)
    [chair] = memory.tracks
    assert (chair.n_views, chair.measured_detections) == (3, 0)
    assert memory.confirmed() == []
    for stamp in (4.0, 5.0, 6.0):
        memory.add([seen(view(1000, int(stamp)), label="chair", stamp=stamp)])
    memory.maintain(now=7.0)
    assert memory.confirmed() == [chair]
    assert (chair.measured_detections, chair.thin_detections) == (3, 0)


def test_a_slab_whose_later_views_show_its_depth_is_confirmed():
    """A cabinet seen head-on is only its front, until views from an angle show its side."""
    def front(seed):
        return blob((1.0, 1.0, 0.5), (0.8, 0.01, 1.0), count=3000, seed=seed)

    memory = ObjectMemory(MergeConfig(confirm_views=2))
    for stamp in (1.0, 2.0):
        memory.add([seen(front(int(stamp)), label="cabinet", stamp=stamp)])
    memory.maintain(now=3.0)
    assert memory.confirmed() == []
    for stamp in (3.0, 4.0, 5.0):
        side = blob((1.4, 1.25, 0.5), (0.01, 0.5, 1.0), count=2000, seed=int(stamp))
        memory.add([seen(np.vstack((front(int(stamp)), side)), label="cabinet", stamp=stamp)])
    memory.maintain(now=6.0)
    [cabinet] = memory.confirmed()
    assert (cabinet.measured_detections, cabinet.thin_detections) == (5, 2)


def test_tracks_that_merge_pool_their_measured_detections():
    memory = ObjectMemory(MergeConfig(confirm_views=1))
    memory.add([seen(partition(0), label="chair", stamp=1.0)])
    memory.add([seen(partition(1, across=0.3), label="chair", stamp=1.0)])  # too far off to join
    first, second = memory.tracks
    second.set_points(first.points.copy())  # now they overlap, as after a correction
    memory.maintain(now=2.0)
    [wall] = memory.tracks
    assert (wall.measured_detections, wall.thin_detections) == (2, 2)


def test_a_wall_is_measured_without_the_floor_and_the_seat_in_its_mask():
    """Masks of a wall's end took in the floor in front of it and the seat of a stool 1.2 m away."""
    def view(seed):
        face = blob((0.6, 1.0, 0.5), (0.8, 0.01, 0.8), count=3000, seed=seed)
        floor = blob((0.6, 0.8, 0.01), (0.8, 0.4, 0.01), count=1500, seed=seed + 10)
        seat = blob((1.8, 0.75, 0.5), (0.3, 0.3, 0.01), count=150, seed=seed + 20)
        return np.vstack((face, floor, seat))

    memory = ObjectMemory(MergeConfig(confirm_views=2))
    for stamp in (1.0, 2.0, 3.0):
        memory.add([seen(view(int(stamp)), label="chair", stamp=stamp)])
    memory.maintain(now=4.0)
    [wall] = memory.tracks
    assert (wall.measured_detections, wall.thin_detections) == (3, 3)
    assert memory.confirmed() == []


def table_legs(seed=0):
    """A table seen only through its four legs, 4 cm square, apart from one another."""
    return np.vstack([blob((x, y, 0.4), (0.04, 0.04, 0.7), count=300, seed=seed + i)
                      for i, (x, y) in enumerate(((0.0, 0.0), (0.6, 0.0), (0.0, 0.5), (0.6, 0.5)))])


def sparse_grid():
    """Points 10 cm apart, too far for DBSCAN to find a cluster."""
    axis = np.arange(5) * 0.1
    return np.array([(x, y, 0.2 + z) for x in axis for y in axis for z in axis[:4]])


def chair_from_behind(seed=0):
    """A chair's back, 0.4 m square, and a strip of its seat 12 cm in front of it: one cluster each."""
    back = blob((0.0, 0.0, 0.7), (0.4, 0.01, 0.4), count=1500, seed=seed)
    seat = blob((0.0, 0.15, 0.45), (0.4, 0.06, 0.01), count=150, seed=seed + 1)
    return np.vstack((back, seat))


@pytest.mark.parametrize("points", [table_legs(), chair_from_behind(), sparse_grid()],
                         ids=["legs", "a seat beside a larger back", "no cluster"])
def test_furniture_seen_as_parts_apart_is_measured_whole(points):
    memory = ObjectMemory(MergeConfig(confirm_views=1))
    memory.add([seen(points, label="table")])
    [table] = memory.tracks
    assert (table.measured_detections, table.thin_detections) == (1, 0)


def test_a_mask_that_is_mostly_floor_is_not_measured():
    floor = blob((1.0, 1.0, 0.01), (0.8, 0.8, 0.01), count=3000)
    sliver = blob((1.0, 1.4, 0.3), (0.1, 0.01, 0.1), count=40)
    memory = ObjectMemory(MergeConfig(confirm_views=1))
    memory.add([seen(np.vstack((floor, sliver)), label="cabinet")])
    [track] = memory.tracks
    assert (track.n_detections, track.measured_detections) == (1, 0)


def test_seeded_stored_and_unruled_anchors_are_never_held_back():
    assert seeded("shelf", (1.0, 1.0, 0.9), (1.6, 0.01, 1.8)).confirmed(MergeConfig())
    stored = Track(key=1, layer=ANCHOR, points=partition(0), labels={"chair": 3}, first_seen=0.0,
                   last_seen=0.0, n_views=3, object_id=7, measured_detections=3, thin_detections=3)
    memory = ObjectMemory(tracks=[stored])
    memory.maintain(now=1.0)
    assert not stored.held_back(memory.config) and memory.confirmed() == [stored]

    unruled = ObjectMemory(MergeConfig(confirm_views=1, slab_thickness=0.0))
    unruled.add([seen(partition(0), label="chair")])
    unruled.maintain(now=1.0)
    assert len(unruled.confirmed()) == 1


def test_a_lower_containment_takes_more_of_a_track_reaching_outside():
    def chair_count(containment):
        table = seeded("table", (2.0, 0.0, 0.4), (1.0, 0.6, 0.8))
        memory = ObjectMemory(MergeConfig(seed_containment=containment), tracks=[table])
        memory.add([seen(half_out_chair(), label="chair", stamp=1.0)])
        memory.maintain(now=2.0)
        return len(memory.tracks)

    assert (chair_count(0.8), chair_count(0.3)) == (2, 1)
