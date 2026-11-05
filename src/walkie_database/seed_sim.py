"""Turn a sim world's authoring file and a locations file into a seed.

This only adapts the inputs. It normalizes the rooms into the rooms document, prints
that document, and passes it to the same checks and builder as
``walkie_database.seed_store``::

    python -m walkie_database.seed_sim --map-folder PATH --arena NAME \\
        --date YYYY-MM-DD --clock-kind wall|ros_sim --output PATH \\
        --world-source PATH --locations PATH --room-names PATH \\
        [--anchors PATH] [--catalogue PATH] [--previous-seed PATH] [--id-floor N] [--replace]

Each input gives one thing:

* ``--world-source`` decides which rooms exist and gives their outlines, from
  ``[rooms]``. Its ``[locations]`` polygons are flat points, and its room poses
  are not where the agent navigates, so neither is read.
* ``--room-names`` maps each room key to its display name: ``schema_version: 1``
  and ``rooms: {living_room: living room}``. Nothing derives one from the other.
* ``--locations`` gives each room's stand pose and description, from
  ``[locations."living room"]``. The location's name is exactly the room's display
  name, because the agent's places and the rooms share that name.

A missing name or location is an error, and no coordinate has a fallback. A
locations file may list other places, which are not rooms. The agent's
``config/locations.sim.toml`` describes a blank-map demo. It has no such names, so
it is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from walkie_database.people import Refused
from walkie_database.seed_store import (
    REFUSALS,
    SCHEMA_VERSION,
    add_seed_arguments,
    check_fields,
    empty_anchors,
    empty_catalogue,
    identify_map,
    read_document,
    report,
    write_seed,
)


def _read_toml(path: str | Path, what: str) -> dict[str, object]:
    try:
        with open(path, "rb") as file:
            return tomllib.load(file)
    except OSError as error:
        raise Refused(f"The {what} file cannot be read: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise Refused(f"The {what} file {path} is malformed: {error}") from error


def _table(document: Mapping, key: str, what: str) -> Mapping:
    table = document.get(key)
    if not isinstance(table, Mapping):
        raise Refused(f"The {what} file has no [{key}] table.")
    return table


def normalize_rooms(
    map_id: str, world: Mapping, locations: Mapping, room_names: Mapping
) -> dict[str, object]:
    """The rooms document for *map_id*, or Refused. It takes no value from anywhere else."""
    check_fields(room_names, "The room names file", ("schema_version", "rooms"))
    version = room_names["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise Refused(f"The room names file has schema_version {version!r}, not {SCHEMA_VERSION}.")
    names = room_names["rooms"]
    if not isinstance(names, Mapping):
        raise Refused("The room names file must map each room key to its display name.")
    world_rooms = _table(world, "rooms", "world")
    places = _table(locations, "locations", "locations")

    unknown = sorted(str(key) for key in names if key not in world_rooms)
    if unknown:
        raise Refused(f"The room names file lists {', '.join(unknown)}, not in the world's [rooms].")
    rooms = []
    for key, source in world_rooms.items():
        if key not in names:
            raise Refused(f"The room names file does not name the world's room {key}.")
        if not isinstance(source, Mapping) or "polygon" not in source:
            raise Refused(f"The world's room {key} has no polygon.")
        name = names[key]
        if not isinstance(name, str) or name not in places:
            raise Refused(
                f"The locations file has no location named exactly {name!r}, for the room {key}."
            )
        place = places[name]
        missing = [axis for axis in ("x", "y", "yaw") if not isinstance(place, Mapping) or axis not in place]
        if missing:
            raise Refused(f"The location {name!r} has no {', '.join(missing)}.")
        rooms.append(
            {
                "key": key,
                "name": name,
                "description": place.get("description", ""),
                "floor": 0,
                "polygon": source["polygon"],
                "stand_pose": [place["x"], place["y"], place["yaw"]],
            }
        )
    return {"schema_version": SCHEMA_VERSION, "map_id": map_id, "rooms": rooms}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m walkie_database.seed_sim",
        description="Build a seed from a sim world's authoring file and a locations file.",
    )
    add_seed_arguments(parser)
    parser.add_argument("--world-source", required=True, help="the world's authoring TOML")
    parser.add_argument("--locations", required=True, help="the TOML with each room's stand pose")
    parser.add_argument("--room-names", required=True, help="the file mapping room keys to display names")
    parser.add_argument("--anchors", help="an anchors file. Without it the seed has no anchors.")
    parser.add_argument("--catalogue", help="a catalogue file. Without it the catalogue is empty.")
    args = parser.parse_args(argv)
    try:
        folder, map_id = identify_map(args.map_folder, args.arena, args.date)
        rooms = normalize_rooms(
            map_id,
            _read_toml(args.world_source, "world"),
            _read_toml(args.locations, "locations"),
            read_document(args.room_names, "room names"),
        )
        anchors = read_document(args.anchors, "anchors") if args.anchors else empty_anchors(map_id)
        catalogue = read_document(args.catalogue, "catalogue") if args.catalogue else empty_catalogue()
        print(json.dumps(rooms, indent=2))
        summary = write_seed(
            folder=folder, map_id=map_id, arena=args.arena, date=args.date,
            clock_kind=args.clock_kind, rooms_document=rooms, anchors_document=anchors,
            catalogue_document=catalogue, output=args.output,
            previous_seed=args.previous_seed, replace=args.replace, id_floor=args.id_floor,
        )
    except REFUSALS as error:
        print(f"Refused, and wrote nothing: {error}", file=sys.stderr)
        return 1
    report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
