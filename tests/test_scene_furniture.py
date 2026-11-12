"""Seed anchors measured from a MuJoCo scene: boxes, surfaces, rooms and refusals."""

import xml.etree.ElementTree as ET

import pytest

from walkie_database.people import Refused
from walkie_database.scene_furniture import furniture_anchors, read_scene

SCENE = """\
<mujoco>
  <default>
    <default class="static">
      <geom contype="1" conaffinity="1"/>
    </default>
    <default class="visual">
      <geom contype="0" conaffinity="0"/>
      <default class="glass">
        <geom rgba="1 1 1 0.3"/>
      </default>
    </default>
  </default>
  <worldbody>
    <geom name="floor" type="plane" size="5 5 0.1"/>
    <body name="w_sofa" pos="2 2 0" quat="1 0 0 0" childclass="static">
      <geom name="base" type="box" pos="0 0 0.175" size="0.3 0.9 0.175"/>
      <geom name="cushion_a" type="box" pos="0 -0.45 0.4" size="0.25 0.2 0.05"/>
      <geom name="cushion_b" type="box" pos="0 0 0.4" size="0.25 0.22 0.05"/>
      <geom name="cushion_c" type="box" pos="0 0.45 0.4" size="0.25 0.2 0.05"/>
      <geom name="back" type="box" pos="0.25 0 0.6" size="0.05 0.9 0.25"/>
      <geom name="cover" type="box" pos="0 0 1.5" size="0.5 0.5 0.01" class="visual"/>
      <geom name="pane" type="box" pos="0 0 2.5" size="0.5 0.5 0.01" class="glass"/>
    </body>
    <body name="w_shelf" pos="6 2 0" childclass="static">
      <geom name="post" type="box" pos="0 0 0.5" size="0.02 0.02 0.5"/>
      <geom name="board_1" type="box" pos="0 0 0.2" size="0.4 0.2 0.01"/>
      <geom name="board_2" type="box" pos="0 0 0.6" size="0.4 0.2 0.01"/>
      <geom name="board_3" type="box" pos="0 0 1.0" size="0.4 0.2 0.01"/>
    </body>
    <body name="w_stool" pos="7 1 0" childclass="static">
      <geom name="seat" type="cylinder" pos="0 0 0.5" size="0.15 0.02"/>
      <geom name="leg" type="cylinder" pos="0 0 0.24" size="0.02 0.24"/>
    </body>
  </worldbody>
</mujoco>
"""

ROOMS = [
    {"key": "living_room", "polygon": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]},
    {"key": "kitchen_room", "polygon": [[5.0, 0.0], [9.0, 0.0], [9.0, 4.0], [5.0, 4.0]]},
]

FURNITURE = [
    {"body": "w_sofa", "key": "sofa", "label": "sofa"},
    {"body": "w_shelf", "key": "shelf", "label": "shelf"},
    {"body": "w_stool", "key": "stool", "label": "chair"},
]


def measure(scene=SCENE, furniture=None, rooms=ROOMS, version=1):
    document = {"schema_version": version, "furniture": FURNITURE if furniture is None else furniture}
    return furniture_anchors("m", ET.fromstring(scene), document, rooms)


def by_key(document):
    return {a["key"]: a for a in document["anchors"]}


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_the_document_has_the_anchors_shape():
    document = measure()
    assert (document["schema_version"], document["map_id"], document["baseline_rev"]) == (1, "m", "")
    assert [a["key"] for a in document["anchors"]] == ["sofa", "shelf", "stool"]
    sofa = by_key(document)["sofa"]
    assert (sofa["label"], sofa["geometry_valid"], sofa["enclosed"], sofa["yaw"]) == ("sofa", True, False, 0.0)


def test_an_official_name_is_passed_on_only_when_given():
    furniture = [FURNITURE[0] | {"official_name": "corner sofa"}, *FURNITURE[1:]]
    anchors = by_key(measure(furniture=furniture))
    assert (anchors["sofa"]["label"], anchors["sofa"]["official_name"]) == ("sofa", "corner sofa")
    assert "official_name" not in anchors["shelf"]


def test_the_box_bounds_the_collision_geoms_and_skips_the_visual_ones():
    sofa = by_key(measure())["sofa"]
    assert sofa["center"] == pytest.approx([2.0, 2.0, 0.425])
    assert sofa["size"] == pytest.approx([0.6, 1.8, 0.85])


def test_boards_at_one_height_a_little_apart_are_one_surface():
    (seat,) = by_key(measure())["sofa"]["surfaces"]
    assert seat["key"] == "sofa_top"
    assert seat["z"] == pytest.approx(0.45)
    assert seat["outline"] == square(1.75, 1.35, 2.25, 2.65)


