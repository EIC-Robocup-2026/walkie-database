"""ObjectRepository's reads: filters, attributes, relations, superlatives, ages and ids."""

import math

import pytest
from query_helpers import (
    KITCHEN,
    MODEL,
    NOW,
    OTHER_MAP,
    Store,
    at,
    direction,
    nowhere,
    with_cosine,
    yaw_rotation,
)

from walkie_database.db import transaction
from walkie_database.geometry import FramePose
from walkie_database.objects import tombstone_object
from walkie_database.people import Refused
from walkie_database.queries import (
    ANCHOR_HALF_LIFE_S,
    MOVABLE_HALF_LIFE_S,
    ObjectFilter,
    belief_now,
    score_of,
)


@pytest.fixture
def store(db):
    return Store(db)


@pytest.fixture
def rooms(store):
    return {"living": store.room("living room"), "kitchen": store.room("kitchen", KITCHEN, stand=None)}


def search(repo, *, text="", vector=None, max_results=20, now=NOW, **fields):
    return repo.search(ObjectFilter(**fields), vector=vector, text=text, max_results=max_results, now=now)


def near(repo, *, max_results=20, now=NOW, **fields):
    return repo.find_near(ObjectFilter(**fields), max_results=max_results, now=now)


def ids(found):
    return [match.record.id for match in found.matches]


def code_of(call, *args, **kwargs) -> str:
    """The OperationStatus code the node answers a refusal with. A plain Refused is INVALID."""
    with pytest.raises(Refused) as refused:
        call(*args, **kwargs)
    return getattr(refused.value, "code", "INVALID")


# Hard filters ----------------------------------------------------------------


def test_the_default_states_are_present_and_held(store):
    present, held = store.add("cup", state="present"), store.add("cup", state="held")
    for state in ("candidate", "grasping", "missing", "handed_over", "discarded"):
        store.add("cup", state=state)

    assert sorted(ids(search(store.repository(), thing="cup"))) == [present, held]


def test_named_states_replace_the_default(store):
    store.add("cup")
    missing = store.add("cup", state="missing")
    discarded = store.add("cup", state="discarded")

    assert ids(search(store.repository(), thing="cup", states=("missing",))) == [missing]
    assert ids(search(store.repository(), thing="cup", states=("discarded",))) == [discarded]


def test_an_unknown_state_is_invalid(store):
    assert code_of(search, store.repository(), states=("lost",)) == "INVALID"


def test_the_room_filter_keeps_other_rooms_out(store, rooms):
    here = store.add("cup", room=rooms["living"])
    store.add("cup", room=rooms["kitchen"])
    store.add("cup")  # in no room

    assert ids(search(store.repository(), thing="cup", room="living room")) == [here]


def test_an_unknown_room_is_not_found(store, rooms):
    assert code_of(search, store.repository(), room="attic") == "NOT_FOUND"


def test_the_layer_filter(store):
    table = store.add("table", layer="anchor", key="table")
    cup = store.add("cup")

    assert ids(search(store.repository(), layer="anchor")) == [table]
    assert ids(search(store.repository(), layer="movable")) == [cup]
    assert code_of(search, store.repository(), layer="furniture") == "INVALID"


def test_the_category_filter_ignores_case(store):
    item = store.item("blue cup", category="Cup")
    by_catalogue = store.add("mug", catalogue=item)
    by_row = store.add("thing", category="cup")
    store.add("bottle", category="bottle")

    assert sorted(ids(search(store.repository(), category="CUP"))) == [by_catalogue, by_row]


def test_another_maps_objects_never_come_back(store):
    other = store.other_map()
    theirs = other.add("cup")
    mine = store.add("cup")

    assert ids(search(store.repository(), thing="cup")) == [mine]
    assert ids(search(other.repository(), thing="cup")) == [theirs]


def test_the_limit_applies_after_every_filter_and_says_when_it_cut(store, rooms):
    for _ in range(5):
        store.add("cup", room=rooms["kitchen"])
    kept = [store.add("cup", room=rooms["living"]) for _ in range(3)]

    found = search(store.repository(), thing="cup", room="living room", max_results=2)

    assert ids(found) == kept[:2]
    assert found.truncated
    assert not search(store.repository(), thing="cup", room="living room", max_results=3).truncated


@pytest.mark.parametrize("limit", [0, 101])
def test_a_limit_out_of_range_is_invalid(store, limit):
    assert code_of(search, store.repository(), max_results=limit) == "INVALID"


