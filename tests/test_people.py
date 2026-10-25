"""Sessions and people, through walkie_database.people on a plain connection."""

import json
import math
import sqlite3

import numpy as np
import pytest

from walkie_database import people, seed
from walkie_database.db import transaction
from walkie_database.people import APPEARANCE, FACE, Refused

FACE_MODEL = "arcface_w600k_r50"
APPEARANCE_MODEL = "osnet_x1_0_msmt17"
FACE_TABLE = f"face_{FACE_MODEL}"
APPEARANCE_TABLE = f"appearance_{APPEARANCE_MODEL}"
NOW = 1_000_000.0
PEOPLE_TABLES = ("session", "person", "face", "appearance", FACE_TABLE, APPEARANCE_TABLE)


def vector(seed_value: int) -> np.ndarray:
    return np.random.default_rng(seed_value).standard_normal(512).astype(np.float32)


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.float64)
    return v / np.linalg.norm(v)


def faces(*vectors: np.ndarray) -> list[tuple[str, np.ndarray]]:
    return [(FACE_MODEL, v) for v in vectors]


def appearances(*vectors: np.ndarray) -> list[tuple[str, np.ndarray]]:
    return [(APPEARANCE_MODEL, v) for v in vectors]


def remember(db, name="Sam", facts=(), face=(), appearance=(), now=NOW) -> people.Remembered:
    return people.remember_person(db, name, list(facts), list(face), list(appearance), now)


def start(db, resume=False, label="", now=NOW) -> people.StartedSession:
    return people.start_session(db, label, resume, now)


def count(db, table: str) -> int:
    return db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


def counts(db) -> dict[str, int]:
    return {table: count(db, table) for table in PEOPLE_TABLES}


def add_kept_person(db, name: str, face: np.ndarray) -> int:
    """A person kept across sessions, such as a team member, with one face."""
    with transaction(db):
        person_id = db.execute(
            "INSERT INTO person (name, first_seen, last_seen) VALUES (?, 0, 0)", (name,)
        ).lastrowid
        face_id = db.execute(
            "INSERT INTO face (person_id, ts) VALUES (?, 0)", (person_id,)
        ).lastrowid
        db.execute(
            f"INSERT INTO {FACE_TABLE} (face_id, embedding, person_id) VALUES (?, ?, ?)",
            (face_id, unit(face).astype(np.float32).tobytes(), person_id),
        )
    return person_id


def add_appearance(db, person_id: int, session_id: int, clothes: np.ndarray) -> None:
    with transaction(db):
        appearance_id = db.execute(
            "INSERT INTO appearance (person_id, session_id, ts) VALUES (?, ?, 0)",
            (person_id, session_id),
        ).lastrowid
        db.execute(
            f"INSERT INTO {APPEARANCE_TABLE} VALUES (?, ?, ?, ?)",
            (appearance_id, unit(clothes).astype(np.float32).tobytes(), person_id, session_id),
        )


def close_open_session(db) -> int:
    """End the open session but keep it, as an earlier node might have."""
    session_id = people.open_session(db)["id"]
    db.execute("UPDATE session SET ended_at = ? WHERE id = ?", (NOW, session_id))
    return session_id


def person_ids(rows) -> list[int]:
    return [row["person_id"] for row in rows]


# Sessions -------------------------------------------------------------------


def test_start_session_starts_one_when_none_is_open(db):
    started = start(db, label="  walkie-agent ")

    assert started == people.StartedSession(1, NOW, False, 0, 0, 0)
    row = db.execute("SELECT id, label, started_at, ended_at FROM session").fetchone()
    assert tuple(row) == (1, "walkie-agent", NOW, None)
    # An empty label stores none.
    start(db)
    assert db.execute("SELECT label FROM session").fetchone()[0] is None


def test_start_session_with_resume_keeps_the_open_session_and_counts_its_people(db):
    start(db, label="walkie-agent")
    remember(db, "Sam", face=faces(vector(1)))
    remember(db, "Ana", face=faces(vector(2)))

    resumed = start(db, resume=True, label="ignored", now=NOW + 60)

    assert resumed == people.StartedSession(1, NOW, True, 2, 0, 0)
    assert [tuple(r) for r in db.execute("SELECT id, label FROM session")] == [(1, "walkie-agent")]


