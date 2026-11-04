"""Rooms, surfaces, the catalogue, stored relations, the task's log and retried writes."""

import json
import sqlite3

import numpy as np
import pytest

from walkie_database.db import connect, transaction
from walkie_database.migrate import migrate
from walkie_database.object_context import (
    INSIDE,
    MAX_EVENT_RESULTS,
    MAX_EVENT_TEXT,
    MAX_JSON_ARRAY,
    MAX_JSON_DEPTH,
    MAX_JSON_STRING,
    MAX_OUTCOME_JSON,
    MAX_PAYLOAD_JSON,
    MAX_REQUEST_ID,
    ON,
    WriteConflict,
    add_catalogue_item,
    add_event,
    add_object_event,
    add_surface,
    canonical_json,
    find_catalogue_items,
    find_events,
    find_write,
    link_catalogue,
    object_events,
    object_weight_g,
    payload_hash,
    record_write,
    relations,
    room_at,
    room_by_key,
    room_outline,
    set_relation,
    set_room_shape,
    usable_surfaces,
)
from walkie_database.objects import tombstone_object
from walkie_database.people import Refused

KITCHEN = [[0, 0], [4, 0], [4, 3], [0, 3]]
LIVING_ROOM = [[4, 0], [9, 0], [9, 3], [4, 3]]  # shares a wall with the kitchen
TOP = [[0, 0], [1, 0], [1, 1], [0, 1]]


def add_room(conn, room_id, map_id="m1", name=None, key=None) -> int:
    conn.execute(
        "INSERT INTO room (id, map_id, name, key) VALUES (?, ?, ?, ?)",
        (room_id, map_id, name or f"room {room_id}", key),
    )
    return room_id


def add_object(conn, object_id, map_id="m1", layer="movable", **columns) -> int:
    """An object written with SQL, with a measured box unless told otherwise."""
    fields = {"id": object_id, "map_id": map_id, "label": "thing", "layer": layer, "state": "present",
              "center_x": 1.0, "center_y": 1.0, "center_z": 0.5,
              "size_x": 0.5, "size_y": 0.5, "size_z": 0.5,
              "first_seen": 0.0, "last_seen": 0.0, **columns}
    names = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO object ({names}) VALUES ({marks})", tuple(fields.values()))
    return object_id


def add_session(conn, session_id, *people, ended_at=1.0) -> int:
    """A session with guests. Only one can be open, so it has ended unless told otherwise."""
    conn.execute(
        "INSERT INTO session (id, started_at, ended_at) VALUES (?, 0, ?)", (session_id, ended_at)
    )
    for person_id in people:
        conn.execute(
            "INSERT INTO person (id, session_id, first_seen, last_seen) VALUES (?, ?, 0, 0)",
            (person_id, session_id),
        )
    return session_id


def add_team_member(conn, person_id) -> int:
    conn.execute(
        "INSERT INTO person (id, session_id, first_seen, last_seen) VALUES (?, NULL, 0, 0)",
        (person_id,),
    )
    return person_id


@pytest.fixture
def store(tmp_path):
    """A migrated file with two maps, so that a restart can reopen it."""
    conn = connect(tmp_path / "walkie.db")
    migrate(conn)
    with transaction(conn):
        conn.execute("INSERT INTO map (id, created_at) VALUES ('m1', 0)")
        conn.execute("INSERT INTO map (id, created_at) VALUES ('m2', 0)")
    yield conn
    conn.close()


@pytest.fixture
def furniture(store):
    """A shelf with one surface, an enclosed fridge, a cup, and a far shelf on m2."""
    with transaction(store):
        add_object(store, 1, layer="anchor", label="shelf")
        add_object(store, 2, layer="anchor", label="fridge", enclosed=1)
        add_object(store, 3, label="cup")
        add_object(store, 4, map_id="m2", layer="anchor", label="far shelf")
        surfaces = {
            "shelf_2": add_surface(store, 1, "shelf_2", 0.8, TOP),
            "far_top": add_surface(store, 4, "far_top", 0.8, TOP),
        }
    return store, surfaces


def count(conn, table) -> int:
    return conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


def schema_text(conn, name) -> str:
    return conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]


# Rooms -----------------------------------------------------------------------


def test_a_room_outline_works_in_either_winding_and_comes_back_counterclockwise(store):
    with transaction(store):
        add_room(store, 1)
        set_room_shape(store, 1, list(reversed(KITCHEN)))
    outline = room_outline(store, 1)
    assert len(outline) == 4
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(outline, [*outline[1:], outline[0]]))
    assert area > 0
    assert room_at(store, "m1", 2, 1) == 1
    assert room_at(store, "m1", 5, 1) is None


def test_a_closed_ring_is_the_same_as_an_open_one(store):
    with transaction(store):
        add_room(store, 1)
        add_room(store, 2)
        set_room_shape(store, 1, KITCHEN)
        set_room_shape(store, 2, [*KITCHEN, KITCHEN[0]])
    assert room_outline(store, 1) == room_outline(store, 2)


def test_setting_a_room_shape_replaces_the_old_one(store):
    with transaction(store):
        add_room(store, 1)
        set_room_shape(store, 1, KITCHEN)
        set_room_shape(store, 1, LIVING_ROOM)
    assert count(store, "room_shape") == 1
    assert room_at(store, "m1", 2, 1) is None
    assert room_at(store, "m1", 5, 1) == 1


@pytest.mark.parametrize(
    "outline",
    [
        [],
        [[0, 0], [1, 1]],
        [[0, 0], [1, 1], [0, 0]],
        [[0, 0], [1, 1], [2, 2]],  # no area
        [[0, 0], [1, 0], [float("nan"), 1]],
        [[0, 0], [1, 0], [float("inf"), 1]],
        [[0, 0, 0], [1, 0, 0], [1, 1, 0]],
        ["ab", "cd", 3],
    ],
)
def test_a_room_shape_with_no_real_polygon_is_refused_and_keeps_the_old_one(store, outline):
    with transaction(store):
        add_room(store, 1)
        set_room_shape(store, 1, KITCHEN)
    with pytest.raises(Refused):
        with transaction(store):
            set_room_shape(store, 1, outline)
    assert room_at(store, "m1", 2, 1) == 1


def test_a_shape_for_a_missing_room_is_refused(store):
    with transaction(store), pytest.raises(Refused, match="does not exist"):
        set_room_shape(store, 99, KITCHEN)


