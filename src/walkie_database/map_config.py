"""Read a ROS map YAML and its image, for the canonical map digest.

walkie_map_id stays free of dependencies, so YAML parsing lives here. This
module rejects what a plain safe_load would let through: a repeated key, where
the last value would silently win.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from walkie_map_id import map_digest

_MERGE_TAG = "tag:yaml.org,2002:merge"


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
                    raise ValueError("map YAML must not use merge keys")
                key = self.construct_object(key_node, deep=True)
                try:
                    repeated = key in seen
                except TypeError as error:
                    raise ValueError("map YAML has a key that is not a scalar") from error
                if repeated:
                    raise ValueError(f"map YAML repeats the key {key!r}")
                seen.add(key)
        return super().construct_mapping(node, deep)


def parse_map_yaml(text: str | bytes) -> dict[str, object]:
    """Return the top-level mapping of a map YAML, or raise ValueError."""
    try:
        document = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as error:
        raise ValueError(f"map YAML is malformed: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("map YAML must be a mapping")
    return document


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
