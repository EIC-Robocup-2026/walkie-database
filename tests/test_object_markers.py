"""The marker array that RViz and Foxglove draw the object memory from."""

import numpy as np
import pytest
from builtin_interfaces.msg import Time
from visualization_msgs.msg import Marker

from walkie_database.object_markers import marker_array
from walkie_database.object_memory import ANCHOR, MOVABLE, Box, Track


def stored(object_id, label, layer, box, surface_z=None):
    return Track(key=object_id, layer=layer, points=np.zeros((0, 3)), labels={label: 3},
                 first_seen=0.0, last_seen=1.0, n_detections=3, object_id=object_id,
                 box=box, surface_z=surface_z)


def test_the_on_line_ends_on_the_anchors_surface_not_its_box_top():
    # The table's box reaches the top of the bottle on it, at 0.97 m.
    table = stored(1, "table", ANCHOR, Box((2.0, 0.0, 0.485), (1.2, 0.8, 0.97)), surface_z=0.75)
    cup = stored(2, "cup", MOVABLE, Box((2.1, 0.1, 0.80), (0.08, 0.08, 0.1)))
    markers = marker_array([table, cup], {2: 1}, frame_id="map", stamp=Time(sec=1)).markers

    [line] = [m for m in markers if m.ns == "relations" and m.type == Marker.LINE_LIST]
    start, end = line.points
    assert (start.x, start.y, start.z) == pytest.approx((2.1, 0.1, 0.75))
    assert (end.x, end.y, end.z) == pytest.approx((2.0, 0.0, 0.75))
    [word] = [m for m in markers if m.ns == "relations" and m.type == Marker.TEXT_VIEW_FACING]
    assert word.text == "on" and word.id == 2


def test_an_anchor_without_a_measured_surface_takes_its_box_top():
    seeded = stored(1, "table", ANCHOR, Box((2.0, 0.0, 0.37), (1.2, 0.8, 0.74)))
    cup = stored(2, "cup", MOVABLE, Box((2.1, 0.1, 0.79), (0.08, 0.08, 0.1)))
    markers = marker_array([seeded, cup], {2: 1}, frame_id="map", stamp=Time(sec=1)).markers

    [line] = [m for m in markers if m.ns == "relations" and m.type == Marker.LINE_LIST]
    assert line.points[1].z == pytest.approx(0.74)


def test_a_relation_to_an_object_not_shown_draws_nothing():
    cup = stored(2, "cup", MOVABLE, Box((2.1, 0.1, 0.79), (0.08, 0.08, 0.1)))
    markers = marker_array([cup], {2: 1}, frame_id="map", stamp=Time(sec=1)).markers
    assert [m for m in markers if m.ns == "relations"] == []
