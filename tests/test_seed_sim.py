"""The sim adapter: rooms from the world, names from a map, stand poses from locations."""

import json
import re
import tomllib

import pytest
from seed_helpers import (
    ARENA,
    DATE,
    anchor,
    anchors_document,
    catalogue_document,
    dump_rows,
    map_id_of,
    room,
    rooms_document,
    seed_argv,
    write_map_folder,
    write_yaml,
)

from walkie_database import seed_sim, seed_store
from walkie_database.db import connect
from walkie_database.people import Refused

WORLD = """\
[rooms]
living_room = { pose = [99.0, 99.0, 0.0], polygon = [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]] }
kitchen_room = { pose = [99.0, 99.0, 0.0], polygon = [[5.0, 0.0], [9.0, 0.0], [9.0, 4.0], [5.0, 4.0]] }

[locations]
shelf = { room = "kitchen_room", pose = [6.0, 1.0, 0.0], polygon = [[6.0, 1.0], [6.0, 1.0], [6.0, 1.0], [6.0, 1.0]] }
"""

LOCATIONS = """\
[locations."living room"]
x = 1.0
y = 1.0
yaw = 0.5
description = "A sofa and a low table."

[locations."kitchen room"]
x = 6.0
y = 1.0
yaw = 1.5

[locations.front_door]
x = 50.0
y = 50.0
yaw = 0.0
"""

# As the agent's blank-map config/locations.sim.toml: its names are not room names.
BLANK_MAP_LOCATIONS = """\
[locations.living_room]
x = 2.0
y = 0.0
yaw = 0.0
description = "A blank-map demo place."

[locations.kitchen]
x = 4.0
y = 0.0
yaw = 0.0
"""

ROOM_NAMES = {
    "schema_version": 1,
    "rooms": {"living_room": "living room", "kitchen_room": "kitchen room"},
}


@pytest.fixture
def folder(tmp_path):
    return write_map_folder(tmp_path / "map")


@pytest.fixture
def output(tmp_path):
    return tmp_path / "out" / "seed.db"


def sim_argv(tmp_path, folder, output, *, world=WORLD, locations=LOCATIONS, names=ROOM_NAMES, extra=()):
    inputs = tmp_path / "sim_inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "world.toml").write_text(world)
    (inputs / "locations.toml").write_text(locations)
    return [
        "--map-folder", str(folder),
        "--arena", ARENA,
        "--date", DATE,
        "--clock-kind", "ros_sim",
        "--output", str(output),
        "--world-source", str(inputs / "world.toml"),
        "--locations", str(inputs / "locations.toml"),
        "--room-names", str(write_yaml(inputs / "names.yaml", names)),
        *extra,
    ]  # fmt: skip


def rooms_printed(capsys):
    text = capsys.readouterr().out
    return json.loads(text[: text.rindex("}") + 1])


def test_the_rooms_come_from_the_world_the_names_and_the_locations(tmp_path, folder, output, capsys):
    assert seed_sim.main(sim_argv(tmp_path, folder, output)) == 0

    conn = connect(output, readonly=True)
    rooms = {r["key"]: r for r in conn.execute("SELECT * FROM room")}
    assert set(rooms) == {"living_room", "kitchen_room"}
    living = rooms["living_room"]
    assert (living["name"], living["description"]) == ("living room", "A sofa and a low table.")
    assert (living["x"], living["y"], living["yaw"]) == (1.0, 1.0, 0.5)
    assert rooms["kitchen_room"]["description"] == ""
    conn.close()


def test_a_sim_seed_has_a_ros_sim_clock_and_no_anchors_by_default(tmp_path, folder, output):
    assert seed_sim.main(sim_argv(tmp_path, folder, output)) == 0

    conn = connect(output, readonly=True)
    assert conn.execute("SELECT clock_kind FROM map").fetchone()[0] == "ros_sim"
    assert conn.execute("SELECT count(*) FROM object").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM object_catalogue").fetchone()[0] == 0
    conn.close()


def test_the_worlds_flat_furniture_polygons_become_nothing(tmp_path, folder, output):
    assert seed_sim.main(sim_argv(tmp_path, folder, output)) == 0

    conn = connect(output, readonly=True)
    assert conn.execute("SELECT count(*) FROM object").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM room WHERE key = 'shelf'").fetchone()[0] == 0
    conn.close()


def test_the_normalized_rooms_are_printed_before_the_seed_is_written(tmp_path, folder, output, capsys):
    assert seed_sim.main(sim_argv(tmp_path, folder, output)) == 0

    printed = rooms_printed(capsys)
    assert printed["schema_version"] == 1
    assert printed["map_id"] == map_id_of(folder)
    assert {r["key"] for r in printed["rooms"]} == {"living_room", "kitchen_room"}


def test_the_printed_rooms_build_the_same_seed_through_seed_store(tmp_path, folder, output, capsys):
    assert seed_sim.main(sim_argv(tmp_path, folder, output)) == 0
    printed = rooms_printed(capsys)
    other = tmp_path / "other" / "seed.db"

    map_id = map_id_of(folder)
    argv = seed_argv(
        tmp_path, folder, other, rooms=printed, clock_kind="ros_sim",
        anchors=anchors_document(map_id, anchors=[]), catalogue=catalogue_document(objects=[]),
    )  # fmt: skip
    assert seed_store.main(argv) == 0

    first, second = connect(output, readonly=True), connect(other, readonly=True)
    assert dump_rows(first) == dump_rows(second)
    first.close()
    second.close()


