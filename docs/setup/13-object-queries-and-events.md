# 13. Serve Object Queries, the Task's Log and the Snapshot

**Goal:** answer the object reads, `RememberEvent`, `FindEvents` and
`GetStoreStatus` from the stored rows, and publish the seeded snapshot, without
merging any detection.

## Background

Step 10 froze the types, and step 11 seeds a store with rooms, anchors and the
catalogue. This step reads those rows. A received array still creates no object row,
and the five object writes still answer `NOT_READY`.
[Object queries](../architecture/database.md#object-queries) has the exact rules,
[ADR-0021](../architecture/decisions/0021-rooms-and-object-queries.md) explains the
filtered scan, and [ADR-0022](../architecture/decisions/0022-task-event-log.md) the
event's pose and retry.

| File | What it holds |
| ---- | ------------- |
| `src/walkie_database/queries.py` | `ObjectRepository`: filters, ranking, belief, relations, superlatives, priors, rooms, the store status and the snapshot |
| `src/walkie_database/events.py` | `remember_event`, with its checks, its retry and its pose |
| `src/walkie_database/results.py` | Records to messages, and the snapshot's JSON |
| `src/walkie_database/geometry.py` | `FramePose`, a frame's pose in the map, for distances and directions |
| `src/walkie_database/node.py` | The handlers, the new parameters and the snapshot publisher |

## Step 1: Run the node in memory mode

Build and restore a store as in [step 11](11-seed-restore-and-map-gate.md), then
start the node as in [step 12](12-observations-and-localize.md). Three read-only
parameters are new:

| Parameter | Default | What it does |
| --------- | ------- | ------------ |
| `reference_frames` | `[base_footprint]` | The frames a query may measure from |
| `robot_frame` | `base_footprint` | The frame whose pose an event records |
| `thing_cosine.clip_vit_b16` | 0.25 | The CLIP cosine that can stand in for a thing's words. A prototype value |

## Step 2: Ask it for objects

Ask for the store status first. It answers in both modes, so it tells a client
which mode the node is in:

```bash title="Terminal 2"
pixi run ros2 service call /walkie_database/get_store_status walkie_database_msgs/srv/GetStoreStatus
```

Then search, for example for something blue in the living room:

```bash title="Terminal 2"
pixi run ros2 service call /walkie_database/search_objects walkie_database_msgs/srv/SearchObjects \
    "{query: {room: 'living room', attributes: [{key: colour, value: blue}]}}"
```

A catalogue item that no object stands for yet comes back in `priors`, with the room
of its usual place.

## Step 3: Read the snapshot

The snapshot is transient local, so subscribe with that durability, or the
subscriber gets nothing:

```bash title="Terminal 2"
pixi run ros2 topic echo --once --qos-durability transient_local --qos-reliability reliable \
    /walkie_database/objects/snapshot
```

In a sim, it appears after the first `/clock` sample.

## Step 4: The tests

```bash
pixi run test tests/test_queries.py tests/test_events.py tests/test_object_services.py
pixi run test -s tests/test_query_benchmark.py
```

They need no robot, no camera and no model. Every vector is a made-up direction.

| File | What it checks |
| ---- | -------------- |
| `tests/test_queries.py` | Each filter, attribute results, the score and belief, missing boxes and weights, relations, superlatives, vectors and the word fallback, priors, merged and discarded ids |
| `tests/test_events.py` | The pose and room of a line, a missing transform, retries and conflicts, refusals that write nothing, people and sessions, forgetting a session, and the search |
| `tests/test_object_services.py` | Every handler over the middleware, the parameters, `NOT_READY` without memory mode or before the first clock sample, the five writes that keep nothing, and the snapshot through a late subscriber |
| `tests/test_query_benchmark.py` | Complete answers over 500 synthetic objects, checked against a plain filter. With `-s` it prints the median and 95th percentile of each query, and asserts no time |

!!! note "What has not run"
    The tests use a synthetic store and synthetic transforms on Fast DDS. No robot,
    sim or real vision has called these services, and the timings are from a
    laptop, outside a running node. The ranking constants are not tuned on real
    phrases. The `ros2` commands above were not run while writing this step: the
    tests call the services and read the snapshot from Python.
