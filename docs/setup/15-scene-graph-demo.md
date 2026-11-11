# 15. Run the Object Memory on the Simulation

**Goal:** merge walkie-vision's detections from the MuJoCo Chula world into stored
objects with `on` relations, watch them in Foxglove or RViz, and replay a recording
offline to tune and score the merging.

## Background

Step 12 checks each `ObjectDetectionArray` and stores nothing. This step adds the
object memory: matching, merging, upkeep, top surfaces, `on` relations, markers and
a cloud. [Object Memory](../architecture/object-memory.md) has the exact rules.
[ADR-0025](../architecture/decisions/0025-merge-by-overlap-and-label.md) explains
the matching without embeddings,
[ADR-0026](../architecture/decisions/0026-anchor-surfaces-and-on-relations.md) the
surfaces, and [ADR-0027](../architecture/decisions/0027-replay-a-recording-offline.md)
the replay.

| File | What it holds |
| ---- | ------------- |
| `src/walkie_database/object_memory.py` | `MergeConfig`, `Track` and `ObjectMemory`: matching, merging and upkeep, with no ROS and no SQL |
| `src/walkie_database/object_intake.py` | `map_detections`: a checked array to map-frame detections, for the node and the replay |
| `src/walkie_database/object_memory_store.py` | Saves tracks, surfaces and `on` relations in one transaction, and loads them at start |
| `src/walkie_database/object_markers.py` | The `MarkerArray` and the `PointCloud2` |
| `tools/replay_memory.py` | The offline replay and its score (`pixi run memory-replay`) |
| `tools/truth/chula_pat14_6.json` | The Chula world's furniture and objects, as boxes in the map frame |

`scipy` and `scikit-learn` are new dependencies, for the KD-tree and DBSCAN.

!!! warning "The world and its locations are not on `main` yet"
    The Chula world is the package `walkie_mujoco_worlds` in walkie-ros
    (`simulation/mujoco/worlds/chula_pat14_6`), and its locations file is
    `config/locations.chula_pat14_6.toml` in walkie-agent-ros. Neither is merged
    into its repository's `main`. Use checkouts that have them.

!!! tip "One command for step 2 and the Foxglove bridge"
    walkie-agent-ros's `scripts/chula-sim.sh up --vision --memory --foxglove`
    starts the simulation, vision, this node and a Foxglove bridge on
    `ws://localhost:8799`, all on a private graph, and `scripts/chula-sim.sh down`
    stops them with this node before vision. Its Foxglove layout,
    `config/foxglove/chula-memory-layout.json`, already shows both topics of step 3.
    The script is not on walkie-agent-ros's `main` yet; its page there is
    `docs/setup/08-chula-simulation.md`. Step 1 is still needed. The steps below
    start each part by hand.

## Step 1: Seed and restore the Chula store

Name the two rooms in a file of your own:

```yaml title="room-names.yaml"
schema_version: 1
rooms:
  living_room: living room
  kitchen_room: kitchen room
```

Build the seed from the world and restore a store from it, as in
[step 11](11-seed-restore-and-map-gate.md). `W` is the world's folder in walkie-ros,
and `A` is the walkie-agent-ros checkout:

```bash
W=<walkie-ros>/simulation/mujoco/worlds/chula_pat14_6
A=<walkie-agent-ros>
pixi run db-seed-sim --map-folder $W/map --arena chula --date 2026-11-12 --clock-kind ros_sim \
    --world-source $W/authoring/source/30_16102026_ChulaPat14_6_world.toml \
    --locations $A/config/locations.chula_pat14_6.toml \
    --room-names room-names.yaml --output /tmp/chula/seed.db
pixi run db-restore --seed /tmp/chula/seed.db --db /tmp/chula/walkie.db \
    --map-folder $W/map --clock-kind ros_sim
```

This seed holds only the two rooms, so the memory finds every piece of furniture
itself. Its map id is `chula-<date>-<digest>`, where the last 8 characters come from
the map's digest. The node refuses any other id, and its message names the right
one.

## Step 2: Run the node in memory mode

Start the simulation, its localization and walkie-vision's detector first. Their
commands live in walkie-ros and walkie-vision. The node needs the arrays on
`/walkie_vision/sim/objects`, and `map` in TF at each array's stamp. Then:

```bash title="Terminal 1"
pixi run db-memory -p db_path:=/tmp/chula/walkie.db -p map_folder:=$W/map \
    -p map_id:=chula-2026-11-12-714b087d -p use_sim_time:=true \
    -p objects_topic:=/walkie_vision/sim/objects
```