def test_start_session_with_resume_starts_one_when_none_is_open(db):
    assert start(db, resume=True) == people.StartedSession(1, NOW, False, 0, 0, 0)


def test_start_session_without_resume_forgets_the_open_session_and_its_vectors(db):
    robin = add_kept_person(db, "Robin", vector(0))
    start(db)
    remember(db, "Sam", face=faces(vector(1), vector(2)), appearance=appearances(vector(3)))
    remember(db, "Ana", face=faces(vector(4)), appearance=appearances(vector(5)))
    add_appearance(db, robin, 1, vector(6))

    started = start(db, now=NOW + 60)

    assert started == people.StartedSession(2, NOW + 60, False, 0, 1, 2)
    assert [row[0] for row in db.execute("SELECT id FROM session")] == [2]
    assert [row[0] for row in db.execute("SELECT name FROM person")] == ["Robin"]
    assert [row[0] for row in db.execute("SELECT person_id FROM face")] == [robin]
    assert [row[0] for row in db.execute(f"SELECT person_id FROM {FACE_TABLE}")] == [robin]
    # Robin's clothes belonged to the forgotten session.
    assert count(db, "appearance") == 0
    assert count(db, APPEARANCE_TABLE) == 0


def test_end_session_forgets_the_open_session(db):
    start(db)
    remember(db, face=faces(vector(1)), appearance=appearances(vector(2)))

    assert people.end_session(db, 0) == (1, 1)
    assert people.open_session(db) is None
    for table in PEOPLE_TABLES:
        assert count(db, table) == 0, table


def test_end_session_forgets_a_named_ended_session(db):
    seed.seed(db, now=NOW)

    assert people.end_session(db, 1) == (1, 3)
    assert [row[0] for row in db.execute("SELECT name FROM person")] == ["Robin"]
    assert count(db, FACE_TABLE) == 2
    assert count(db, "conversation") == 0


def test_end_session_with_nothing_to_forget_succeeds(db):
    assert people.end_session(db, 0) == (0, 0)
    start(db)
    assert people.end_session(db, 9) == (0, 0)
    assert people.open_session(db)["id"] == 1


def test_ids_are_never_given_twice(db):
    start(db)
    sam = remember(db, "Sam", face=faces(vector(1)))
    people.end_session(db, 0)
    # The next remember starts a session. Neither may get a forgotten id.
    ana = remember(db, "Ana", face=faces(vector(2)))
    assert (sam.session_id, sam.person_id) == (1, 1)
    assert (ana.session_id, ana.person_id) == (2, 2)
    # A retry of the first end_session forgets nothing.
    assert people.end_session(db, 1) == (0, 0)
    assert start(db).id == 3

    # Ids that another writer gives count too.
    db.execute("INSERT INTO person (id, first_seen, last_seen) VALUES (50, 0, 0)")
    db.execute("DELETE FROM person WHERE id = 50")
    assert remember(db, face=faces(vector(3))).person_id == 51


# Remember -------------------------------------------------------------------


def test_remember_person_adds_a_guest_to_the_open_session(db):
    start(db)
    face_vectors, clothes = (vector(1), vector(2)), vector(3)

    remembered = remember(
        db,
        "  Sam ",
        [(" drink ", " coke "), ("seat", "sofa")],
        faces(*face_vectors),
        appearances(clothes),
    )

    assert remembered == people.Remembered(1, 1, False, 2, 1)
    person = db.execute("SELECT * FROM person").fetchone()
    assert (person["id"], person["session_id"], person["name"]) == (1, 1, "Sam")
    assert json.loads(person["attributes"]) == {"drink": "coke", "seat": "sofa"}
    assert person["first_seen"] == person["last_seen"] == NOW
    assert [tuple(r) for r in db.execute("SELECT person_id, ts FROM face")] == [(1, NOW)] * 2
    assert [tuple(r) for r in db.execute("SELECT person_id, session_id, ts FROM appearance")] == [
        (1, 1, NOW)
    ]
    face_rows = db.execute(f"SELECT face_id, person_id FROM {FACE_TABLE} ORDER BY face_id")
    assert [tuple(r) for r in face_rows] == [(1, 1), (2, 1)]
    appearance_row = db.execute(
        f"SELECT appearance_id, person_id, session_id FROM {APPEARANCE_TABLE}"
    ).fetchone()
    assert tuple(appearance_row) == (1, 1, 1)


