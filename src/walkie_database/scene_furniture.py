"""Measure a sim world's furniture from its MuJoCo scene, as seed anchors.

In simulation this stands in for the static scan of the arena. A furniture file
names the scene bodies that are furniture, with each one's anchor key and label::

    schema_version: 1
    furniture:
    - {body: chula_sofa, key: sofa, label: sofa}
    - {body: chula_coffee_table, key: coffee_table, label: table}

Each body gives one anchor, in the room whose outline holds its centre:

* Its box bounds the body's collision geoms. A geom whose contype and conaffinity
  are both 0, directly or through its default class, only draws, and is skipped.
* Each surface is the top of a board: a collision geom at most 0.12 m thick whose
  footprint is at least 0.05 m^2. Boards at one height less than 0.05 m apart are
  one surface when one lies inside the other or their edges line up across the gap
  (within 5 mm), as the seat cushions of a sofa do. Boards in an L or a step stay
  separate, and are refused, since their bounds would claim empty space as surface.
  The highest surface is ``<key>_top`` and the lower ones ``<key>_level_<n>``,
  counted up from the floor. A cylinder's outline is the square around it.

Only box and cylinder geoms are read, on unrotated bodies directly in the
worldbody, with no joints, child bodies or frames. Anything else is refused,
since a guess here becomes a wrong surface. The scene is the team's own world
file, read with the standard library's parser.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from walkie_database.people import Refused
from walkie_database.seed_store import SCHEMA_VERSION, _contains, _items, _key, _text, check_fields

MAX_BOARD_THICKNESS = 0.12  # metres
MIN_BOARD_AREA = 0.05  # square metres
BOARD_GAP = 0.05  # metres; boards at one height closer than this, and lined up, are one surface
EDGE_TOLERANCE = 0.005  # metres; how closely the edges of boards to be joined must line up
_ROTATIONS = ("euler", "axisangle", "xyaxes", "zaxis")
_BODY_CHILDREN = ("geom", "inertial", "site", "camera", "light")


@dataclass(frozen=True)
class _Geom:
    low: tuple[float, float, float]
    high: tuple[float, float, float]


def _numbers(text: str | None, count: int, where: str) -> tuple[float, ...]:
    try:
        values = tuple(float(v) for v in (text or "").split())
    except ValueError as error:
        raise Refused(f"{where} is not a list of numbers.") from error
    if len(values) < count:
        raise Refused(f"{where} has {len(values)} numbers, not {count}.")
    return values[:count]


class _Defaults:
    """The scene's default classes, for the geom attributes they set."""

    def __init__(self, root: ET.Element) -> None:
        self._classes: dict[str, tuple[str | None, dict[str, str]]] = {}
        for top in root.findall("default"):
            self._walk(top, None)

    def _walk(self, element: ET.Element, parent: str | None) -> None:
        name = element.get("class", "main")
        geom = element.find("geom")
        self._classes[name] = (parent, dict(geom.attrib) if geom is not None else {})
        for child in element.findall("default"):
            self._walk(child, name)

    def geom(self, geom: ET.Element, child_class: str | None, attribute: str) -> str | None:
        """An attribute of *geom*: its own, else its class's, else that class's parents'."""
        if attribute in geom.attrib:
            return geom.get(attribute)
        current = geom.get("class") or child_class or "main"
        if current not in self._classes and current != "main":
            raise Refused(f"The geom {geom.get('name', '')!r} names the class {current!r}, which is not defined.")
        while current is not None:
            parent, attributes = self._classes.get(current, (None, {}))
            if attribute in attributes:
                return attributes[attribute]
            current = parent
        return None


def _unrotated(attributes: Mapping[str, str | None], what: str) -> None:
    """Refused unless *what* (``body x`` or ``geom 'y' of x``) has no rotation."""
    quat = attributes.get("quat")
    if quat is not None:
        w, x, y, z = _numbers(quat, 4, f"The quat of the {what}")
        if w <= 0 or (x, y, z) != (0, 0, 0):
            raise Refused(f"The {what} is rotated, and only unrotated furniture is read.")
    turned = [name for name in _ROTATIONS if attributes.get(name) is not None]
    if turned:
        raise Refused(f"The {what} sets {turned[0]}, and only unrotated furniture is read.")


def _body_geoms(body: ET.Element, defaults: _Defaults) -> list[_Geom]:
    name = body.get("name")
    what = f"body {name}"
    _unrotated(body.attrib, what)
    other = sorted({child.tag for child in body if child.tag not in _BODY_CHILDREN})
    if other:
        raise Refused(f"The {what} holds a {other[0]}; furniture is one fixed body of geoms.")
    origin = _numbers(body.get("pos", "0 0 0"), 3, f"The pos of the {what}")
    child_class = body.get("childclass")
    geoms = []
    for geom in body.findall("geom"):
        geom_what = f"geom {geom.get('name', '')!r} of {name}"

        def attribute(key: str, geom: ET.Element = geom) -> str | None:
            return defaults.geom(geom, child_class, key)

        if attribute("contype") == "0" and attribute("conaffinity") == "0":
            continue
        _unrotated({key: attribute(key) for key in ("quat", *_ROTATIONS)}, geom_what)
        if attribute("fromto") is not None:
            raise Refused(f"The {geom_what} is placed by fromto, which is not read.")
        kind = attribute("type") or "sphere"
        if kind == "box":
            half = _numbers(attribute("size"), 3, f"The size of the {geom_what}")
        elif kind == "cylinder":
            radius, height = _numbers(attribute("size"), 2, f"The size of the {geom_what}")
            half = (radius, radius, height)
        else:
            raise Refused(f"The {geom_what} is a {kind}; only box and cylinder geoms are read.")
        pos = _numbers(attribute("pos") or "0 0 0", 3, f"The pos of the {geom_what}")
        center = [o + p for o, p in zip(origin, pos)]
        geoms.append(_Geom(
            tuple(c - h for c, h in zip(center, half)),
            tuple(c + h for c, h in zip(center, half)),
        ))
    if not geoms:
        raise Refused(f"The {what} has no box or cylinder collision geom.")
    return geoms


