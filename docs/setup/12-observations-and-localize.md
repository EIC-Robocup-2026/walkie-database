# 12. Check Observations and Localize Them

**Goal:** make the node receive vision's arrays and check them, serve
`LocalizeObjects`, and answer the object memory's writes with `NOT_READY`.

## Background

Until this step, nothing subscribed to `/walkie_vision/objects`. This step adds the
database's side of the boundary, without any fusion: a received array creates no
object row.
[Receive detections](../architecture/object-memory.md#1-receive-detections) has the
exact checks and statuses, and
[ADR-0012](../architecture/decisions/0012-per-detection-clouds-any-frame.md) explains
the listener's thread.

## Step 1: Run the node in memory mode

Build and restore a store as in
[step 11](11-seed-restore-and-map-gate.md), then:

```bash
pixi run db-memory -p db_path:=/tmp/arena/walkie.db -p map_folder:=/tmp/arena/map \
    -p map_id:=arena-2026-11-05-1a2b3c4d -p objects_topic:=/walkie_vision/objects
```

`objects_topic` already has that default. A simulated vision publisher uses
`/walkie_vision/sim/objects`.

## Step 2: Understand what the node does with an array

It counts the array, checks it and keeps it in one slot if it passes. A refused array
leaves the slot as it was. The log says why it was refused, at most once every 5
seconds:

```text
[WARN] [walkie_database]: Refused an array: Detection 0: The store has no vector table for the model 'fake_clip'. Known models: clip_vit_b16.
```

## Step 3: Localize an array

`LocalizeObjects` takes a whole `ObjectDetectionArray`, so call it from code that has
one. It stores nothing. It needs TF from the array's frame to the map at the array's
exact stamp, and it waits at most 50 ms for it.

## Step 4: The tests

```bash
pixi run test tests/test_ingress.py tests/test_localize.py
```

They need no robot and no camera. They teach the buffer, or publish to `/tf`, with
synthetic poses.

| File | What it checks |
| ---- | -------------- |
| `tests/test_ingress.py` | Each rule of the checks, the JPEG header reader, and that the slot keeps only the newest valid array |
| `tests/test_localize.py` | A moving camera uses its pose at each stamp, with no fallback to the latest. Indexes stay in order. Stale and future arrays, a clock reset, the real `/tf` topic, zero rows after arrays on the topic, and `NOT_READY` writes that keep nothing |

!!! note "What has not run"
    The tests use synthetic arrays and transforms on Fast DDS. No real camera, robot
    or sim has sent the node an array, and real vision has not run against it.
