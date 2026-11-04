"""Read a map folder, and check that a store belongs to it.

walkie_map_id stays free of dependencies, so YAML parsing lives here. This
module rejects what a plain safe_load would let through: a repeated key, where
the last value would silently win.

:func:`check_store` is the one map gate (ADR-0023). The node runs it at start-up,
and the restore tool runs it on a seed. Launchers do not repeat it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from walkie_map_id import map_digest

_MERGE_TAG = "tag:yaml.org,2002:merge"

WALL, ROS_SIM = "wall", "ros_sim"
CLOCK_KINDS = (WALL, ROS_SIM)
FRAME_ID = "map"  # every store coordinate is in this frame (ADR-0008)
MAP_FILE, MANIFEST_FILE = "map.yaml", "manifest.yaml"


class MapGateError(ValueError):
    """A map folder is unusable, or a store does not belong to it."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses repeated keys and merge keys.

    A merge key is refused because an explicit key overrides it silently, which
    is the same mistake as a repeated key.
    """

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == _MERGE_TAG:
                    raise ValueError("it uses a merge key")
                key = self.construct_object(key_node, deep=True)
                try:
                    repeated = key in seen
                except TypeError as error:
                    raise ValueError("it has a key that is not a scalar") from error
                if repeated:
                    raise ValueError(f"it repeats the key {key!r}")
                seen.add(key)
        return super().construct_mapping(node, deep)


def parse_yaml_mapping(text: str | bytes, what: str) -> dict[str, object]:
    """Return the top-level mapping of a YAML or JSON document, or raise ValueError.

    *what* names the document in the error.
    """
    try:
        document = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except (yaml.YAMLError, ValueError) as error:
        raise ValueError(f"{what} is malformed: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{what} must be a mapping")
    return document


def parse_map_yaml(text: str | bytes) -> dict[str, object]:
    """Return the top-level mapping of a map YAML, or raise ValueError."""
    return parse_yaml_mapping(text, "map YAML")


def digest_map_files(yaml_path: str | Path) -> str:
    """Return the canonical digest of a map YAML and the image it names.

    A relative image path is relative to the YAML file, as in nav2's map server.
    Bad content raises ValueError. A file that cannot be read raises OSError.
    """
    path = Path(yaml_path)
    metadata = parse_map_yaml(path.read_bytes())
    image = metadata.get("image")
    if not isinstance(image, str) or not image:
        raise ValueError("map YAML must name its image")
    return map_digest((path.parent / image).read_bytes(), metadata)


@dataclass(frozen=True)
class Manifest:
    """What the gate reads of a map folder's manifest. walkie-ros owns the other keys."""

    map_id: str
    baseline_rev: str
    map_hash: str


@dataclass(frozen=True)
class MapFolder:
    """A map folder's full digest, and its manifest if it has one."""

    path: Path
    digest: str
    manifest: Manifest | None


def _read_manifest(path: Path) -> Manifest | None:
    try:
        text = path.read_bytes()
    except FileNotFoundError:
        return None
    document = parse_yaml_mapping(text, MANIFEST_FILE)
    values = {}
    for key in ("map_id", "baseline_rev", "map_hash"):
        value = document.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{MANIFEST_FILE} must have {key} as a string")
        values[key] = value
    return Manifest(**values)


def read_map_folder(folder: str | Path) -> MapFolder:
    """Read ``map.yaml``, its image and any ``manifest.yaml``, or raise MapGateError.

    A folder with no manifest is valid: it has no baseline. A manifest that names
    another digest is not.
    """
    path = Path(folder)
    try:
        digest = digest_map_files(path / MAP_FILE)
        manifest = _read_manifest(path / MANIFEST_FILE)
    except (OSError, ValueError) as error:
        raise MapGateError(f"Map folder {path} cannot be used: {error}") from error
    if manifest is not None and manifest.map_hash != digest:
        raise MapGateError(
            f"{path / MANIFEST_FILE} records the map hash {manifest.map_hash}, "
            f"but the map's digest is {digest}."
        )
    return MapFolder(path, digest, manifest)


def check_baseline(baseline_rev: str, folder: MapFolder, map_id: str) -> None:
    """Refuse a store's baseline revision that its map folder does not back.

    No manifest and an empty revision is valid, and means no baseline.
    """
    manifest = folder.manifest
    if manifest is None:
        if baseline_rev:
            raise MapGateError(
                f"The store names baseline {baseline_rev!r}, but {folder.path} has no "
                f"{MANIFEST_FILE}."
            )
        return
    if manifest.map_id != map_id:
        raise MapGateError(
            f"{folder.path / MANIFEST_FILE} names the map {manifest.map_id!r}, not {map_id!r}."
        )
    if manifest.baseline_rev != baseline_rev:
        raise MapGateError(
            f"The store names baseline {baseline_rev!r}, but {folder.path / MANIFEST_FILE} "
            f"names {manifest.baseline_rev!r}."
        )


def check_store(
    conn: sqlite3.Connection, folder: MapFolder, clock_kind: str, map_id: str | None = None
) -> sqlite3.Row:
    """Return the store's map row if the store belongs to *folder*, or raise MapGateError.

    It compares the **full digest**. The 8 characters in a map ID only name a
    directory. *map_id*, when given, must be the map the store says it holds.
    This only reads, so a store that fails stays as it was. A file that is not a
    database raises sqlite3.DatabaseError.
    """
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_meta'"
    ).fetchone()
    meta = conn.execute("SELECT * FROM store_meta WHERE id = 1").fetchone() if present else None
    if meta is None:
        raise MapGateError(
            "The store has no store_meta row, so it is not registered to a map. "
            "Restore a seed to a new path, or run it with memory_enabled false."
        )
    if map_id is not None and meta["map_id"] != map_id:
        raise MapGateError(f"The store holds the map {meta['map_id']!r}, not {map_id!r}.")
    row = conn.execute("SELECT * FROM map WHERE id = ?", (meta["map_id"],)).fetchone()
    if row is None:
        raise MapGateError(f"The store has no map row for {meta['map_id']!r}.")
    if not row["map_hash"]:
        raise MapGateError(f"The store's map {row['id']!r} has a blank hash, so it is not registered.")
    if row["map_hash"] != folder.digest:
        raise MapGateError(
            f"The store's map {row['id']!r} has the digest {row['map_hash']}, "
            f"but {folder.path} has {folder.digest}."
        )
    if not row["id"].endswith(f"-{folder.digest[:8]}"):
        raise MapGateError(f"The map ID {row['id']!r} does not end with its digest's 8 characters.")
    if row["frame_id"] != FRAME_ID:
        raise MapGateError(f"The store's map uses the frame {row['frame_id']!r}, not {FRAME_ID!r}.")
    for source, value in (("map", row["clock_kind"]), ("store_meta", meta["clock_kind"])):
        if value != clock_kind:
            raise MapGateError(f"The store's {source} row says clock {value!r}, not {clock_kind!r}.")
    check_baseline(row["baseline_rev"], folder, row["id"])
    return row