Drive the robot past the furniture. An object is stored once three arrays have shown
it (`confirm_views`), and the saves follow every 2 s (`merge_period_s`). The other
parameters are in
[Object Memory: Parameters](../architecture/object-memory.md#parameters).

Stop the node with Ctrl-C. It saves once more before it exits, and exits 0.

## Step 3: Watch it in Foxglove or RViz

The node publishes `/walkie_database/objects/markers` (boxes, labels and `on` lines)
and `/walkie_database/objects/cloud` (every object's points, coloured by label), both
in the `map` frame and transient local.

**Foxglove:** run the bridge from the walkie-ros environment, which has
`ros-jazzy-foxglove-bridge`:

```bash title="Terminal 2, in walkie-ros"
pixi run ros2 launch foxglove_bridge foxglove_bridge_launch.xml
```

Open Foxglove, connect to `ws://localhost:8765`, add a **3D** panel with the display
frame `map`, and turn on both topics.

**RViz:** add a **MarkerArray** display for the markers and a **PointCloud2** display
for the cloud, with the colour transformer **RGB8**. On both, set **Durability
Policy** to **Transient Local**, or RViz shows nothing until the next change.

Ask the node, as in [step 13](13-object-queries-and-events.md#step-2-ask-it-for-objects):

```bash title="Terminal 3"
pixi run ros2 service call /walkie_database/search_objects walkie_database_msgs/srv/SearchObjects \
    "{query: {room: 'kitchen room'}}"
```

The objects on the kitchen table come back in the kitchen room, each with an `on`
relation to the table once the table's surface is measured.

## Step 4: Replay the recording offline

Record the detections with TF and the clock while the simulation runs:

```bash
pixi run ros2 bag record -s mcap -o chula-drive-01 \
    /walkie_vision/sim/objects /tf /tf_static /clock
```

Then replay the recording through the object memory as often as you like. It needs
no ROS graph and no node, and writes only a scratch store in a temporary folder:

```bash
pixi run memory-replay chula-drive-01 --truth tools/truth/chula_pat14_6.json
pixi run memory-replay chula-drive-01 --truth tools/truth/chula_pat14_6.json \
    --set match_distance=0.15 --set confirm_views=2 --json /tmp/replay.json
```

`--set NAME=VALUE` overrides any `MergeConfig` field. `--json` writes the summary,
the settings and every track. One run of the 2600 arrays of `chula-drive-01` takes
60 to 80 s. The summary it prints:

| Key | What it counts |
| --- | -------------- |
| `arrays`, `rejected`, `tf_misses` | Arrays read, refused by the checks (by reason), and dropped for a missing transform |
| `detections`, `with_embedding` | Detections merged, and how many had a vector |
| `confirmed`, `relations` | Stored objects and `on` relations at the end |
| `objects_found` | Known objects that a movable track stands for, out of all known objects |
| `tracks_per_found_object` | How many tracks each found object split into. 1.0 is best |
| `movable_unmatched` | Movable tracks that stand for no known object |
| `furniture_found`, `anchors_per_found_furniture`, `anchors_unmatched` | The same for furniture and anchors |
| `on_expected_and_found` | Found objects that rest on known furniture |
| `on_recalled`, `on_stated`, `on_right` | Of those, how many got the right `on`. All `on` relations stated. Those that are right |

??? info "How the score matches a track to a known box"
    A movable track stands for the known object whose centre is within 0.15 m of
    the track's centre, seen from above, when their heights overlap
    (`--object-radius`). An anchor stands for the known furniture whose footprint
    shares at least 30% of the smaller one with it (`--furniture-overlap`). An
    object should rest on the furniture whose outline holds its centre and whose
    top is 2 cm above to 5 cm below its bottom.

With the defaults, `chula-drive-01` gives 10 of 12 objects at 1.0 track each, 9 of 9
pieces of furniture at 1.22 anchors each, and 7 right `on` relations out of 8, with
none wrong. The two objects missed are on the shelf's inner level and on the bar.

To score another world, write its own truth file: a JSON object of named boxes, each
with `kind` (`furniture` or `object`), `min` and `max` in the map frame.

## Step 5: The tests

```bash
pixi run test tests/test_object_memory.py tests/test_object_memory_store.py \
    tests/test_object_memory_node.py tests/test_object_markers.py tests/test_node_shutdown.py
```

| File | What it checks |
| ---- | -------------- |
| `tests/test_object_memory.py` | Downsampling, boxes and clusters, matching with and without vectors, anchors' names, ignored labels, partial views, confirming, ghosts, surfaces, duplicates and seeded anchors |
| `tests/test_object_memory_store.py` | What a save writes, tombstones, a top surface and an `on` relation, a lifted cup losing it, and a restart merging into the stored objects |
| `tests/test_object_memory_node.py` | Arrays to a stored object that `search_objects` finds, a missing transform, one view not being enough, markers and cloud for a late subscriber, and a restarted node |
| `tests/test_object_markers.py` | The `on` line ends on the anchor's surface, and a relation to an object that is not shown draws nothing |
| `tests/test_node_shutdown.py` | A second Ctrl-C does not cut the last save short, and Ctrl-C exits 0 when rclpy is already shut down |

`tests/test_node_shutdown.py` runs the node in its own process with
`tests/shutdown_harness.py`, and sends SIGINT to its process group, as a terminal
does.

!!! note "What has not run"
    One drive through the simulated Chula world, with walkie-vision's sim detector,
    ran live and was recorded. All values are tuned on that recording. No real
    camera, no real robot and no other world has fed the memory. No collision
    boxes reach MoveIt, and confirmed objects never expire
    ([Known limits](../architecture/object-memory.md#known-limits)).
