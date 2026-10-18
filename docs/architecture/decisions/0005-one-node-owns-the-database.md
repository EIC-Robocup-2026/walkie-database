# ADR-0005: One ROS 2 node owns and writes the database

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-19

**Context**

Perception, the agent, RViz and MoveIt all need memory. They are separate
processes, some in other repositories, and all of them already speak
ROS 2. If each opened the file itself, each would need the schema, the
merging logic and sqlite-vec, and SQLite would serialize their writes.

**Decision**

The `walkie_database` node is the only program that writes `walkie.db`.
Other programs store and search memory through its ROS 2 topics and
services, and receive objects as standard ROS messages
([ADR-0013](/architecture/decisions/0013-publish-boxes-to-rviz-and-moveit.md)).
Tools that only look, such as `pixi run db-shell`, may open the file
read-only.

**Consequences**

- The ROS 2 messages are the interface. The schema can change without
  changing any caller.
- Merging object clouds can keep state in memory, because one process sees
  every detection
  ([ADR-0011](/architecture/decisions/0011-merge-clouds-in-memory.md)).
- When the node is down, nothing can store or search memory.
- The node runs on ROS 2's single-threaded executor, because a Python
  `sqlite3` connection works only on the thread that opened it. A slow query
  delays every other callback, so queries must stay fast.

**See also:** [Run it as a ROS 2 node](/setup/05-ros-node.md)
<!-- --8<-- [end:body] -->