def test_remember_person_starts_a_session_when_none_is_open(db):
    remembered = remember(db, "", face=faces(vector(1)))

    session = people.open_session(db)
    assert remembered.started_session
    assert remembered.session_id == session["id"] == 1
    assert session["label"] is None
    assert db.execute("SELECT name FROM person").fetchone()[0] is None
    # The next one joins it.
    assert not remember(db, face=faces(vector(2))).started_session


def test_remember_person_stores_unit_vectors(db):
    v = vector(1)

    remember(db, face=faces(20 * v), appearance=appearances(v / 1000))

    for table in (FACE_TABLE, APPEARANCE_TABLE):
        stored = np.frombuffer(db.execute(f"SELECT embedding FROM {table}").fetchone()[0], np.float32)
        assert stored.shape == (512,)
        assert np.linalg.norm(stored) == pytest.approx(1.0, abs=1e-6)
        assert np.allclose(stored, unit(v), atol=1e-6)


def test_remember_person_refuses_without_vectors(db):
    with pytest.raises(Refused, match="at least one face or appearance vector"):
        remember(db, "Sam", [("drink", "coke")])


def test_remember_person_refuses_an_unknown_model_and_names_the_known_ones(db):
    with pytest.raises(Refused) as face_error:
        remember(db, face=faces(vector(1)) + [("fake_face", vector(2))])
    with pytest.raises(Refused) as appearance_error:
        remember(db, appearance=[(FACE_MODEL, vector(1))])

    assert str(face_error.value) == (
        "faces[1] names the model 'fake_face', which has no face table. "
        "Face models in the database: arcface_w600k_r50."
    )
    # A face model is not an appearance model.
    assert str(appearance_error.value) == (
        "appearances[0] names the model 'arcface_w600k_r50', which has no appearance "
        "table. Appearance models in the database: osnet_x1_0_msmt17."
    )


def test_remember_person_refuses_a_vector_of_the_wrong_length(db):
    with pytest.raises(Refused) as error:
        remember(db, face=faces(vector(1)[:511]))

    assert str(error.value) == "faces[0] has 511 values, but arcface_w600k_r50 vectors have 512."


def bad(value: str) -> np.ndarray:
    if value == "zeros":
        return np.zeros(512, np.float32)
    v = vector(1)
    v[7] = float(value)
    return v


BAD_VALUE_MESSAGES = {
    "nan": "has a NaN or infinite value.",
    "inf": "has a NaN or infinite value.",
    "-inf": "has a NaN or infinite value.",
    "zeros": "is all zeros, so it has no direction.",
}


@pytest.mark.parametrize("value", BAD_VALUE_MESSAGES)
@pytest.mark.parametrize("field", ["faces", "appearances"])
def test_remember_person_refuses_bad_values(db, field, value):
    samples = {"face": faces(vector(1)), "appearance": appearances(vector(2))}
    key = field.removesuffix("s")
    model = FACE_MODEL if key == "face" else APPEARANCE_MODEL
    samples[key] = samples[key] + [(model, bad(value))]

    with pytest.raises(Refused) as error:
        remember(db, face=samples["face"], appearance=samples["appearance"])

    assert str(error.value) == f"{field}[1] {BAD_VALUE_MESSAGES[value]}"