def _joinable(a: Sequence[float], b: Sequence[float]) -> bool:
    """True when two rectangles' bounds cover only the two and the gap between them.

    That holds when they are less than BOARD_GAP apart and either one lies inside the
    other or they share their extent along one axis, within EDGE_TOLERANCE. An L or a
    step is not joinable: its bounds would claim empty space as surface.
    """
    near = (a[0] < b[2] + BOARD_GAP and b[0] < a[2] + BOARD_GAP
            and a[1] < b[3] + BOARD_GAP and b[1] < a[3] + BOARD_GAP)

    def same_extent(axis: int) -> bool:
        return abs(a[axis] - b[axis]) <= EDGE_TOLERANCE and abs(a[axis + 2] - b[axis + 2]) <= EDGE_TOLERANCE

    def inside(outer: Sequence[float], inner: Sequence[float]) -> bool:
        return (inner[0] >= outer[0] - EDGE_TOLERANCE and inner[1] >= outer[1] - EDGE_TOLERANCE
                and inner[2] <= outer[2] + EDGE_TOLERANCE and inner[3] <= outer[3] + EDGE_TOLERANCE)

    return near and (same_extent(0) or same_extent(1) or inside(a, b) or inside(b, a))


def _join(rects: list[list[float]]) -> list[list[float]]:
    """Joinable rectangles, joined into their bounds until none are."""
    rects = [list(r) for r in rects]
    i = 0
    while i < len(rects):
        a = rects[i]
        near = next((b for b in rects[i + 1:] if _joinable(a, b)), None)
        if near is None:
            i += 1
            continue
        rects.remove(near)
        rects[i] = [min(a[0], near[0]), min(a[1], near[1]), max(a[2], near[2]), max(a[3], near[3])]
        i = 0
    return rects


def _surfaces(key: str, geoms: Sequence[_Geom]) -> list[dict[str, object]]:
    boards: dict[float, list[list[float]]] = {}
    for g in geoms:
        thickness = g.high[2] - g.low[2]
        area = (g.high[0] - g.low[0]) * (g.high[1] - g.low[1])
        if thickness <= MAX_BOARD_THICKNESS and area >= MIN_BOARD_AREA:
            boards.setdefault(round(g.high[2], 4), []).append([g.low[0], g.low[1], g.high[0], g.high[1]])
    heights = sorted(boards)
    surfaces = []
    for n, z in enumerate(heights, start=1):
        rects = _join(boards[z])
        if len(rects) > 1:
            raise Refused(
                f"The furniture {key} has {len(rects)} separate boards at {z} m. "
                "Write its anchor in an anchors file instead."
            )
        x0, y0, x1, y1 = (round(v, 4) for v in rects[0])
        surfaces.append({
            "key": f"{key}_top" if z == heights[-1] else f"{key}_level_{n}",
            "z": z,
            "outline": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        })
    return surfaces


def read_scene(path: str | Path) -> ET.Element:
    """The root element of a MuJoCo scene file, or Refused."""
    try:
        return ET.parse(path).getroot()
    except OSError as error:
        raise Refused(f"The scene file cannot be read: {error}") from error
    except ET.ParseError as error:
        raise Refused(f"The scene file {path} is malformed: {error}") from error


def furniture_anchors(
    map_id: str, scene: ET.Element, furniture: Mapping, rooms: Sequence[Mapping]
) -> dict[str, object]:
    """The anchors document for *map_id*, one anchor per furniture body, or Refused."""
    check_fields(furniture, "The furniture file", ("schema_version", "furniture"))
    version = furniture["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise Refused(f"The furniture file has schema_version {version!r}, not {SCHEMA_VERSION}.")
    worldbody = scene.find("worldbody")
    bodies = {body.get("name"): body for body in (worldbody if worldbody is not None else [])
              if body.tag == "body"}
    defaults = _Defaults(scene)
    anchors = []
    for i, item in enumerate(_items(furniture["furniture"], "The furniture file's furniture")):
        where = f"furniture[{i}]"
        check_fields(item, where, ("body", "key", "label"))
        body, key, label = _text(item["body"], where), _key(item["key"], where), _text(item["label"], where)
        if body not in bodies:
            raise Refused(f"The scene's worldbody has no body {body!r}, for the furniture {key}.")
        geoms = _body_geoms(bodies[body], defaults)
        low = [min(g.low[axis] for g in geoms) for axis in range(3)]
        high = [max(g.high[axis] for g in geoms) for axis in range(3)]
        center = [round((lo + hi) / 2, 4) for lo, hi in zip(low, high)]
        inside = [room["key"] for room in rooms if _contains(room["polygon"], center[0], center[1])]
        if len(inside) != 1:
            rooms_text = " and ".join(inside) if inside else "no room"
            raise Refused(f"The furniture {key} at ({center[0]}, {center[1]}) is in {rooms_text}.")
        anchors.append({
            "key": key,
            "label": label,
            "room_key": inside[0],
            "geometry_valid": True,
            "enclosed": False,
            "center": center,
            "size": [round(hi - lo, 4) for lo, hi in zip(low, high)],
            "yaw": 0.0,
            "surfaces": _surfaces(key, geoms),
        })
    return {"schema_version": SCHEMA_VERSION, "map_id": map_id, "baseline_rev": "", "anchors": anchors}
