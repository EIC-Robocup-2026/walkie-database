"""Durable object ids, new object rows, tombstones and the lifecycle columns."""

import json
import math
import sqlite3

import pytest

from walkie_database.db import connect, transaction
from walkie_database.migrate import migrate
from walkie_database.objects import (
    MAX_LINK_HOPS,
    IdReservation,
    NewObject,
    ObjectBox,
    insert_object,
    object_id_highwater,
    reserve_object_ids,
    set_split_from,
    tombstone_object,
)
from walkie_database.people import Refused

BOX = ObjectBox(center=(1.0, 2.0, 0.5), size=(0.2, 0.4, 0.6), stamp=12.0)
SHARES = [0.5, 0.25, 0.25, 0, 0, 0, 0, 0, 0, 0, 0, 0]


@pytest.fixture
def store(tmp_path):
    """A migrated file, so that a restart can reopen it and synchronous starts at NORMAL."""
    conn = connect(tmp_path / "walkie.db")
    migrate(conn)
    conn.execute("INSERT INTO map (id, created_at) VALUES ('m1', 0)")
    conn.execute("INSERT INTO map (id, created_at) VALUES ('m2', 0)")
    yield conn
    conn.close()


def new_cup(**fields) -> NewObject:
    return NewObject(**{"map_id": "m1", "label": "cup", "first_seen": 1.0, "last_seen": 2.0, **fields})


def add_objects(conn, count: int, **fields) -> list[int]:
    """*count* committed objects with new ids."""
    reservation = reserve_object_ids(conn, count)
    with transaction(conn):
        for object_id in reservation:
            insert_object(conn, new_cup(**fields), object_id, reservation=reservation)
    return list(reservation)


def synchronous(conn) -> int:
    return conn.execute("PRAGMA synchronous").fetchone()[0]


class WatchedConnection:
    """Forwards to a real connection, and records the state at each transaction statement."""

    def __init__(self, conn, fail_on: str | None = None):
        self.conn = conn
        self.fail_on = fail_on
        self.steps: list[tuple[str, int, bool]] = []

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    def execute(self, sql, *args):
        if sql.split()[0] in ("BEGIN", "COMMIT", "ROLLBACK"):
            self.steps.append((sql.split()[0], synchronous(self.conn), self.conn.in_transaction))
        if sql == self.fail_on:
            raise sqlite3.OperationalError("disk I/O error")
        return self.conn.execute(sql, *args)


# Reserving ids ---------------------------------------------------------------


def test_reservation_syncs_its_commit_and_restores_the_setting_after_it(store):
    assert synchronous(store) == 1  # NORMAL, from connect()
    watched = WatchedConnection(store)

    reservation = reserve_object_ids(watched, 3)

    # FULL (2) is on before BEGIN and still on at COMMIT, because SQLite
    # refuses to change it in between.
    assert watched.steps == [("BEGIN", 2, False), ("COMMIT", 2, True)]
    assert synchronous(store) == 1
    assert len(reservation) == 3


def test_reservation_runs_its_statements_in_order(store):
    statements: list[str] = []
    store.set_trace_callback(statements.append)
    reserve_object_ids(store, 2)
    store.set_trace_callback(None)
    kinds = [statement.split("SET")[0].strip() for statement in statements]
    assert kinds == [
        "PRAGMA synchronous",
        "PRAGMA synchronous = FULL",
        "BEGIN IMMEDIATE",
        "UPDATE last_id",
        "COMMIT",
        "PRAGMA synchronous = 1",
    ]


def test_reservation_is_committed_when_it_returns(tmp_path, store):
    reservation = reserve_object_ids(store, 4)
    other = connect(tmp_path / "walkie.db", readonly=True)
    assert object_id_highwater(other) == reservation.last
    other.close()
    assert not store.in_transaction


def test_failed_commit_gives_no_id_and_restores_the_setting(store):
    before = object_id_highwater(store)
    watched = WatchedConnection(store, fail_on="COMMIT")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        reserve_object_ids(watched, 5)

    assert not store.in_transaction
    assert object_id_highwater(store) == before
    assert synchronous(store) == 1
    assert watched.steps[-1][0] == "ROLLBACK"


def test_reservation_inside_a_transaction_is_refused_and_leaves_it_open(store):
    before = object_id_highwater(store)
    with transaction(store):
        store.execute("INSERT INTO map (id, created_at) VALUES ('kept', 0)")
        with pytest.raises(Refused, match="outside a transaction"):
            reserve_object_ids(store)
        assert store.in_transaction
    assert object_id_highwater(store) == before
    assert store.execute("SELECT count(*) FROM map WHERE id = 'kept'").fetchone()[0] == 1
    assert synchronous(store) == 1


