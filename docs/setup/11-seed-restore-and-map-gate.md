# 11. Seed Tools, Restore and the Map Gate

**Goal:** build a store's seed from reviewed files, reset a stopped store to it, and
run the node so that it refuses the wrong store.

## Background

Until this step, the node opened any file at `db_path`, and created one if it was
missing. A typo gave an empty store that looked healthy. `db-seed` writes a made-up
house, which is good for learning and wrong for a robot.

This step adds tools that build a store offline, and a mode of the node that checks
the store before it starts.
[Seeds, restore and the map gate](../architecture/database.md#seeds-restore-and-the-map-gate)
explains the rules, and
[ADR-0023](../architecture/decisions/0023-offline-restore-and-map-gate.md) explains
why. The node's default behaviour does not change: `pixi run db-node` still needs no
map.

All paths are arguments, and nothing here writes to `data/` unless you pass such a
path. The commands below use a scratch folder.

## Step 1: Prepare the inputs

You need a map folder, with `map.yaml` and its image. An optional `manifest.yaml`
names its baseline. You also need a rooms file, and anchors and catalogue files, whose
forms are in
[The input files](../architecture/database.md#the-input-files).

A rooms-only seed is valid. Its anchors and catalogue files hold empty lists:

```yaml title="anchors.yaml"
schema_version: 1
map_id: arena-2026-11-05-1a2b3c4d
baseline_rev: ""
anchors: []
```

```yaml title="catalogue.yaml"
schema_version: 1
objects: []
```

The `map_id` in these files must be the computed one. Its last 8 characters come from
the map's digest. A first run with any `map_id` is refused, and the message names the
ID to use.

## Step 2: Build the seed

```bash
pixi run db-seed-store --map-folder /tmp/arena/map --arena arena --date 2026-11-05 \
    --rooms rooms.yaml --anchors anchors.yaml --catalogue catalogue.yaml \
    --clock-kind wall --output /tmp/arena/seed.db
```

`--clock-kind` is `wall` for the robot and `ros_sim` for a store that runs under
`use_sim_time`. A bad input prints `Refused, and wrote nothing:` with the reason, and
exits with code 1.

To change a seed later, build it again and name the old one, so that repeated anchors
keep their IDs:

```bash
pixi run db-seed-store ... --previous-seed /tmp/arena/seed.db --output /tmp/arena/seed.db --replace
```

A sim world uses `pixi run db-seed-sim` instead. It takes `--world-source`,
`--locations` and `--room-names` in place of `--rooms`
([The sim seed](../architecture/harvest.md#the-sim-seed)).

## Step 3: Restore it to a store

With no node running on that file:

```bash
pixi run db-restore --seed /tmp/arena/seed.db --db /tmp/arena/walkie.db \
    --map-folder /tmp/arena/map --clock-kind wall --replace
```

The target may be a new path. An existing store must be of the same map, and its ID
counters carry over. The tool refuses while a node holds the store's lock.

## Step 4: Run the node in memory mode

```bash
pixi run db-memory -p db_path:=/tmp/arena/walkie.db -p map_folder:=/tmp/arena/map \
    -p map_id:=arena-2026-11-05-1a2b3c4d
```

Add `-p use_sim_time:=true` on a `ros_sim` store. The node then waits for `/clock`
before it stamps the anchors, and `start_session` and `remember_person` fail with the
`The database could not` prefix until the first sample.

Any mismatch logs `Not starting:` with the reason, and exits with code 1:

```text
[FATAL] [walkie_database]: Not starting: The store holds the map 'other-2026-11-05-1a2b3c4d', not 'arena-2026-11-05-1a2b3c4d'.
```

## Step 5: The tests

```bash
pixi run test tests/test_store_lock.py tests/test_seed_store.py tests/test_seed_sim.py \
    tests/test_restore.py tests/test_map_gate.py
```

They write tiny map folders and input files into `tmp_path`, and need no robot or
world repository.

| File | What it checks |
| ---- | -------------- |
| `tests/test_store_lock.py` | A second holder is refused, a killed holder's lock is released, and the WAL is checkpointed before an install |
| `tests/test_seed_store.py` | A bad input writes nothing, the same inputs give the same rows, a reseed keeps anchor IDs, and no fake vector enters |
| `tests/test_seed_sim.py` | The adapter takes outlines from the world and poses from the locations, and refuses a blank-map locations file |
| `tests/test_restore.py` | A fresh `store_id`, the larger counters, a killed node's WAL recovered first, and a crash leaving a usable store |
| `tests/test_map_gate.py` | A wrong map, digest, baseline or clock stops the start, the default mode is unchanged, and people's times follow a fake `/clock` |

!!! note "What has not run"
    The tests use small synthetic maps on Fast DDS. No real map folder has been
    seeded, and the node has not run on the robot or in the sim in memory mode.
