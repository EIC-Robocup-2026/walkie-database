"""The seed tool: what it refuses, what it writes, and how IDs cross re-seeds."""

import ast
import math
import os
import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml
from seed_helpers import (
    ARENA,
    DATE,
    anchor,
    anchors_document,
    bare_anchor,
    catalogue_document,
    digest_of,
    dump_rows,
    files_in,
    map_id_of,
    other_room,
    room,
    rooms_document,
    seed_argv,
    write_manifest,
    write_map_folder,
    write_yaml,
)
from walkie_map_id import make_map_id

import walkie_database
from walkie_database.db import connect, transaction
from walkie_database.map_config import read_map_folder
from walkie_database.migrate import load_migrations, migrate, schema_version
from walkie_database.object_context import (
    find_catalogue_items,
    room_at,
    room_outline,
    usable_surfaces,
)
from walkie_database.objects import NewObject, insert_object, reserve_object_ids
from walkie_database.people import Refused
from walkie_database.seed_store import RUN_TABLES, SEED_STORE_ID, main, write_seed
from walkie_database.store_lock import StoreLock, lock_path

CLIP = "clip_vit_b16"
SHA = "a" * 64


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


@pytest.fixture
def map_id(folder):
    return map_id_of(folder)


@pytest.fixture
def output(tmp_path):
    return tmp_path / "out" / "seed.db"


def build(tmp_path, folder, output, **kwargs):
    assert main(seed_argv(tmp_path, folder, output, **kwargs)) == 0


def read(path):
    return connect(path, readonly=True)


def object_ids(path):
    conn = read(path)
    try:
        return {row["key"]: row["id"] for row in conn.execute("SELECT key, id FROM object")}
    finally:
        conn.close()


def counters(path):
    conn = read(path)
    try:
        return {row["table_name"]: row["id"] for row in conn.execute("SELECT * FROM last_id")}
    finally:
        conn.close()


def fingerprint(**changes):
    return {
        "model": CLIP,
        "dimension": 512,
        "embedding": [1.0] + [0.0] * 511,
        "provenance": {"weights_sha256": SHA, "crop_sha256": SHA},
    } | changes


# What a seed holds ---------------------------------------------------------------


def test_a_seed_holds_the_map_its_rooms_anchors_and_catalogue(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, clock_kind="ros_sim")

    conn = read(output)
    assert schema_version(conn) == load_migrations()[-1].version
    row = conn.execute("SELECT * FROM map").fetchone()
    assert (row["id"], row["frame_id"], row["map_hash"]) == (map_id, "map", digest_of(folder))
    assert (row["clock_kind"], row["baseline_rev"]) == ("ros_sim", "")
    meta = conn.execute("SELECT * FROM store_meta").fetchone()
    assert (meta["store_id"], meta["map_id"]) == (SEED_STORE_ID, map_id)
    assert (meta["clock_kind"], meta["refresh_anchors_pending"]) == ("ros_sim", 1)
    keys = [r["key"] for r in conn.execute("SELECT key FROM room ORDER BY key")]
    assert keys == ["kitchen_room", "living_room"]

    table = conn.execute("SELECT * FROM object WHERE key = 'table'").fetchone()
    assert (table["source"], table["layer"], table["state"]) == ("map", "anchor", "present")
    assert (table["first_seen"], table["last_seen"], table["n_detections"]) == (0.0, 0.0, 0)
    assert (table["geometry_valid"], table["center_z"], table["size_z"]) == (1, 0.4, 0.8)
    item = conn.execute("SELECT * FROM object_catalogue").fetchone()
    assert (item["official_name"], item["weight_g"]) == ("blue cup", 250.0)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()


def test_the_map_id_comes_from_the_folder(tmp_path, folder, output):
    build(tmp_path, folder, output)
    conn = read(output)
    expected = make_map_id(ARENA, DATE, digest_of(folder))
    assert conn.execute("SELECT id FROM map").fetchone()[0] == expected
    conn.close()


def test_a_seed_has_no_run_data_and_no_vectors(tmp_path, folder, output):
    build(tmp_path, folder, output)

    conn = read(output)
    for table in RUN_TABLES:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT count(*) FROM object WHERE layer != 'anchor'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM object_clip_vit_b16").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM object WHERE fingerprint_weight != 0").fetchone()[0] == 0
    conn.close()


def test_a_seed_is_a_closed_file(tmp_path, folder, output):
    build(tmp_path, folder, output)
    assert sorted(p.name for p in output.parent.iterdir()) == ["seed.db", "seed.db.lock"]