@pytest.mark.parametrize("count", [0, -1])
def test_an_empty_reservation_is_refused(store, count):
    with pytest.raises(Refused, match="Cannot reserve"):
        reserve_object_ids(store, count)


def test_reservations_never_overlap_and_ids_are_positive(store):
    first = reserve_object_ids(store, 3)
    second = reserve_object_ids(store, 2)
    assert list(first) == [1, 2, 3]
    assert list(second) == [4, 5]
    assert 3 in first and 4 not in first
    assert IdReservation(4, 5) == second


def test_in_memory_connection_keeps_its_own_setting(db):
    assert synchronous(db) == 2
    reserve_object_ids(db)
    assert synchronous(db) == 2


# Never given twice -----------------------------------------------------------


def test_an_id_is_not_given_again_after_a_delete(store):
    [object_id] = add_objects(store, 1)
    with transaction(store):
        store.execute("DELETE FROM object WHERE id = ?", (object_id,))
    assert reserve_object_ids(store).first == object_id + 1


def test_an_id_is_not_given_again_after_a_restart(tmp_path, store):
    [object_id] = add_objects(store, 1)
    with transaction(store):
        store.execute("DELETE FROM object WHERE id = ?", (object_id,))
    store.close()
    reopened = connect(tmp_path / "walkie.db")
    assert reserve_object_ids(reopened).first == object_id + 1
    reopened.close()


def test_unused_ids_of_a_reservation_are_not_given_again_after_a_restart(tmp_path, store):
    unused = reserve_object_ids(store, 10)
    store.close()
    reopened = connect(tmp_path / "walkie.db")
    assert reserve_object_ids(reopened).first == unused.last + 1
    reopened.close()


def test_an_insert_outside_the_allocator_moves_the_counter(store):
    # A fresh store's fixture may set explicit ids, so the trigger must see them.
    with transaction(store):
        store.execute(
            """INSERT INTO object (id, map_id, label, center_x, center_y, center_z,
                                   size_x, size_y, size_z, first_seen, last_seen)
               VALUES (70, 'm1', 'cup', 0, 0, 0, 0, 0, 0, 0, 0)"""
        )
    assert object_id_highwater(store) == 70
    assert reserve_object_ids(store).first == 71


def test_deleting_a_map_with_tombstones_keeps_the_counter(store):
    loser, winner = add_objects(store, 2)
    with transaction(store):
        tombstone_object(store, loser, merged_into=winner)
    with transaction(store):
        store.execute("DELETE FROM map WHERE id = 'm1'")
    assert store.execute("SELECT count(*) FROM object").fetchone()[0] == 0
    assert reserve_object_ids(store).first == winner + 1


def test_an_id_outside_the_reservation_is_refused(store):
    [old] = add_objects(store, 1)
    with transaction(store):
        store.execute("DELETE FROM object WHERE id = ?", (old,))
    reservation = reserve_object_ids(store)
    with transaction(store):
        with pytest.raises(Refused, match="not in the current reservation"):
            insert_object(store, new_cup(), old, reservation=reservation)
        with pytest.raises(Refused, match="not in the current reservation"):
            insert_object(store, new_cup(), reservation.last + 1, reservation=reservation)
        with pytest.raises(Refused, match="not in the current reservation"):
            insert_object(store, new_cup(), reservation.first)
    assert store.execute("SELECT count(*) FROM object").fetchone()[0] == 0


def test_an_id_of_the_reservation_cannot_come_back_after_a_delete(store):
    reservation = reserve_object_ids(store, 2)
    with transaction(store):
        insert_object(store, new_cup(), reservation.first, reservation=reservation)
    with transaction(store):
        store.execute("DELETE FROM object WHERE id = ?", (reservation.first,))
    with transaction(store):
        with pytest.raises(Refused, match="never given twice"):
            insert_object(store, new_cup(), reservation.first, reservation=reservation)


def test_a_rolled_back_insert_can_retry_its_id(store):
    reservation = reserve_object_ids(store)
    with pytest.raises(RuntimeError, match="later write failed"):
        with transaction(store):
            insert_object(store, new_cup(), reservation.first, reservation=reservation)
            raise RuntimeError("later write failed")
    assert store.execute("SELECT count(*) FROM object").fetchone()[0] == 0
    with transaction(store):
        insert_object(store, new_cup(), reservation.first, reservation=reservation)
    assert store.execute("SELECT count(*) FROM object").fetchone()[0] == 1


