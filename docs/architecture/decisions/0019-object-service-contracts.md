# ADR-0019: Freeze the object services in new types, pinned to walkie-vision

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-11-05

**Context**

Object memory, its agent tools and its skills need services that do not exist:
filtered search, stateless localization, room and event reads, and state writes.
The fusion core behind some of them comes later. If each service got its type
only when its handler lands, every consumer would rebuild once per service, and
agent work would wait for the core.

`RememberedObject` and `FindObjects` are in use by walkie-agent-ros. Adding
fields to them changes their type hashes, and a client built from the old
definition then hangs (see
[ADR-0015](/architecture/decisions/0015-messages-in-this-repository.md)).

`LocalizeObjects`, `SubmitDetections` and `ReportSighting` carry a whole
`walkie_vision_msgs/ObjectDetectionArray`, the camera-frame observation that
walkie-vision publishes. walkie-vision has no dependency on this repository
(its ADR-0013).

**Decision**

- All object interfaces are new types. `RememberedObject`, `ModelVector`,
  `FindObjects` and the people and session types keep their exact fields and
  hashes.
- The new types are `OperationStatus`, `ObjectSummary`, `ObjectQuery`,
  `ObjectMatch`, `SearchPrior`, `Surface`, `RoomSummary`, `ObjectIdChange`,
  `ObjectUpdates`, `LocalizedDetection` and `Event`, and the services
  `LocalizeObjects`, `SubmitDetections`, `SearchObjects`, `GetObject`,
  `FindNear`, `ListRooms`, `GetRoomContents`, `SetObjectState`,
  `ReportSighting`, `ArenaReset`, `RememberEvent`, `FindEvents`,
  `DescribeObject` and `GetStoreStatus`. They are all defined now, so a later
  handler changes no type.
- A type existing does not mean its service is served. Each server replies
  through `OperationStatus`. A write whose core is not built replies
  `NOT_READY`. It never claims to have accepted the input.
- New write requests carry a nonempty `request_id`, `task_id` and `cause`.
  Repeating a request ID with the same payload returns the recorded outcome. A
  different payload is `CONFLICT`.
- `ros2_ws.repos` pins walkie-vision to one full commit.
  `tools/import_interfaces.py` clones that commit, detached, into
  `ros2_ws/src/walkie-vision`, which Git ignores. It refuses a commit other than
  the pin, and refuses a clone with changes. The build compiles
  `walkie_vision_msgs` first, then `walkie_database_msgs`. This repository
  installs no vision Python project, models or environment.
- The dependency runs one way: database on vision. So the pin cannot form a
  cycle.

**Consequences**

- `pixi run import-interfaces` must run before `pixi run build-msgs`. The import
  fails until the pinned commit can be fetched, either from GitHub or from a
  local repository with `--vision-source` and `--vision-ref`.
- **This changes one line of ADR-0015's consumer instructions.** A repository
  that builds `walkie_database_msgs` must now also build `walkie_vision_msgs`, at
  a commit with the same type hashes as this repository's pin. Its build list and
  pins need updating when it moves past this commit.
- A change to `ObjectDetection` or `ObjectDetectionArray` in walkie-vision also
  changes the hash of the three services here that nest it. Both repositories
  and every consumer must then rebuild together.
- The old `find_objects` stays as limited as it is. A consumer selects a new
  capability to get the new fields. The legacy endpoint is cheap to remove once
  no client uses it.
- `tests/test_interfaces.py` pins the fields, defaults and hashes of every type.
  A change to one of these pins is a wire contract change.

**See also:** [The object contract](/architecture/database.md#the-object-contract),
[Pin vision and freeze the object interfaces](/setup/10-object-interfaces.md)
<!-- --8<-- [end:body] -->