# Names and aliases -----------------------------------------------------------


def test_a_thing_matches_the_label_alias_official_name_and_extra_labels(store):
    item = store.item("blue cup", category="cup", aliases=["Coffee Mug"])
    cup = store.add("cup", catalogue=item, labels=["tumbler"])
    store.add("bottle")

    for thing in ("cup", "Cup", "blue cup", "mug", "coffee mug", "tumbler"):
        assert ids(search(store.repository(), thing=thing)) == [cup], thing
    assert ids(search(store.repository(), thing="red cup")) == []


def test_a_thing_matches_whole_words_only(store):
    store.add("cupboard", layer="anchor", key="cupboard")
    assert ids(search(store.repository(), thing="cup")) == []


def test_a_thing_with_no_word_is_invalid(store):
    assert code_of(search, store.repository(), thing="?!") == "INVALID"


def test_the_object_row_names_win_over_the_catalogue(store):
    item = store.item("blue cup", category="cup")
    named = store.add("cup", catalogue=item, official_name="teacup", category="crockery")

    [match] = search(store.repository(), thing="teacup").matches

    assert match.record.id == named
    assert match.record.official_name == "teacup"
    assert match.record.category == "crockery"


# Attributes ------------------------------------------------------------------


def test_attributes_come_back_as_matched_unknown_and_mismatched(store):
    item = store.item("blue cup", attributes={"colour": "blue", "fragile": True})
    cup = store.add("cup", catalogue=item)
    store.add("cup", attributes={"colour": "red"})

    [match] = search(
        store.repository(), thing="cup", attributes=(("colour", "blue"), ("material", "glass"), ("fragile", "true"))
    ).matches

    assert match.record.id == cup
    assert match.matched == ("colour", "fragile")
    assert match.unknown == ("material",)
    assert match.mismatched == ()


def test_a_mismatch_comes_back_only_when_nothing_else_passed(store):
    red = store.add("cup", attributes={"colour": "red"})
    green = store.add("cup", attributes={"colour": "green"})

    found = search(store.repository(), thing="cup", attributes=(("colour", "blue"),))

    assert sorted(ids(found)) == [red, green]
    assert all(match.mismatched == ("colour",) for match in found.matches)

    unknown = store.add("cup")
    assert ids(search(store.repository(), thing="cup", attributes=(("colour", "blue"),))) == [unknown]


def test_an_object_attribute_overrides_its_catalogue_item(store):
    item = store.item("blue cup", attributes={"colour": "blue"})
    painted = store.add("cup", catalogue=item, attributes={"colour": "red"})

    [match] = search(store.repository(), thing="cup", attributes=(("colour", "red"),)).matches

    assert match.record.id == painted
    assert match.record.attributes["colour"] == "red"


@pytest.mark.parametrize("attribute", [("flavour", "sweet"), ("colour", "teal"), ("fragile", "yes")])
def test_an_attribute_outside_the_vocabulary_is_invalid(store, attribute):
    assert code_of(search, store.repository(), attributes=(attribute,)) == "INVALID"


# Scores ----------------------------------------------------------------------


def test_the_score_follows_the_formula(store):
    store.add("cup", p_exist=0.8, attributes={"colour": "blue"})

    [match] = search(store.repository(), thing="cup", attributes=(("colour", "blue"), ("size", "small"))).matches

    assert match.similarity is None
    assert match.score == pytest.approx(0.6 * 1.0 + 0.25 * 0.8 + 0.15 * 0.5)


def test_an_unconstrained_query_has_no_lexical_match(store):
    store.add("cup", p_exist=0.8)
    [match] = search(store.repository()).matches
    assert match.score == pytest.approx(0.25 * 0.8)


def test_the_score_is_clamped():
    assert score_of(similarity=1.0, lexical_match=0.0, p_now=1.0, attribute_fraction=1.0) == 1.0
    assert score_of(similarity=-1.0, lexical_match=0.0, p_now=0.0, attribute_fraction=0.0) == 0.0


def test_a_tie_in_score_goes_to_the_lower_id(store):
    first, second = store.add("cup"), store.add("cup")
    assert ids(search(store.repository(), thing="cup")) == [first, second]


# Ages ------------------------------------------------------------------------


