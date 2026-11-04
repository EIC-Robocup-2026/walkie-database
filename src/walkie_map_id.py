"""The canonical identity of an occupancy map, version walkie-map-v1.

Two copies of a map are the same map when their decoded pixels and their
semantic metadata are the same. A re-saved file, a renamed image or reordered
YAML keeps the digest. A changed pixel, origin or resolution does not.

This file is a public contract. Other repositories obtain it verbatim from a
pinned commit, so it imports only the standard library, reads no files and
parses no YAML. Changing what it hashes needs a new algorithm version and new
map ids (ADR-0020).

Every rejection is a ValueError.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
import struct

ALGORITHM = "walkie-map-v1"

_WHITESPACE = b" \t\r\n"
_COMMENT = 0x23  # "#"
_LINE_END = re.compile(rb"[\r\n]")
_MAX_DIMENSION = 0xFFFFFFFF
_MAX_MAXVAL = 65535
_SAMPLES_PER_UPDATE = 65536

_MODES = frozenset({"trinary", "scale", "raw"})
_REQUIRED_METADATA = ("resolution", "origin", "negate", "occupied_thresh", "free_thresh")

_ARENA = re.compile(r"[a-z][a-z0-9_-]*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _read_header_number(data: bytes, start: int, name: str) -> tuple[int, int]:
    """Return one header number and the position just after its last digit."""
    position = start
    while position < len(data) and (data[position] in _WHITESPACE or data[position] == _COMMENT):
        if data[position] == _COMMENT:
            line_end = _LINE_END.search(data, position)
            if line_end is None:
                raise ValueError("PGM header comment has no line end")
            position = line_end.end()
        else:
            position += 1
    if position == start:
        raise ValueError(f"PGM header needs whitespace or a comment before {name}")

    end = position
    while end < len(data) and 0x30 <= data[end] <= 0x39:
        end += 1
    if end == position:
        raise ValueError(f"PGM header has no number for {name}")
    return int(data[position:end]), end


def decode_pgm(data: bytes) -> tuple[int, int, int, tuple[int, ...]]:
    """Return width, height, maxval and row-major samples of a P2 or P5 image.

    Comments are allowed only in the header, before maxval. Exactly one
    whitespace byte ends the header, so in P5 a raster byte that looks like
    whitespace or "#" is a pixel.
    """
    if not isinstance(data, bytes):
        raise ValueError("PGM data must be bytes")
    magic = data[:2]
    if magic not in (b"P2", b"P5"):
        raise ValueError("PGM must start with P2 or P5")

    width, position = _read_header_number(data, 2, "width")
    height, position = _read_header_number(data, position, "height")
    maxval, position = _read_header_number(data, position, "maxval")
    if not 0 < width <= _MAX_DIMENSION or not 0 < height <= _MAX_DIMENSION:
        raise ValueError(f"PGM size {width}x{height} is out of range")
    if not 0 < maxval <= _MAX_MAXVAL:
        raise ValueError(f"PGM maxval {maxval} is out of range")
    if position >= len(data) or data[position] not in _WHITESPACE:
        raise ValueError("PGM maxval must be followed by one whitespace byte")
    raster = data[position + 1 :]

    count = width * height
    if magic == b"P5":
        sample_size = 2 if maxval > 255 else 1
        if len(raster) != count * sample_size:
            raise ValueError(f"P5 raster must hold exactly {count} samples of {sample_size} bytes")
        samples = tuple(raster) if sample_size == 1 else struct.unpack(f">{count}H", raster)
    else:
        if raster.translate(None, b"0123456789" + _WHITESPACE):
            raise ValueError("P2 raster may hold only decimal digits and whitespace")
        tokens = raster.split()
        if len(tokens) != count:
            raise ValueError(f"P2 raster must hold exactly {count} samples")
        samples = tuple(int(token) for token in tokens)

    if max(samples) > maxval:
        raise ValueError(f"PGM sample is above maxval {maxval}")
    return width, height, maxval, samples


def _real(name: str, value: object) -> float:
    """Return value as a finite float, with negative zero normalized to zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} is out of range") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return 0.0 if number == 0.0 else number


def _canonical_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Return the validated JSON object that the digest covers."""
    if not isinstance(metadata, dict):
        raise ValueError("map metadata must be a dict")
    for key in _REQUIRED_METADATA:
        if key not in metadata:
            raise ValueError(f"map metadata needs {key}")

    resolution = _real("resolution", metadata["resolution"])
    if resolution <= 0.0:
        raise ValueError("resolution must be above zero")

    origin = metadata["origin"]
    if not isinstance(origin, (list, tuple)) or len(origin) != 3:
        raise ValueError("origin must have exactly 3 entries")
    origin = [_real(f"origin[{index}]", value) for index, value in enumerate(origin)]

    negate = metadata["negate"]
    if type(negate) is not int or negate not in (0, 1):
        raise ValueError("negate must be the integer 0 or 1")

    occupied = _real("occupied_thresh", metadata["occupied_thresh"])
    free = _real("free_thresh", metadata["free_thresh"])
    if not 0.0 <= free < occupied <= 1.0:
        raise ValueError("thresholds must satisfy 0 <= free_thresh < occupied_thresh <= 1")

    mode = metadata.get("mode", "trinary")
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError("mode must be trinary, scale or raw")

    return {
        "resolution": resolution.hex(),
        "origin": [value.hex() for value in origin],
        "negate": negate,
        "occupied_thresh": occupied.hex(),
        "free_thresh": free.hex(),
        "mode": mode,
    }


def map_digest(pgm: bytes, metadata: dict[str, object]) -> str:
    """Return the lowercase 64-character canonical SHA-256 of a map.

    pgm holds the image's bytes, not a path. metadata holds the map YAML's
    values, already parsed by the caller, which also rejects duplicate keys.
    Keys other than the six that the digest covers are ignored.
    """
    canonical = _canonical_metadata(metadata)
    width, height, maxval, samples = decode_pgm(pgm)
    normalized = [(sample * 65535 + maxval // 2) // maxval for sample in range(maxval + 1)]

    digest = hashlib.sha256(ALGORITHM.encode("ascii") + b"\0")
    digest.update(struct.pack(">II", width, height))
    for start in range(0, len(samples), _SAMPLES_PER_UPDATE):
        chunk = samples[start : start + _SAMPLES_PER_UPDATE]
        digest.update(struct.pack(f">{len(chunk)}H", *map(normalized.__getitem__, chunk)))
    digest.update(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def make_map_id(arena: str, date: str, digest: str) -> str:
    """Return <arena>-<YYYY-MM-DD>-<digest[:8]> after validating all three.

    The 8-character suffix names a directory. It is not a collision-proof gate:
    compare the full digest for that.
    """
    if not isinstance(arena, str) or _ARENA.fullmatch(arena) is None:
        raise ValueError("arena must match [a-z][a-z0-9_-]*")
    if not isinstance(date, str) or _DATE.fullmatch(date) is None:
        raise ValueError("date must be YYYY-MM-DD")
    datetime.date.fromisoformat(date)
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError("digest must be 64 lowercase hexadecimal characters")
    return f"{arena}-{date}-{digest[:8]}"
