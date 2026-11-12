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

The memory measures each detection that joins an anchor track it made, on its
points at least `slab_floor_clearance` (0.05 m) above the floor of the map frame.
When their largest DBSCAN cluster holds at least `slab_cluster_share` (80%) of
them, it measures only that cluster and the clusters within `slab_cluster_gap`
(0.5 m) of it. It needs at least `slab_min_points` (50, after downsampling) such
points. The detection is thin when they span less than
`slab_thickness` (0.05 m) across their main horizontal direction. The track is not confirmed before
`confirm_views` of its detections were measured, nor while more than half of
them are thin, however often it is seen. It stays in memory while it is seen, so
views that show its depth later confirm it, and it is dropped like any
unconfirmed track once it has not been seen for `ghost_age_s`. Seeded anchors,
anchors already in the store and movable objects are never held back.
`slab_thickness` is a node parameter, and 0 turns the rule off;
`slab_min_points`, `slab_floor_clearance`, `slab_cluster_share` and
`slab_cluster_gap` are `MergeConfig` fields. When a
detection fits a seeded anchor and another track equally well, it joins the
seeded anchor.

The point floor came from the replay. The room divider's first seven detections
were fragments of 14 to 34 points near its end, each 0.05 to 0.07 m across, and
they stored it as a cabinet before any view of its face. Nine in ten detections
of each piece of furniture had 50 points or more, and the only thin detections of
furniture, three of a table called a chair, had fewer.

The floor clearance, the clusters and the tie came from the first live run with
the rule. The diagonal partition's track was dropped as a ghost, and a second later
the detector named the partition's south-west end a chair again. Three masks of
that end also took in the seat of a stool, a cluster of its own 0.96 to 1.04 m
from the partition's, with 9 to 13% of their points above the floor. Two of them
also took in the floor in front of the partition, 25 to 28% of all their points,
which falls in the partition's cluster. They measured 0.27 to 0.45 m deep. Three
views of the seat alone then fitted the seeded stool and that new track equally
well, and the later track took them. Three of its nine detections were thin, and
it was stored as a chair. Measured as above, all six detections of the partition
are thin, and the seat joins the stool.

The share and the gap keep furniture seen as parts apart whole, as far as the
recordings show. The two Chula recordings hold 1737 detections with at least half
their points above the floor in a box of the truth file. Measuring the largest
cluster alone made 34 of the shelf's thin (a side panel is a slab) and left 34
others unmeasured, 29 of them of the kitchen table and a stool, whose legs stand
apart. With the share at 0.8 and the gap at 0.5 m, none of the 1737 changes its
verdict from measuring all its points, and the three masks of the partition's end
are thin. At a share of 0.75 a detection of the shelf turns thin, and at 0.9 two
of the three masks stay thick. Any gap from 0.15 to 0.7 m gives the same verdicts;
the gap keeps a chair's seat seen beside its larger back, a separate cluster a few
centimetres to a few decimetres away, which the share alone would leave out.

**Consequences**

- On the `chula-drive-01` replay with the furniture seeded, no anchor stands for
  no furniture (4 before), and the objects, furniture and `on` relations score as
  before. Without a seed, the four such anchors, on the two partitions and the
  door, are gone too, and the furniture (9 of 9 at 1.22 anchors each) and the `on`
  relations score as before.
- Furniture seen only as sparse fragments, from afar or mostly hidden, is never
  measured and never stored.
- On the recording of the first live run with the rule, no anchor stands for no
  furniture (1 with the rule as first written, 2 without the rule), and the objects
  and `on` relations score as before.
- A wall measures deep in a view whose mask also takes in something within 0.5 m
  of it, or more than a fifth of its points above the floor elsewhere. A piece of
  furniture measures thin in a view where a slab-like part of it, such as a
  chair's back, holds four fifths of the points and the rest lies over 0.5 m away.
  The rule counts views: such furniture is held back only when this happens in
  more than half of its measured detections, and such a wall is stored only when
  it happens in at least half.
- A cabinet or a wardrobe seen mostly head-on is a slab too. It is stored once at
  least half of its measured detections show its depth, which may be never if it
  stands in a corner.
  Seeding it avoids that.
- The values come from simulated depth, which has no noise. A real depth camera
  spreads a wall's points along each ray, so a real recording must set
  `slab_thickness`. The floor clearance takes the floor to be at z 0 in the map
  frame.
- An anchor stored before the rule stays. Restoring the store from the seed
  removes it.

**See also:** [Object memory: upkeep](/architecture/object-memory.md#5-upkeep),
[ADR-0028](/architecture/decisions/0028-seeded-anchors-own-their-box.md)
<!-- --8<-- [end:body] -->
