import math
import time

import numpy as np
import pytest
from rclpy.parameter import Parameter
from walkie_database_msgs.msg import Fact, ModelVector
from walkie_database_msgs.srv import (
    EndSession,
    FindObjects,
    MatchPeople,
    RememberPerson,
    StartSession,
)

from walkie_database import seed
from walkie_database.db import connect
from walkie_database.migrate import load_migrations, schema_version
from walkie_database.node import DatabaseNode

FACE_MODEL = "arcface_w600k_r50"
APPEARANCE_MODEL = "osnet_x1_0_msmt17"


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


# People and sessions --------------------------------------------------------


def vector(seed_value: int) -> np.ndarray:
    return np.random.default_rng(seed_value).standard_normal(512).astype(np.float32)


def model_vector(model: str, values: np.ndarray) -> ModelVector:
    return ModelVector(model=model, embedding=values.tolist())


def start(node: DatabaseNode, resume: bool = False, label: str = "") -> StartSession.Response:
    return node.start_session(
        StartSession.Request(label=label, resume=resume), StartSession.Response()
    )


def end(node: DatabaseNode, session_id: int = 0) -> EndSession.Response:
    return node.end_session(EndSession.Request(session_id=session_id), EndSession.Response())


def remember(
    node: DatabaseNode,
    name: str = "Sam",
    facts: dict[str, str] | None = None,
    faces: list[ModelVector] | None = None,
    appearances: list[ModelVector] | None = None,
) -> RememberPerson.Response:
    return node.remember_person(
        RememberPerson.Request(
            name=name,
            facts=[Fact(key=k, value=v) for k, v in (facts or {}).items()],
            faces=faces or [],
            appearances=appearances or [],
        ),
        RememberPerson.Response(),
    )


def match(
    node: DatabaseNode, kind: str, queries: list[ModelVector], max_results: int = 0
) -> MatchPeople.Response:
    handler = node.match_faces if kind == "face" else node.match_appearances
    return handler(
        MatchPeople.Request(queries=queries, max_results=max_results), MatchPeople.Response()
    )


def test_node_serves_the_people_services_under_the_names_callers_use(node):
    # walkie-agent-ros calls these exact names and types.
    services = {(s.service_name, s.srv_type) for s in node.services}

    assert {
        ("/walkie_database/start_session", StartSession),
        ("/walkie_database/end_session", EndSession),
        ("/walkie_database/remember_person", RememberPerson),
        ("/walkie_database/match_faces", MatchPeople),
        ("/walkie_database/match_appearances", MatchPeople),
    } <= services


def test_a_remembered_person_is_matched_by_face_and_by_appearance(node):
    session = start(node, label="walkie-agent")
    face, clothes = vector(1), vector(2)
    remembered = remember(
        node,
        name="Sam",
        facts={"drink": "coke", "seat": "sofa"},
        faces=[model_vector(FACE_MODEL, face), model_vector(FACE_MODEL, vector(3))],
        appearances=[model_vector(APPEARANCE_MODEL, clothes)],
    )

    by_face = match(node, "face", [model_vector(FACE_MODEL, 20 * face)])
    by_clothes = match(node, "appearance", [model_vector(APPEARANCE_MODEL, clothes)])

    assert remembered.success, remembered.message
    assert remembered.session_id == session.session_id
    for response in (by_face, by_clothes):
        assert response.success, response.message
        assert response.message == "Matched 1 query."
        [result] = response.results
        sam = result.people[0]
        assert (sam.person_id, sam.session_id, sam.name) == (
            remembered.person_id, session.session_id, "Sam"
        )
        assert [(f.key, f.value) for f in sam.facts] == [("drink", "coke"), ("seat", "sofa")]
        assert sam.distance == pytest.approx(0.0, abs=1e-6)