def test_a_point_on_a_shared_wall_goes_to_the_room_that_holds_it_inside(store):
    with transaction(store):
        add_room(store, 1)
        add_room(store, 2)
        set_room_shape(store, 1, KITCHEN)
        set_room_shape(store, 2, LIVING_ROOM)
        # A third room overlaps the wall, so one side has the wall point strictly inside.
        add_room(store, 3)
        set_room_shape(store, 3, [[3, 1], [5, 1], [5, 2], [3, 2]])
    assert room_at(store, "m1", 4, 1.5) == 3


def test_a_point_on_a_wall_with_no_inside_room_takes_the_lowest_id(store):
    with transaction(store):
        add_room(store, 1)
        add_room(store, 2)
        set_room_shape(store, 1, KITCHEN)
        set_room_shape(store, 2, LIVING_ROOM)
    assert room_at(store, "m1", 4, 1.5) == 1


def test_room_at_looks_only_in_the_named_map(store):
    with transaction(store):
        add_room(store, 1)
        add_room(store, 2, map_id="m2")
        set_room_shape(store, 2, KITCHEN)
    assert room_at(store, "m1", 2, 1) is None
    assert room_at(store, "m2", 2, 1) == 2


def test_deleting_a_room_takes_its_shape(store):
    with transaction(store):
        add_room(store, 1)
        set_room_shape(store, 1, KITCHEN)
        store.execute("DELETE FROM room WHERE id = 1")
    assert count(store, "room_shape") == 0
    assert room_outline(store, 1) == []


def test_a_room_key_and_its_display_name_are_separate(store):
    with transaction(store):
        add_room(store, 1, name="living room", key="living_room")
    assert room_by_key(store, "m1", "living_room")["name"] == "living room"
    assert room_by_key(store, "m1", "living room") is None
    assert room_by_key(store, "m2", "living_room") is None


def test_a_room_key_is_unique_per_map_and_many_rooms_may_have_none(store):
    with transaction(store):
        add_room(store, 1, key="kitchen")
        add_room(store, 2, map_id="m2", key="kitchen")
        add_room(store, 3)
        add_room(store, 4)
    with pytest.raises(sqlite3.IntegrityError), transaction(store):
        add_room(store, 5, key="kitchen")


def test_a_room_name_stays_unique_per_map(store):
    with transaction(store):
        add_room(store, 1, name="kitchen", key="a")
    with pytest.raises(sqlite3.IntegrityError), transaction(store):
        add_room(store, 2, name="kitchen", key="b")


# Surfaces --------------------------------------------------------------------


def test_a_surface_takes_its_map_from_its_anchor_and_stores_an_open_outline(furniture):
    store, surfaces = furniture
    row = store.execute("SELECT * FROM surface WHERE id = ?", (surfaces["shelf_2"],)).fetchone()
    assert row["map_id"] == "m1"
    assert row["anchor_id"] == 1
    assert row["geometry_valid"] == 1
    assert json.loads(row["outline"]) == TOP


def test_a_closed_surface_outline_is_stored_open(furniture):
    store, _ = furniture
    with transaction(store):
        surface_id = add_surface(store, 2, "fridge_top", 1.8, [*TOP, TOP[0]])
    row = store.execute("SELECT outline FROM surface WHERE id = ?", (surface_id,)).fetchone()
    assert json.loads(row["outline"]) == TOP


def test_a_surface_key_is_unique_per_map(furniture):
    store, _ = furniture
    with transaction(store), pytest.raises(Refused, match="UNIQUE"):
        add_surface(store, 2, "shelf_2", 1.0, TOP)
    with transaction(store):
        add_surface(store, 4, "shelf_2", 1.0, TOP)


def test_a_surface_needs_an_anchor_on_its_map(furniture):
    store, _ = furniture
    with transaction(store), pytest.raises(Refused, match="anchor"):
        add_surface(store, 3, "cup_top", 0.1, TOP)
    with transaction(store), pytest.raises(Refused, match="does not exist"):
        add_surface(store, 99, "nothing", 0.1, TOP)
    with pytest.raises(sqlite3.IntegrityError, match="anchor on its map"), transaction(store):
        store.execute(
            "INSERT INTO surface (map_id, anchor_id, key, z) VALUES ('m2', 1, 'crossed', 1)"
        )


@pytest.mark.parametrize("column, value", [("anchor_id", 2), ("map_id", "m2")])
def test_a_surface_cannot_move_to_another_anchor(furniture, column, value):
    store, surfaces = furniture
    with pytest.raises(sqlite3.IntegrityError, match="cannot move"), transaction(store):
        store.execute(f"UPDATE surface SET {column} = ? WHERE id = ?", (value, surfaces["shelf_2"]))


@pytest.mark.parametrize("outline", ['{"x": 1}', "not json", "3"])
def test_a_surface_outline_is_a_json_array(furniture, outline):
    store, _ = furniture
    with pytest.raises(sqlite3.IntegrityError), transaction(store):
        store.execute(
            "INSERT INTO surface (map_id, anchor_id, key, z, outline) VALUES ('m1', 1, 'odd', 1, ?)",
            (outline,),
        )


def test_a_surface_with_nan_height_or_blank_key_or_bad_outline_is_refused(furniture):
    store, _ = furniture
    with transaction(store):
        for key, z, outline in (
            ("flat", float("nan"), TOP),
            (" ", 1.0, TOP),
            ("line", 1.0, [[0, 0], [1, 1]]),
            ("none", 1.0, []),
        ):
            with pytest.raises(Refused):
                add_surface(store, 1, key, z, outline)


def test_an_unmeasured_surface_is_stored_but_not_usable(furniture):
    store, surfaces = furniture
    with transaction(store):
        add_surface(store, 1, "shelf_9", 0.5, geometry_valid=False)
    assert [row["id"] for row in usable_surfaces(store, 1)] == [surfaces["shelf_2"]]


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE object SET geometry_valid = 0 WHERE id = 1",
        "UPDATE object SET state = 'discarded' WHERE id = 1",
        "UPDATE object SET state = 'merged', merged_into = 2 WHERE id = 1",
    ],
)
def test_an_anchor_with_no_measured_box_or_gone_has_no_usable_surface(furniture, change):
    store, _ = furniture
    with transaction(store):
        store.execute(change)
    assert usable_surfaces(store, 1) == []


# Relations -------------------------------------------------------------------