def test_the_highest_board_is_the_top_and_the_lower_ones_count_up_from_the_floor():
    surfaces = by_key(measure())["shelf"]["surfaces"]
    assert [(s["key"], s["z"]) for s in surfaces] == [
        ("shelf_level_1", pytest.approx(0.21)),
        ("shelf_level_2", pytest.approx(0.61)),
        ("shelf_top", pytest.approx(1.01)),
    ]
    assert all(s["outline"] == square(5.6, 1.8, 6.4, 2.2) for s in surfaces)


def test_a_cylinder_seat_is_the_square_around_it():
    stool = by_key(measure())["stool"]
    (seat,) = stool["surfaces"]
    assert (seat["key"], seat["z"]) == ("stool_top", pytest.approx(0.52))
    assert seat["outline"] == square(6.85, 0.85, 7.15, 1.15)
    assert stool["size"] == pytest.approx([0.3, 0.3, 0.52])


def test_each_anchor_is_in_the_room_that_holds_its_centre():
    rooms = {key: a["room_key"] for key, a in by_key(measure()).items()}
    assert rooms == {"sofa": "living_room", "shelf": "kitchen_room", "stool": "kitchen_room"}


def test_a_board_too_thick_or_too_small_is_no_surface():
    scene = SCENE.replace('name="seat" type="cylinder" pos="0 0 0.5" size="0.15 0.02"',
                          'name="seat" type="cylinder" pos="0 0 0.5" size="0.15 0.07"')
    scene = scene.replace('name="board_1" type="box" pos="0 0 0.2" size="0.4 0.2 0.01"',
                          'name="board_1" type="box" pos="0 0 0.2" size="0.05 0.2 0.01"')
    anchors = by_key(measure(scene))
    assert anchors["stool"]["surfaces"] == []
    assert [s["key"] for s in anchors["shelf"]["surfaces"]] == ["shelf_level_1", "shelf_top"]
    assert anchors["shelf"]["surfaces"][0]["z"] == pytest.approx(0.61)


def test_a_class_given_only_by_the_default_main_applies():
    scene = SCENE.replace("<default>\n    <default", '<default>\n    <geom contype="0" conaffinity="0"/>\n    <default', 1)
    scene = scene.replace('<body name="w_stool" pos="7 1 0" childclass="static">', '<body name="w_stool" pos="7 1 0">')
    with pytest.raises(Refused, match="w_stool has no box or cylinder collision geom"):
        measure(scene)


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('name="w_sofa" pos="2 2 0" quat="1 0 0 0"', 'name="w_sofa" pos="2 2 0" quat="0.92 0 0 0.38"', "rotated"),
        ('name="w_shelf" pos="6 2 0"', 'name="w_shelf" pos="6 2 0" euler="0 0 30"', "sets euler"),
        ('name="seat" type="cylinder"', 'name="seat" axisangle="0 0 1 30" type="cylinder"', "seat.*sets axisangle"),
        ('name="seat" type="cylinder"', 'name="seat" quat="0.92 0 0 0.38" type="cylinder"', "seat.*rotated"),
        ('name="leg" type="cylinder" pos="0 0 0.24" size="0.02 0.24"', 'name="leg" type="capsule" fromto="0 0 0 0 0 0.48" size="0.02"', "fromto"),
        ('name="leg" type="cylinder"', 'name="leg" type="mesh"', "leg.*mesh; only box and cylinder"),
        ('name="leg" type="cylinder" pos="0 0 0.24" size="0.02 0.24"', 'name="leg" type="sphere" size="0.02"', "sphere"),
        ('<geom name="seat"', '<joint name="spin"/><geom name="seat"', "holds a joint"),
        ('<geom name="seat"', '<body name="inner"/><geom name="seat"', "holds a body"),
        ('<geom name="seat"', '<frame/><geom name="seat"', "holds a frame"),
        ('name="post"', 'name="post" class="missing"', "class 'missing'"),
        ('size="0.15 0.02"', 'size="0.15"', "size of the geom 'seat' of w_stool has 1 numbers"),
        ('name="w_stool" pos="7 1 0"', 'name="w_stool" pos="7 x 0"', "not a list of numbers"),
        ('name="w_stool" pos="7 1 0"', 'name="w_stool" pos="20 1 0"', "stool at \\(20.0, 1.0\\) is in no room"),
    ],
)  # fmt: skip
def test_what_cannot_be_read_safely_is_refused(old, new, message):
    assert old in SCENE
    with pytest.raises(Refused, match=message):
        measure(SCENE.replace(old, new, 1))