@pytest.mark.parametrize(
    "name, facts, n_faces, message",
    [
        ("x" * 101, [], 1, "The name is longer than 100 characters."),
        ("Sam", [(f"k{i}", "v") for i in range(40)], 1, "At most 32 facts, got 40."),
        ("Sam", [("drink", "coke"), (" ", "tea")], 1, "facts[1] has an empty key."),
        ("Sam", [("drink", "coke"), ("seat", "")], 1, "facts[1] has an empty value."),
        ("Sam", [("drink", "coke"), ("k" * 65, "v")], 1,
         "facts[1]'s key is longer than 64 characters."),
        ("Sam", [("drink", "coke"), ("seat", "v" * 257)], 1,
         "facts[1]'s value is longer than 256 characters."),
        ("Sam", [("drink", "coke"), ("seat", "sofa"), (" drink", "tea")], 1,
         "facts[2] repeats the key 'drink'."),
        ("Sam", [], 120, "At most 100 faces, got 120."),
    ],
)
def test_remember_person_refuses_broken_limits(db, name, facts, n_faces, message):
    with pytest.raises(Refused) as error:
        remember(db, name, facts, faces(*[vector(i) for i in range(n_faces)]))

    assert str(error.value) == message


def test_remember_person_takes_the_largest_allowed_request(db):
    remembered = remember(
        db,
        "x" * 100,
        [(f"{i:064d}", "v" * 256) for i in range(32)],
        faces(*[vector(i) for i in range(100)]),
        appearances(*[vector(i) for i in range(100)]),
    )

    assert (remembered.faces, remembered.appearances) == (100, 100)


def test_a_refused_remember_writes_nothing(db):
    before = counts(db)

    with pytest.raises(Refused):
        remember(db, face=faces(vector(1), vector(2), bad("nan")))

    assert counts(db) == before
    assert people.open_session(db) is None


def test_a_failed_remember_writes_nothing(db):
    # A vector table that takes the vector but has no person_id column, so
    # the write fails halfway, after the person and one face are in.
    db.execute(
        "CREATE VIRTUAL TABLE face_broken USING vec0("
        "face_id INTEGER PRIMARY KEY, embedding FLOAT[4])"
    )
    before = counts(db)

    with pytest.raises(sqlite3.OperationalError):
        remember(db, face=faces(vector(1)) + [("broken", [1.0, 0.0, 0.0, 0.0])])

    assert counts(db) == before
    assert people.open_session(db) is None


# Match ----------------------------------------------------------------------


def test_match_faces_returns_each_person_once_with_their_nearest_sample(db):
    rng = np.random.default_rng(5)
    start(db)
    samples = {}
    for name in ("A", "B", "C", "D", "E"):
        center = rng.standard_normal(512)
        vectors = [(center + rng.standard_normal(512)).astype(np.float32) for _ in range(4)]
        samples[remember(db, name, face=faces(*vectors)).person_id] = vectors
    query = rng.standard_normal(512).astype(np.float32)

    [rows] = people.match(db, FACE, faces(query), 10).results

    expected = sorted(
        (min(1 - unit(v) @ unit(query) for v in vectors), person_id)
        for person_id, vectors in samples.items()
    )
    assert person_ids(rows) == [person_id for _, person_id in expected]
    assert [row["distance"] for row in rows] == pytest.approx(
        [distance for distance, _ in expected], abs=1e-5
    )


def test_match_faces_ranks_people_beyond_the_old_k5_query(db):
    # The k = 5 query that database.md showed before ADR-0018 took the 5
    # nearest samples, then grouped them by person. With 6 samples of A
    # nearer than any of B's, it could only ever name A.
    rng = np.random.default_rng(6)
    query = rng.standard_normal(512).astype(np.float32)
    start(db)
    a = remember(db, "A", face=faces(*[query + 0.3 * rng.standard_normal(512) for _ in range(6)]))
    b = remember(db, "B", face=faces(*[query + 0.6 * rng.standard_normal(512) for _ in range(6)]))
    blob = unit(query).astype(np.float32).tobytes()

    # The old query without its LIMIT 1.
    old = db.execute(
        f"""WITH matches AS (
                SELECT person_id, distance
                FROM {FACE_TABLE}
                WHERE embedding MATCH ? AND k = 5
            )
            SELECT p.id AS person_id, p.name, min(matches.distance) AS distance
            FROM matches JOIN person p ON p.id = matches.person_id
            GROUP BY p.id
            ORDER BY distance""",
        (blob,),
    ).fetchall()
    [rows] = people.match(db, FACE, faces(query), 10).results

    assert person_ids(old) == [a.person_id]
    assert person_ids(rows) == [a.person_id, b.person_id]