def test_the_stores_readers_work_on_a_seed(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output)

    conn = read(output)
    living = conn.execute("SELECT id FROM room WHERE key = 'living_room'").fetchone()[0]
    assert room_at(conn, map_id, 1.0, 1.0) == living
    assert room_at(conn, map_id, 20.0, 20.0) is None
    assert room_outline(conn, living)[0] == [0.0, 0.0]
    table = conn.execute("SELECT id FROM object WHERE key = 'table'").fetchone()[0]
    assert [s["key"] for s in usable_surfaces(conn, table)] == ["table_top"]
    box = conn.execute("SELECT * FROM object_box WHERE id = ?", (table,)).fetchone()
    assert box["max_z"] == pytest.approx(0.8)
    assert find_catalogue_items(conn, map_id, "mug")[0]["official_name"] == "blue cup"
    conn.close()


def test_an_anchor_with_no_measurement_gets_no_box(tmp_path, folder, output):
    build(tmp_path, folder, output)

    conn = read(output)
    row = conn.execute("SELECT * FROM object WHERE key = 'wardrobe'").fetchone()
    assert (row["geometry_valid"], row["enclosed"]) == (0, 1)
    assert (row["center_x"], row["size_x"]) == (0.0, 0.0)
    assert conn.execute("SELECT count(*) FROM object_box WHERE id = ?", (row["id"],)).fetchone()[0] == 0
    conn.close()


def test_a_rooms_only_seed_is_valid(tmp_path, folder, map_id, output):
    build(
        tmp_path, folder, output,
        anchors=anchors_document(map_id, anchors=[]),
        catalogue=catalogue_document(objects=[]),
    )  # fmt: skip

    conn = read(output)
    assert conn.execute("SELECT count(*) FROM room").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM object").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM object_catalogue").fetchone()[0] == 0
    conn.close()


def test_a_baseline_needs_a_manifest_that_names_it(tmp_path, map_id, output):
    folder = write_map_folder(tmp_path / "with_manifest", baseline_rev="r1")
    map_id = map_id_of(folder)

    build(tmp_path, folder, output, anchors=anchors_document(map_id, baseline_rev="r1"))

    conn = read(output)
    assert conn.execute("SELECT baseline_rev FROM map").fetchone()[0] == "r1"
    assert conn.execute("SELECT count(*) FROM object WHERE in_baseline != 1").fetchone()[0] == 0
    conn.close()


# Refusals -------------------------------------------------------------------------


def _rooms(change):
    def apply(map_id):
        document = rooms_document(map_id)
        change(document)
        return {"rooms": document}

    return apply


def _anchors(change):
    def apply(map_id):
        document = anchors_document(map_id)
        change(document)
        return {"anchors": document}

    return apply


def _catalogue(change):
    def apply(map_id):
        document = catalogue_document()
        change(document)
        return {"catalogue": document}

    return apply


def _set_first(section, **changes):
    def change(document):
        document[section][0] = document[section][0] | changes

    return change


def _drop_first(section, key):
    def change(document):
        del document[section][0][key]

    return change


def _set_top(**changes):
    return lambda document: document.update(changes)