def test_belief_halves_at_each_half_life():
    assert belief_now(0.8, "movable", NOW - MOVABLE_HALF_LIFE_S, NOW) == pytest.approx((0.4, MOVABLE_HALF_LIFE_S))
    assert belief_now(0.8, "anchor", NOW - ANCHOR_HALF_LIFE_S, NOW) == pytest.approx((0.4, ANCHOR_HALF_LIFE_S))
    # An anchor fades slower: half an hour costs it about 1.4 per cent.
    assert belief_now(0.8, "anchor", NOW - MOVABLE_HALF_LIFE_S, NOW)[0] == pytest.approx(0.8 * 0.5 ** (1 / 48))


def test_belief_never_claims_more_than_it_may():
    assert belief_now(0.99, "movable", NOW, NOW) == (0.95, 0.0)
    # A verification time after now, as after a clock reset, is age 0 and not a boost.
    assert belief_now(0.8, "movable", NOW + 60, NOW) == (0.8, 0.0)


def test_an_unknown_verification_time_claims_nothing():
    assert belief_now(0.9, "movable", None, NOW) == (0.0, 0.0)


def test_an_old_object_keeps_its_state_and_ranks_below_a_fresh_one(store):
    old = store.add("cup", last_verified=NOW - 10 * MOVABLE_HALF_LIFE_S)
    fresh = store.add("cup", last_verified=NOW - 60)
    never = store.add("cup", last_verified=None)

    found = search(store.repository(), thing="cup")

    assert ids(found) == [fresh, old, never]
    by_id = {match.record.id: match.record for match in found.matches}
    assert by_id[old].state == "present"
    assert by_id[old].age_s == pytest.approx(10 * MOVABLE_HALF_LIFE_S)
    assert by_id[old].p_now == pytest.approx(0.9 * 0.5**10)
    assert by_id[never].last_verified is None
    assert by_id[never].p_now == 0.0
    assert by_id[never].age_s == 0.0


# Missing measurements --------------------------------------------------------


def test_an_object_with_no_box_reports_no_dimensions(store):
    item = store.item("cup", weight_g=None)
    found = store.add("cup", center=None, catalogue=item)

    record = store.repository().get_object(found, include_cloud=False, now=NOW).record

    assert not record.geometry_valid
    assert record.geometry_stamp is None
    assert record.center == (0.0, 0.0, 0.0)
    assert record.size == (0.0, 0.0, 0.0)
    assert record.rotation == (0.0, 0.0, 0.0, 1.0)
    assert record.weight_g is None


def test_weight_comes_only_from_the_catalogue(store):
    weighed = store.add("cup", catalogue=store.item("blue cup", weight_g=250.0))
    unweighed = store.add("cup")

    repo = store.repository()
    assert repo.get_object(weighed, include_cloud=False, now=NOW).record.weight_g == 250.0
    assert repo.get_object(unweighed, include_cloud=False, now=NOW).record.weight_g is None


# Relations -------------------------------------------------------------------


@pytest.fixture
def furniture(store, rooms):
    """A table with two surfaces, a cabinet that encloses, and what is on and in them."""
    table = store.add("table", layer="anchor", key="table", center=(2.0, 2.0, 0.4), size=(1.0, 0.5, 0.8),
                      room=rooms["living"])
    top = store.surface(table, "table_top", 0.8)
    shelf = store.surface(table, "table_shelf", 0.2)
    cabinet = store.add("cabinet", layer="anchor", key="cabinet", center=(3.0, 3.0, 0.5), size=(0.5, 0.5, 1.0),
                        enclosed=True, room=rooms["living"])
    cup = store.add("cup", center=(2.1, 2.0, 0.85))
    bowl = store.add("bowl", center=(1.9, 2.0, 0.25))
    plate = store.add("plate", center=(3.0, 3.0, 0.5))
    store.on(cup, table, top)
    store.on(bowl, table, shelf)
    store.inside(plate, cabinet)
    return {"table": table, "top": top, "cabinet": cabinet, "cup": cup, "bowl": bowl, "plate": plate}


def test_on_a_surface_key_is_only_that_surface(store, furniture):
    assert ids(near(store.repository(), relation="on", reference_key="table_top")) == [furniture["cup"]]


def test_on_an_anchor_is_any_of_its_surfaces(store, furniture):
    expected = sorted([furniture["cup"], furniture["bowl"]])
    assert sorted(ids(near(store.repository(), relation="on", reference_key="table"))) == expected
    assert sorted(ids(near(store.repository(), relation="on", reference_id=furniture["table"]))) == expected


def test_inside_reads_the_stored_relation(store, furniture):
    assert ids(near(store.repository(), relation="inside", reference_key="cabinet")) == [furniture["plate"]]
    assert ids(near(store.repository(), relation="inside", reference_key="table")) == []