def test_match_faces_searches_kept_people_and_the_open_session_only(db):
    face = vector(1)
    robin = add_kept_person(db, "Robin", face)
    start(db)
    remember(db, "Old guest", face=faces(face))
    close_open_session(db)
    start(db)
    sam = remember(db, "Sam", face=faces(face))

    matched = people.match(db, FACE, faces(face), 10)

    assert matched.session_id == 2
    assert sorted(person_ids(matched.results[0])) == [robin, sam.person_id]


def test_match_faces_with_no_open_session_finds_only_kept_people(db):
    face = vector(1)
    robin = add_kept_person(db, "Robin", face)
    remember(db, "Sam", face=faces(face))
    close_open_session(db)

    matched = people.match(db, FACE, faces(face), 10)

    assert matched.session_id is None
    assert person_ids(matched.results[0]) == [robin]
    assert matched.results[0][0]["session_id"] is None


def test_match_appearances_searches_only_the_open_session(db):
    clothes = vector(1)
    robin = add_kept_person(db, "Robin", vector(0))
    start(db)
    remember(db, "Old guest", appearance=appearances(clothes))
    add_appearance(db, robin, 1, clothes)
    close_open_session(db)
    start(db)
    sam = remember(db, "Sam", appearance=appearances(clothes))
    # Robin's clothes today are not quite those of the old session.
    today = clothes + 0.5 * vector(2)
    add_appearance(db, robin, 2, today)

    [rows] = people.match(db, APPEARANCE, appearances(clothes), 10).results

    assert person_ids(rows) == [sam.person_id, robin]
    assert rows[1]["distance"] == pytest.approx(1 - unit(today) @ unit(clothes), abs=1e-5)


def test_match_appearances_with_no_open_session_finds_no_one(db):
    seed.seed(db, now=NOW)
    stored = db.execute(f"SELECT embedding FROM {APPEARANCE_TABLE}").fetchone()[0]
    query = np.frombuffer(stored, np.float32)

    matched = people.match(db, APPEARANCE, appearances(query, query), 10)

    assert matched == people.Matched([[], []], False, None)


def test_match_returns_one_list_per_query_in_order(db):
    a_face, b_face = vector(1), vector(2)
    a = remember(db, "A", face=faces(a_face))
    b = remember(db, "B", face=faces(b_face))

    results = people.match(db, FACE, faces(b_face, a_face, b_face), 10).results

    assert [rows[0]["person_id"] for rows in results] == [b.person_id, a.person_id, b.person_id]
    assert all(len(rows) == 2 for rows in results)


def test_match_applies_the_limit_and_reports_more(db):
    for i in range(3):
        remember(db, face=faces(vector(i)))

    cut = people.match(db, FACE, faces(vector(0)), 2)
    whole = people.match(db, FACE, faces(vector(0)), 3)

    assert (len(cut.results[0]), cut.more) == (2, True)
    assert (len(whole.results[0]), whole.more) == (3, False)


def test_match_refuses_more_than_32_queries(db):
    people.match(db, FACE, faces(*[vector(i) for i in range(32)]), 10)

    with pytest.raises(Refused) as error:
        people.match(db, FACE, faces(*[vector(i) for i in range(33)]), 10)

    assert str(error.value) == "At most 32 queries, got 33."


