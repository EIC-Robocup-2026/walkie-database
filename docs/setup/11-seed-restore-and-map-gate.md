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

To change a seed later, build a new one at another path and name the old one, so that
repeated anchors keep their IDs. `--output` may not be the `--previous-seed`. Keep the
old seed until the new one has restored, because
[Step 3](#restore-the-new-seed) may send you back to it:

```bash
pixi run db-seed-store ... --previous-seed /tmp/arena/seed.db --output /tmp/arena/seed-2.db
```

A store restored from the old seed may have given out IDs of its own since, for
example to movable objects. If the new seed adds an anchor,
[Step 3](#restore-the-new-seed) can refuse it and name the store's high-water mark. Build the new seed again from the same old seed,
with `--id-floor` set to that number, so the new anchor's ID is above every one of
them:

```bash
pixi run db-seed-store ... --previous-seed /tmp/arena/seed.db --id-floor 42 \
    --output /tmp/arena/seed-2.db --replace
```

The floor moves only the keys that the previous seed lacks. A seed built from the
refused `seed-2.db` keeps the clashing ID, and Step 3 refuses it again. Without the old
seed, restore to a new path instead.

A sim world uses `pixi run db-seed-sim` instead. It takes `--world-source`,
`--locations` and `--room-names` in place of `--rooms`, and can measure the anchors
from the world's scene with `--scene` and `--furniture` in place of `--anchors`
([The sim seed](../architecture/harvest.md#the-sim-seed)).

## Step 3: Restore it to a store

With no node running on that file:

```bash
pixi run db-restore --seed /tmp/arena/seed.db --db /tmp/arena/walkie.db \
    --map-folder /tmp/arena/map --clock-kind wall --replace
```

The target may be a new path. An existing store must be of the same map, and its ID
counters carry over. The tool refuses while a node holds the store's lock.

### Restore the new seed {#restore-the-new-seed}

After a reseed or a floor rebuild in Step 2, restore the new seed, not the old one:

```bash
pixi run db-restore --seed /tmp/arena/seed-2.db --db /tmp/arena/walkie.db \
    --map-folder /tmp/arena/map --clock-kind wall --replace
```

Restoring `seed.db` again would succeed, but it would put back the old anchors and
never check the new seed's IDs against the store.

The restore refuses a seed that would give an ID the store has already given out to
another object, and names the store's high-water mark:

```text
Refused, and changed nothing: The seed /tmp/arena/seed-2.db gives the ID 3 to 'new_shelf', but the store's ID 3 is 'observed cup'. The store /tmp/arena/walkie.db has given out object IDs up to 42. Rebuild the seed with --id-floor 42 and, as --previous-seed, the seed this store was restored from, not this one, so that its new anchors get IDs above that. Or restore to a new path, which starts a new lineage.
```

Rebuild the seed with that `--id-floor` from the seed the store was restored from
(Step 2), then run the restore of `seed-2.db` above again. Or restore to a new path. A
new path starts its IDs again from the seed's counters.

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
    tests/test_scene_furniture.py tests/test_restore.py tests/test_map_gate.py
```

They write tiny map folders and input files into `tmp_path`, and need no robot or
world repository.

| File | What it checks |
| ---- | -------------- |
| `tests/test_store_lock.py` | A second holder is refused, a killed holder's lock is released, and the WAL is checkpointed before an install |
| `tests/test_seed_store.py` | A bad input writes nothing, the same inputs give the same rows, a reseed keeps anchor IDs and may not overwrite its previous seed, `--id-floor` puts new anchors above it, and no fake vector enters |
| `tests/test_seed_sim.py` | The adapter takes outlines from the world and poses from the locations, passes `--id-floor` on, refuses a blank-map locations file, and builds the same seed from `--scene` as from the anchors it prints |
| `tests/test_scene_furniture.py` | Boxes skip visual geoms, boards make surfaces counted up from the floor, lined-up boards join while an L of boards is refused, each anchor gets one room, and a rotated, jointed or non-box geometry is refused |
| `tests/test_restore.py` | A fresh `store_id`, the larger counters, a killed node's WAL recovered first, a seed that would give an issued ID to another object refused, the floor rebuild working only from the original seed, and a crash leaving a usable store |
| `tests/test_map_gate.py` | A wrong map, digest, baseline or clock stops the start, the default mode is unchanged, and people's times follow a fake `/clock` |

!!! note "What has not run"
    The tests use small synthetic maps on Fast DDS. No real map folder has been
    seeded, and the node has not run on the robot or in the sim in memory mode.
