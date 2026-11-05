"""RememberEvent and FindEvents: the pose, retries, privacy and the search."""

import pytest
from query_helpers import KITCHEN, MAP_ID, NOW, Store, at, nowhere

from walkie_database import people
from walkie_database.db import connect, transaction
from walkie_database.events import REMEMBER_EVENT, NewEvent
from walkie_database.migrate import migrate
from walkie_database.object_context import WriteConflict, record_write
from walkie_database.people import Refused


@pytest.fixture
def store(db):
    return Store(db)


@pytest.fixture
def rooms(store):
    return {"living": store.room("living room"), "kitchen": store.room("kitchen", KITCHEN)}


def event(request_id="r1", text="Put the cup on the table.", **fields) -> NewEvent:
    return NewEvent(**({"request_id": request_id, "task_id": "t1", "cause": "a test", "kind": "action",
                        "text": text} | fields))


class Locator:
    """A locate function that counts its calls."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def __call__(self, frame, now):
        self.calls.append((frame, now))
        return self.inner(frame, now)


def rows(store, table: str) -> list:
    return store.conn.execute(f"SELECT * FROM {table}").fetchall()


def face(conn) -> tuple[str, list[float]]:
    model, length = next(iter(people.vector_tables(conn, people.FACE).lengths.items()))
    return model, [1.0] + [0.0] * (length - 1)


def person(conn, now: float = NOW) -> people.Remembered:
    return people.remember_person(conn, "", [], [face(conn)], [], now)


# The pose --------------------------------------------------------------------


def test_an_event_records_the_robot_pose_and_its_room(store, rooms):
    locate = Locator(at(1.0, 2.0, yaw=0.5))

    recorded = store.repository(locate=locate, robot_frame="base_link").remember_event(event(), now=NOW)

    assert locate.calls == [("base_link", NOW)]
    assert recorded.pose == pytest.approx((1.0, 2.0, 0.5))
    assert recorded.room_id == rooms["living"]
    assert recorded.ts == NOW
    assert (recorded.task_id, recorded.cause, recorded.kind) == ("t1", "a test", "action")


def test_with_no_transform_the_event_is_still_recorded_with_no_pose(store, rooms):
    recorded = store.repository(locate=nowhere).remember_event(event(), now=NOW)

    assert recorded.pose is None
    assert recorded.room_id == 0
    [row] = rows(store, "event")
    assert (row["x"], row["y"], row["yaw"], row["room_id"]) == (None, None, None, None)


def test_a_pose_outside_every_room_has_no_room(store, rooms):
    recorded = store.repository(locate=at(20.0, 20.0)).remember_event(event(), now=NOW)
    assert recorded.pose == pytest.approx((20.0, 20.0, 0.0))
    assert recorded.room_id == 0


# Retries ---------------------------------------------------------------------


def test_a_retry_gets_the_first_answer_and_writes_nothing(store, rooms):
    locate = Locator(at(1.0, 1.0))
    repo = store.repository(locate=locate)
    first = repo.remember_event(event(), now=NOW)

    again = repo.remember_event(event(), now=NOW + 30)

    assert again == first
    assert len(rows(store, "event")) == 1
    assert len(rows(store, "write_request")) == 1
    # TF is looked up only for a new line.
    assert len(locate.calls) == 1


def test_a_request_id_used_again_with_other_fields_is_a_conflict(store):
    repo = store.repository(locate=nowhere)
    repo.remember_event(event(), now=NOW)

    with pytest.raises(WriteConflict):
        repo.remember_event(event(text="Something else."), now=NOW)
    with pytest.raises(WriteConflict):
        repo.remember_event(event(kind="note"), now=NOW)
    assert len(rows(store, "event")) == 1


def test_a_request_id_of_another_operation_is_a_conflict(store):
    with transaction(store.conn):
        record_write(store.conn, "r1", "t1", "set_object_state", {"x": 1}, {"ok": True}, NOW)

    with pytest.raises(WriteConflict, match="set_object_state"):
        store.repository(locate=nowhere).remember_event(event(), now=NOW)
    assert rows(store, "event") == []


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "gossip"},
        {"text": "   "},
        {"text": "x" * 513},
        {"text": "a\x00b"},
        {"request_id": ""},
        {"request_id": "r" * 129},
        {"task_id": " "},
        {"cause": ""},
        {"object_id": -1},
        {"person_id": -2},
        {"session_id": -3},
    ],
    ids=["kind", "blank_text", "long_text", "nul", "no_request_id", "long_request_id", "no_task_id",
         "no_cause", "negative_object", "negative_person", "negative_session"],
)
def test_a_refused_line_leaves_no_event_and_no_ledger_row(store, fields):
    with pytest.raises(Refused):
        store.repository(locate=nowhere).remember_event(event(**fields), now=NOW)
    assert rows(store, "event") == []
    assert rows(store, "write_request") == []


def test_a_refused_line_can_be_sent_again_once_fixed(store):
    repo = store.repository(locate=nowhere)
    with pytest.raises(Refused):
        repo.remember_event(event(kind="gossip"), now=NOW)
    assert repo.remember_event(event(), now=NOW).text == "Put the cup on the table."


# What a line names -----------------------------------------------------------


def test_a_merged_object_is_recorded_as_its_winner(store):
    winner, loser = store.add("cup"), store.add("cup")
    store.update(loser, state="merged", merged_into=winner)

    recorded = store.repository(locate=nowhere).remember_event(event(object_id=loser), now=NOW)

    assert recorded.object_id == winner


def test_an_unknown_or_foreign_object_is_refused(store):
    elsewhere = store.other_map().add("cup")
    repo = store.repository(locate=nowhere)

    for object_id, code in ((99_999, "NOT_FOUND"), (elsewhere, "WRONG_MAP")):
        with pytest.raises(Refused) as refused:
            repo.remember_event(event(request_id=f"r{object_id}", object_id=object_id), now=NOW)
        assert refused.value.code == code
    assert rows(store, "write_request") == []


def test_a_person_with_no_session_named_gets_the_open_one(store):
    met = person(store.conn)

    recorded = store.repository(locate=nowhere).remember_event(event(person_id=met.person_id), now=NOW)

    assert (recorded.person_id, recorded.session_id) == (met.person_id, met.session_id)


def test_a_forgotten_person_or_session_is_not_found_and_a_kept_person_is_not(store):
    met = person(store.conn)
    later = people.start_session(store.conn, "", False, NOW)  # forgets met and met's session
    kept = person(store.conn)
    with transaction(store.conn):
        store.conn.execute("UPDATE person SET session_id = NULL WHERE id = ?", (kept.person_id,))

    repo = store.repository(locate=nowhere)
    with pytest.raises(Refused) as unknown:
        repo.remember_event(event(request_id="a", person_id=met.person_id), now=NOW)
    assert unknown.value.code == "NOT_FOUND"
    with pytest.raises(Refused) as gone:
        repo.remember_event(event(request_id="b", session_id=met.session_id), now=NOW)
    assert gone.value.code == "NOT_FOUND"

    # A person kept across sessions may be named in any of them.
    recorded = repo.remember_event(event(request_id="c", person_id=kept.person_id), now=NOW)
    assert (recorded.person_id, recorded.session_id) == (kept.person_id, later.id)


def test_a_person_named_with_the_wrong_session_is_refused(store):
    first = person(store.conn)
    with transaction(store.conn):
        # A closed session, since only one may be open.
        other_session = store.conn.execute(
            "INSERT INTO session (label, started_at, ended_at) VALUES ('other', ?, ?)", (NOW - 60, NOW - 30)
        ).lastrowid

    with pytest.raises(Refused, match="belongs to session"):
        store.repository(locate=nowhere).remember_event(
            event(person_id=first.person_id, session_id=other_session), now=NOW
        )
    assert rows(store, "event") == []


# Privacy ---------------------------------------------------------------------


def test_forgetting_a_session_removes_its_lines_and_a_retry_gets_no_text(store):
    met = person(store.conn)
    repo = store.repository(locate=nowhere)
    first = repo.remember_event(event(text="The guest likes tea.", person_id=met.person_id), now=NOW)

    people.end_session(store.conn, met.session_id)

    assert rows(store, "event") == []
    assert repo.find_events(text="tea", since=None, room="", max_results=20) == ([], False)
    again = repo.remember_event(event(text="The guest likes tea.", person_id=met.person_id), now=NOW + 5)
    assert (again.id, again.ts, again.text) == (first.id, first.ts, "")
    assert (again.person_id, again.session_id) == (0, 0)
    assert rows(store, "event") == []


def test_a_retry_of_a_forgotten_line_never_shows_a_newer_line_with_its_id(store):
    met = person(store.conn)
    repo = store.repository(locate=nowhere)
    forgotten = repo.remember_event(event(text="The guest likes tea.", person_id=met.person_id), now=NOW)
    people.end_session(store.conn, met.session_id)
    newer = repo.remember_event(event(request_id="r2", task_id="t2", text="Opened the door."), now=NOW + 1)
    assert newer.id == forgotten.id + 1  # the forgotten line's id is not given again

    again = repo.remember_event(event(text="The guest likes tea.", person_id=met.person_id), now=NOW + 2)

    assert again.text == ""
    assert again.ts == forgotten.ts


def test_a_retry_of_a_forgotten_line_never_shows_another_line_of_its_time_and_task(store):
    # A paused sim clock gives every line of a task the same time, so neither tells
    # two lines apart.
    met = person(store.conn)
    repo = store.repository(locate=nowhere)
    forgotten = repo.remember_event(event(text="The guest chose tea.", person_id=met.person_id), now=NOW)
    people.end_session(store.conn, met.session_id)
    newer = repo.remember_event(event(request_id="r2", text="Opened a door."), now=NOW)

    again = repo.remember_event(event(text="The guest chose tea.", person_id=met.person_id), now=NOW)

    assert again.text == ""
    assert (again.id, again.ts, again.task_id) == (forgotten.id, forgotten.ts, forgotten.task_id)
    assert (again.person_id, again.session_id) == (0, 0)
    assert newer.id == forgotten.id + 1
    assert [row["text"] for row in rows(store, "event")] == ["Opened a door."]


def test_a_retry_after_a_restart_never_shows_a_newer_line_with_the_forgotten_id(tmp_path):
    # The counter is in the file, so a restart does not let the id be given again.
    path = tmp_path / "walkie.db"
    conn = connect(path)
    migrate(conn)
    store = Store(conn)
    met = person(conn)
    forgotten = store.repository(locate=nowhere).remember_event(
        event(text="The guest chose tea.", person_id=met.person_id), now=NOW
    )
    people.end_session(conn, met.session_id)
    conn.close()

    conn = connect(path)
    migrate(conn)
    repo = Store.existing(conn, MAP_ID).repository(locate=nowhere)
    newer = repo.remember_event(event(request_id="r2", text="Opened a door."), now=NOW)
    again = repo.remember_event(event(text="The guest chose tea.", person_id=met.person_id), now=NOW)
    conn.close()

    assert again.text == ""
    assert again.id == forgotten.id
    assert newer.id == forgotten.id + 1


def test_the_ledger_never_holds_the_text(store):
    store.repository(locate=nowhere).remember_event(event(text="The guest likes tea."), now=NOW)
    [ledger] = rows(store, "write_request")
    assert ledger["operation"] == REMEMBER_EVENT
    assert "tea" not in ledger["outcome_json"]


# FindEvents ------------------------------------------------------------------


@pytest.fixture
def log(store, rooms):
    repo = store.repository(locate=at(1.0, 1.0))
    kitchen = store.repository(locate=at(6.0, 1.0))
    lines = {
        "door": repo.remember_event(event("a", "Opened the front door."), now=NOW),
        "cup": repo.remember_event(event("b", "Put the blue cup on the table."), now=NOW + 10),
        "cups": kitchen.remember_event(event("c", "Washed two cups in the sink."), now=NOW + 20),
        "same": repo.remember_event(event("d", "Wiped the table."), now=NOW + 20),
    }
    return lines


def find(store, *, text="", since=None, room="", max_results=20):
    return store.repository().find_events(text=text, since=since, room=room, max_results=max_results)


def test_events_come_newest_first_with_the_id_breaking_ties(store, log):
    found, truncated = find(store)
    assert [e.id for e in found] == [log["same"].id, log["cups"].id, log["cup"].id, log["door"].id]
    assert not truncated


def test_every_word_must_match_in_any_word_form(store, log):
    assert {e.id for e in find(store, text="cup")[0]} == {log["cup"].id, log["cups"].id}
    assert [e.id for e in find(store, text="blue cup")[0]] == [log["cup"].id]
    assert find(store, text="blue sink")[0] == []


def test_match_syntax_in_the_text_is_only_words(store, log):
    assert find(store, text='door" OR *')[0] == []
    assert [e.id for e in find(store, text="front NEAR door")[0]] == []


def test_since_and_room_narrow_the_search(store, log, rooms):
    assert [e.id for e in find(store, since=NOW + 10)[0]] == [log["same"].id, log["cups"].id, log["cup"].id]
    assert [e.id for e in find(store, room="kitchen")[0]] == [log["cups"].id]
    assert all(e.room_id == rooms["living"] for e in find(store, room="living room")[0])


def test_an_unknown_room_is_not_found(store, log):
    with pytest.raises(Refused) as refused:
        find(store, room="attic")
    assert refused.value.code == "NOT_FOUND"


def test_the_limit_says_when_it_cut(store, log):
    found, truncated = find(store, max_results=2)
    assert len(found) == 2
    assert truncated
    for limit in (0, 101):
        with pytest.raises(Refused):
            find(store, max_results=limit)


def test_the_search_text_is_capped(store, log):
    with pytest.raises(Refused) as refused:
        find(store, text="a" * 513)
    assert refused.value.code == "INVALID"


def test_another_maps_lines_never_come_back(store, log):
    other = store.other_map()
    other.repository(locate=nowhere).remember_event(event("z", "Opened the front door."), now=NOW)
    assert [e.map_id for e in find(store, text="door")[0]] == [store.map_id]