def test_a_retained_anchor_keeps_its_identity_below_the_highwater(store):
    reserve_object_ids(store, 10)
    with transaction(store):
        insert_object(store, new_cup(layer="anchor", key="table"), 4, retained_ids={4})
    assert store.execute("SELECT id FROM object WHERE key = 'table'").fetchone()[0] == 4
    assert object_id_highwater(store) == 10


def test_insert_needs_the_callers_transaction(store):
    reservation = reserve_object_ids(store)
    with pytest.raises(Refused, match="inside the caller's transaction"):
        insert_object(store, new_cup(), reservation.first, reservation=reservation)


# Geometry --------------------------------------------------------------------


def test_a_candidate_with_no_geometry_is_not_in_the_spatial_index(store):
    [object_id] = add_objects(store, 1)
    row = store.execute(
        "SELECT state, geometry_valid, geometry_stamp, center_x, size_x FROM object WHERE id = ?",
        (object_id,),
    ).fetchone()
    assert tuple(row) == ("candidate", 0, None, 0.0, 0.0)
    assert store.execute("SELECT count(*) FROM object_box").fetchone()[0] == 0


def test_a_measured_object_is_in_the_spatial_index(store):
    [object_id] = add_objects(store, 1, box=BOX)
    row = store.execute(
        "SELECT geometry_valid, geometry_stamp, center_y FROM object WHERE id = ?", (object_id,)
    ).fetchone()
    assert tuple(row) == (1, 12.0, 2.0)
    box = store.execute("SELECT * FROM object_box WHERE id = ?", (object_id,)).fetchone()
    assert tuple(box) == pytest.approx((object_id, 0.9, 1.1, 1.8, 2.2, 0.2, 0.8))


def test_the_spatial_box_contains_a_turned_box(store):
    half = math.sqrt(0.5)
    turned = ObjectBox(center=(0, 0, 0), size=(2, 4, 6), rotation=(0, 0, half, half))
    [object_id] = add_objects(store, 1, box=turned)
    box = store.execute("SELECT * FROM object_box WHERE id = ?", (object_id,)).fetchone()
    # A quarter turn about z swaps the x and y sides.
    assert tuple(box) == pytest.approx((object_id, -2, 2, -1, 1, -3, 3), abs=1e-5)


def test_losing_geometry_removes_the_object_from_the_spatial_index(store):
    [object_id] = add_objects(store, 1, box=BOX)
    with transaction(store):
        store.execute("UPDATE object SET geometry_valid = 0 WHERE id = ?", (object_id,))
    assert store.execute("SELECT count(*) FROM object_box").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("box", "message"),
    [
        (ObjectBox((math.nan, 0, 0), (1, 1, 1)), "NaN or infinite"),
        (ObjectBox((0, 0, 0), (1, math.inf, 1)), "NaN or infinite"),
        (ObjectBox((0, 0, 0), (1, -1, 1)), "negative"),
        (ObjectBox((0, 0, 0), (1, 1, 1), rotation=(0, 0, 0, 0)), "unit quaternion"),
    ],
)
def test_a_bad_box_writes_nothing(store, box, message):
    reservation = reserve_object_ids(store)
    with transaction(store):
        with pytest.raises(Refused, match=message):
            insert_object(store, new_cup(box=box), reservation.first, reservation=reservation)
    assert store.execute("SELECT count(*) FROM object").fetchone()[0] == 0
    # A refused box does not use up its id.
    with transaction(store):
        insert_object(store, new_cup(box=BOX), reservation.first, reservation=reservation)


# Tombstones and links --------------------------------------------------------


def test_discarding_keeps_the_row_and_moves_the_revision(store):
    [object_id] = add_objects(store, 1, box=BOX)
    with transaction(store):
        tombstone_object(store, object_id)
    row = store.execute(
        "SELECT state, merged_into, revision FROM object WHERE id = ?", (object_id,)
    ).fetchone()
    assert tuple(row) == ("discarded", None, 1)


def test_merging_points_to_the_winner(store):
    loser, winner = add_objects(store, 2)
    with transaction(store):
        tombstone_object(store, loser, merged_into=winner)
    row = store.execute("SELECT state, merged_into FROM object WHERE id = ?", (loser,)).fetchone()
    assert tuple(row) == ("merged", winner)
    assert store.execute("SELECT state FROM object WHERE id = ?", (winner,)).fetchone()[0] == "candidate"


def test_a_tombstone_cannot_be_written_twice(store):
    loser, winner = add_objects(store, 2)
    with transaction(store):
        tombstone_object(store, loser, merged_into=winner)
    with transaction(store):
        with pytest.raises(Refused, match="already merged"):
            tombstone_object(store, loser)