def test_an_on_row_names_a_surface_of_the_anchor(furniture):
    store, surfaces = furniture
    with transaction(store):
        set_relation(store, 3, 1, ON, 0.7, 10.0, surface_id=surfaces["shelf_2"])
    row = relations(store, src_id=3)[0]
    assert tuple(row) == (3, 1, "on", surfaces["shelf_2"], "shelf_2", 0.7, 10.0)


def test_an_inside_row_names_only_the_anchor(furniture):
    store, _ = furniture
    with transaction(store):
        set_relation(store, 3, 2, INSIDE, 0.6, 10.0)
    row = relations(store, dst_id=2)[0]
    assert (row["predicate"], row["surface_id"], row["surface_key"]) == ("inside", None, None)


def test_setting_a_relation_again_updates_the_same_row(furniture):
    store, surfaces = furniture
    with transaction(store):
        set_relation(store, 3, 1, ON, 0.7, 10.0, surface_id=surfaces["shelf_2"])
        set_relation(store, 3, 1, ON, 0.9, 11.0, surface_id=surfaces["shelf_2"])
        set_relation(store, 3, 2, INSIDE, 0.2, 10.0)
        set_relation(store, 3, 2, INSIDE, 0.3, 11.0)
    assert count(store, "object_relation") == 2
    assert [(r["weight"], r["updated_at"]) for r in relations(store, src_id=3)] == [
        (0.9, 11.0), (0.3, 11.0),
    ]


def test_one_object_can_be_related_through_two_surfaces_of_an_anchor(furniture):
    store, surfaces = furniture
    with transaction(store):
        other = add_surface(store, 1, "shelf_3", 1.2, TOP)
        set_relation(store, 3, 1, ON, 0.5, 10.0, surface_id=surfaces["shelf_2"])
        set_relation(store, 3, 1, ON, 0.5, 10.0, surface_id=other)
    assert len(relations(store, src_id=3)) == 2


@pytest.mark.parametrize("predicate", ["near", "in_room", "ON", ""])
def test_near_and_in_room_are_not_stored(furniture, predicate):
    store, surfaces = furniture
    with transaction(store), pytest.raises(Refused, match="computed"):
        set_relation(store, 3, 1, predicate, 0.5, 10.0, surface_id=surfaces["shelf_2"])
    with pytest.raises(sqlite3.IntegrityError), transaction(store):
        store.execute(
            """INSERT INTO object_relation (src_id, dst_id, predicate, weight, updated_at)
               VALUES (3, 1, ?, 0.5, 10)""",
            (predicate,),
        )