def test_inside_a_surface_is_invalid(store, furniture):
    assert code_of(near, store.repository(), relation="inside", reference_key="table_top") == "INVALID"


def test_a_relation_to_an_anchor_that_lost_its_box_is_no_match(store, furniture):
    store.update(furniture["table"], geometry_valid=0)
    assert ids(near(store.repository(), relation="on", reference_key="table")) == []


def test_a_merged_reference_is_read_through_its_winner(store, furniture):
    loser = store.add("table", layer="anchor")
    store.update(loser, state="merged", merged_into=furniture["table"])

    found = near(store.repository(), relation="on", reference_id=loser)

    assert sorted(ids(found)) == sorted([furniture["cup"], furniture["bowl"]])


def test_near_a_reference_uses_the_centre_distance_and_leaves_it_out(store, furniture):
    close = store.add("spoon", center=(2.5, 2.0, 0.4))  # 0.5 m from the table's centre
    # Its box reaches the table, and its centre is 0.8 m away.
    store.add("tray", center=(2.8, 2.0, 0.4), size=(1.0, 1.0, 0.1))
    store.add("ghost", center=None)  # no box, so never near

    found = near(store.repository(), relation="near", reference_key="table", near_distance_m=0.6)

    assert sorted(ids(found)) == sorted([furniture["cup"], furniture["bowl"], close])
    assert furniture["table"] not in ids(found)


def test_near_a_reference_with_no_box_is_invalid(store):
    store.add("wardrobe", layer="anchor", key="wardrobe", center=None)
    assert code_of(near, store.repository(), relation="near", reference_key="wardrobe") == "INVALID"


def test_near_a_discarded_reference_matches_nothing(store, furniture):
    store.update(furniture["table"], state="discarded")
    assert ids(near(store.repository(), relation="near", reference_key="table")) == []


def test_near_with_no_reference_uses_the_reference_frame(store):
    close = store.add("cup", center=(6.3, 1.0, 0.0))
    store.add("cup", center=(7.0, 1.0, 0.0))

    found = near(store.repository(locate=at(6.0, 1.0)), relation="near", near_distance_m=0.5)

    assert ids(found) == [close]


def test_near_the_robot_without_a_transform_is_tf_unavailable(store):
    store.add("cup")
    assert code_of(near, store.repository(locate=nowhere), relation="near") == "TF_UNAVAILABLE"
    assert code_of(near, store.repository(), relation="near") == "TF_UNAVAILABLE"


def test_a_reference_frame_must_be_one_the_node_allows(store):
    repo = store.repository(locate=at(0, 0))
    assert code_of(near, repo, relation="near", reference_frame="camera_link") == "INVALID"
    assert code_of(near, repo, relation="near", reference_frame="") == "INVALID"


def test_in_room_takes_the_room(store, rooms):
    here = store.add("cup", room=rooms["living"])
    store.add("cup", room=rooms["kitchen"])
    assert ids(near(store.repository(), relation="in_room", room="living room")) == [here]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"relation": "beside", "reference_id": 1},
        {"relation": "on"},
        {"relation": "inside"},
        {"relation": "in_room"},
        {"relation": "in_room", "room": "living room", "reference_id": 1},
        {"relation": "on", "reference_id": 1, "reference_key": "table"},
        {"relation": "on", "reference_id": -1},
        {"relation": "near", "reference_key": "table", "near_distance_m": 0.0},
        {"relation": "near", "reference_key": "table", "near_distance_m": math.nan},
        {"relation": "near", "reference_key": "table", "near_distance_m": math.inf},
    ],
    ids=["no_relation", "unknown_relation", "on_alone", "inside_alone", "in_room_alone",
         "in_room_with_reference", "id_and_key", "negative_id", "zero_distance", "nan_distance",
         "infinite_distance"],
)
def test_a_relation_that_does_not_make_sense_is_invalid(store, furniture, fields):
    assert code_of(near, store.repository(locate=at(0, 0)), **fields) == "INVALID"


def test_a_reference_without_a_relation_is_invalid(store, furniture):
    assert code_of(search, store.repository(), reference_id=furniture["table"]) == "INVALID"


def test_an_unknown_reference_is_not_found(store, furniture):
    assert code_of(near, store.repository(), relation="on", reference_key="sofa") == "NOT_FOUND"
    assert code_of(near, store.repository(), relation="on", reference_id=99_999) == "NOT_FOUND"


