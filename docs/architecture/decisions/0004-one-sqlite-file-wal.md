# ADR-0004: Keep all memory in one SQLite file in WAL mode

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-19

**Context**

Walkie must remember conversations, people, objects and rooms, and search
them by meaning, by position and by words. The previous version kept its
vectors in ChromaDB, opened by several processes at once, and the store was
corrupted.

The options we compared:

- **PostgreSQL with pgvector and PostGIS.** Does everything, but is a server
  to install, start, upgrade and back up on the robot.
- **A vector database** (ChromaDB, Qdrant, Milvus). Good at vectors only.
  Positions and words need a second store, and keeping two stores in step
  is its own problem.
- **SQLite with sqlite-vec.** One file and no server. R*Tree (boxes),
  Geopoly (polygons) and FTS5 (words) are built in, and sqlite-vec adds
  vectors.

**Decision**

All memory is in one SQLite file, `data/walkie.db`, in WAL mode.
`connect()` in `src/walkie_database/db.py` sets the same options on every
connection: WAL, `synchronous=NORMAL`, foreign keys on, and a 5 second busy
timeout.

**Consequences**

- One transaction can change an object, its box and its vector together, so
  the indexes never disagree with the tables.
- In WAL mode, readers never block the writer and the writer never blocks
  readers. Only one process writes at a time
  ([ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md)).
- The file must be on a local disk. WAL does not work over network file
  systems.
- A plain `cp` of the file can miss recent changes, which are still in the
  `-wal` file. Back up with `VACUUM INTO`.
- `synchronous=NORMAL` can lose the last commits in a power cut, but never
  corrupts the file. Losing a few seconds of memory is acceptable here.
- Vector search compares every vector, with no approximate index
  ([ADR-0007](/architecture/decisions/0007-sqlite-vec-one-table-per-model.md)).

**See also:** [Add SQLite and sqlite-vec](/setup/03-sqlite.md),
[What lives where](/architecture/database.md#what-lives-where)
<!-- --8<-- [end:body] -->
