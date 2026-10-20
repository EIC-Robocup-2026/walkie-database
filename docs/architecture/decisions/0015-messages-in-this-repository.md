# ADR-0015: Keep the node's interfaces in a package in this repository

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-20

**Context**

Other programs reach memory through the node's services and topics
([ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md)),
so the node needs message and service types of its own, starting with
`FindObjects`. `src/` holds the importable Python package, in the PyPA src
layout, and pip installs it. colcon builds rosidl interfaces only from an
`ament_cmake` package, so the types cannot live in `src/`. walkie-vision
keeps `walkie_vision_msgs` the same way, in its own `ros2_ws/src/`.

Every position query must stay inside one map
([ADR-0008](/architecture/decisions/0008-coordinates-in-a-named-map.md)).
Callers often do not know which map the robot is localized in: the agent
knows only the label it is looking for.

**Decision**

- The node's messages and services are in `walkie_database_msgs`, an
  `ament_cmake` package in `ros2_ws/src/` of this repository. It holds only
  interface files.
- The package holds the types the node serves. A message the node receives
  from another repository, such as walkie-vision's `ObjectDetection`, stays
  in its producer's package, and this repository builds that package too.
- `pixi run build-msgs` builds it with colcon. `scripts/activate.sh` adds
  the build to every later `pixi run` and `pixi shell`.
- Other repositories, first walkie-agent-ros, clone this repository with
  vcstool, pinned to a commit, and build only this package with
  `colcon build --packages-select walkie_database_msgs`.
- `FindObjects` searches exactly one map: the one the request names, or
  else the node's `map_id` parameter, the map the robot is localized in. The
  call fails, with `success` false, when both are empty, or when the map is
  not in the `map` table.
- `RememberedObject` carries the object's whole box (centre, rotation and
  size), its detection count and when it was last seen, so that the next
  consumer does not need a new field.

**Consequences**

- `build-msgs` must run before the tests. Pixi computes a task's
  environment before its dependencies run, so `test` cannot depend on
  `build-msgs`. `tests/conftest.py` stops and says what to run, and CI
  builds the package in a separate step.
- A change to a message or service definition changes its type hash
  (`RIHS01_...`). With rmw_zenoh_cpp 0.2.11, a client built from another
  definition still passes `wait_for_service`, but its requests are never
  answered, and `ros2 service call` hangs the same way (observed
  2026-10-19). A change to comments only keeps the hash.
- So an interface change needs the consumers' pins bumped, and both sides
  rebuilt and restarted.
- Query results use this package's types, such as `RememberedObject`. The
  "standard ROS messages" of ADR-0005 are the RViz and MoveIt topics of
  [ADR-0013](/architecture/decisions/0013-publish-boxes-to-rviz-and-moveit.md).
- On the robot, the node must run with `-p map_id:=<map>`, the map the
  robot is localized in. Otherwise every request that names no map fails,
  and walkie-agent-ros's `find_objects` tool names none. The node reads the
  parameter on every request, so `ros2 param set` changes it at once.
- A wrong map id fails with a message that names it, instead of finding
  nothing. So "nothing remembered" and "wrong map" look different to the
  caller.

**See also:** [Add the message package](/setup/08-message-package.md),
[The node's interface](/architecture/database.md#the-nodes-interface)
<!-- --8<-- [end:body] -->