def test_people_services_say_what_they_did(node):
    face = [model_vector(FACE_MODEL, vector(1))]
    clothes = [model_vector(APPEARANCE_MODEL, vector(2))]

    assert start(node).message == "Started session 1."
    assert remember(node, faces=face * 2, appearances=clothes).message == (
        "Remembered Sam as person 1 in session 1, from 2 faces and 1 appearance."
    )
    assert start(node).message == "Forgot session 1 and its 1 person, and started session 2."
    assert end(node).message == "Forgot session 2, which had no people."
    assert end(node).message == "No session is open, so nothing was forgotten."
    assert end(node, 9).message == "Session 9 is not in the database, so nothing was forgotten."
    assert match(node, "face", face).message == (
        "Matched 1 query. No session is open, so only people kept across sessions were searched."
    )
    assert match(node, "appearance", clothes).message == (
        "Matched 1 query. No session is open, so no appearance was searched."
    )
    assert remember(node, name="", faces=face).message == (
        "Remembered a person with no name as person 2 in session 3, from 1 face and no "
        "appearances. Session 3 was started for them, because no session was open."
    )
    assert start(node, resume=True).message == "Resumed session 3, with 1 person."
    assert end(node).message == "Forgot session 3 and its 1 person."
    assert start(node, resume=True).message == "No session was open, so started session 4."


def test_match_max_results_defaults_to_10_and_caps_at_100(node):
    start(node)
    for i in range(120):
        assert remember(node, faces=[model_vector(FACE_MODEL, vector(i))]).success
    query = [model_vector(FACE_MODEL, vector(0))]

    default = match(node, "face", query)
    capped = match(node, "face", query, max_results=1000)
    small = match(node, "face", query, max_results=3)

    assert len(default.results[0].people) == 10
    assert default.message == "Matched 1 query (more not shown)."
    assert len(capped.results[0].people) == 100
    assert capped.message == "Matched 1 query (more not shown)."
    assert len(small.results[0].people) == 3
    # The query is person 1's own face.
    assert capped.results[0].people[0].person_id == 1


def test_start_session_returns_a_valid_started_at_and_the_models_it_takes(node):
    before = time.time()
    response = start(node)
    after = time.time()

    started_at = response.started_at.sec + response.started_at.nanosec * 1e-9
    assert before - 1e-6 <= started_at <= after + 1e-6
    assert [(m.model, m.length) for m in response.face_models] == [(FACE_MODEL, 512)]
    assert [(m.model, m.length) for m in response.appearance_models] == [(APPEARANCE_MODEL, 512)]
    # A resumed session reports the same start.
    assert start(node, resume=True).started_at == response.started_at


def test_a_session_and_its_guests_survive_a_node_restart(ros, tmp_path):
    path = str(tmp_path / "walkie.db")
    face = vector(7)
    first = DatabaseNode(parameter_overrides=[Parameter("db_path", value=path)])
    try:
        session = start(first)
        guest = remember(first, faces=[model_vector(FACE_MODEL, face)])
    finally:
        first.destroy_node()

    second = DatabaseNode(parameter_overrides=[Parameter("db_path", value=path)])
    try:
        resumed = start(second, resume=True)
        found = match(second, "face", [model_vector(FACE_MODEL, face)])
    finally:
        second.destroy_node()

    assert resumed.resumed
    assert (resumed.session_id, resumed.people) == (session.session_id, 1)
    assert found.results[0].people[0].person_id == guest.person_id


def query(kind: str) -> list[ModelVector]:
    model = FACE_MODEL if kind == "face" else APPEARANCE_MODEL
    return [model_vector(model, vector(0))]


@pytest.mark.parametrize(
    "call, verb",
    [
        (lambda node: start(node), "start a session"),
        (lambda node: end(node), "forget the session"),
        (lambda node: remember(node, faces=query("face")), "remember the person"),
        (lambda node: match(node, "face", query("face")), "match faces"),
        (lambda node: match(node, "appearance", query("appearance")), "match appearances"),
    ],
)
def test_people_services_report_a_database_error(node, call, verb):
    node.conn.close()

    response = call(node)

    assert not response.success
    assert response.message.startswith(f"The database could not {verb}: ")


def test_match_with_no_queries_succeeds_without_reading_the_database(node):
    node.conn.close()

    response = match(node, "face", [])

    assert response.success
    assert response.message == "No queries, so nothing was matched."
    assert list(response.results) == []


def test_refusals_do_not_say_the_database_failed(node):
    unknown = [model_vector("fake_face", vector(0))]

    remembered = remember(node, faces=unknown)
    matched = match(node, "face", unknown)

    for response in (remembered, matched):
        assert not response.success
        assert "fake_face" in response.message
        assert not response.message.startswith("The database could not")
    assert (remembered.person_id, remembered.session_id) == (0, 0)
    assert list(matched.results) == []