def test_a_tombstone_needs_the_callers_transaction_and_a_real_object(store):
    with pytest.raises(Refused, match="inside the caller's transaction"):
        tombstone_object(store, 1)
    with transaction(store):
        with pytest.raises(Refused, match="does not exist"):
            tombstone_object(store, 1)


def test_deleting_the_winner_clears_the_links_to_it(store):
    loser, child, winner = add_objects(store, 3)
    with transaction(store):
        tombstone_object(store, loser, merged_into=winner)
        set_split_from(store, child, winner)
    with transaction(store):
        store.execute("DELETE FROM object WHERE id = ?", (winner,))
    row = store.execute("SELECT merged_into FROM object WHERE id = ?", (loser,)).fetchone()
    assert row[0] is None
    row = store.execute("SELECT split_from FROM object WHERE id = ?", (child,)).fetchone()
    assert row[0] is None


def test_a_link_to_itself_is_refused(store):
    [object_id] = add_objects(store, 1)
    with transaction(store):
        with pytest.raises(Refused, match="link to itself"):
            tombstone_object(store, object_id, merged_into=object_id)
        with pytest.raises(Refused, match="link to itself"):
            set_split_from(store, object_id, object_id)
    assert store.execute("SELECT state FROM object").fetchone()[0] == "candidate"


def test_a_link_to_a_missing_object_is_refused(store):
    [object_id] = add_objects(store, 1)
    with transaction(store):
        with pytest.raises(Refused, match="999 does not exist"):
            tombstone_object(store, object_id, merged_into=999)


def test_a_link_across_maps_is_refused(store):
    [here] = add_objects(store, 1)
    [there] = add_objects(store, 1, map_id="m2")
    with transaction(store):
        with pytest.raises(Refused, match="different maps"):
            tombstone_object(store, here, merged_into=there)
        with pytest.raises(Refused, match="different maps"):
            set_split_from(store, here, there)


def test_a_merge_cycle_is_refused(store):
    a, b, c = add_objects(store, 3)
    with transaction(store):
        tombstone_object(store, a, merged_into=b)
        tombstone_object(store, b, merged_into=c)
        with pytest.raises(Refused, match="cycle"):
            tombstone_object(store, c, merged_into=a)
    assert store.execute("SELECT state FROM object WHERE id = ?", (c,)).fetchone()[0] == "candidate"


def test_a_split_cycle_is_refused(store):
    a, b = add_objects(store, 2)
    with transaction(store):
        set_split_from(store, a, b)
        with pytest.raises(Refused, match="cycle"):
            set_split_from(store, b, a)


def test_a_chain_over_the_hop_cap_is_refused(store):
    ids = add_objects(store, MAX_LINK_HOPS + 2)
    with transaction(store):
        # Written directly, since a chain this long is never built on purpose.
        for loser, winner in zip(ids[1:], ids[2:]):
            store.execute("UPDATE object SET merged_into = ? WHERE id = ?", (winner, loser))
        with pytest.raises(Refused, match="over 32 links"):
            tombstone_object(store, ids[0], merged_into=ids[1])


def test_a_chain_at_the_hop_cap_is_allowed(store):
    ids = add_objects(store, MAX_LINK_HOPS + 1)
    with transaction(store):
        for loser, winner in zip(ids[1:], ids[2:]):
            store.execute("UPDATE object SET merged_into = ? WHERE id = ?", (winner, loser))
        tombstone_object(store, ids[0], merged_into=ids[1])
    assert store.execute("SELECT state FROM object WHERE id = ?", (ids[0],)).fetchone()[0] == "merged"


# Columns ---------------------------------------------------------------------


@pytest.fixture
def cup(store):
    return add_objects(store, 1)[0]


def add_observation(conn, object_id, **columns):
    names = ", ".join(("object_id", "ts", "label", *columns))
    marks = ", ".join("?" * (3 + len(columns)))
    conn.execute(
        f"INSERT INTO object_observation ({names}) VALUES ({marks})",
        (object_id, 0.0, "cup", *columns.values()),
    )


