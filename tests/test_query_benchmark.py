"""Queries over 500 synthetic objects: complete answers, and their timing as a diagnostic.

Every answer is checked against a plain Python filter over the same rows. The timings
are printed, never asserted: this machine's numbers are not the robot's loaded-node
latency. Run with `pytest -s tests/test_query_benchmark.py` to see them.
"""

import math
import random
import statistics
import time

import numpy as np
import pytest
from query_helpers import KITCHEN, LIVING, MODEL, NOW, Store, at, random_unit

from walkie_database.db import connect
from walkie_database.migrate import migrate
from walkie_database.object_context import ATTRIBUTE_VALUES
from walkie_database.queries import ObjectFilter

COUNT = 500
REPEATS = 30
LABELS = ("cup", "bottle", "book", "box", "apple")
ROBOT = (7.0, 2.0, 0.5)


@pytest.fixture(scope="module")
def arena(tmp_path_factory):
    """A file store with 500 objects in two rooms, and what each object was given."""
    path = tmp_path_factory.mktemp("benchmark") / "walkie.db"
    conn = connect(path)
    migrate(conn)
    store = Store(conn, ids=COUNT + 10)
    rooms = {"living room": store.room("living room", LIVING), "kitchen": store.room("kitchen", KITCHEN)}
    rng = random.Random(20261105)
    vectors = np.random.default_rng(20261105)
    made = []
    for _ in range(COUNT):
        room = rng.choice(sorted(rooms))
        x0 = 0.0 if room == "living room" else 5.0
        center = (x0 + rng.uniform(0.2, 3.8), rng.uniform(0.2, 3.8), rng.uniform(0.0, 1.0))
        attributes = {}
        if rng.random() < 0.8:
            attributes["colour"] = rng.choice(ATTRIBUTE_VALUES["colour"])
        if rng.random() < 0.5:
            attributes["material"] = rng.choice(ATTRIBUTE_VALUES["material"])
        label = rng.choice(LABELS)
        fields = dict(
            center=center,
            size=tuple(rng.uniform(0.03, 0.4) for _ in range(3)),
            state=rng.choices(("present", "held", "discarded"), weights=(85, 5, 10))[0],
            last_verified=NOW - rng.uniform(0.0, 7200.0),
            attributes=attributes,
            vector=random_unit(vectors),
        )
        object_id = store.add(label, room=rooms[room], **fields)
        made.append({"id": object_id, "label": label, "room": room, **fields})
    yield store, made
    conn.close()


def live(made):
    return [row for row in made if row["state"] in ("present", "held")]


def clean_for(row, key: str, value: str) -> bool:
    """Not a mismatch: the attribute matches or is unknown."""
    return row["attributes"].get(key, value) == value


def timed(call) -> tuple[object, float, float]:
    """The answer, and the p50 and p95 of REPEATS calls in milliseconds."""
    times = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        answer = call()
        times.append((time.perf_counter() - start) * 1000.0)
    times.sort()
    return answer, statistics.median(times), times[math.ceil(0.95 * len(times)) - 1]


def report(name: str, p50: float, p95: float, returned: int) -> None:
    print(f"\n{name}: p50 {p50:.1f} ms, p95 {p95:.1f} ms, {returned} returned, {COUNT} rows")


def test_a_filtered_vector_search_returns_every_passing_row(arena):
    store, made = arena
    repo = store.repository(locate=at(*ROBOT))
    query_vector = made[0]["vector"]
    query = ObjectFilter(room="kitchen", attributes=(("colour", "red"),))

    found, p50, p95 = timed(
        lambda: repo.search(query, vector=(MODEL, query_vector), text="", max_results=100, now=NOW)
    )
    report("vector search, room and colour", p50, p95, len(found.matches))

    expected = {row["id"] for row in live(made) if row["room"] == "kitchen" and clean_for(row, "colour", "red")}
    assert 0 < len(expected) <= 100
    assert {match.record.id for match in found.matches} == expected
    assert not found.truncated
    order = [(-match.score, match.record.id) for match in found.matches]
    assert order == sorted(order)


def test_a_cut_keeps_the_best_rows_of_the_whole_filtered_set(arena):
    store, made = arena
    repo = store.repository(locate=at(*ROBOT))
    vector = (MODEL, made[1]["vector"])
    query = ObjectFilter(room="living room")

    everything = repo.search(query, vector=vector, text="", max_results=100, now=NOW)
    top = repo.search(query, vector=vector, text="", max_results=20, now=NOW)

    assert everything.truncated
    assert top.truncated
    assert [m.record.id for m in top.matches] == [m.record.id for m in everything.matches[:20]]


def test_a_lexical_search_returns_every_passing_row(arena):
    store, made = arena
    repo = store.repository(locate=at(*ROBOT))
    query = ObjectFilter(thing="cup", attributes=(("material", "glass"),))

    found, p50, p95 = timed(lambda: repo.search(query, vector=None, text="", max_results=100, now=NOW))
    report("lexical search, thing and material", p50, p95, len(found.matches))

    cups = [row for row in live(made) if row["label"] == "cup"]
    expected = {row["id"] for row in cups if clean_for(row, "material", "glass")}
    assert 0 < len(expected) <= 100
    assert {match.record.id for match in found.matches} == expected


def test_near_the_robot_returns_every_row_in_range(arena):
    store, made = arena
    repo = store.repository(locate=at(*ROBOT))
    query = ObjectFilter(relation="near", near_distance_m=1.0)

    found, p50, p95 = timed(lambda: repo.find_near(query, max_results=100, now=NOW))
    report("near the robot, 1 m", p50, p95, len(found.matches))

    expected = {row["id"] for row in live(made) if math.dist(row["center"], ROBOT) <= 1.0}
    assert 0 < len(expected) <= 100
    assert {match.record.id for match in found.matches} == expected


def test_nearest_ranks_the_whole_map(arena):
    store, made = arena
    repo = store.repository(locate=at(*ROBOT))
    query = ObjectFilter(superlative="nearest")

    found, p50, p95 = timed(lambda: repo.search(query, vector=None, text="", max_results=10, now=NOW))
    report("nearest over the map", p50, p95, len(found.matches))

    by_distance = sorted(live(made), key=lambda row: (math.dist(row["center"], ROBOT), row["id"]))
    assert [match.record.id for match in found.matches] == [row["id"] for row in by_distance[:10]]
    assert found.truncated


def test_a_room_lists_every_row_in_it(arena):
    store, made = arena
    repo = store.repository(locate=at(*ROBOT))
    query = ObjectFilter(room="kitchen", thing="book")

    (room, found), p50, p95 = timed(lambda: repo.room_contents(query, max_results=100, now=NOW))
    report("room contents, kitchen books", p50, p95, len(found.matches))

    expected = {row["id"] for row in live(made) if row["room"] == "kitchen" and row["label"] == "book"}
    assert room.name == "kitchen"
    assert {match.record.id for match in found.matches} == expected