@pytest.mark.parametrize(
    "query, message",
    [
        (("fake", vector(1)), "queries[1] names the model 'fake', which has no {kind} table."),
        (("{model}", vector(1)[:100]), "queries[1] has 100 values, but {model} vectors have 512."),
        (("{model}", bad("nan")), "queries[1] has a NaN or infinite value."),
        (("{model}", bad("zeros")), "queries[1] is all zeros, so it has no direction."),
    ],
)
@pytest.mark.parametrize("kind", [FACE, APPEARANCE])
def test_match_refuses_bad_queries(db, kind, query, message):
    model = FACE_MODEL if kind == FACE else APPEARANCE_MODEL
    start(db)
    queries = [(model, vector(0)), (query[0].format(model=model), query[1])]

    with pytest.raises(Refused) as error:
        people.match(db, kind, queries, 10)

    assert str(error.value).startswith(message.format(kind=kind, model=model))


def test_match_skips_a_stored_zero_vector(db):
    start(db)
    sam = remember(db, "Sam", face=faces(vector(1)))
    with transaction(db):
        db.execute("INSERT INTO person (id, session_id, first_seen, last_seen) VALUES (9, 1, 0, 0)")
        db.execute("INSERT INTO face (id, person_id, ts) VALUES (9, 9, 0)")
        db.execute(
            f"INSERT INTO {FACE_TABLE} VALUES (9, ?, 9)", (np.zeros(512, np.float32).tobytes(),)
        )

    [rows] = people.match(db, FACE, faces(vector(2)), 10).results

    assert person_ids(rows) == [sam.person_id]


def test_match_appearances_scans_the_samples_first(db):
    # With a plain JOIN, SQLite read the scanned samples once per appearance
    # instead (ADR-0018). The plan wording is SQLite's, and pixi.lock pins it.
    plan = db.execute(
        "EXPLAIN QUERY PLAN " + people._MATCH_APPEARANCES_SQL.format(table=APPEARANCE_TABLE),
        {"query": unit(vector(1)).astype(np.float32).tobytes(), "session": 1, "limit": 10},
    ).fetchall()
    details = [row["detail"] for row in plan]

    scan = next(i for i, d in enumerate(details) if d.startswith("SCAN s"))
    search = next(i for i, d in enumerate(details) if d.startswith("SEARCH a"))
    assert scan < search, details


# Facts and models -----------------------------------------------------------


def test_facts_come_back_as_text(db):
    seed.seed(db, now=NOW)
    attributes = db.execute("SELECT attributes FROM person WHERE name = 'Charlie'").fetchone()[0]

    assert people.facts_of(attributes, 2) == [
        ("drink", "coke"), ("interest", "football"), ("glasses", "true")
    ]
    with pytest.raises(ValueError, match="person 7"):
        people.facts_of("[1, 2]", 7)


def test_vector_tables_lists_the_models_of_each_kind(db):
    assert people.vector_tables(db, FACE) == people.VectorTables({FACE_MODEL: 512}, [])
    assert people.vector_tables(db, APPEARANCE) == people.VectorTables(
        {APPEARANCE_MODEL: 512}, []
    )


def test_a_vector_table_with_an_unreadable_length_is_named_in_the_refusal(db):
    db.execute(
        "CREATE VIRTUAL TABLE face_odd USING vec0("
        "face_id INTEGER PRIMARY KEY, vector FLOAT[8], person_id INTEGER)"
    )

    assert people.vector_tables(db, FACE).unreadable == ["face_odd"]
    with pytest.raises(Refused, match="The table face_odd exists, but the node cannot read"):
        remember(db, face=[("odd", np.ones(8))])


def test_a_model_name_never_reaches_the_sql(db):
    model = f'{FACE_MODEL}" (face_id) VALUES (1); DROP TABLE person; --'

    with pytest.raises(Refused, match="which has no face table"):
        remember(db, face=[(model, vector(1))])

    assert count(db, "person") == 0


def test_unit_vector_returns_float32_bytes_of_length_1():
    tables = people.VectorTables({"m": 3}, [])

    # The squares of these overflow float32.
    blob = people.unit_vector(
        np.array([3e37, 4e37, 0], np.float32), model="m", tables=tables, kind=FACE, where="x"
    )

    v = np.frombuffer(blob, np.float32)
    assert v.tolist() == pytest.approx([0.6, 0.8, 0.0])
    assert math.isclose(float(np.linalg.norm(v)), 1.0, abs_tol=1e-6)