BAD_INPUTS = {
    "a nan stand pose": _rooms(_set_first("rooms", stand_pose=[math.nan, 1.0, 0.0])),
    "an infinite corner": _rooms(_set_first("rooms", polygon=[[0.0, 0.0], [math.inf, 0.0], [4.0, 4.0]])),
    "a stand pose as text": _rooms(_set_first("rooms", stand_pose=["1.0", 1.0, 0.0])),
    "a boolean coordinate": _rooms(_set_first("rooms", stand_pose=[True, 1.0, 0.0])),
    "a flat polygon": _rooms(_set_first("rooms", polygon=[[1.0, 1.0]] * 4)),
    "a line for a polygon": _rooms(_set_first("rooms", polygon=[[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]])),
    "two corners": _rooms(_set_first("rooms", polygon=[[0.0, 0.0], [1.0, 1.0]])),
    "a stand pose outside": _rooms(_set_first("rooms", stand_pose=[20.0, 20.0, 0.0])),
    "a repeated room key": _rooms(lambda d: d["rooms"].append(room("living_room", name="other"))),
    "a repeated room name": _rooms(lambda d: d["rooms"].append(room("spare_room", name="living room"))),
    "a blank room name": _rooms(_set_first("rooms", name="  ")),
    "a room key with a capital": _rooms(_set_first("rooms", key="Living")),
    "a room key with a space": _rooms(_set_first("rooms", key="living room")),
    "a room with no stand pose": _rooms(_drop_first("rooms", "stand_pose")),
    "a room with no description": _rooms(_drop_first("rooms", "description")),
    "a room with an unknown key": _rooms(_set_first("rooms", door="north")),
    "a fractional floor": _rooms(_set_first("rooms", floor=1.5)),
    "no rooms": _rooms(_set_top(rooms=[])),
    "another map in the rooms": _rooms(_set_top(map_id="other-2026-01-01-00000000")),
    "another schema in the rooms": _rooms(_set_top(schema_version=2)),
    "a boolean schema in the rooms": _rooms(_set_top(schema_version=True)),
    "an unknown key in the rooms": _rooms(_set_top(version=1)),
    "another map in the anchors": _anchors(_set_top(map_id="other-2026-01-01-00000000")),
    "another schema in the anchors": _anchors(_set_top(schema_version=0)),
    "a baseline with no manifest": _anchors(_set_top(baseline_rev="r1")),
    "a repeated anchor key": _anchors(lambda d: d["anchors"].append(anchor("table", surfaces=[]))),
    "a repeated surface key": _anchors(lambda d: d["anchors"].append(anchor("desk", surfaces=anchor()["surfaces"]))),
    "an unknown room": _anchors(_set_first("anchors", room_key="garage")),
    "a nan center": _anchors(_set_first("anchors", center=[math.nan, 0.0, 0.0])),
    "an infinite yaw": _anchors(_set_first("anchors", yaw=math.inf)),
    "a nan surface height": _anchors(_set_first("anchors", surfaces=[{"key": "top", "z": math.nan, "outline": []}])),
    "a flat surface outline": _anchors(_set_first("anchors", surfaces=[{"key": "top", "z": 1.0, "outline": [[1.0, 1.0]] * 3}])),
    "geometry with a zero size": _anchors(_set_first("anchors", size=[0.0, 1.0, 1.0])),
    "geometry with no size": _anchors(_drop_first("anchors", "size")),
    "no geometry with a stand pose as center": _anchors(_set_first("anchors", geometry_valid=False)),
    "geometry_valid as text": _anchors(_set_first("anchors", geometry_valid="yes")),
    "a fake vector": _anchors(_set_first("anchors", fingerprint=fingerprint(model="fake_clip"))),
    "a vector with no provenance": _anchors(
        _set_first("anchors", fingerprint={k: v for k, v in fingerprint().items() if k != "provenance"})
    ),
    "a vector with a bad hash": _anchors(
        _set_first("anchors", fingerprint=fingerprint(provenance={"weights_sha256": "abc", "crop_sha256": SHA}))
    ),
    "a vector of an unknown model": _anchors(_set_first("anchors", fingerprint=fingerprint(model="dinov2"))),
    "a vector shorter than it says": _anchors(_set_first("anchors", fingerprint=fingerprint(embedding=[1.0, 0.0]))),
    "a vector of the wrong size": _anchors(
        _set_first("anchors", fingerprint=fingerprint(dimension=2, embedding=[1.0, 0.0]))
    ),
    "a zero vector": _anchors(_set_first("anchors", fingerprint=fingerprint(embedding=[0.0] * 512))),
    "another schema in the catalogue": _catalogue(_set_top(schema_version=3)),
    "a zero weight": _catalogue(_set_first("objects", weight_g=0)),
    "a negative weight": _catalogue(_set_first("objects", weight_g=-1.0)),
    "a nan weight": _catalogue(_set_first("objects", weight_g=math.nan)),
    "a colour outside the vocabulary": _catalogue(_set_first("objects", attributes={"colour": "mauve"})),
    "an attribute outside the vocabulary": _catalogue(_set_first("objects", attributes={"smell": "nice"})),
    "a flag as text": _catalogue(_set_first("objects", attributes={"fragile": "yes"})),
    "a repeated official name": _catalogue(lambda d: d["objects"].append({"official_name": "blue cup"})),
    "an item with no name": _catalogue(_drop_first("objects", "official_name")),
    "a blank alias": _catalogue(_set_first("objects", aliases=["  "])),
}


def assert_wrote_nothing(tmp_path, output, code, capsys):
    assert code == 1
    assert "Refused, and wrote nothing" in capsys.readouterr().err
    assert not output.parent.exists()