def test_a_reference_on_another_map_is_wrong_map(store, furniture):
    elsewhere = store.other_map().add("table", layer="anchor")
    assert code_of(near, store.repository(), relation="on", reference_id=elsewhere) == "WRONG_MAP"


# Superlatives ----------------------------------------------------------------


def test_largest_and_smallest_use_the_volume_and_put_the_unmeasured_last(store):
    big = store.add("box", size=(0.5, 0.5, 0.5))
    small = store.add("box", size=(0.1, 0.2, 0.1))
    tall = store.add("box", size=(0.1, 0.1, 2.0))
    unknown = store.add("box", center=None)

    largest = search(store.repository(), thing="box", superlative="largest")
    smallest = search(store.repository(), thing="box", superlative="smallest")

    assert ids(largest) == [big, tall, small, unknown]
    assert ids(smallest) == [small, tall, big, unknown]
    assert [match.measure_unknown for match in largest.matches] == [False, False, False, True]


def test_thickest_and_thinnest_use_the_shortest_side(store):
    slab = store.add("book", size=(0.3, 0.2, 0.02))
    brick = store.add("book", size=(0.2, 0.1, 0.06))

    assert ids(search(store.repository(), thing="book", superlative="thickest")) == [brick, slab]
    assert ids(search(store.repository(), thing="book", superlative="thinnest")) == [slab, brick]


def test_heaviest_needs_a_measured_catalogue_weight(store):
    light = store.add("can", catalogue=store.item("soda can", weight_g=350.0))
    heavy = store.add("can", catalogue=store.item("paint can", weight_g=1200.0), center=None)
    unweighed = store.add("can", size=(5.0, 5.0, 5.0))

    heaviest = search(store.repository(), thing="can", superlative="heaviest")
    lightest = search(store.repository(), thing="can", superlative="lightest")

    # Weight needs no box, and a big box is no weight.
    assert ids(heaviest) == [heavy, light, unweighed]
    assert ids(lightest) == [light, heavy, unweighed]
    assert heaviest.matches[-1].measure_unknown


def test_nearest_and_farthest_measure_from_the_reference_frame(store):
    a = store.add("cup", center=(1.0, 0.0, 0.0))
    b = store.add("cup", center=(3.0, 0.0, 0.0))
    c = store.add("cup", center=(0.0, 0.0, 2.5))
    repo = store.repository(locate=at(0.0, 0.0))

    assert ids(search(repo, thing="cup", superlative="nearest")) == [a, c, b]
    assert ids(search(repo, thing="cup", superlative="farthest")) == [b, c, a]


def test_leftmost_and_rightmost_use_y_in_the_reference_frame(store):
    # The robot faces +y in the map, so its left is -x in the map.
    west = store.add("cup", center=(-1.0, 2.0, 0.0))
    east = store.add("cup", center=(1.0, 0.0, 0.0))
    repo = store.repository(locate=at(0.0, 0.0, yaw=math.pi / 2))

    assert ids(search(repo, thing="cup", superlative="leftmost")) == [west, east]
    assert ids(search(repo, thing="cup", superlative="rightmost")) == [east, west]


def test_a_superlative_tie_goes_to_the_score_then_the_id(store):
    stale = store.add("box", size=(0.2, 0.2, 0.2), last_verified=NOW - 5 * MOVABLE_HALF_LIFE_S)
    fresh = store.add("box", size=(0.2, 0.2, 0.2))
    twin = store.add("box", size=(0.2, 0.2, 0.2))

    assert ids(search(store.repository(), thing="box", superlative="largest")) == [fresh, twin, stale]


def test_a_frame_superlative_needs_a_transform(store):
    store.add("cup")
    assert code_of(search, store.repository(locate=nowhere), superlative="nearest") == "TF_UNAVAILABLE"
    assert code_of(search, store.repository(), superlative="biggest") == "INVALID"


def test_a_superlative_ranks_only_what_passed_the_filters(store, rooms):
    store.add("box", size=(3.0, 3.0, 3.0), room=rooms["kitchen"])
    here = store.add("box", size=(0.2, 0.2, 0.2), room=rooms["living"])

    found = search(store.repository(), thing="box", room="living room", superlative="largest", max_results=1)

    assert ids(found) == [here]


# Vectors ---------------------------------------------------------------------


