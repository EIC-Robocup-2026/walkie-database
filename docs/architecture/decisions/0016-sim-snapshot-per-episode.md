# ADR-0016: Restore a seeded sim database for every harvest episode

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-22

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

**Decision**

- A new module, `walkie_database.seed_sim`, run as `pixi run db-seed-sim`
  (new), builds a sim seed from walkie-agent-ros's locations file for the
  world (planned: `config/locations.chula_pat14_6.toml`) and the world's
  own `world.toml`, the only source of the room outlines. The world file
  sits on a walkie-ros branch that is not merged to main, so its path may
  change. Both files, the map id and the output path are arguments.
- The seed holds one `map` row, with `frame_id` `map`, a fixed
  `created_at` and a description that names the source files. It holds one
  `room` row for each room in `world.toml`, with the stand pose and
  description of the location of the same name, and the room's outline in
  `room_shape`. It holds nothing else: no objects, people, faces, sessions,
  conversations or vectors.
- The builder fills a database in memory and writes it with `VACUUM INTO`,
  which refuses a path that exists. It writes nothing when a room has no
  location of the same name, or when a stand pose is outside its outline.
- Once per run, the harness builds the seed in the run folder and records
  its SHA-256. For each episode it:
    1. stops the worker's database node together with the sim stack;
    2. copies the seed to a new file in the episode's folder;
    3. starts the node on the worker's domain and router, unremapped, with
       `--ros-args -p db_path:=<that file> -p map_id:=chula_pat14_6`;
    4. calls `/walkie_database/find_objects` until it answers with
       `success` true, and only then starts the agent.
- An episode's file is never reused, and nothing is restored into a file
  that a node has open. A plain copy is safe here because the seed is
  closed and has no `-wal` file, unlike the live database.
- The node, the schema and the messages do not change.

**Consequences**

- Every episode starts from the same memory, and no episode, worker or real
  run sees another's.
- In the harvest's first version (v0), nothing writes during an episode,
  so `find_objects` answers `Found 0 objects.` and the agent moves on to
  detection. The restore starts to matter once object memory ingests
  detections in sim.
- Each episode pays one node start. It is not measured, and it can overlap
  the sim's readiness wait.
- The harness runs `db-node` and `db-seed-sim` from a checkout at the
  commit walkie-agent-ros pins. Under rmw_zenoh_cpp 0.2.11, a node built
  from another `FindObjects` definition never answers (ADR-0015), so the
  probe in step 4 needs a timeout.
- Room names must agree between the two source files. The builder fails
  when they do not.
- The seed holds no personal data, so a harvest trace that quotes
  `find_objects` holds none from this database.
- A change to the builder's arguments needs walkie-agent-ros's pin bumped.

**See also:** [Sim Harvest Support](/architecture/harvest.md),
[The node's interface](/architecture/database.md#the-nodes-interface),
[Object Memory](/architecture/object-memory.md)
<!-- --8<-- [end:body] -->