def test_an_on_row_needs_a_surface_and_an_inside_row_has_none(furniture):
    store, surfaces = furniture
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), transaction(store):
        store.execute(
            """INSERT INTO object_relation (src_id, dst_id, predicate, weight, updated_at)
               VALUES (3, 1, 'on', 0.5, 10)"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), transaction(store):
        store.execute(
            """INSERT INTO object_relation (src_id, dst_id, predicate, surface_id, weight, updated_at)
               VALUES (3, 1, 'inside', ?, 0.5, 10)""",
            (surfaces["shelf_2"],),
        )
    with transaction(store), pytest.raises(Refused, match="usable surface"):
        set_relation(store, 3, 1, ON, 0.5, 10.0)


def test_a_surface_of_another_anchor_is_refused(furniture):
    store, surfaces = furniture
    with transaction(store), pytest.raises(Refused, match="usable surface"):
        set_relation(store, 3, 2, ON, 0.5, 10.0, surface_id=surfaces["shelf_2"])
    with pytest.raises(sqlite3.IntegrityError, match="its own surface"), transaction(store):
        store.execute(
            """INSERT INTO object_relation (src_id, dst_id, predicate, surface_id, weight, updated_at)
               VALUES (3, 2, 'on', ?, 0.5, 10)""",
            (surfaces["shelf_2"],),
        )


def test_a_relation_across_maps_is_refused(furniture):
    store, surfaces = furniture
    with transaction(store), pytest.raises(Refused, match="same map"):
        set_relation(store, 3, 4, ON, 0.5, 10.0, surface_id=surfaces["far_top"])
    with pytest.raises(sqlite3.IntegrityError, match="same map"), transaction(store):
        store.execute(
            """INSERT INTO object_relation (src_id, dst_id, predicate, surface_id, weight, updated_at)
               VALUES (3, 4, 'on', ?, 0.5, 10)""",
            (surfaces["far_top"],),
        )


def test_a_relation_to_a_movable_object_is_refused(furniture):
    store, _ = furniture
    with transaction(store):
        add_object(store, 5, label="bowl")
    with transaction(store), pytest.raises(Refused, match="not a measured anchor"):
        set_relation(store, 3, 5, INSIDE, 0.5, 10.0)
    with pytest.raises(sqlite3.IntegrityError, match="same map"), transaction(store):
        store.execute(
            """INSERT INTO object_relation (src_id, dst_id, predicate, weight, updated_at)
               VALUES (3, 5, 'inside', 0.5, 10)"""
        )


def test_an_object_cannot_relate_to_itself(furniture):
    store, _ = furniture
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), transaction(store):
        store.execute(
            """INSERT INTO object_relation (src_id, dst_id, predicate, weight, updated_at)
               VALUES (2, 2, 'inside', 0.5, 10)"""
        )
    with transaction(store), pytest.raises(Refused, match="CHECK"):
        set_relation(store, 2, 2, INSIDE, 0.5, 10.0)


def test_a_relation_cannot_be_pointed_elsewhere(furniture):
    store, surfaces = furniture
    with transaction(store):
        set_relation(store, 3, 2, INSIDE, 0.5, 10.0)
    with pytest.raises(sqlite3.IntegrityError, match="pointed elsewhere"), transaction(store):
        store.execute("UPDATE object_relation SET dst_id = 1, surface_id = ?, predicate = 'on'",
                      (surfaces["shelf_2"],))
    with transaction(store):
        store.execute("UPDATE object_relation SET weight = 0.25")
    assert relations(store, src_id=3)[0]["weight"] == 0.25


@pytest.mark.parametrize("weight", [-0.1, 1.1, float("nan")])
def test_a_relation_weight_is_from_zero_to_one(furniture, weight):
    store, _ = furniture
    with transaction(store), pytest.raises(Refused, match="weight"):
        set_relation(store, 3, 2, INSIDE, weight, 10.0)


def test_inside_needs_an_enclosed_anchor(furniture):
    store, _ = furniture
    with transaction(store), pytest.raises(Refused, match="not enclosed"):
        set_relation(store, 3, 1, INSIDE, 0.5, 10.0)


def test_an_unmeasured_surface_or_anchor_takes_no_new_relation(furniture):
    store, surfaces = furniture
    with transaction(store):
        unmeasured = add_surface(store, 1, "shelf_9", 0.5, geometry_valid=False)
        add_object(store, 5, layer="anchor", geometry_valid=0, enclosed=1)
    with transaction(store), pytest.raises(Refused, match="usable surface"):
        set_relation(store, 3, 1, ON, 0.5, 10.0, surface_id=unmeasured)
    with transaction(store), pytest.raises(Refused, match="not a measured anchor"):
        set_relation(store, 3, 5, INSIDE, 0.5, 10.0)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE object SET geometry_valid = 0 WHERE id = {anchor}",
        "UPDATE object SET state = 'discarded' WHERE id = {anchor}",
        "UPDATE surface SET geometry_valid = 0 WHERE anchor_id = {anchor}",
    ],
)
def test_a_stored_relation_is_kept_but_hidden_when_it_stops_being_usable(furniture, change):
    store, surfaces = furniture
    with transaction(store):
        set_relation(store, 3, 1, ON, 0.5, 10.0, surface_id=surfaces["shelf_2"])
    before = len(relations(store, src_id=3))
    with transaction(store):
        store.execute(change.format(anchor=1))
    assert before == 1
    assert relations(store, src_id=3) == []
    assert count(store, "object_relation") == 1


def test_relations_needs_something_to_look_up(furniture):
    store, _ = furniture
    with pytest.raises(Refused, match="needs an object"):
        relations(store)


def test_no_relation_is_inferred_from_where_things_are(furniture):
    store, _ = furniture
    with transaction(store):
        # The cup's box is on the shelf's surface. Nothing writes a row for it.
        store.execute(
            """UPDATE object SET center_x = 0.5, center_y = 0.5, center_z = 0.9,
                                 size_x = 0.1, size_y = 0.1, size_z = 0.2 WHERE id = 3"""
        )
    assert count(store, "object_relation") == 0


# What deleting a row takes with it -------------------------------------------


def test_deleting_an_anchor_takes_its_surfaces_and_relations(furniture):
    store, surfaces = furniture
    with transaction(store):
        set_relation(store, 3, 1, ON, 0.5, 10.0, surface_id=surfaces["shelf_2"])
        store.execute("DELETE FROM object WHERE id = 1")
    assert store.execute("SELECT id FROM surface WHERE anchor_id = 1").fetchall() == []
    assert count(store, "object_relation") == 0
    assert store.execute("SELECT 1 FROM object WHERE id = 3").fetchone()


def test_deleting_the_source_object_takes_its_relations(furniture):
    store, _ = furniture
    with transaction(store):
        set_relation(store, 3, 2, INSIDE, 0.5, 10.0)
        store.execute("DELETE FROM object WHERE id = 3")
    assert count(store, "object_relation") == 0


def test_deleting_a_surface_takes_only_the_relations_through_it(furniture):
    store, surfaces = furniture
    with transaction(store):
        set_relation(store, 3, 1, ON, 0.5, 10.0, surface_id=surfaces["shelf_2"])
        set_relation(store, 3, 2, INSIDE, 0.5, 10.0)
        store.execute("DELETE FROM surface WHERE id = ?", (surfaces["shelf_2"],))
    assert [row["predicate"] for row in relations(store, src_id=3)] == ["inside"]


def test_deleting_a_map_takes_everything_on_it(furniture):
    store, surfaces = furniture
    with transaction(store):
        set_relation(store, 3, 1, ON, 0.5, 10.0, surface_id=surfaces["shelf_2"])
        add_catalogue_item(store, "m1", "Water", aliases=["bottle"])
        add_event(store, "m1", "note", "started", 1.0)
        add_object_event(store, 3, None, "present", 1.0)
        store.execute("DELETE FROM map WHERE id = 'm1'")
    for table in ("object_catalogue", "catalogue_alias", "object_relation", "object_event", "event",
                  "event_fts"):
        assert count(store, table) == 0, table
    assert [row["key"] for row in store.execute("SELECT key FROM surface")] == ["far_top"]


def test_deleting_an_item_clears_the_link_and_keeps_the_object(furniture):
    store, _ = furniture
    with transaction(store):
        item = add_catalogue_item(store, "m1", "Water", aliases=["bottle"])
        link_catalogue(store, 3, item)
        store.execute("DELETE FROM object_catalogue WHERE id = ?", (item,))
    assert store.execute("SELECT catalogue_id FROM object WHERE id = 3").fetchone()[0] is None
    assert count(store, "catalogue_alias") == 0


def test_deleting_an_object_clears_events_and_takes_its_audit_trail(furniture):
    store, _ = furniture
    with transaction(store):
        event_id = add_event(store, "m1", "action", "picked up", 1.0, object_id=3)
        add_object_event(store, 3, None, "present", 1.0)
        store.execute("DELETE FROM object WHERE id = 3")
    assert store.execute("SELECT object_id FROM event WHERE id = ?", (event_id,)).fetchone()[0] is None
    assert count(store, "object_event") == 0


def test_deleting_a_room_clears_the_link_on_an_event_and_keeps_the_event(store):
    with transaction(store):
        add_room(store, 1)
        event_id = add_event(store, "m1", "note", "went in", 1.0, room_id=1)
        store.execute("DELETE FROM room WHERE id = 1")
    assert store.execute("SELECT room_id FROM event WHERE id = ?", (event_id,)).fetchone()[0] is None


# Catalogue -------------------------------------------------------------------


def test_an_item_is_found_by_official_name_or_any_alias_in_any_case(store):
    with transaction(store):
        item = add_catalogue_item(
            store, "m1", "Water Bottle", category="drink", default_place="kitchen",
            weight_g=500.0, attributes={"colour": "blue"}, aliases=["Bottle ", "H2O  bottle"],
        )
    for name in ("Water Bottle", "bottle", "BOTTLE", " h2o bottle "):
        assert [row["id"] for row in find_catalogue_items(store, "m1", name)] == [item]
    assert find_catalogue_items(store, "m1", "water") == []
    assert find_catalogue_items(store, "m2", "bottle") == []
    row = find_catalogue_items(store, "m1", "bottle")[0]
    assert (row["category"], row["default_place"], row["weight_g"]) == ("drink", "kitchen", 500.0)
    assert json.loads(row["attributes"]) == {"colour": "blue"}


def test_an_alias_shared_by_two_items_finds_both_without_choosing(store):
    with transaction(store):
        first = add_catalogue_item(store, "m1", "Cola", aliases=["drink"])
        second = add_catalogue_item(store, "m1", "Juice", aliases=["drink"])
    assert [row["id"] for row in find_catalogue_items(store, "m1", "drink")] == [first, second]


def test_an_item_that_matches_by_name_and_alias_comes_back_once(store):
    with transaction(store):
        add_catalogue_item(store, "m1", "cola", aliases=["cola", "COLA"])
    assert len(find_catalogue_items(store, "m1", "cola")) == 1


def test_an_official_name_is_unique_per_map(store):
    with transaction(store):
        add_catalogue_item(store, "m1", "Cola")
        add_catalogue_item(store, "m2", "Cola")
        with pytest.raises(Refused, match="UNIQUE"):
            add_catalogue_item(store, "m1", "Cola")


@pytest.mark.parametrize("weight", [0, -1])
def test_a_weight_must_be_above_zero(store, weight):
    with transaction(store), pytest.raises(Refused, match="CHECK"):
        add_catalogue_item(store, "m1", "Air", weight_g=weight)


def test_a_catalogue_item_needs_a_name_and_a_real_alias(store):
    with transaction(store):
        with pytest.raises(Refused, match="official name"):
            add_catalogue_item(store, "m1", " ")
        with pytest.raises(Refused, match="blank alias"):
            add_catalogue_item(store, "m1", "Cola", aliases=[" "])
        with pytest.raises(Refused, match="FOREIGN KEY"):
            add_catalogue_item(store, "no such map", "Cola")


@pytest.mark.parametrize("attributes", ["[]", "3", "not json"])
def test_catalogue_attributes_are_a_json_object(store, attributes):
    with pytest.raises(sqlite3.IntegrityError), transaction(store):
        store.execute(
            "INSERT INTO object_catalogue (map_id, official_name, attributes) VALUES ('m1', 'x', ?)",
            (attributes,),
        )


def test_an_objects_weight_comes_from_its_catalogue_item(furniture):
    store, _ = furniture
    before = store.execute("SELECT revision FROM object WHERE id = 3").fetchone()[0]
    assert object_weight_g(store, 3) is None
    with transaction(store):
        item = add_catalogue_item(store, "m1", "Cup", weight_g=120.0)
        link_catalogue(store, 3, item)
    assert object_weight_g(store, 3) == 120.0
    assert store.execute("SELECT revision FROM object WHERE id = 3").fetchone()[0] == before + 1
    with transaction(store):
        link_catalogue(store, 3, None)
    assert object_weight_g(store, 3) is None


def test_an_object_links_only_to_an_item_of_its_map(furniture):
    store, _ = furniture
    with transaction(store):
        far = add_catalogue_item(store, "m2", "Cup")
        with pytest.raises(Refused, match="different maps"):
            link_catalogue(store, 3, far)
        with pytest.raises(Refused, match="Catalogue item 99"):
            link_catalogue(store, 3, 99)
        with pytest.raises(Refused, match="Object 99"):
            link_catalogue(store, 99, far)


# State changes ---------------------------------------------------------------


def test_the_audit_trail_records_changes_in_order_and_changes_no_state(furniture):
    store, _ = furniture
    with transaction(store):
        add_object_event(store, 3, "candidate", "present", 2.0, cause="seen twice", task_id="t1")
        add_object_event(store, 3, None, "candidate", 1.0)
    rows = object_events(store, 3)
    assert [(r["old_state"], r["new_state"]) for r in rows] == [
        (None, "candidate"), ("candidate", "present"),
    ]
    assert (rows[1]["cause"], rows[1]["task_id"]) == ("seen twice", "t1")
    assert store.execute("SELECT state FROM object WHERE id = 3").fetchone()[0] == "present"


def test_an_audit_row_for_a_missing_object_is_refused(store):
    with transaction(store), pytest.raises(Refused, match="FOREIGN KEY"):
        add_object_event(store, 99, None, "present", 1.0)


def test_a_tombstone_keeps_the_audit_trail(furniture):
    store, _ = furniture
    with transaction(store):
        add_object_event(store, 3, "present", "discarded", 1.0)
        tombstone_object(store, 3)
    assert len(object_events(store, 3)) == 1


# The task's log --------------------------------------------------------------


def fts_ids(conn, query) -> list[int]:
    return [row[0] for row in conn.execute(
        "SELECT rowid FROM event_fts WHERE event_fts MATCH ? ORDER BY rowid", (query,)
    )]


def check_index(conn) -> None:
    """Raises when the word index and the event rows disagree."""
    conn.execute("INSERT INTO event_fts (event_fts, rank) VALUES ('integrity-check', 1)")


def test_a_new_event_can_be_found_by_a_form_of_its_words(store):
    with transaction(store):
        event_id = add_event(store, "m1", "action", "Delivered the drinks", 1.0)
    assert fts_ids(store, "drink") == [event_id]
    assert fts_ids(store, "deliver") == [event_id]
    check_index(store)


def test_an_edit_replaces_the_words_in_the_index(store):
    with transaction(store):
        event_id = add_event(store, "m1", "note", "saw a red cup", 1.0)
    with transaction(store):
        store.execute("UPDATE event SET text = 'saw a blue plate' WHERE id = ?", (event_id,))
    assert fts_ids(store, "cup") == []
    assert fts_ids(store, "plate") == [event_id]
    check_index(store)


def test_a_change_to_another_column_leaves_the_index_alone(store):
    with transaction(store):
        event_id = add_event(store, "m1", "note", "saw a red cup", 1.0)
    with transaction(store):
        store.execute("UPDATE event SET cause = 'asked' WHERE id = ?", (event_id,))
    assert fts_ids(store, "cup") == [event_id]
    check_index(store)


def test_a_deleted_event_leaves_the_index(store):
    with transaction(store):
        event_id = add_event(store, "m1", "note", "saw a red cup", 1.0)
    with transaction(store):
        store.execute("DELETE FROM event WHERE id = ?", (event_id,))
    assert fts_ids(store, "cup") == []
    check_index(store)


def test_a_rolled_back_event_is_not_in_the_index(store):
    with pytest.raises(RuntimeError), transaction(store):
        add_event(store, "m1", "note", "saw a red cup", 1.0)
        raise RuntimeError
    assert fts_ids(store, "cup") == []
    check_index(store)


def test_an_event_stores_what_it_was_given(furniture):
    store, _ = furniture
    with transaction(store):
        add_room(store, 1)
        add_session(store, 7, 70)
        event_id = add_event(
            store, "m1", "handover", "  gave the cup  ", 1.5, pose=(1.0, 2.0, 0.5), room_id=1,
            object_id=3, person_id=70, session_id=7, task_id="t1", cause="asked",
        )
    row = store.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
    assert dict(row) == {
        "id": event_id, "ts": 1.5, "kind": "handover", "text": "gave the cup", "map_id": "m1",
        "x": 1.0, "y": 2.0, "yaw": 0.5, "room_id": 1, "object_id": 3, "person_id": 70,
        "session_id": 7, "task_id": "t1", "cause": "asked",
    }


def test_an_event_has_a_whole_pose_or_none(store):
    with transaction(store):
        add_event(store, "m1", "note", "no pose", 1.0)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), transaction(store):
        store.execute(
            "INSERT INTO event (ts, kind, text, map_id, x) VALUES (1, 'note', 'half', 'm1', 1.5)"
        )
    with transaction(store), pytest.raises(Refused, match="NaN"):
        add_event(store, "m1", "note", "bad pose", 1.0, pose=(float("nan"), 0.0, 0.0))


@pytest.mark.parametrize("kind", ["", "ACTION", "chat"])
def test_an_unknown_kind_is_refused(store, kind):
    with transaction(store), pytest.raises(Refused, match="kind"):
        add_event(store, "m1", kind, "hello", 1.0)


def test_event_text_is_bounded(store):
    with transaction(store):
        add_event(store, "m1", "note", "x" * MAX_EVENT_TEXT, 1.0)
        for text in ("", "  ", "x" * (MAX_EVENT_TEXT + 1), "a\x00b"):
            with pytest.raises(Refused, match="text"):
                add_event(store, "m1", "note", text, 1.0)
    for text in ("", "x" * (MAX_EVENT_TEXT + 1)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"), transaction(store):
            store.execute(
                "INSERT INTO event (ts, kind, text, map_id) VALUES (1, 'note', ?, 'm1')", (text,)
            )


def test_an_event_cannot_name_a_room_or_object_of_another_map(furniture):
    store, _ = furniture
    with transaction(store):
        add_room(store, 1, map_id="m2")
        with pytest.raises(Refused, match="room 1"):
            add_event(store, "m1", "note", "hello", 1.0, room_id=1)
        with pytest.raises(Refused, match="object 4"):
            add_event(store, "m1", "note", "hello", 1.0, object_id=4)
        with pytest.raises(Refused, match="room 99"):
            add_event(store, "m1", "note", "hello", 1.0, room_id=99)
        with pytest.raises(Refused, match="object 99"):
            add_event(store, "m1", "note", "hello", 1.0, object_id=99)
        with pytest.raises(Refused, match="FOREIGN KEY"):
            add_event(store, "no such map", "note", "hello", 1.0)


def test_a_guest_can_be_named_only_with_their_session(store):
    with transaction(store):
        add_session(store, 7, 70)
        add_session(store, 8)
        for session_id in (None, 8):
            with pytest.raises(Refused, match="belongs to session 7"):
                add_event(store, "m1", "note", "hello", 1.0, person_id=70, session_id=session_id)
        with pytest.raises(Refused, match="person 99"):
            add_event(store, "m1", "note", "hello", 1.0, person_id=99)
        add_event(store, "m1", "note", "hello", 1.0, person_id=70, session_id=7)


def test_find_events_returns_the_newest_first_with_the_id_as_tie_break(store):
    with transaction(store):
        first = add_event(store, "m1", "note", "one", 5.0)
        second = add_event(store, "m1", "note", "two", 5.0)
        newest = add_event(store, "m1", "note", "three", 9.0)
        oldest = add_event(store, "m1", "note", "four", 1.0)
    rows, truncated = find_events(store, "m1")
    assert [row["id"] for row in rows] == [newest, second, first, oldest]
    assert truncated is False


def test_find_events_says_when_it_stopped_early(store):
    with transaction(store):
        ids = [add_event(store, "m1", "note", "same", float(ts)) for ts in range(3)]
    rows, truncated = find_events(store, "m1", max_results=2)
    assert [row["id"] for row in rows] == [ids[2], ids[1]]
    assert truncated is True
    rows, truncated = find_events(store, "m1", max_results=3)
    assert len(rows) == 3
    assert truncated is False


def test_find_events_filters_by_map_time_room_and_words(store):
    with transaction(store):
        add_room(store, 1)
        add_room(store, 2)
        far = add_event(store, "m2", "note", "cup seen far away", 9.0)
        old = add_event(store, "m1", "note", "cup seen long ago", 1.0)
        kitchen = add_event(store, "m1", "note", "cup seen in kitchen", 5.0, room_id=1)
        hall = add_event(store, "m1", "note", "plate seen in hall", 6.0, room_id=2)

    def ids(**query) -> list[int]:
        return [row["id"] for row in find_events(store, "m1", **query)[0]]

    assert ids() == [hall, kitchen, old]
    assert ids(text="cup") == [kitchen, old]
    assert ids(text="cups seen") == [kitchen, old]
    assert ids(text="cup hall") == []
    assert ids(since=5.0) == [hall, kitchen]
    assert ids(room_id=1) == [kitchen]
    assert ids(text="seen", since=2.0, room_id=2) == [hall]
    assert far not in ids(text="far")


@pytest.mark.parametrize(
    "text",
    [
        'cup" OR "plate',
        "cup OR plate",
        "NEAR(cup plate)",
        "text:cup*",
        "cup AND",
        "(",
        '"',
        '""',
        "*",
        "-",
        "^cup",
        "a\x00b",
    ],
)
def test_search_words_are_never_match_syntax(store, text):
    with transaction(store):
        add_event(store, "m1", "note", "a plate", 1.0)
    # No syntax error and no widening: every word must match, so the plate stays unfound.
    assert find_events(store, "m1", text=text) == ([], False)


@pytest.mark.parametrize("text", ['"plate"', "plate (", "PLATES", "plate -"])
def test_a_word_in_odd_clothes_still_finds_that_word(store, text):
    with transaction(store):
        event_id = add_event(store, "m1", "note", "a plate", 1.0)
    assert [row["id"] for row in find_events(store, "m1", text=text)[0]] == [event_id]


@pytest.mark.parametrize("max_results", [0, -1, MAX_EVENT_RESULTS + 1])
def test_find_events_refuses_a_limit_out_of_range(store, max_results):
    with pytest.raises(Refused, match="max_results"):
        find_events(store, "m1", max_results=max_results)


def test_helpers_that_write_refuse_to_run_outside_a_transaction(furniture):
    store, surfaces = furniture
    calls = [
        lambda: set_room_shape(store, 1, KITCHEN),
        lambda: add_surface(store, 1, "k", 1.0, TOP),
        lambda: add_catalogue_item(store, "m1", "x"),
        lambda: link_catalogue(store, 3, None),
        lambda: set_relation(store, 3, 2, INSIDE, 0.5, 1.0),
        lambda: add_object_event(store, 3, None, "present", 1.0),
        lambda: add_event(store, "m1", "note", "hello", 1.0),
        lambda: record_write(store, "r", "t", "op", {}, {}, 1.0),
    ]
    for call in calls:
        with pytest.raises(Refused, match="caller's transaction"):
            call()
    assert count(store, "event") == 0


# Forgetting a session --------------------------------------------------------


def event_rows(conn) -> dict[str, tuple]:
    return {
        row["text"]: (row["person_id"], row["session_id"])
        for row in conn.execute("SELECT text, person_id, session_id FROM event")
    }


@pytest.fixture
def session_log(store):
    """Events of session 7 and its guest 70, a team member, and another session."""
    with transaction(store):
        add_session(store, 7, 70, ended_at=None)
        add_session(store, 8, 80)
        add_team_member(store, 90)
        add_event(store, "m1", "note", "guest asked for water", 1.0, person_id=70, session_id=7)
        add_event(store, "m1", "note", "team member joined", 2.0, person_id=90, session_id=7)
        add_event(store, "m1", "note", "session seven moved", 3.0, session_id=7)
        add_event(store, "m1", "note", "other guest waved", 4.0, person_id=80, session_id=8)
        add_event(store, "m1", "note", "team member alone", 5.0, person_id=90)
        add_event(store, "m1", "note", "robot docked", 6.0)
    return store


def test_forgetting_a_session_takes_the_events_that_name_a_person_in_it(session_log):
    with transaction(session_log):
        session_log.execute("DELETE FROM session WHERE id = 7")
    assert event_rows(session_log) == {
        "session seven moved": (None, None),
        "other guest waved": (80, 8),
        "team member alone": (90, None),
        "robot docked": (None, None),
    }


def test_forgetting_a_session_leaves_no_words_of_those_events_searchable(session_log):
    assert fts_ids(session_log, "water")
    with transaction(session_log):
        session_log.execute("DELETE FROM session WHERE id = 7")
    assert fts_ids(session_log, "water") == []
    assert fts_ids(session_log, "joined") == []
    assert find_events(session_log, "m1", text="water")[0] == []
    assert fts_ids(session_log, "docked")
    check_index(session_log)


def test_the_event_goes_before_the_set_null_can_empty_it(session_log):
    """The rule's whole point: no row is left with its text and a person_id of NULL."""
    with transaction(session_log):
        session_log.execute("DELETE FROM session WHERE id = 7")
    orphans = session_log.execute(
        "SELECT text FROM event WHERE text LIKE '%guest asked%' OR text LIKE '%team member joined%'"
    ).fetchall()
    assert orphans == []
    assert session_log.execute("SELECT 1 FROM person WHERE id = 70").fetchone() is None


def test_the_foreign_key_alone_would_keep_the_text(session_log):
    """Without the trigger the line stays with no person. This is what the trigger prevents."""
    with transaction(session_log):
        session_log.execute("DROP TRIGGER session_forget_events")
        session_log.execute("DELETE FROM session WHERE id = 7")
    assert event_rows(session_log)["guest asked for water"] == (None, None)


def test_the_trigger_runs_before_delete_on_session(store):
    assert "BEFORE DELETE ON session" in schema_text(store, "session_forget_events")


def test_a_guests_event_with_no_session_on_it_goes_with_the_guest(store):
    """SQL can write a row that add_event refuses. Forgetting must still reach it."""
    with transaction(store):
        add_session(store, 7, 70)
        add_session(store, 8)
        for session_id, text in ((None, "no session"), (8, "wrong session")):
            store.execute(
                """INSERT INTO event (ts, kind, text, map_id, person_id, session_id)
                   VALUES (1, 'note', ?, 'm1', 70, ?)""",
                (text, session_id),
            )
    with transaction(store):
        store.execute("DELETE FROM session WHERE id = 7")
    assert event_rows(store) == {}
    check_index(store)


def test_a_forgotten_session_rolls_back_whole(session_log):
    before = event_rows(session_log)
    with pytest.raises(RuntimeError), transaction(session_log):
        session_log.execute("DELETE FROM session WHERE id = 7")
        raise RuntimeError
    assert event_rows(session_log) == before
    assert session_log.execute("SELECT 1 FROM person WHERE id = 70").fetchone()
    assert fts_ids(session_log, "water")
    check_index(session_log)


def test_deleting_a_person_alone_clears_the_link_and_keeps_the_event(session_log):
    with transaction(session_log):
        session_log.execute("DELETE FROM person WHERE id = 80")
    assert event_rows(session_log)["other guest waved"] == (None, 8)


def test_end_session_forgets_events_through_the_people_api(session_log):
    from walkie_database.people import end_session

    assert end_session(session_log, 7) == (7, 1)
    assert "guest asked for water" not in event_rows(session_log)
    assert session_log.execute("SELECT 1 FROM person WHERE id = 90").fetchone()


def test_starting_a_new_session_forgets_the_open_ones_events(store):
    from walkie_database.people import start_session

    started = start_session(store, "first", False, 1.0)
    with transaction(store):
        store.execute(
            "INSERT INTO person (id, session_id, first_seen, last_seen) VALUES (70, ?, 0, 0)",
            (started.id,),
        )
        add_event(store, "m1", "note", "guest arrived", 1.0, person_id=70, session_id=started.id)
    assert start_session(store, "second", False, 2.0).forgot_id == started.id
    assert event_rows(store) == {}


def test_forgetting_a_session_still_takes_its_people_and_keeps_the_team(session_log):
    with transaction(session_log):
        session_log.execute("DELETE FROM session WHERE id = 7")
    people = [row[0] for row in session_log.execute("SELECT id FROM person ORDER BY id")]
    assert people == [80, 90]


# Retried writes --------------------------------------------------------------


REQUEST = {"map_id": "m1", "kind": "note", "text": "hello", "object_id": 0}


def test_canonical_json_is_the_same_for_the_same_content():
    assert canonical_json({"b": 1, "a": [1.5, None, "é"]}, limit=100) == '{"a":[1.5,null,"é"],"b":1}'
    assert payload_hash({"b": 1, "a": 2}) == payload_hash({"a": 2, "b": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})
    assert len(payload_hash({})) == 64


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        {"a": [float("-inf")]},
        b"bytes",
        {"a": b"bytes"},
        {1: "number key"},
        {"a": object()},
        {"a": np.zeros(3)},
        {"a": {1, 2}},
        "\ud800",
    ],
)
def test_what_json_cannot_hold_exactly_is_refused(value):
    with pytest.raises(Refused):
        canonical_json(value, limit=1024)