def test_optional_anchors_and_catalogue_are_used(tmp_path, folder, output):
    map_id = map_id_of(folder)
    anchors = write_yaml(tmp_path / "a.yaml", anchors_document(map_id, anchors=[anchor()]))
    catalogue = write_yaml(tmp_path / "c.yaml", catalogue_document())
    argv = sim_argv(tmp_path, folder, output, extra=("--anchors", str(anchors), "--catalogue", str(catalogue)))

    assert seed_sim.main(argv) == 0

    conn = connect(output, readonly=True)
    assert conn.execute("SELECT key FROM object").fetchone()[0] == "table"
    assert conn.execute("SELECT official_name FROM object_catalogue").fetchone()[0] == "blue cup"
    conn.close()


def test_the_id_floor_reaches_the_seed(tmp_path, folder, output):
    anchors = write_yaml(tmp_path / "a.yaml", anchors_document(map_id_of(folder), anchors=[anchor()]))
    argv = sim_argv(tmp_path, folder, output, extra=("--anchors", str(anchors), "--id-floor", "7"))

    assert seed_sim.main(argv) == 0

    conn = connect(output, readonly=True)
    assert tuple(conn.execute("SELECT key, id FROM object").fetchone()) == ("table", 8)
    assert conn.execute("SELECT id FROM last_id WHERE table_name = 'object'").fetchone()[0] == 8
    conn.close()


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"names": {"schema_version": 1, "rooms": {"living_room": "living room"}}}, "does not name"),
        ({"names": {"schema_version": 1, "rooms": {**ROOM_NAMES["rooms"], "garage": "garage"}}}, "garage"),
        ({"names": {"schema_version": 2, "rooms": ROOM_NAMES["rooms"]}}, "schema_version"),
        ({"names": {"schema_version": 1, "rooms": ["living_room"]}}, "must map"),
        ({"names": {"schema_version": 1, "rooms": {**ROOM_NAMES["rooms"], "living_room": "Living Room"}}}, "named exactly"),
        ({"locations": BLANK_MAP_LOCATIONS}, "named exactly"),
        ({"locations": LOCATIONS.replace("y = 1.0\nyaw = 0.5", "yaw = 0.5")}, "no y"),
        ({"locations": LOCATIONS.replace("x = 1.0", "x = 90.0")}, "outside"),
        ({"locations": "[other]\n"}, "no \\[locations\\]"),
        ({"locations": "not = toml = at all"}, "malformed"),
        ({"world": "[locations]\n"}, "no \\[rooms\\]"),
        ({"world": "[rooms]\nliving_room = { pose = [1.0, 1.0, 0.0] }\nkitchen_room = { pose = [1.0, 1.0, 0.0] }\n"}, "no polygon"),
        ({"world": WORLD.replace("[4.0, 0.0], [4.0, 4.0], [0.0, 4.0]", "[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]")}, "polygon|outline"),
    ],
)  # fmt: skip
def test_a_bad_input_writes_nothing(tmp_path, folder, output, changes, message, capsys):
    assert seed_sim.main(sim_argv(tmp_path, folder, output, **changes)) == 1

    error = capsys.readouterr().err
    assert "Refused, and wrote nothing" in error
    assert re.search(message, error), error
    assert not output.parent.exists()


@pytest.mark.parametrize(
    "flag",
    ["--map-folder", "--arena", "--date", "--clock-kind", "--output", "--world-source", "--locations", "--room-names"],
)
def test_every_path_and_identity_argument_is_required(tmp_path, folder, output, flag, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv = sim_argv(tmp_path, folder, output)
    at = argv.index(flag)
    del argv[at : at + 2]

    with pytest.raises(SystemExit) as stopped:
        seed_sim.main(argv)

    assert stopped.value.code == 2
    assert not output.parent.exists()
    assert not (tmp_path / "data").exists()


def test_normalizing_takes_no_coordinate_from_the_worlds_pose():
    rooms = seed_sim.normalize_rooms("m", tomllib.loads(WORLD), tomllib.loads(LOCATIONS), ROOM_NAMES)["rooms"]
    assert [r["stand_pose"] for r in rooms] == [[1.0, 1.0, 0.5], [6.0, 1.0, 1.5]]


def test_normalizing_gives_the_rooms_documents_shape(folder):
    map_id = map_id_of(folder)
    normal = seed_sim.normalize_rooms(map_id, tomllib.loads(WORLD), tomllib.loads(LOCATIONS), ROOM_NAMES)
    assert set(normal) == set(rooms_document(map_id))
    assert set(normal["rooms"][0]) == set(room())


def test_normalizing_refuses_a_non_mapping_room_names_file():
    with pytest.raises(Refused, match="must be a mapping"):
        seed_sim.normalize_rooms("m", {}, {}, ["living_room"])
