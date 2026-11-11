# ADR-0026: Measure an anchor's top surface from its points, and derive `on` from geometry

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-12

**Context**

Surfaces and `on` relations are rows
([ADR-0021](/architecture/decisions/0021-rooms-and-object-queries.md)), and the
seed tools give seeded anchors their surfaces. An anchor that the object memory
builds from detections has no surface, so nothing could rest on it.

A table's mask takes in the things on the table, so its merged cloud does too.
The top of its box is the top of the tallest thing on it, for example a bottle at
0.97 m on a 0.75 m table. A surface at the box top would leave every object
below it.

**Decision**

- **Surface height.** In each round of upkeep, an anchor that changed gets the
  height of its highest dense layer of points:
    1. Sort the points' heights into 2 cm bins (`surface_bin`).
    2. Count each bin together with its two neighbours.
    3. Take the highest bin that holds at least 30% (`surface_min_share`) of the
       fullest bin's count.
    4. Among that bin and its neighbours, take the bin with the most points of its
       own. The surface height is the median height of those points.
- **The surface row.** The store gives each anchor the memory made one surface,
  with the key `object-<id>-top`. Its outline is the box seen from above, and its
  height is the measured one. The box top is used only when no height was
  measured. The same row moves with each save.
- **The `on` relation.** At each save, every confirmed movable object with a box
  gets the usable surface, seeded or measured, that meets all of these:
    - its outline contains the object's centre, seen from above;
    - the object's bottom is at most 0.06 m below its height or 0.10 m above it;
    - it is the nearest in height of the surfaces that pass.

    The object keeps one `on` row, weight 0.9, which names the surface. An object
    that matches no surface loses its `on` row.

**Consequences**

- In the Chula drive, the kitchen table and the bar measured 0.759 m against a
  true 0.75 m, and the stools about 0.505 m against 0.49 m.
- Relations follow the geometry: an object lifted off a table loses its relation
  at the next save, and nothing stores a relation that the boxes contradict.
- Only the top surface is found. A shelf's inner levels are not, so an object
  inside a shelf gets no relation (`chips_bag_2` in the drive). The memory never
  stores `inside`.
- An anchor whose highest dense layer is not where things rest, such as a sofa
  with a broad backrest, gets its surface there.
- Seeded anchors keep their seeded surfaces, and the memory never moves them.

**See also:** [Object memory: upkeep](/architecture/object-memory.md#5-upkeep)
<!-- --8<-- [end:body] -->
