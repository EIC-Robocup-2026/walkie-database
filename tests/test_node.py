import math

import pytest
from rclpy.parameter import Parameter
from walkie_database_msgs.srv import FindObjects

from walkie_database import seed
from walkie_database.db import connect
from walkie_database.migrate import load_migrations, schema_version
from walkie_database.node import DatabaseNode


@pytest.fixture
def node(ros, tmp_path):
    node = DatabaseNode(
        parameter_overrides=[
            Parameter("db_path", value=str(tmp_path / "walkie.db")),
            Parameter("map_id", value=seed.MAP_ID),
        ]
    )
    yield node
    node.destroy_node()


def test_node_creates_and_migrates_its_database(ros, tmp_path):
    path = tmp_path / "walkie.db"
    node = DatabaseNode(parameter_overrides=[Parameter("db_path", value=str(path))])
    try:
        assert path.exists()
        assert schema_version(connect(path, readonly=True)) == len(load_migrations())
    finally:
        node.destroy_node()


def test_node_serves_find_objects_under_the_name_callers_use(node):
    # walkie-agent-ros calls this exact name and type.
    services = {(s.service_name, s.srv_type) for s in node.services}

    assert ("/walkie_database/find_objects", FindObjects) in services


def find(
    node: DatabaseNode, label: str, max_results: int = 0, map_id: str = ""
) -> FindObjects.Response:
    return node.find_objects(
        FindObjects.Request(map_id=map_id, label=label, max_results=max_results),
        FindObjects.Response(),
    )


def add_object(
    node: DatabaseNode,
    label: str,
    *,
    map_id: str = seed.MAP_ID,
    x: float = 1.0,
    n_detections: int = 1,
    last_seen: float = 0.0,
) -> None:
    node.conn.execute(
        """INSERT INTO object (map_id, label, center_x, center_y, center_z,
                               size_x, size_y, size_z, n_detections,
                               first_seen, last_seen)
           VALUES (:map_id, :label, :x, 9, 1, 0.1, 0.1, 0.1, :n_detections,
                   0, :last_seen)""",
        {
            "map_id": map_id,
            "label": label,
            "x": x,
            "n_detections": n_detections,
            "last_seen": last_seen,
        },
    )


def add_old_map_cup(node: DatabaseNode) -> None:
    """A cup in a second map, as an earlier visit to another venue leaves."""
    node.conn.execute(
        "INSERT INTO map (id, created_at) VALUES ('old-venue', 0)"
    )
    add_object(node, "cup", map_id="old-venue", x=9.0, last_seen=2e9)


def test_find_objects_searches_only_the_nodes_map(node):
    seed.seed(node.conn)
    add_old_map_cup(node)

    response = find(node, "cup")

    assert len(response.objects) == 2
    assert {o.map_id for o in response.objects} == {seed.MAP_ID}


def test_find_objects_searches_the_map_the_request_names(node):
    seed.seed(node.conn)
    add_old_map_cup(node)

    response = find(node, "cup", map_id="old-venue")

    assert [(o.map_id, o.position.x) for o in response.objects] == [("old-venue", 9.0)]


def test_find_objects_follows_a_map_id_set_while_the_node_runs(node):
    seed.seed(node.conn)
    add_old_map_cup(node)

    [result] = node.set_parameters([Parameter("map_id", value="old-venue")])
    response = find(node, "cup")

    assert result.successful
    assert [o.map_id for o in response.objects] == ["old-venue"]


def test_find_objects_fails_without_a_map(ros, tmp_path):
    node = DatabaseNode(
        parameter_overrides=[Parameter("db_path", value=str(tmp_path / "walkie.db"))]
    )
    try:
        response = find(node, "cup")
    finally:
        node.destroy_node()

    assert not response.success
    assert "map_id" in response.message


def test_find_objects_fails_for_a_map_the_database_does_not_have(node):
    seed.seed(node.conn)

    from_request = find(node, "cup", map_id="no-such-map")
    node.set_parameters([Parameter("map_id", value="arena-typo")])
    from_parameter = find(node, "cup")

    assert not from_request.success
    assert "'no-such-map', from the request" in from_request.message
    assert not from_parameter.success
    assert "'arena-typo', from the node's map_id parameter" in from_parameter.message


def test_find_objects_returns_matches_newest_first(node):
    seed.seed(node.conn, now=1_000_000.0)

    response = find(node, "CUP")

    assert response.success
    assert response.message == "Found 2 objects."
    assert find(node, "remote").message == "Found 1 object."
    assert [o.caption for o in response.objects] == [
        "a red mug with a handle on the counter",
        "a white ceramic cup on the table",
    ]
    mug = response.objects[0]
    assert (mug.label, mug.room, mug.map_id, mug.frame_id) == (
        "cup", "kitchen", seed.MAP_ID, "map"
    )
    assert (mug.position.x, mug.position.y, mug.position.z) == (3.6, 0.4, 0.95)
    # The seed turns the mug 0.6 rad about the vertical axis.
    assert (mug.orientation.x, mug.orientation.y) == (0.0, 0.0)
    assert (mug.orientation.z, mug.orientation.w) == pytest.approx(
        (math.sin(0.3), math.cos(0.3))
    )
    assert (mug.size.x, mug.size.y, mug.size.z) == (0.11, 0.08, 0.1)
    assert mug.n_detections == 5
    assert mug.last_seen.sec > response.objects[1].last_seen.sec


def test_find_objects_with_an_empty_label_returns_every_object_up_to_the_limit(node):
    seed.seed(node.conn)

    assert len(find(node, "").objects) == 10
    assert len(find(node, "", max_results=3).objects) == 3


def test_find_objects_returns_at_most_100_objects_and_says_when_it_left_some_out(node):
    seed.seed(node.conn)
    for i in range(150):
        add_object(node, "box", last_seen=float(i))

    capped = find(node, "box", max_results=1000)
    exact = find(node, "cup", max_results=2)

    assert len(capped.objects) == 100
    assert capped.message == "Found 100 objects (more not shown)."
    assert capped.objects[0].last_seen.sec == 149
    assert exact.message == "Found 2 objects."


def test_find_objects_finds_nothing_for_an_unknown_label(node):
    seed.seed(node.conn)

    response = find(node, "unicorn")

    assert response.success
    assert list(response.objects) == []


def test_find_objects_converts_last_seen_to_a_valid_time(node):
    seed.seed(node.conn)
    add_object(node, "stamp", last_seen=1.9999999999)
    add_object(node, "stamp", last_seen=-0.5)

    response = find(node, "stamp")

    assert [(o.last_seen.sec, o.last_seen.nanosec) for o in response.objects] == [
        (1, 999_999_999),
        (-1, 500_000_000),
    ]


@pytest.mark.parametrize(
    "column, value",
    [
        ("last_seen", 3e9),  # after 2038, past Time.sec's int32
        ("last_seen", 1.7e12),  # milliseconds instead of seconds
        ("last_seen", math.inf),
        ("n_detections", -1),
    ],
)
def test_find_objects_fails_for_a_value_its_message_cannot_hold(node, column, value):
    # Sent as is, such a value fails only when rclpy serializes the response,
    # and that stops the node.
    seed.seed(node.conn)
    add_object(node, "clock", **{column: value})

    response = find(node, "clock")

    assert not response.success
    assert column in response.message


def test_find_objects_reports_a_database_error(node):
    node.conn.close()

    response = find(node, "cup")

    assert not response.success
    assert response.message.startswith("The database could not search its objects")