@pytest.mark.parametrize(
    "value",
    [
        {"embedding": [0.0] * (MAX_JSON_ARRAY + 1)},
        {"embedding": np.zeros(512).tolist()},
        {"crop": "A" * (MAX_JSON_STRING + 1)},
        {f"k{index}": 0 for index in range(MAX_JSON_ARRAY + 1)},
    ],
)
def test_raw_data_does_not_fit_in_a_request(value):
    with pytest.raises(Refused):
        payload_hash(value)


def test_nesting_and_total_size_are_bounded():
    deep: list = []
    for _ in range(MAX_JSON_DEPTH + 1):
        deep = [deep]
    with pytest.raises(Refused, match="nested"):
        payload_hash(deep)
    wide = {f"k{index}": "x" * MAX_JSON_STRING for index in range(MAX_JSON_ARRAY)}
    with pytest.raises(Refused, match=str(MAX_PAYLOAD_JSON)):
        payload_hash(wide)
    with pytest.raises(Refused, match=str(MAX_OUTCOME_JSON)):
        canonical_json({f"k{index}": "x" * 100 for index in range(50)}, limit=MAX_OUTCOME_JSON)


def test_a_new_request_has_no_outcome_and_then_keeps_its_first_one(store):
    assert find_write(store, "r1", "remember_event", REQUEST) is None
    with transaction(store):
        first = record_write(store, "r1", "t1", "remember_event", REQUEST, {"event_id": 5}, 1.0)
    assert first == {"event_id": 5}
    assert find_write(store, "r1", "remember_event", REQUEST) == {"event_id": 5}
    with transaction(store):
        again = record_write(store, "r1", "t1", "remember_event", REQUEST, {"event_id": 6}, 2.0)
    assert again == {"event_id": 5}
    row = store.execute("SELECT * FROM write_request").fetchone()
    assert (row["task_id"], row["operation"], row["committed_at"]) == ("t1", "remember_event", 1.0)
    assert row["payload_hash"] == payload_hash(REQUEST)
    assert count(store, "write_request") == 1