def test_filters_run_before_the_vector_rank(store, rooms):
    for _ in range(30):
        store.add("cup", room=rooms["kitchen"], vector=direction(1.0))
    wanted = store.add("cup", room=rooms["living"], vector=with_cosine(0.4))

    found = search(store.repository(), thing="cup", room="living room", vector=(MODEL, direction(1.0)), max_results=1)

    assert ids(found) == [wanted]
    assert found.matches[0].similarity == pytest.approx(0.4, abs=1e-5)
    assert not found.lexical_only


def test_the_score_uses_the_cosine_when_there_is_one(store):
    store.add("cup", p_exist=0.8, vector=with_cosine(0.5))
    [match] = search(store.repository(), thing="cup", vector=(MODEL, direction(1.0))).matches
    assert match.score == pytest.approx(0.6 * 0.75 + 0.25 * 0.8, abs=1e-5)


def test_a_thing_with_no_word_match_needs_the_cosine(store):
    close = store.add("widget", vector=with_cosine(0.3))
    store.add("gadget", vector=with_cosine(0.1))
    store.add("gizmo")  # no vector and no word match

    vector = (MODEL, direction(1.0))
    assert ids(search(store.repository(), thing="cup", vector=vector)) == [close]
    stricter = store.repository(thing_cosine={MODEL: 0.5})
    assert ids(search(stricter, thing="cup", vector=vector)) == []


def test_an_object_with_no_vector_falls_back_to_its_words(store):
    plain = store.add("cup")
    store.add("bottle")

    [match] = search(store.repository(), thing="cup", vector=(MODEL, direction(1.0))).matches

    assert match.record.id == plain
    assert match.similarity is None
    assert match.score == pytest.approx(0.6 + 0.25 * 0.9)


def test_an_unknown_model_is_unsupported(store):
    store.add("cup", vector=direction(1.0))
    assert code_of(search, store.repository(), vector=("fake_clip", direction(1.0))) == "UNSUPPORTED"


@pytest.mark.parametrize(
    "vector",
    [("", direction(1.0)), (MODEL, [1.0, 0.0]), (MODEL, direction(math.nan)), (MODEL, direction())],
    ids=["no_model", "wrong_length", "nan", "zero"],
)
def test_a_bad_query_vector_is_invalid(store, vector):
    store.add("cup", vector=direction(1.0))
    assert code_of(search, store.repository(), vector=vector) == "INVALID"


def test_without_a_vector_the_search_is_lexical_only(store):
    store.add("cup", vector=direction(1.0))
    found = search(store.repository(), thing="cup")
    assert found.lexical_only
    assert found.matches[0].similarity is None


# Words -----------------------------------------------------------------------


def test_text_matches_the_caption_and_printed_text_with_every_word_required(store):
    coffee = store.add("cup", caption="A mug of coffee on the desk")
    acme = store.add("box", printed_text="ACME Corn Flakes")
    both = store.add("jar", caption="coffee beans", printed_text="Acme")

    repo = store.repository()
    assert sorted(ids(search(repo, text="coffee"))) == sorted([coffee, both])
    assert sorted(ids(search(repo, text="acme"))) == sorted([acme, both])
    assert ids(search(repo, text="coffee ACME")) == [both]
    assert ids(search(repo, text="coffee tea")) == []


def test_words_fold_unicode_case(store):
    street = store.add("sign", printed_text="STRASSE")
    assert ids(search(store.repository(), text="Straße")) == [street]


def test_text_is_capped(store):
    assert code_of(search, store.repository(), text="a" * 513) == "INVALID"


# Priors ----------------------------------------------------------------------


@pytest.fixture
def table_and_cup(store, rooms):
    store.add("table", layer="anchor", key="table", room=rooms["living"])
    return store.item("blue cup", category="cup", aliases=["mug"], default_place="table",
                      attributes={"colour": "blue"})


def test_an_unseen_catalogue_item_is_a_prior_with_its_place(store, table_and_cup):
    found = search(store.repository(), thing="mug")

    assert found.matches == []
    [prior] = found.priors
    assert (prior.official_name, prior.category, prior.place, prior.room) == ("blue cup", "cup", "table", "living room")


def test_an_item_that_an_object_stands_for_is_no_prior(store, table_and_cup):
    cup = store.add("cup", catalogue=table_and_cup)
    assert search(store.repository(), thing="mug").priors == []

    store.update(cup, state="discarded")
    assert [prior.official_name for prior in search(store.repository(), thing="mug").priors] == ["blue cup"]