@pytest.mark.parametrize("case", BAD_INPUTS)
def test_a_bad_input_writes_nothing(tmp_path, folder, map_id, output, case, capsys):
    argv = seed_argv(tmp_path, folder, output, **BAD_INPUTS[case](map_id))
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


def test_geometry_false_with_real_numbers_is_refused_with_a_reason(tmp_path, folder, map_id, output, capsys):
    document = anchors_document(map_id, anchors=[bare_anchor(center=[1.0, 1.0, 0.0])])
    assert main(seed_argv(tmp_path, folder, output, anchors=document)) == 1
    assert "gives center" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["rooms", "anchors", "catalogue"])
def test_a_repeated_yaml_key_writes_nothing(tmp_path, folder, output, name, capsys):
    argv = seed_argv(tmp_path, folder, output)
    path = argv[argv.index(f"--{name}") + 1]
    (tmp_path / "inputs" / f"{name}.yaml").write_text("schema_version: 1\nschema_version: 1\n")
    assert path.endswith(f"{name}.yaml")
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


@pytest.mark.parametrize("name", ["rooms", "anchors", "catalogue"])
def test_a_missing_input_writes_nothing(tmp_path, folder, output, name, capsys):
    argv = seed_argv(tmp_path, folder, output)
    argv[argv.index(f"--{name}") + 1] = str(tmp_path / "absent.yaml")
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


def test_plain_text_nan_in_yaml_is_refused(tmp_path, folder, map_id, output, capsys):
    # YAML 1.1 reads NaN and 1e3 as text, so they cannot slip through as numbers.
    argv = seed_argv(tmp_path, folder, output)
    rooms = tmp_path / "inputs" / "rooms.yaml"
    document = yaml.safe_load(rooms.read_text())
    document["rooms"][0]["stand_pose"] = ["NaN", "1e3", 0.0]
    rooms.write_text(yaml.safe_dump(document))
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


@pytest.mark.parametrize(
    "flag, value",
    [("--date", "2026-13-40"), ("--date", "yesterday"), ("--arena", "Not An Arena")],
)
def test_a_bad_identity_writes_nothing(tmp_path, folder, output, flag, value, capsys):
    argv = seed_argv(tmp_path, folder, output)
    argv[argv.index(flag) + 1] = value
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


def test_a_missing_map_folder_writes_nothing(tmp_path, folder, output, capsys):
    argv = seed_argv(tmp_path, folder, output)
    argv[argv.index("--map-folder") + 1] = str(tmp_path / "no_map")
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)
    assert not (tmp_path / "no_map").exists()


def test_a_manifest_with_another_hash_writes_nothing(tmp_path, folder, output, capsys):
    argv = seed_argv(tmp_path, folder, output)
    write_manifest(folder, "", map_hash="b" * 64)
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


def test_a_manifest_of_another_map_writes_nothing(tmp_path, folder, output, capsys):
    argv = seed_argv(tmp_path, folder, output)
    write_manifest(folder, "", map_id="other-2026-01-01-00000000")
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


def test_a_manifest_with_another_baseline_writes_nothing(tmp_path, folder, map_id, output, capsys):
    write_manifest(folder, "r2")
    argv = seed_argv(tmp_path, folder, output, anchors=anchors_document(map_id, baseline_rev="r1"))
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


