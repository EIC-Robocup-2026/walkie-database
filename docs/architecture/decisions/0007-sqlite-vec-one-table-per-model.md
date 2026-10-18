# ADR-0007: Search vectors with sqlite-vec, one table per model

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19

**Context**

sqlite-vec adds `vec0` tables to SQLite: a vector column, a distance metric,
and metadata columns that filter during the search. It compares the query
with every vector, with no approximate index. It is at version 0.1, so a
minor release may change its SQL.

Vectors from two models cannot be compared, even when they have the same
length: a 512-number CLIP vector and a 512-number ArcFace vector mean
different things.

**Decision**

- Each model gets its own `vec0` table, named `<what>_<model>`, for example
  `object_clip_vit_b16` and `face_arcface`. All use cosine distance.
- A vector table's rows share ids with the table they index, and carry as
  metadata the columns that searches filter on, such as `map_id` and
  `room_id`.
- A new model means a new migration with a new table. The old table stays
  until every row has a vector from the new model.
- `sqlite-vec-python` is pinned below 0.2.

**Consequences**

- A model cannot be swapped by accident: a vector for the wrong table is
  refused by name ([ADR-0006](/architecture/decisions/0006-callers-send-vectors.md)).
- A search takes about 7 ms for 10,000 vectors and 75 ms for 100,000 on a
  laptop. Walkie's memory stays far below that, but a table that grows past
  about 100,000 rows needs a different index.
- Metadata columns cannot hold `NULL`, so `0` means "none", and a trigger
  keeps them in step with their table.
- A failed write to a `vec0` table can leave half a row behind, so every
  write must be inside a transaction.
- Moving to sqlite-vec 0.2 is a deliberate change, made after the tests pass.

**See also:** [Add SQLite and sqlite-vec](/setup/03-sqlite.md),
[Example queries](/architecture/database.md#example-queries)
<!-- --8<-- [end:body] -->
