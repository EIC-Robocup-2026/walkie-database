# ADR-0018: Match people with an exact scan, one result per person

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-26

**Context**

walkie-agent-ros's `remember_person` stores about 20 face samples and 20
appearance samples per guest. Its `recognize_person` names several people
at once, so for each query it needs several different people, each with
the distance of their nearest sample. sqlite-vec's KNN search
(`MATCH ... AND k`) returns samples, not people:

- The `k = 5` query that the database page showed before took the 5
  nearest samples, then grouped them by person. When one person had 5
  close samples, it found 1 person out of 30.
- `k` is capped at 4096. To stay exact, `k` must be at least the number of
  candidate samples.
- The face table has no session column. Guests of other sessions would
  count toward `k` and be dropped only after it, so a search would return
  fewer people than asked for.

**Decision**

- For each query, the node computes the cosine distance to every candidate
  sample of the query's model in SQL (`vec_distance_cosine`), groups the
  samples by person, and keeps each person's nearest. People come nearest
  first, and people at the same distance in the order of their id.
- A call takes at most 32 queries, and returns at most 100 people per
  query.
- A vector names its model. The node refuses, naming the field, a model
  with no vector table of its kind, the wrong number of values, NaN or
  infinity, and all zeros. One bad query refuses the whole call. Every
  vector is scaled to length 1 before it is stored or compared.
- `start_session` returns the models the node takes, with their lengths,
  so that a caller can leave out vectors of other models instead of being
  refused.
- The node never decides whether a query is a stranger. The caller applies
  its own limit for each model.

**Consequences**

- The result is exact at any size, and the session filters are plain SQL.
- The cost grows with the number of samples, at about 10 µs per sample,
  because each embedding is read out of its vec0 table. Per query, that is
  5 ms for 600 samples and 55 ms for 5,000. The worst call at competition
  scale, 32 queries over 600 samples, takes about 160 ms, and the node
  answers one call at a time.
- For thousands of samples, the node can read the samples once per call and
  compare them with NumPy: 62 ms for 32 queries over 5,000 samples. That
  needs no interface change.
- The scan must stay outside the joins, with `MATERIALIZED` and
  `CROSS JOIN`. With a plain `JOIN`, SQLite planned the appearance query to
  read the scanned samples once per appearance: 1.1 s instead of 54 ms at
  5,000 samples. A test checks the plan.
- Unit length accepts raw model outputs as they are, and keeps the stored
  rows ready for a dot-product or L2 search later.
- A refused model is a caller or configuration bug, such as new weights
  that arrive before the migration that adds their table. A refusal fails a
  whole remember or match, so callers filter by the model lists first.

**See also:** [`match_faces` and `match_appearances`](/architecture/database.md#match_faces-and-match_appearances),
[Vectors](/architecture/database.md#vectors)
<!-- --8<-- [end:body] -->
