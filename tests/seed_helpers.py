"""Tiny synthetic map folders and seed inputs for the seed, restore and map gate tests.

Nothing here reads a real world or a real map. A room is a 4 m square, and each
anchor is a made-up table or shelf.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from walkie_map_id import make_map_id

from walkie_database.map_config import digest_map_files

ARENA = "testlab"
DATE = "2026-10-16"
ROOM_OUTLINE = [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]
OTHER_OUTLINE = [[5.0, 0.0], [9.0, 0.0], [9.0, 4.0], [5.0, 4.0]]
MAP_YAML = """\
image: map.pgm
mode: trinary
resolution: 0.05
origin: [0.0, 0.0, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""


def write_map_folder(folder: Path, *, pixel: int = 0, baseline_rev: str | None = None) -> Path:
    """Write a 2 by 2 map. Another *pixel* gives another digest.

    *baseline_rev* adds a manifest that names it.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "map.pgm").write_bytes(f"P2\n2 2\n255\n{pixel} 255\n255 0\n".encode())
    (folder / "map.yaml").write_text(MAP_YAML)
    if baseline_rev is not None:
        write_manifest(folder, baseline_rev)
    return folder


def write_manifest(folder: Path, baseline_rev: str, **changes: str) -> None:
    fields = {"map_id": map_id_of(folder), "baseline_rev": baseline_rev, "map_hash": digest_of(folder)}
    (folder / "manifest.yaml").write_text(yaml.safe_dump({**fields, **changes, "resolution": 0.05}))


def digest_of(folder: Path) -> str:
    return digest_map_files(folder / "map.yaml")


def map_id_of(folder: Path) -> str:
    return make_map_id(ARENA, DATE, digest_of(folder))


def room(key: str = "living_room", **changes: object) -> dict[str, object]:
    return {
        "key": key,
        "name": key.replace("_", " "),
        "description": f"The {key.replace('_', ' ')}.",
        "floor": 0,
        "polygon": ROOM_OUTLINE,
        "stand_pose": [1.0, 1.0, 0.0],
    } | changes


def other_room(**changes: object) -> dict[str, object]:
    return room("kitchen_room", polygon=OTHER_OUTLINE, stand_pose=[6.0, 1.0, 0.0]) | changes


def anchor(key: str = "table", **changes: object) -> dict[str, object]:
    """An anchor with a measured box and one surface."""
    return {
        "key": key,
        "label": key,
        "room_key": "living_room",
        "geometry_valid": True,
        "enclosed": False,
        "center": [2.0, 2.0, 0.4],
        "size": [1.0, 0.5, 0.8],
        "yaw": 0.0,
        "surfaces": [{"key": f"{key}_top", "z": 0.8, "outline": [[1.5, 1.75], [2.5, 1.75], [2.5, 2.25], [1.5, 2.25]]}],
    } | changes


def bare_anchor(key: str = "wardrobe", **changes: object) -> dict[str, object]:
    """An anchor known by name, with no measurement at all."""
    return {
        "key": key,
        "label": key,
        "room_key": "living_room",
        "geometry_valid": False,
        "enclosed": True,
        "surfaces": [],
    } | changes


def rooms_document(map_id: str, rooms: list[dict] | None = None, **changes: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "map_id": map_id,
        "rooms": [room(), other_room()] if rooms is None else rooms,
    } | changes


def anchors_document(
    map_id: str, anchors: list[dict] | None = None, baseline_rev: str = "", **changes: object
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "map_id": map_id,
        "baseline_rev": baseline_rev,
        "anchors": [anchor(), bare_anchor()] if anchors is None else anchors,
    } | changes


def catalogue_document(objects: list[dict] | None = None, **changes: object) -> dict[str, object]:
    cup = {
        "official_name": "blue cup",
        "category": "cup",
        "aliases": ["mug"],
        "default_place": "table",
        "weight_g": 250.0,
        "attributes": {"colour": "blue", "fragile": True},
    }
    return {"schema_version": 1, "objects": [cup] if objects is None else objects} | changes


def write_yaml(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def seed_argv(
    directory: Path,
    folder: Path,
    output: Path,
    *,
    rooms: object = None,
    anchors: object = None,
    catalogue: object = None,
    clock_kind: str = "wall",
    extra: tuple[str, ...] = (),
) -> list[str]:
    """Write the three input files, and return seed_store's arguments."""
    map_id = map_id_of(folder)
    inputs = directory / "inputs"
    return [
        "--map-folder", str(folder),
        "--arena", ARENA,
        "--date", DATE,
        "--rooms", str(write_yaml(inputs / "rooms.yaml", rooms_document(map_id) if rooms is None else rooms)),
        "--anchors", str(write_yaml(inputs / "anchors.yaml", anchors_document(map_id) if anchors is None else anchors)),
        "--catalogue", str(write_yaml(inputs / "catalogue.yaml", catalogue_document() if catalogue is None else catalogue)),
        "--clock-kind", clock_kind,
        "--output", str(output),
        *extra,
    ]  # fmt: skip


def dump_rows(conn) -> dict[str, list[tuple]]:
    """Every row of every real table, shadow tables included, in a stable order."""
    tables = conn.execute(
        """SELECT name FROM sqlite_master
           WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%'
           ORDER BY name"""
    ).fetchall()
    return {
        row["name"]: sorted(
            (tuple(r) for r in conn.execute(f'SELECT * FROM "{row["name"]}"')), key=repr
        )
        for row in tables
    }


def files_in(directory: Path) -> list[str]:
    """Every file under *directory*, for checking what a refused tool left behind."""
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file())
