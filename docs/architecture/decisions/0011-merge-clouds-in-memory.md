# ADR-0011: Merge clouds in memory, store only the merged result

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19

**Context**

Every detection adds points to an object, and the object's cloud is
downsampled again after each merge. Perception may send several detections
per camera frame, many times a second. A detection's raw cloud can have
tens of thousands of points.

Keeping every raw cloud in SQLite would grow the file by megabytes per
minute. Rewriting each merged cloud on every detection would rewrite the
same blobs many times a second.

**Decision**

- The node keeps the live objects, with their clouds, in memory, and loads
  them from SQLite when it starts.
- Every few seconds, and on shutdown, it writes the objects that changed in
  one transaction.
- SQLite keeps the merged cloud (`object_cloud`) and one small row per
  detection (`object_observation`), without its points.
- To re-tune merging, record the detection topic with `ros2 bag` and
  replay it.
- Object ids do not wait for that write. They are reserved in blocks, in their
  own committed transaction with `synchronous = FULL`, before the node uses
  them. A block works only on the connection that reserved it, so the
  connection that writes the objects reserves their ids, and a restart
  reserves a new block. A merged or discarded object keeps its row and its id,
  as a tombstone.
  The fields and the allocator are in migration 0004 and
  `src/walkie_database/objects.py`.

**Consequences**

- A crash loses the merging of the last few seconds. It never gives an object id
  twice, and it can leave gaps in them.
- The file stays small: one downsampled cloud per object.
- Merging cannot be re-run from the database alone. Tuning needs a
  recording.
- Only the node holds the live objects, which is safe because it is the only
  writer ([ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md)).

**See also:** [Object memory](/architecture/object-memory.md)
<!-- --8<-- [end:body] -->
