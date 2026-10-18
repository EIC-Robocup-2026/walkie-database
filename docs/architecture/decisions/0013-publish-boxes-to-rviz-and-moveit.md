# ADR-0013: Publish confirmed objects to RViz and MoveIt as boxes

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19

**Context**

The team needs to see what Walkie remembers, and MoveIt needs the objects
as obstacles so the arms do not hit them. Both understand standard ROS
messages. A merged cloud is detailed, but MoveIt plans faster around simple
shapes, and a box that jitters between updates makes plans fail.

**Decision**

- Each object gets an upright box, fitted with PCA on `x` and `y` only.
- For RViz, the node publishes a `MarkerArray` (box and label per object)
  and a `PointCloud2` of all merged clouds, with transient local
  durability.
- For MoveIt, it publishes `PlanningScene` diffs with one
  `CollisionObject` box per object, with ids like `walkie_database/42`.
- Only confirmed objects, seen enough times, are sent.

**Consequences**

- Neither RViz nor MoveIt needs to know about this database.
- An upright box over-covers tilted or oddly shaped objects. A mesh from the
  cloud could replace it later for objects that need it.
- A grasp target is a collision box. Something must attach or remove it
  before the grasp, and the node must not send it back until it is released.
- MoveIt keeps world objects fixed in its planning frame. That frame must not
  be attached to the robot.

**See also:** [What other programs see](/architecture/object-memory.md#what-other-programs-see)
<!-- --8<-- [end:body] -->
