# ADR-0008: Store coordinates in the frame of a named map

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19

**Context**

A position only means something in the frame it was measured in. The robot
builds a new map for each venue, and sometimes again on a later day at the
same venue. A cup at (2, 3) in one map is somewhere else in the next one.

**Decision**

- Every row with a position belongs to a row in `map`, whose `frame_id` is
  the TF frame of its coordinates, usually `map`.
- Coordinates are metres in that frame. The node transforms incoming data
  into it with TF before storing
  ([ADR-0012](/architecture/decisions/0012-per-detection-clouds-any-frame.md)).
- A new map gets a new id, for example `arena-2026-10-19`. Rooms and objects
  from an old map are kept until that map is deleted.

**Consequences**

- Deleting a map deletes its rooms, its objects and their index rows
  (`ON DELETE CASCADE` and the index triggers).
- Every position query must filter by map. The vector tables carry `map_id`
  as metadata for this.
- Moving memory from one map to another needs the transform between the two
  maps. That is not planned.
- Relocalizing in the same map keeps every memory valid.

**See also:** [The tables](/architecture/database.md#the-tables)
<!-- --8<-- [end:body] -->