def test_key_order_in_a_retry_does_not_matter(store):
    with transaction(store):
        record_write(store, "r1", "t1", "op", {"a": 1, "b": 2}, {"ok": True}, 1.0)
    assert find_write(store, "r1", "op", {"b": 2, "a": 1}) == {"ok": True}


def test_a_request_id_used_for_something_else_is_refused(store):
    with transaction(store):
        record_write(store, "r1", "t1", "remember_event", REQUEST, {"event_id": 5}, 1.0)
    with pytest.raises(WriteConflict, match="remember_event"):
        find_write(store, "r1", "forget_object", REQUEST)
    with pytest.raises(WriteConflict, match="different payload"):
        find_write(store, "r1", "remember_event", {**REQUEST, "text": "other"})
    with transaction(store), pytest.raises(WriteConflict):
        record_write(store, "r1", "t1", "remember_event", {**REQUEST, "text": "other"}, {}, 2.0)
    assert issubclass(WriteConflict, Refused)
    assert count(store, "write_request") == 1


def test_a_request_outcome_is_an_object_of_bounded_json(store):
    with transaction(store):
        for outcome in ([1], "ok", {"embedding": [0.0] * 512}, {"a": b"raw"}):
            with pytest.raises(Refused):
                record_write(store, "r1", "t1", "op", {}, outcome, 1.0)
    assert count(store, "write_request") == 0