@pytest.mark.parametrize(
    "flag",
    ["--map-folder", "--arena", "--date", "--rooms", "--anchors", "--catalogue", "--clock-kind", "--output"],
)
def test_every_path_and_identity_argument_is_required(tmp_path, folder, output, flag, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    argv = seed_argv(tmp_path, folder, output)
    at = argv.index(flag)
    del argv[at : at + 2]

    with pytest.raises(SystemExit) as stopped:
        main(argv)

    assert stopped.value.code == 2
    assert not output.parent.exists()
    assert not (tmp_path / "data").exists()


def test_the_tool_writes_only_the_path_it_is_given(tmp_path, folder, output, monkeypatch):
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    build(tmp_path, folder, output)
    assert list(work.iterdir()) == []
    assert files_in(output.parent) == ["seed.db", "seed.db.lock"]


# The same inputs give the same rows -----------------------------------------------


def test_the_same_inputs_give_the_same_rows(tmp_path, folder, output):
    second = tmp_path / "again" / "seed.db"
    build(tmp_path, folder, output)
    build(tmp_path, folder, second)

    first_conn, second_conn = read(output), read(second)
    assert dump_rows(first_conn) == dump_rows(second_conn)
    first_conn.close()
    second_conn.close()


def test_the_order_of_the_inputs_does_not_change_the_rows(tmp_path, folder, map_id, output):
    second = tmp_path / "again" / "seed.db"
    desk = anchor(
        "desk", surfaces=[{"key": "desk_top", "z": 0.7, "outline": []}, {"key": "desk_shelf", "z": 0.2, "outline": []}]
    )
    items = [{"official_name": "blue cup"}, {"official_name": "apple", "aliases": ["fruit"]}]
    build(tmp_path, folder, output,
          rooms=rooms_document(map_id, rooms=[room(), other_room()]),
          anchors=anchors_document(map_id, anchors=[anchor(), bare_anchor(), desk]),
          catalogue=catalogue_document(objects=items))  # fmt: skip
    build(tmp_path, folder, second,
          rooms=rooms_document(map_id, rooms=[other_room(), room()]),
          anchors=anchors_document(map_id, anchors=[
              desk | {"surfaces": list(reversed(desk["surfaces"]))}, bare_anchor(), anchor()
          ]),
          catalogue=catalogue_document(objects=list(reversed(items))))  # fmt: skip

    first_conn, second_conn = read(output), read(second)
    assert dump_rows(first_conn) == dump_rows(second_conn)
    first_conn.close()
    second_conn.close()


# Re-seeding -------------------------------------------------------------------------


def reseed(tmp_path, folder, map_id, previous, anchors, name="second.db"):
    output = tmp_path / "out" / name
    build(
        tmp_path, folder, output,
        anchors=anchors_document(map_id, anchors=anchors),
        extra=("--previous-seed", str(previous)),
    )  # fmt: skip
    return output


def test_a_reseed_keeps_the_ids_of_its_anchors(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor(), anchor("desk", surfaces=[])]))
    second = reseed(tmp_path, folder, map_id, output, [anchor("desk", surfaces=[]), anchor()])
    assert object_ids(second) == object_ids(output)


def test_a_new_key_gets_an_id_above_the_high_water_mark(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor()]))
    second = reseed(tmp_path, folder, map_id, output, [anchor(), anchor("desk", surfaces=[])])

    ids = object_ids(second)
    assert ids["desk"] > object_ids(output)["table"]
    assert counters(second)["object"] == ids["desk"]


def test_a_removed_anchor_becomes_a_tombstone_and_its_id_stays_taken(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor(), anchor("desk", surfaces=[])]))
    first = object_ids(output)
    second = reseed(tmp_path, folder, map_id, output, [anchor(), anchor("chair", surfaces=[])])

    conn = read(second)
    gone = conn.execute("SELECT * FROM object WHERE key = 'desk'").fetchone()
    assert (gone["id"], gone["state"], gone["geometry_valid"]) == (first["desk"], "discarded", 0)
    assert gone["p_exist"] == pytest.approx(0.05)
    conn.close()
    ids = object_ids(second)
    assert ids["chair"] not in first.values()
    assert ids["table"] == first["table"]


def test_a_returning_key_gets_its_old_id_back(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor(), anchor("desk", surfaces=[])]))
    first = object_ids(output)
    second = reseed(tmp_path, folder, map_id, output, [anchor()])
    third = reseed(tmp_path, folder, map_id, second, [anchor(), anchor("desk", surfaces=[])], name="third.db")

    conn = read(third)
    back = conn.execute("SELECT id, state FROM object WHERE key = 'desk'").fetchone()
    assert (back["id"], back["state"]) == (first["desk"], "present")
    conn.close()


def test_a_tombstone_survives_a_further_reseed(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor(), anchor("desk", surfaces=[])]))
    second = reseed(tmp_path, folder, map_id, output, [anchor()])
    third = reseed(tmp_path, folder, map_id, second, [anchor()], name="third.db")

    conn = read(third)
    assert conn.execute("SELECT state FROM object WHERE key = 'desk'").fetchone()[0] == "discarded"
    conn.close()


def test_a_tombstone_keeps_a_removed_rooms_anchor(tmp_path, folder, map_id, output):
    kitchen = anchor("fridge", room_key="kitchen_room", surfaces=[])
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor(), kitchen]))
    second = tmp_path / "out" / "second.db"
    build(
        tmp_path, folder, second,
        rooms=rooms_document(map_id, rooms=[room()]),
        anchors=anchors_document(map_id, anchors=[anchor()]),
        extra=("--previous-seed", str(output)),
    )  # fmt: skip

    conn = read(second)
    gone = conn.execute("SELECT state, room_id FROM object WHERE key = 'fridge'").fetchone()
    assert (gone["state"], gone["room_id"]) == ("discarded", None)
    conn.close()