def test_separate_boards_at_one_height_are_refused():
    scene = SCENE.replace(
        '<geom name="board_3" type="box" pos="0 0 1.0" size="0.4 0.2 0.01"/>',
        '<geom name="board_3" type="box" pos="-0.25 0 1.0" size="0.15 0.2 0.01"/>'
        '<geom name="board_4" type="box" pos="0.25 0 1.0" size="0.15 0.2 0.01"/>',
    )
    with pytest.raises(Refused, match="shelf has 2 separate boards at 1.01 m"):
        measure(scene)


def test_boards_that_touch_in_an_l_are_refused_not_filled_in():
    # Their bounds would claim the empty corner (6.2, 2.2) as surface.
    scene = SCENE.replace(
        '<geom name="board_3" type="box" pos="0 0 1.0" size="0.4 0.2 0.01"/>',
        '<geom name="board_3" type="box" pos="0 -0.1 1.0" size="0.4 0.1 0.01"/>'
        '<geom name="board_4" type="box" pos="-0.3 0.2 1.0" size="0.1 0.2 0.01"/>',
    )
    with pytest.raises(Refused, match="shelf has 2 separate boards at 1.01 m"):
        measure(scene)


def test_a_board_inside_another_at_one_height_adds_nothing():
    scene = SCENE.replace(
        '<geom name="board_3" type="box" pos="0 0 1.0" size="0.4 0.2 0.01"/>',
        '<geom name="board_3" type="box" pos="0 0 1.0" size="0.4 0.2 0.01"/>'
        '<geom name="board_4" type="box" pos="0.1 0 1.0" size="0.15 0.15 0.01"/>',
    )
    top = by_key(measure(scene))["shelf"]["surfaces"][-1]
    assert (top["key"], top["outline"]) == ("shelf_top", square(5.6, 1.8, 6.4, 2.2))


@pytest.mark.parametrize("shift, joined", [(0.004, True), (0.03, False)])
def test_boards_join_only_when_their_edges_line_up(shift, joined):
    scene = SCENE.replace('name="cushion_b" type="box" pos="0 0 0.4"', f'name="cushion_b" type="box" pos="{shift} 0 0.4"')
    if joined:
        (seat,) = by_key(measure(scene))["sofa"]["surfaces"]
        assert seat["outline"] == square(1.75, 1.35, round(2.25 + shift, 4), 2.65)
    else:
        with pytest.raises(Refused, match="sofa has 3 separate boards at 0.45 m"):
            measure(scene)


def test_a_centre_in_two_rooms_is_refused():
    rooms = [*ROOMS, {"key": "hall", "polygon": [[0.0, 0.0], [9.0, 0.0], [9.0, 4.0], [0.0, 4.0]]}]
    with pytest.raises(Refused, match="sofa .* is in living_room and hall"):
        measure(rooms=rooms)


def test_a_body_inside_another_body_is_not_furniture():
    scene = SCENE.replace('<geom name="seat"', '<body name="nested"><geom type="box" size="1 1 1"/></body><geom name="seat"')
    with pytest.raises(Refused, match="worldbody has no body 'nested'"):
        measure(scene, furniture=[{"body": "nested", "key": "nested", "label": "box"}])


@pytest.mark.parametrize(
    "furniture, version, message",
    [
        ([{"body": "w_none", "key": "none", "label": "x"}], 1, "no body 'w_none'"),
        ([{"body": "w_sofa", "key": "Sofa", "label": "sofa"}], 1, "key 'Sofa'"),
        ([{"body": "w_sofa", "key": "sofa"}], 1, "missing label"),
        ([{"body": "w_sofa", "key": "sofa", "label": " "}], 1, "not blank"),
        ([{"body": "w_sofa", "key": "sofa", "label": "sofa", "yaw": 1.0}], 1, "unknown key yaw"),
        ([{"body": "w_sofa", "key": "sofa", "label": "sofa", "official_name": " "}], 1, "not blank"),
        ([{"body": "w_sofa", "key": "sofa", "label": "sofa", "official_name": None}], 1, "not blank"),
        ("w_sofa", 1, "must be a list"),
        (FURNITURE, 2, "schema_version 2"),
        (FURNITURE, True, "schema_version True"),
    ],
)  # fmt: skip
def test_a_bad_furniture_file_is_refused(furniture, version, message):
    with pytest.raises(Refused, match=message):
        measure(furniture=furniture, version=version)


def test_a_scene_file_that_cannot_be_read_is_refused(tmp_path):
    with pytest.raises(Refused, match="cannot be read"):
        read_scene(tmp_path / "missing.xml")
    (tmp_path / "bad.xml").write_text("<mujoco><worldbody>")
    with pytest.raises(Refused, match="malformed"):
        read_scene(tmp_path / "bad.xml")