@pytest.mark.parametrize("request_id", ["", "r" * (MAX_REQUEST_ID + 1)])
def test_a_request_id_has_a_bounded_length(store, request_id):
    with pytest.raises(Refused, match="request id"):
        find_write(store, request_id, "op", {})


def test_a_rolled_back_write_records_no_outcome(store):
    with pytest.raises(RuntimeError), transaction(store):
        record_write(store, "r1", "t1", "op", {}, {"ok": True}, 1.0)
        raise RuntimeError
    assert find_write(store, "r1", "op", {}) is None


def test_retry_history_survives_a_restart(tmp_path):
    path = tmp_path / "walkie.db"
    conn = connect(path)
    migrate(conn)
    with transaction(conn):
        record_write(conn, "r1", "t1", "op", {"a": 1}, {"event_id": 5}, 1.0)
    conn.close()
    conn = connect(path)
    migrate(conn)
    assert find_write(conn, "r1", "op", {"a": 1}) == {"event_id": 5}
    with pytest.raises(WriteConflict):
        find_write(conn, "r1", "op", {"a": 2})
    conn.close()


def test_a_new_store_id_does_not_clear_retry_history(store):
    with transaction(store):
        store.execute(
            """INSERT INTO store_meta (id, store_id, clock_kind, map_id, created_at)
               VALUES (1, 'first', 'wall', 'm1', 0)"""
        )
        record_write(store, "r1", "t1", "op", {}, {"ok": True}, 1.0)
    with transaction(store):
        store.execute("UPDATE store_meta SET store_id = 'second'")
    assert find_write(store, "r1", "op", {}) == {"ok": True}


def test_the_ledger_has_no_column_for_raw_data(store):
    columns = {row["name"]: row["type"] for row in store.execute("PRAGMA table_info(write_request)")}
    assert columns == {
        "request_id": "TEXT", "task_id": "TEXT", "operation": "TEXT", "payload_hash": "TEXT",
        "outcome_json": "TEXT", "committed_at": "REAL",
    }