def test_a_reseed_carries_the_person_and_session_counters(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output)
    conn = connect(output)
    with transaction(conn):
        conn.execute("UPDATE last_id SET id = 40 WHERE table_name = 'person'")
        conn.execute("UPDATE last_id SET id = 50 WHERE table_name = 'session'")
        conn.execute("UPDATE last_id SET id = 60 WHERE table_name = 'object'")
    conn.close()

    second = reseed(tmp_path, folder, map_id, output, [anchor(), anchor("desk", surfaces=[])])

    values = counters(second)
    assert (values["person"], values["session"]) == (40, 50)
    assert values["object"] == object_ids(second)["desk"] == 61


def test_a_reseed_can_read_a_previous_seed_with_a_wal(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor()]))
    conn = connect(output)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    with transaction(conn):
        conn.execute("UPDATE last_id SET id = 77 WHERE table_name = 'object'")
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    shutil.copy(output, crashed / "seed.db")
    shutil.copy(f"{output}-wal", crashed / "seed.db-wal")
    conn.close()

    second = reseed(tmp_path, folder, map_id, crashed / "seed.db", [anchor(), anchor("desk", surfaces=[])])

    assert object_ids(second)["desk"] == 78


def test_a_previous_seed_of_another_map_is_refused(tmp_path, folder, map_id, output, capsys):
    other = write_map_folder(tmp_path / "other_map", pixel=7)
    build(tmp_path, other, output, rooms=rooms_document(map_id_of(other)),
          anchors=anchors_document(map_id_of(other)))  # fmt: skip
    second = tmp_path / "second" / "seed.db"

    code = main(seed_argv(tmp_path, folder, second, extra=("--previous-seed", str(output))))

    assert code == 1
    assert "not of the map" in capsys.readouterr().err
    assert not second.parent.exists()


def test_a_missing_previous_seed_is_refused(tmp_path, folder, output, capsys):
    argv = seed_argv(tmp_path, folder, output, extra=("--previous-seed", str(tmp_path / "absent.db")))
    assert_wrote_nothing(tmp_path, output, main(argv), capsys)


def test_a_previous_seed_is_not_changed_by_a_reseed(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output)
    before = output.read_bytes()
    reseed(tmp_path, folder, map_id, output, [anchor()])
    assert output.read_bytes() == before


# An ID floor --------------------------------------------------------------------------


def test_an_id_floor_gives_new_anchors_ids_above_it(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor()]))
    second = tmp_path / "out" / "second.db"
    build(tmp_path, folder, second, anchors=anchors_document(map_id, anchors=[anchor(), anchor("desk", surfaces=[])]),
          extra=("--previous-seed", str(output), "--id-floor", "10"))  # fmt: skip

    assert object_ids(second) == {"table": object_ids(output)["table"], "desk": 11}
    assert counters(second)["object"] == 11


def test_an_id_floor_raises_the_object_counter_with_no_new_anchor(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor()]))
    second = tmp_path / "out" / "second.db"
    build(tmp_path, folder, second, anchors=anchors_document(map_id, anchors=[anchor()]),
          extra=("--previous-seed", str(output), "--id-floor", "10"))  # fmt: skip

    assert object_ids(second) == object_ids(output)
    assert counters(second)["object"] == 10


def test_an_id_floor_applies_to_a_first_seed(tmp_path, folder, output):
    build(tmp_path, folder, output, extra=("--id-floor", "5"))

    assert object_ids(output) == {"table": 6, "wardrobe": 7}
    assert counters(output)["object"] == 7


def test_an_id_floor_at_or_below_the_previous_counter_changes_nothing(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output)  # table=1, wardrobe=2
    anchors = anchors_document(map_id, anchors=[anchor(), bare_anchor(), anchor("desk", surfaces=[])])
    plain, floored = tmp_path / "out" / "plain.db", tmp_path / "out" / "floored.db"
    build(tmp_path, folder, plain, anchors=anchors, extra=("--previous-seed", str(output)))
    build(tmp_path, folder, floored, anchors=anchors, extra=("--previous-seed", str(output), "--id-floor", "2"))

    first, second = read(plain), read(floored)
    assert dump_rows(first) == dump_rows(second)
    first.close()
    second.close()
    assert object_ids(floored)["desk"] == 3


def test_an_id_floor_leaves_the_person_and_session_counters_alone(tmp_path, folder, output):
    build(tmp_path, folder, output, extra=("--id-floor", "5"))
    values = counters(output)
    assert (values["person"], values["session"]) == (0, 0)


