# ADR-0020: Identify a map by its pixels and metadata, and keep seed lineage

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-11-05

**Context**

Every coordinate in the database belongs to a named map
([ADR-0008](/architecture/decisions/0008-coordinates-in-a-named-map.md)), and
`map_id` is any string. Nothing shows that two copies with the same name hold the
same map. A store seeded on one map could run on another, and object positions
would be wrong with no error.

A hash of the YAML and PGM files changes on a harmless re-save. A name alone does
not change when the map does. This repository, walkie-ros's launcher and baseline
tools, and walkie-agent-ros's episode harness all need the same answer, and
separate implementations drift.

The same work settles what a seeded store carries from one test to the next.

**Decision**

The map hash, in effect now:

- `src/walkie_map_id.py` is owned here. It imports only the standard library,
  reads no files and parses no YAML. The module is packaged at the top level,
  beside `walkie_database`.
- Algorithm `walkie-map-v1` is SHA-256 over `b"walkie-map-v1\0"`, width and height
  as big-endian `uint32`, each decoded pixel as a big-endian `uint16`
  (`(sample*65535 + maxval//2)//maxval`), then compact JSON with sorted keys and
  no trailing newline.
- The JSON keys are `resolution`, `origin`, `negate`, `occupied_thresh`,
  `free_thresh` and `mode`. Real numbers are binary64 `float.hex()` strings, with
  negative zero as zero. `mode` defaults to `trinary` when it is missing.
- The image path, whitespace, key order, comments and other metadata do not
  count.
- `make_map_id` builds `<arena>-<YYYY-MM-DD>-<digest[:8]>`. The suffix names a
  directory. The **full digest** is the gate, because 8 characters are too weak to
  prove anything.
- The caller parses YAML and rejects duplicate keys. Here,
  `walkie_database.map_config` does it. Other repositories use their own safe
  parser with this exact module.
- Every consumer pins a commit of this repository, obtains this file verbatim and
  records the file's SHA-256. Nobody reimplements it.

Decided now, and built later with the seed tools and the startup gate:

- **One gate.** Only the database checks the map, at start-up. A launcher does not
  repeat the check. A preflight reads `GetStoreStatus`. A node that fails the gate
  does not start, so a preflight may see no service and read the start-up log.
- **IDs are never reused within a map's lineage.** An offline reset keeps the ID
  high-water mark and gets a fresh `store_id`, and the agent is relaunched between
  episodes. A restored seed is logical contents plus that metadata, not a
  bit-identical copy.
- **Only rooms and anchors cross tests.** Events and movable objects do not. The
  online `ArenaReset` only reduces belief in active movable objects: it keeps their
  age and held skill state, and it never restores a file.
- **Seed tools need explicit paths and a replace flag.** A fresh-seed marker
  refreshes timestamps once, after the first clock sample. A sim needs a ROS clock
  sample before fresh seeded anchors are stamped.

**Consequences**

- Pixel parsing and float canonicalization are now versioned public behaviour. A
  change needs a new algorithm version, new map IDs and a reseed. Old IDs are never
  silently reinterpreted.
- A consumer needs a source pin of this repository, even if it imports nothing else
  from it. Moving the module later is possible with the golden tests.
- The parser is strict on purpose, because accepting more later is easy and
  rejecting more later would change which maps work. It rejects a comment right
  after maxval, a comment in a P2 raster, vertical tabs and form feeds, trailing
  bytes, and a `negate` that is not the integer 0 or 1.
- PyYAML reads `5e-2`, with no dot, as a string, so the wrapper rejects it. `0.05`
  and `5.0e-2` work. A map file with that form must be rewritten once.
- The node still accepts any `map_id`, so nothing enforces this yet. A map row has
  no digest column yet either.
- Restoring a store from a file while its node is stopped will be an exception to
  [ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md), and gets
  its own ADR with those tools.
  [ADR-0016](/architecture/decisions/0016-sim-snapshot-per-episode.md) is revised
  then too.

**See also:** [Map identity](/architecture/database.md#map-identity),
`tests/fixtures/map_id/golden.json`
<!-- --8<-- [end:body] -->
