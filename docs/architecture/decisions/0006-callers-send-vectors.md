# ADR-0006: Callers send vectors, the database runs no models

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-19

**Context**

Searching by meaning needs embeddings: CLIP for objects and rooms, ArcFace
for faces, OSNet for appearance. Perception already runs these models on
the GPU to make its detections. Running them again inside the database node
would load each model twice and bring PyTorch and CUDA into this
repository.

**Decision**

Callers compute the vectors and send them with the name of the model that
made them. The database stores and searches vectors, and never computes
one. It refuses a vector from a model it has no table for, or with the
wrong length.

**Consequences**

- This repository has no PyTorch and no GPU dependency, and installs in
  minutes.
- Every caller must use the same model, with the same settings, as the
  vectors already stored. A different model needs a new vector table
  ([ADR-0007](/architecture/decisions/0007-sqlite-vec-one-table-per-model.md)).
- A text search, such as "a red mug", needs CLIP's text encoder. The caller
  (the agent, or a perception service) must run it.

**See also:** [The search indexes](/architecture/database.md#the-search-indexes)
<!-- --8<-- [end:body] -->