@pytest.mark.parametrize("value", ["-1", "1.5", "ten", "", " 3", "+3", "1_000", str(2**63)])
def test_a_bad_id_floor_writes_nothing(tmp_path, folder, output, value, capsys):
    argv = seed_argv(tmp_path, folder, output, extra=("--id-floor", value))

    with pytest.raises(SystemExit) as stopped:
        main(argv)

    assert stopped.value.code == 2
    assert "--id-floor" in capsys.readouterr().err
    assert not output.parent.exists()


@pytest.mark.parametrize("value", [-1, True, 1.0, "3", 2**63])
def test_write_seed_refuses_an_id_floor_that_is_not_a_whole_number_in_range(tmp_path, folder, map_id, output, value):
    with pytest.raises(Refused, match="ID floor"):
        write_seed(
            folder=read_map_folder(folder), map_id=map_id, arena=ARENA, date=DATE, clock_kind="wall",
            rooms_document=rooms_document(map_id), anchors_document=anchors_document(map_id),
            catalogue_document=catalogue_document(), output=output, id_floor=value,
        )  # fmt: skip
    assert not output.parent.exists()


def test_an_id_floor_at_the_largest_id_with_a_new_anchor_writes_no_seed(tmp_path, folder, output, capsys):
    argv = seed_argv(tmp_path, folder, output, extra=("--id-floor", str(2**63 - 1)))

    assert main(argv) == 1

    err = capsys.readouterr().err
    assert "Refused, and wrote nothing" in err
    assert "Traceback" not in err
    assert files_in(output.parent) == ["seed.db.lock"]


# Replacing --output -----------------------------------------------------------------


def test_an_existing_output_stays_without_replace(tmp_path, folder, output, capsys):
    build(tmp_path, folder, output)
    before = output.read_bytes()

    assert main(seed_argv(tmp_path, folder, output)) == 1

    assert "--replace" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_replacing_a_seed_of_the_same_map_needs_a_previous_seed(tmp_path, folder, output, capsys):
    build(tmp_path, folder, output)
    before = output.read_bytes()

    assert main(seed_argv(tmp_path, folder, output, extra=("--replace",))) == 1

    assert "--previous-seed" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_a_seed_can_replace_itself_keeping_its_ids(tmp_path, folder, map_id, output):
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor()]))
    first = object_ids(output)

    build(
        tmp_path, folder, output,
        anchors=anchors_document(map_id, anchors=[anchor(), anchor("desk", surfaces=[])]),
        extra=("--previous-seed", str(output), "--replace"),
    )  # fmt: skip

    assert object_ids(output)["table"] == first["table"]
    assert object_ids(output)["desk"] > first["table"]
    assert sorted(p.name for p in output.parent.iterdir()) == ["seed.db", "seed.db.lock"]


def test_a_seed_of_another_map_can_be_replaced(tmp_path, folder, output):
    other = write_map_folder(tmp_path / "other_map", pixel=7)
    build(tmp_path, other, output, rooms=rooms_document(map_id_of(other)),
          anchors=anchors_document(map_id_of(other)))  # fmt: skip

    build(tmp_path, folder, output, extra=("--replace",))

    conn = read(output)
    assert conn.execute("SELECT id FROM map").fetchone()[0] == map_id_of(folder)
    conn.close()


def test_replace_will_not_destroy_a_store_with_people(tmp_path, folder, output, capsys):
    build(tmp_path, folder, output)
    conn = connect(output)
    with transaction(conn):
        conn.execute("INSERT INTO session (id, started_at) VALUES (1, 1.0)")
    conn.close()
    before = output.read_bytes()

    assert main(seed_argv(tmp_path, folder, output, extra=("--replace",))) == 1

    assert "not a seed" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_replace_will_not_destroy_a_store_with_movable_objects(tmp_path, folder, map_id, output, capsys):
    build(tmp_path, folder, output)
    conn = connect(output)
    reservation = reserve_object_ids(conn)
    with transaction(conn):
        insert_object(conn, NewObject(map_id, "cup", 1.0, 1.0), reservation.first, reservation=reservation)
    conn.close()

    assert main(seed_argv(tmp_path, folder, output, extra=("--replace",))) == 1

    assert "movable objects" in capsys.readouterr().err