def test_priors_follow_the_filters(store, table_and_cup):
    repo = store.repository()
    assert search(repo, thing="bottle").priors == []
    assert search(repo, attributes=(("colour", "red"),)).priors == []
    assert search(repo, room="kitchen").priors == []
    assert search(repo, layer="anchor").priors == []
    assert len(search(repo, attributes=(("material", "glass"),)).priors) == 1


def test_a_relation_query_has_no_priors(store, table_and_cup):
    assert near(store.repository(), relation="in_room", room="living room").matches[0].record.label == "table"
    assert search(store.repository(), relation="in_room", room="living room").priors == []


# GetObject -------------------------------------------------------------------


def test_a_merged_id_answers_with_its_winner(store):
    winner = store.add("cup")
    middle = store.add("cup")
    loser = store.add("cup")
    store.update(middle, state="merged", merged_into=winner)
    store.update(loser, state="merged", merged_into=middle)

    found = store.repository().get_object(loser, include_cloud=False, now=NOW)

    assert found.requested_id == loser
    assert found.merged
    assert found.record.id == winner


def test_a_current_id_is_not_merged(store):
    cup = store.add("cup")
    found = store.repository().get_object(cup, include_cloud=False, now=NOW)
    assert (found.requested_id, found.merged, found.record.id) == (cup, False, cup)


def test_a_discarded_object_answers_as_itself(store):
    cup = store.add("cup")
    with transaction(store.conn):
        tombstone_object(store.conn, cup)

    found = store.repository().get_object(cup, include_cloud=False, now=NOW)

    assert not found.merged
    assert found.record.state == "discarded"


def test_unknown_zero_and_foreign_ids(store):
    elsewhere = store.other_map().add("cup")
    repo = store.repository()
    assert code_of(repo.get_object, 99_999, include_cloud=False, now=NOW) == "NOT_FOUND"
    assert code_of(repo.get_object, 0, include_cloud=False, now=NOW) == "INVALID"
    assert code_of(repo.get_object, elsewhere, include_cloud=False, now=NOW) == "WRONG_MAP"


def test_a_cycle_of_merges_is_the_stores_fault(store):
    a, b = store.add("cup"), store.add("cup")
    store.update(a, state="merged", merged_into=b)
    store.update(b, state="merged", merged_into=a)

    with pytest.raises(ValueError, match="cycle"):
        store.repository().get_object(a, include_cloud=False, now=NOW)


def test_a_merge_whose_winner_was_deleted_is_the_stores_fault(store):
    winner, loser = store.add("cup"), store.add("cup")
    store.update(loser, state="merged", merged_into=winner)
    with transaction(store.conn):
        store.conn.execute("DELETE FROM object WHERE id = ?", (winner,))

    with pytest.raises(ValueError, match="names no winner"):
        store.repository().get_object(loser, include_cloud=False, now=NOW)


def test_a_merge_to_another_map_is_the_stores_fault(store):
    elsewhere = store.other_map().add("cup")
    loser = store.add("cup")
    store.update(loser, state="merged", merged_into=elsewhere)

    with pytest.raises(ValueError, match="not on its map"):
        store.repository().get_object(loser, include_cloud=False, now=NOW)


def test_a_chain_of_merges_stops_after_the_cap(store):
    chain = [store.add("cup") for _ in range(35)]
    for loser, winner in zip(chain, chain[1:]):
        store.update(loser, state="merged", merged_into=winner)

    repo = store.repository()
    with pytest.raises(ValueError, match="over 32"):
        repo.get_object(chain[0], include_cloud=False, now=NOW)
    assert repo.get_object(chain[-33], include_cloud=False, now=NOW).record.id == chain[-1]


def test_an_anchor_returns_every_surface_with_an_honest_flag(store, furniture):
    store.surface(furniture["table"], "table_side", valid=False)
    repo = store.repository()

    surfaces = repo.get_object(furniture["table"], include_cloud=False, now=NOW).surfaces
    assert [(s.key, s.geometry_valid) for s in surfaces] == [
        ("table_shelf", True), ("table_side", False), ("table_top", True)
    ]

    store.update(furniture["table"], geometry_valid=0)
    surfaces = repo.get_object(furniture["table"], include_cloud=False, now=NOW).surfaces
    assert [s.geometry_valid for s in surfaces] == [False, False, False]
    assert repo.get_object(furniture["cup"], include_cloud=False, now=NOW).surfaces == []


