# ADR-0029: An anchor the memory made is not stored while most of its detections are thin slabs

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-12

**Context**

The detector names walls, partitions and open doors as furniture. In the Chula
world it calls the diagonal partition and the room divider `chair` or `cabinet`,
and the open entrance door and a piece of wall `cabinet`. Each one becomes an
anchor, and a room listing then names chairs and cabinets that are not there. On
the `chula-drive-01` replay with the furniture seeded, the four anchors that
stand for no furniture were all of these. In a live run, the agent told a person
about "two cabinets near the entrance": the door and a piece of wall.

The merged cloud cannot tell them from furniture. Localization drift between
views spreads one wall over parallel layers: the diagonal partition, 0.08 m
thick, had a stored box from 0.04 to 0.31 m thick in three live runs, and 0.83
by 0.31 m in one, the shape of a chair. A single detection comes from one camera
pose, so no drift is in it. On the replay, measured across the main horizontal
direction of its points with the box's 2% trim, the median detection of each wall
and door spanned 0.027 m or less, and of each piece of furniture 0.10 m or more,
also for a partial view of the TV desk and a strip of the sofa.

Seeding every piece of furniture and making no anchors from detections would also
remove them, but the memory could then not find furniture missing from the seed.
Asking the detector for `wall` and `door`, which the memory already drops
(`ignore_labels`), depends on the detector telling them apart.

**Decision**

The memory measures each detection that joins an anchor track it made, when the
detection has at least `slab_min_points` points (50, after downsampling). The
detection is thin when its points span less than `slab_thickness` (0.05 m)
across their main horizontal direction. The track is not confirmed before
`confirm_views` of its detections were measured, nor while more than half of
them are thin, however often it is seen. It stays in memory while it is seen, so
views that show its depth later confirm it, and it is dropped like any
unconfirmed track once it has not been seen for `ghost_age_s`. Seeded anchors,
anchors already in the store and movable objects are never held back.
`slab_thickness` is a node parameter, and 0 turns the rule off;
`slab_min_points` is a `MergeConfig` field.

The point floor came from the replay. The room divider's first seven detections
were fragments of 14 to 34 points near its end, each 0.05 to 0.07 m across, and
they stored it as a cabinet before any view of its face. Nine in ten detections
of each piece of furniture had 50 points or more, and the only thin detections of
furniture, three of a table called a chair, had fewer.

**Consequences**

- On the `chula-drive-01` replay with the furniture seeded, no anchor stands for
  no furniture (4 before), and the objects, furniture and `on` relations score as
  before. Without a seed, the four such anchors, on the two partitions and the
  door, are gone too, and the furniture (9 of 9 at 1.22 anchors each) and the `on`
  relations score as before.
- Furniture seen only as sparse fragments, from afar or mostly hidden, is never
  measured and never stored.
- The live runs' stores keep only merged clouds, so the rule was checked there
  against their boxes, not their detections. A new live run is the check.
- A cabinet or a wardrobe seen mostly head-on is a slab too. It is stored once at
  least half of its measured detections show its depth, which may be never if it
  stands in a corner.
  Seeding it avoids that.
- The values come from simulated depth, which has no noise. A real depth camera
  spreads a wall's points along each ray, so a real recording must set
  `slab_thickness`.
- An anchor stored before the rule stays. Restoring the store from the seed
  removes it.

**See also:** [Object memory: upkeep](/architecture/object-memory.md#5-upkeep),
[ADR-0028](/architecture/decisions/0028-seeded-anchors-own-their-box.md)
<!-- --8<-- [end:body] -->