def test_replace_will_not_destroy_a_people_only_store(tmp_path, folder, output, capsys):
    conn = connect(output)
    migrate(conn)
    conn.close()
    before = output.read_bytes()

    assert main(seed_argv(tmp_path, folder, output, extra=("--replace",))) == 1

    assert "no store_meta row" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_replace_will_not_destroy_something_else(tmp_path, folder, output, capsys):
    output.parent.mkdir(parents=True)
    output.write_bytes(b"my thesis" * 1000)
    before = output.read_bytes()

    assert main(seed_argv(tmp_path, folder, output, extra=("--replace",))) == 1

    assert "not a seed" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_a_held_lock_refuses_the_tool_and_leaves_no_output(tmp_path, folder, output, capsys):
    with StoreLock(output):
        assert main(seed_argv(tmp_path, folder, output)) == 1
    assert "in use" in capsys.readouterr().err
    assert not output.exists()
    assert lock_path(output).exists()


def test_a_failed_write_leaves_no_temporary_file(tmp_path, folder, output, monkeypatch, capsys):
    build(tmp_path, folder, output)
    before = output.read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    argv = seed_argv(tmp_path, folder, output, extra=("--replace", "--previous-seed", str(output)))
    assert main(argv) == 1

    assert "disk full" in capsys.readouterr().err
    assert output.read_bytes() == before
    assert sorted(p.name for p in output.parent.iterdir()) == ["seed.db", "seed.db.lock"]


# Fingerprints -----------------------------------------------------------------------


def test_a_real_crops_vector_is_stored_with_its_weight(tmp_path, folder, map_id, output):
    document = anchors_document(map_id, anchors=[anchor(fingerprint=fingerprint())])
    build(tmp_path, folder, output, anchors=document)

    conn = read(output)
    row = conn.execute("SELECT id, fingerprint_weight FROM object WHERE key = 'table'").fetchone()
    assert row["fingerprint_weight"] == 1.0
    stored = conn.execute("SELECT count(*) FROM object_clip_vit_b16 WHERE object_id = ?", (row["id"],)).fetchone()[0]
    assert stored == 1
    conn.close()


def test_a_real_vector_is_stored_as_a_unit_vector(tmp_path, folder, map_id, output):
    embedding = [3.0, 4.0] + [0.0] * 510
    document = anchors_document(map_id, anchors=[anchor(fingerprint=fingerprint(embedding=embedding))])
    build(tmp_path, folder, output, anchors=document)

    conn = read(output)
    raw = conn.execute("SELECT embedding FROM object_clip_vit_b16").fetchone()[0]
    values = np.frombuffer(raw, dtype=np.float32)
    assert values[:2] == pytest.approx([0.6, 0.8])
    conn.close()


def test_no_seed_tool_imports_a_model_or_the_sample_house():
    source = Path(walkie_database.__file__).parent
    for name in ("seed_store.py", "seed_sim.py", "restore.py", "map_config.py", "store_lock.py"):
        tree = ast.parse((source / name).read_text())
        imported = set()
        for statement in ast.walk(tree):
            if isinstance(statement, ast.Import):
                imported.update(alias.name for alias in statement.names)
            elif isinstance(statement, ast.ImportFrom):
                imported.add(statement.module)
        assert not imported & {"torch", "transformers", "open_clip", "walkie_database.seed"}, name


def test_a_directory_as_output_is_refused(tmp_path, folder, output, capsys):
    output.mkdir(parents=True)
    assert main(seed_argv(tmp_path, folder, output, extra=("--replace",))) == 1
    assert "not a file" in capsys.readouterr().err
    assert output.is_dir()


def test_a_seed_from_an_older_schema_can_be_replaced_and_read_for_its_ids(tmp_path, folder, map_id, output):
    """A seed from before migration 6 lacks some of the run tables and room keys."""
    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor()]))
    first = object_ids(output)
    conn = connect(output)
    conn.execute("DROP TABLE write_request")
    conn.execute("PRAGMA user_version = 5")
    conn.close()

    build(tmp_path, folder, output, anchors=anchors_document(map_id, anchors=[anchor()]),
          extra=("--replace", "--previous-seed", str(output)))  # fmt: skip

    assert object_ids(output) == first
    conn = read(output)
    assert schema_version(conn) == load_migrations()[-1].version
    conn.close()


def test_write_yaml_keeps_a_yaml_nan_as_a_float(tmp_path):
    # The cases above rely on this: safe_dump writes .nan, and it reads back as a float.
    path = write_yaml(tmp_path / "x.yaml", {"value": math.nan})
    assert math.isnan(yaml.safe_load(path.read_text())["value"])
