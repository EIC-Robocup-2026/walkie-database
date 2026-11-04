# ADR-0016: Restore a seeded sim database for every harvest episode

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-22 · **Revised:** 2026-11-05

The database's side is built
([ADR-0023](/architecture/decisions/0023-offline-restore-and-map-gate.md)). The
harness's side is not, so this stays Proposed. The revision replaces the plain copy
with the restore tool, and the fixed map name with a computed one.

**Context**

walkie-agent-ros proposes a
[sim harvest](https://eic-robocup-2026.github.io/walkie-agent-ros/architecture/harvest/):
parallel workers run many short episodes of its self-evolving agent in
walkie-ros's MuJoCo simulation, first in the world `chula_pat14_6`. Every
episode relaunches the sim from the map origin and starts a fresh agent.
The agent asks memory first, through its `find_objects` tool, so the
harness needs a database node in every worker, restored from a snapshot
for every episode.

Three facts of this repository shape how:

- `FindObjects` fails, with `success` false, unless the map it searches is
  in the `map` table
  ([ADR-0015](/architecture/decisions/0015-messages-in-this-repository.md)).
  An empty database makes every lookup fail instead of finding nothing.
- One node owns and writes each file
  ([ADR-0005](/architecture/decisions/0005-one-node-owns-the-database.md)),
  in WAL mode ([ADR-0004](/architecture/decisions/0004-one-sqlite-file-wal.md)).
  Proposed object memory keeps live objects in RAM and writes them back
  every few seconds and on shutdown
  ([ADR-0011](/architecture/decisions/0011-merge-clouds-in-memory.md)). A
  file replaced under a running node can be overwritten by the node's next
  write, or corrupted when it gets paired with the node's old `-wal` file.
- `pixi run db-seed` writes only `data/demo.db`: the sample house
  `demo-house`, with made-up people and a conversation. Nothing builds a
  database for a sim world.

We considered:

- **One database for the whole run.** What one episode writes would be
  found by the next, and by other workers. A success could depend on an
  earlier episode.
- **Delete rows after each episode.** Needs a list of every table an
  episode may write, which grows with each feature, and misses what the
  node still holds in RAM.
- **A reset service on the node.** The robot would get a service that
  wipes memory, which generated code at the Unrestricted level could call
  unless the agent's never-list names it.
- **One node per worker for the whole run.** Correct only while nothing
  writes, and it breaks silently once object memory does. The relaunch
  also checks that no node is left on the worker's domain.
- **A copy of the robot's `data/walkie.db`.** It is meant to hold people,
  faces and conversations from real runs
  ([ADR-0010](/architecture/decisions/0010-session-owns-people-and-conversations.md)),
  and it uses the robot's map, not the sim world's.
- **No database.** Every episode would test only "memory unavailable".
- **A plain copy of the seed to a new file per episode.** This was the first
  version of this decision. Each new file would start its ID counters again from
  the seed's, so an object ID in one episode's trace could name another object in
  the next. A restore to one path per worker keeps IDs unique across the worker's
  episodes, and it recovers whatever a killed node left in its `-wal`.

**Decision**

- `pixi run db-seed-sim` runs `walkie_database.seed_sim`. It takes the map folder,
  the arena, the date, `--clock-kind ros_sim` and the output path, and also the
  world's authoring file, a locations file and a room-names file. All are explicit
  arguments. The map ID is computed from the map's digest
  ([ADR-0020](/architecture/decisions/0020-map-identity-and-seed-lineage.md)), and
  never given by hand.
- The world's authoring file gives the room keys and outlines, and nothing else. It
  sits on a walkie-ros branch that is not merged to main, so its path may change.
  The room-names file maps each key to a display name, such as `living_room` to
  `living room`. The locations file gives the stand pose and description at exactly
  that display name. The builder reads no coordinate from the world's room poses.
- The seed holds one `map` row, a `store_meta` row, and one `room` row and outline
  for each room. It holds keyed anchors, surfaces and catalogue rows only when a
  reviewed file supplies them. It holds no L3 objects, people, faces, sessions,
  conversations or events, and no synthetic vectors. The world's furniture polygons
  are single points, and they never become anchors.
- The builder checks every input before it creates the output, and writes nothing
  when a check fails. An existing output needs `--replace`, and only a seed can be
  replaced.
- Once per run, the harness builds the seed in the run folder and records its
  SHA-256. Each worker has one store path. For each episode the harness:
    1. stops the worker's database node together with the sim stack;
    2. runs `pixi run db-restore --seed <seed> --db <worker's store>
       --map-folder <folder> --clock-kind ros_sim --replace`;
    3. starts the node on the worker's domain and router, unremapped, with
       `pixi run db-memory -p db_path:=<worker's store> -p map_folder:=<folder>
       -p map_id:=<computed id> -p use_sim_time:=true`;
    4. calls `/walkie_database/find_objects` until it answers with `success` true,
       and only then starts the agent. It treats an exit of the node as a failed
       start.
- The tool and the node share the store's lock, so a restore into a file that a
  node holds open is refused
  ([ADR-0023](/architecture/decisions/0023-offline-restore-and-map-gate.md)).

**Consequences**

- Every episode starts from the same rows, and no episode, worker or real run sees
  another's. IDs differ between episodes, because the counters carry. A scenario
  must not depend on a literal object ID.
- In the harvest's first version (v0), nothing writes during an episode,
  so `find_objects` answers `Found 0 objects.` and the agent moves on to
  detection. The restore starts to matter once object memory ingests
  detections in sim.
- Each episode pays one restore and one node start. Neither is measured, and the
  node start can overlap the sim's readiness wait.
- The harness runs these tasks from a checkout at the commit walkie-agent-ros pins.
  Under rmw_zenoh_cpp 0.2.11, a node built from another `FindObjects` definition
  never answers (ADR-0015), so the probe in step 4 needs a timeout.
- The room names must agree across the three source files. The builder fails when
  they do not. `config/locations.sim.toml` in walkie-agent-ros describes a blank-map
  demo, and the builder refuses it. The world needs its own locations file.
- A wrong path or map stops the node's start, so a failed episode cannot be blamed
  on an empty store.
- The seed holds no personal data, so a harvest trace that quotes
  `find_objects` holds none from this database.
- A change to the builder's arguments needs walkie-agent-ros's pin bumped.

**See also:** [Sim Harvest Support](/architecture/harvest.md),
[Seeds, restore and the map gate](/architecture/database.md#seeds-restore-and-the-map-gate),
[The node's interface](/architecture/database.md#the-nodes-interface),
[Object Memory](/architecture/object-memory.md)
<!-- --8<-- [end:body] -->
