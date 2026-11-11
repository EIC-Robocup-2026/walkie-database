# ADR-0028: A seeded anchor owns its box, whatever the detector calls what is inside

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-12

**Context**

A seeded anchor is a fixed track with a box and no points. A detection matches
it only under its own label
([ADR-0025](/architecture/decisions/0025-merge-by-overlap-and-label.md)), and
anchors of different labels never merge. The detector names furniture
inconsistently. In the Chula world it calls a strip of the sofa a table, in
every pass. Seeding the sofa then leaves a second anchor, a "table" inside the
sofa's box, and objects on the sofa can be stated on either one.

Furniture does not share its space. An anchor track that lies inside a seeded
anchor's box is the seeded anchor under another name.

**Decision**

At upkeep, an anchor track merges into a seeded anchor when at least
`seed_containment` (0.8) of its points lie in the seeded anchor's box, grown by
`match_distance`. The label does not matter. The seeded anchor keeps its box and
its label, and it counts the absorbed sightings under its own label.

Only a seeded box is trusted for this. A box the memory measured can be wrong,
as when a sofa and a coffee table merge into one. Movable objects are never
absorbed, since they stand on furniture and inside its box.

**Consequences**

- With no seeded anchors, nothing changes.
- On the `chula-drive-01` replay, seeding the sofa and the coffee table puts
  the mug on the coffee table, so 8 of 8 expected `on` relations are right
  (7 without a seed). Seeding all nine pieces of furniture leaves one anchor per
  piece (1.22 without the rule) and states 12 `on` relations, the shelf objects
  included.
- A seeded anchor takes in furniture that stands inside its box. A chair pushed
  under a seeded table, with 80% of its points in the table's box, merges into
  the table unless the chair is seeded too.
- The seeded label is final. A seeded anchor's label is not corrected by what
  the detector sees.

**See also:** [Object memory: upkeep](/architecture/object-memory.md#5-upkeep)
<!-- --8<-- [end:body] -->