def test_the_place_is_the_strongest_stored_relation(store, furniture):
    repo = store.repository()

    def place(name):
        record = repo.get_object(furniture[name], include_cloud=False, now=NOW).record
        return record.place, record.place_anchor_id

    assert place("cup") == ("table_top", furniture["table"])
    assert place("plate") == ("cabinet", furniture["cabinet"])
    assert place("table") == ("", 0)


def test_the_place_names_an_anchor_without_a_key_by_its_id(store):
    drawer = store.add("cabinet", layer="anchor", key=None, size=(0.5, 0.5, 0.5), enclosed=True)
    spoon = store.add("spoon")
    store.inside(spoon, drawer)

    record = store.repository().get_object(spoon, include_cloud=False, now=NOW).record
    assert (record.place, record.place_anchor_id) == ("", drawer)


def test_search_results_carry_the_place_anchor(store, furniture):
    [match] = search(store.repository(), text="bowl").matches
    assert (match.record.place, match.record.place_anchor_id) == ("table_shelf", furniture["table"])


def test_a_cloud_comes_only_when_asked_and_measured(store):
    cup = store.add("cup")
    boxless = store.add("cup", center=None)
    for object_id in (cup, boxless):
        with transaction(store.conn):
            store.conn.execute(
                "INSERT INTO object_cloud (object_id, voxel_size, n_points, points) VALUES (?, 0.01, 1, ?)",
                (object_id, bytes(12)),
            )
    repo = store.repository()

    assert repo.get_object(cup, include_cloud=False, now=NOW).cloud is None
    cloud = repo.get_object(cup, include_cloud=True, now=NOW).cloud
    assert (cloud.n_points, cloud.points, cloud.stamp) == (1, bytes(12), NOW - 5)
    assert repo.get_object(boxless, include_cloud=True, now=NOW).cloud is None


# Rooms, status and the snapshot ----------------------------------------------


def test_rooms_come_sorted_by_name_with_their_outlines(store, rooms):
    listed = store.repository().rooms()

    assert [room.name for room in listed] == ["kitchen", "living room"]
    assert listed[0].stand_pose is None
    assert listed[1].stand_pose == (1.0, 1.0, 0.0)
    assert sorted(map(tuple, listed[1].outline)) == [(0.0, 0.0), (0.0, 4.0), (4.0, 0.0), (4.0, 4.0)]


def test_room_contents_needs_a_known_room(store, rooms):
    here = store.add("cup", room=rooms["living"])
    store.add("cup", room=rooms["kitchen"])
    repo = store.repository()

    room, found = repo.room_contents(ObjectFilter(room="living room"), max_results=100, now=NOW)

    assert room.name == "living room"
    assert ids(found) == [here]
    assert code_of(repo.room_contents, ObjectFilter(), max_results=100, now=NOW) == "INVALID"
    assert code_of(repo.room_contents, ObjectFilter(room="attic"), max_results=100, now=NOW) == "NOT_FOUND"


def test_the_status_names_the_store_and_its_models(store):
    status = store.repository().store_status()
    assert status.map_hash == "ab" * 32
    assert status.baseline_rev == ""
    assert status.models == [MODEL]
    assert status.schema_version >= 6


def test_the_snapshot_holds_every_object_but_tombstones(store):
    kept = [store.add("cup", state="candidate"), store.add("cup", state="held"), store.add("cup", state="missing")]
    store.add("cup", state="discarded")
    store.other_map().add("cup")

    snapshot = store.repository().snapshot(NOW)

    assert [record.id for record in snapshot.records] == kept
    assert snapshot.map_id == store.map_id


def test_a_map_point_in_a_frame_that_faces_plus_y():
    frame = FramePose((1.0, 0.0, 0.0), yaw_rotation(math.pi / 2))
    # One metre along map +y from the frame is one metre ahead of it.
    assert frame.local((1.0, 1.0, 0.0)) == pytest.approx((1.0, 0.0, 0.0))
    # One metre along map -x is one metre to its left.
    assert frame.local((0.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0))


def test_a_query_on_an_empty_map_succeeds_with_nothing(store):
    found = search(store.repository(), thing="cup")
    assert (found.matches, found.priors, found.truncated) == ([], [], False)
    assert store.repository().rooms() == []


def test_the_repository_reads_only_its_own_map(store, rooms):
    other = store.other_map()
    other.room("living room")
    assert [room.name for room in store.repository().rooms()] == ["kitchen", "living room"]
    assert [room.map_id for room in other.repository().rooms()] == [OTHER_MAP]
