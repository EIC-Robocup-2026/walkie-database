"""The demo data is consistent with itself and with the schema."""

import numpy as np

from walkie_database import seed
from walkie_database.db import connect


def test_every_object_is_inside_its_room(db):
    seed.seed(db)
    rows = db.execute(
        """SELECT o.label, o.room_id,
                  (SELECT s.rowid FROM room_shape s
                   WHERE geopoly_contains_point(s._shape, o.center_x, o.center_y) = 2) AS found
           FROM object o"""
    ).fetchall()
    assert len(rows) == len(seed.OBJECTS)
    assert [(row["label"], row["found"]) for row in rows] == [
        (row["label"], row["room_id"]) for row in rows
    ]


def test_nearest_object_to_a_cup_is_the_other_cup(db):
    seed.seed(db)
    cup_id, embedding = db.execute(
        """SELECT o.id, v.embedding FROM object o
           JOIN object_clip_vit_b16 v ON v.object_id = o.id
           WHERE o.label = 'cup' ORDER BY o.id LIMIT 1"""
    ).fetchone()
    labels = [
        row["label"]
        for row in db.execute(
            """WITH matches AS (
                   SELECT object_id, distance FROM object_clip_vit_b16
                   WHERE embedding MATCH ? AND k = 2
               )
               SELECT o.label FROM matches JOIN object o ON o.id = matches.object_id
               WHERE o.id != ? ORDER BY matches.distance""",
            (embedding, cup_id),
        )
    ]
    assert labels == ["cup"]


def test_a_face_is_recognised_as_its_person(db):
    seed.seed(db)
    # Charlie has three face samples, so the two nearest to one of them are both his.
    person_id, embedding = db.execute(
        """SELECT v.person_id, v.embedding FROM face_arcface_w600k_r50 v
           JOIN person p ON p.id = v.person_id
           WHERE p.name = 'Charlie' LIMIT 1"""
    ).fetchone()
    nearest = db.execute(
        """SELECT person_id FROM face_arcface_w600k_r50
           WHERE embedding MATCH ? AND k = 2 ORDER BY distance""",
        (embedding,),
    ).fetchall()
    assert [row["person_id"] for row in nearest] == [person_id, person_id]


def test_clouds_lie_on_their_boxes(db):
    seed.seed(db)
    for row in db.execute(
        """SELECT c.points, b.min_x, b.max_x, b.min_y, b.max_y, b.min_z, b.max_z
           FROM object_cloud c JOIN object_box b ON b.id = c.object_id"""
    ):
        points = np.frombuffer(row["points"], dtype="<f4").reshape(-1, 3)
        low = np.array([row["min_x"], row["min_y"], row["min_z"]]) - 1e-4
        high = np.array([row["max_x"], row["max_y"], row["max_z"]]) + 1e-4
        assert ((points >= low) & (points <= high)).all()


def test_deleting_the_session_keeps_only_the_team_member(db):
    seed.seed(db)
    db.execute("DELETE FROM session")
    assert [row["name"] for row in db.execute("SELECT name FROM person")] == ["Robin"]
    assert db.execute("SELECT count(*) FROM message").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM object").fetchone()[0] == len(seed.OBJECTS)


def test_main_recreates_the_demo_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    seed.main()
    seed.main()
    conn = connect(seed.DEMO_PATH, readonly=True)
    assert conn.execute("SELECT count(*) FROM object").fetchone()[0] == len(seed.OBJECTS)
    assert not (tmp_path / "data" / "walkie.db").exists()
    assert "pixi run db-browse" in capsys.readouterr().out