def test_store_meta_holds_one_row_and_keeps_its_map(store):
    insert = (
        "INSERT INTO store_meta (id, store_id, clock_kind, map_id, created_at) "
        "VALUES (?, 's', 'wall', 'm1', 0)"
    )
    store.execute(insert, (1,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.execute(insert, (2,))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.execute("DELETE FROM map WHERE id = 'm1'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.execute(
            "INSERT OR REPLACE INTO store_meta (id, store_id, clock_kind, map_id, created_at) "
            "VALUES (1, 's', 'wall', 'nowhere', 0)"
        )


def test_map_clock_kind_is_limited(store):
    with pytest.raises(sqlite3.IntegrityError, match="clock_kind"):
        store.execute("INSERT INTO map (id, created_at, clock_kind) VALUES ('m3', 0, 'gps')")


def test_a_key_is_unique_per_map_when_it_is_set(store):
    add_objects(store, 2)  # no key: any number of them
    add_objects(store, 1, key="table")
    add_objects(store, 1, key="table", map_id="m2")
    reservation = reserve_object_ids(store)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with transaction(store):
            insert_object(store, new_cup(key="table"), reservation.first, reservation=reservation)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("layer", "wall"),
        ("state", "gone"),
        ("p_exist", 0.04),
        ("p_exist", 0.96),
        ("geometry_valid", 2),
        ("in_baseline", 2),
        ("depth_seen", -1),
        ("revision", -1),
        ("attributes", "[1]"),
        ("attributes", "not json"),
        ("best_view_quality", 1.5),
        ("fingerprint_weight", -0.5),
        ("viewpoints", -1),
        ("anchor_offset", "not json"),
        ("expected_pose", "not json"),
    ],
)
def test_object_columns_refuse_bad_values(store, cup, column, value):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.execute(f"UPDATE object SET {column} = ? WHERE id = ?", (value, cup))


@pytest.mark.parametrize(
    "state",
    ["candidate", "present", "grasping", "held", "missing", "handed_over", "discarded", "merged"],
)
def test_object_state_takes_the_values_of_object_summary(store, cup, state):
    store.execute("UPDATE object SET state = ? WHERE id = ?", (state, cup))


def test_object_labels_go_with_their_object(store, cup):
    store.execute("INSERT INTO object_label (object_id, label, count) VALUES (?, 'mug', 3)", (cup,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.execute("INSERT INTO object_label (object_id, label, count) VALUES (?, 'cup', -1)", (cup,))
    with transaction(store):
        store.execute("DELETE FROM object WHERE id = ?", (cup,))
    assert store.execute("SELECT count(*) FROM object_label").fetchone()[0] == 0


def test_observations_keep_their_view_evidence(store, cup):
    add_observation(
        store, cup, view_score=0.8, camera_x=1.0, camera_qw=1.0, color_shares=json.dumps(SHARES)
    )
    row = store.execute(
        "SELECT view_score, camera_x, camera_qw, color_shares FROM object_observation"
    ).fetchone()
    assert tuple(row) == (0.8, 1.0, 1.0, json.dumps(SHARES))
    with pytest.raises(sqlite3.IntegrityError, match="view_score"):
        add_observation(store, cup, view_score=1.5)


@pytest.mark.parametrize(
    "shares",
    [
        None,
        json.dumps(SHARES),
        json.dumps([1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
        # Within the tolerance walkie-vision publishes with.
        json.dumps([0.50005, 0.5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ],
)
def test_good_color_shares_are_kept(store, cup, shares):
    add_observation(store, cup, color_shares=shares)
    stored = store.execute("SELECT color_shares FROM object_observation").fetchone()[0]
    assert stored == shares


BAD_SHARES = [
    "not json",
    "{}",
    "[]",
    json.dumps([1] + [0] * 10),  # eleven
    json.dumps([1] + [0] * 12),  # thirteen
    json.dumps([0.5] * 2 + [0] * 9 + [0.1]),  # sums to 1.1
    json.dumps([0.1] * 11 + [0]),  # sums to 1.1
    json.dumps([1.5, -0.5] + [0] * 10),  # sums to 1, out of range
    json.dumps(["1"] + [0] * 11),
    "[true, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]",  # JSON true reads as 1
    "[null, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]",
    "[1e999, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]",  # reads as infinity
    "[[1], 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]",
]


@pytest.mark.parametrize("shares", BAD_SHARES)
def test_bad_color_shares_are_refused_on_insert(store, cup, shares):
    with pytest.raises(sqlite3.IntegrityError):
        add_observation(store, cup, color_shares=shares)
    assert store.execute("SELECT count(*) FROM object_observation").fetchone()[0] == 0


@pytest.mark.parametrize("shares", BAD_SHARES)
def test_bad_color_shares_are_refused_on_update(store, cup, shares):
    add_observation(store, cup, color_shares=json.dumps(SHARES))
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE object_observation SET color_shares = ?", (shares,))
    stored = store.execute("SELECT color_shares FROM object_observation").fetchone()[0]
    assert stored == json.dumps(SHARES)


def test_last_id_names_only_the_three_counters(store):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.execute("INSERT INTO last_id (table_name, id) VALUES ('room', 0)")
