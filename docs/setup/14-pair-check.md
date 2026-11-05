# 14. Check the Pair Locally Before Pushing

**Goal:** prove, on one computer, that a walkie-vision commit and a walkie-database
commit build the same types and talk to each other, before either commit is on GitHub.

## Background

This repository pins a walkie-vision commit in `ros2_ws.repos`
([step 10](10-object-interfaces.md)). Remote CI clones that pin from GitHub, so it
cannot test a pin that is not pushed yet. Vision's CI cannot test an unpushed
database commit either. `tools/pair_check.py` is the local stand-in: it checks two
local worktrees against each other.
[ADR-0024](../architecture/decisions/0024-local-pair-gate.md) explains why it builds
each side in its own environment, and why it checks hashes before any process meets
another on the wire.

| File | What it holds |
| ---- | ------------- |
| `tools/pair_check.py` | The gate. It uses only the Python standard library, so the host's `python3` runs it |
| `tools/contract_peer.py` | The peers the gate starts: probes, the database node, vision's fake publisher, vision's services with fakes, and the clients of both |
| `tests/fixtures/pair/` | A tiny map, two rooms, two anchors and one catalogue entry, for the gate's seed |
| `tests/test_pair_check.py` | The gate's own tests, which start no gate |

## Step 1: Commit both sides

The gate refuses a worktree with changes, untracked files included, and a worktree
that is not at the commit it is given. The pin in `ros2_ws.repos` and the clone in
`ros2_ws/src/walkie-vision` must both be at the vision commit. So commit vision first,
pin its full SHA here, run `pixi run import-interfaces`, and commit the pin.

If the vision commit is not on GitHub yet, import it from the local worktree as
[Import it](10-object-interfaces.md#step-3-import-it) shows.

## Step 2: Run the gate

Run it from this repository, with the host's `python3`, not inside `pixi run`:

```bash
wv_commit=$(git -C ../walkie-vision rev-parse HEAD)
wd_commit=$(git rev-parse HEAD)
env RMW_IMPLEMENTATION=rmw_fastrtps_cpp ROS_DOMAIN_ID=78 ROS_LOCALHOST_ONLY=1 \
    CUDA_VISIBLE_DEVICES= HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    python3 tools/pair_check.py --vision-root ../walkie-vision --database-root . \
    --vision-commit "$wv_commit" --database-commit "$wd_commit" --domain 78 \
    --build --report /tmp/pair/pair.json
```

| Option | What it does |
| ------ | ------------ |
| `--vision-root`, `--database-root` | The two worktrees |
| `--vision-commit`, `--database-commit` | The full SHAs each worktree must be at |
| `--domain` | The ROS domain of every peer. Pick one nobody else uses |
| `--build` | Rebuilds both message installations and the client overlay. A run without it cannot pass |
| `--report` | The JSON report. Its folder also holds the client overlay and the run's files |
| `--forbid` | A folder no peer may import from. The default is `~/walkie-vision` and `~/walkie-database`, so a gate run on two other worktrees cannot pick up the owner's checkouts |
| `--pixi` | The `pixi` executable, if it is not on `PATH` |

Every peer runs with `pixi run --locked --clean-env` in its own repository. The gate
sets Fast DDS, localhost only, the domain, no GPU and offline models after the
environment's activation, so `scripts/activate.sh` cannot put a peer on Zenoh. The
gate starts no Zenoh router and calls no `ros2` command. It starts every process in
its own process group, and stops it by that process's ID: `SIGINT`, then `SIGTERM`,
then `SIGKILL`, each with a time limit.

With `--build`, a first run takes a few minutes: the client overlay is a full build of
`walkie_database_msgs`. Builds use two workers.

## Step 3: Read the report

The command prints one line per check, then `PASSED` or `FAILED`. It exits 0 only when
every check passed and every process it started has stopped.

| Check | What must hold |
| ----- | -------------- |
| `sources` | Both worktrees are clean at the given commits |
| `pin` | `ros2_ws.repos` and the imported clone are at the vision commit |
| `locks` | The SHA-256 of each `pixi.lock`, for the record |
| `build` | Vision's `walkie_vision_msgs`, this repository's two packages, and the client overlay all built |
| `probe.vision`, `probe.database` | Python, the source package and every message module come from that side's own builds. A path in the other worktree, the other environment or a `--forbid` folder fails |
| `hashes` | Every installed type hash agrees across the builds, nested types included |
| `control.hash`, `control.import` | The two negative controls in step 4 were caught |
| `seed` | The fixtures made a seed, and `restore` made a store from it |
| `observe.nonempty`, `observe.empty`, `observe.vector`, `observe.malformed` | The node accepted the good arrays, refused the fake-vector and the malformed ones, and counted each |
| `localize` | Nine `LocalizeObjects` cases: centroids at the exact stamp with a moving camera, and each failure in its own index |
| `database-services` | Vision's environment calls the seeded reads, `RememberEvent`, and the five writes that answer `NOT_READY` |
| `vision-services` | This repository's environment calls `DetectObjects`, `EmbedText`, `EmbedCrop` and `DescribeObject` on vision's fakes |
| `store-rows` | The run made no object row. It added one event, and only the anchors' first-start stamp changed |
| `processes`, `teardown` | Every process stopped |

The report's folder holds:

| Path | What it is |
| ---- | ---------- |
| `pair.json` | Every check with its evidence, the hash inventories, each probe's import paths, and each process's command, exit status and output |
| `client-overlay/` | `walkie_database_msgs`, built in vision's environment against vision's build of `walkie_vision_msgs` |
| `pair-run/` | The peers' JSON results, logs and the temporary seed and store. It is replaced on the next run |

The client overlay exists so that vision's peers never source this repository's
compiled environment. A Python module built against another interpreter or other
libraries can import and still misbehave, so the gate builds the types again instead.

## Step 4: See the negative controls fail

Every run checks two broken inputs, and passes only if both are caught:

- an altered hash for `walkie_vision_msgs/msg/ObjectDetectionArray` inside
  `walkie_database_msgs/srv/LocalizeObjects`;
- a vision probe that imports this repository's own build of `walkie_database_msgs`.

To see one fail the whole gate, add `--negative-control hash` or
`--negative-control import`. Without `--build`, the run reuses the client overlay in
the report's folder:

```bash
env ... python3 tools/pair_check.py ... --domain 78 \
    --negative-control hash --report /tmp/pair/pair-negative-hash.json
```

It exits 1, and every wire check is a skip: no peer with a wrong type ever joins the
domain.

## Step 5: The tests

```bash
pixi run test tests/test_pair_check.py
```

| What | What it checks |
| ---- | -------------- |
| Sources | A clean worktree, a short or other commit, a change or untracked file, an ignored file, and a dirty worktree that stops the gate before any process |
| Inventories | Every nested hash is read, a type with two hashes or a missing file is named, and this repository's installed types agree with their source |
| Import paths | Good evidence passes, and each kind of leak is named. A shared package cache is not a leak |
| Processes | A child that ignores `SIGINT`, or `SIGTERM` too, a forked grandchild, a timeout, and a child that already finished |
| Store rows | One event and the anchor stamp pass. Any other change fails |
| Peers | The camera poses, bit-exact `float32`, each observation phase's counters, and that the fixtures serve what the vision client expects |

!!! note "What has not run"
    The gate exchanges fake data on Fast DDS on one computer. No camera, model, GPU,
    sim or robot takes part, and no Zenoh router. A pass says the two builds agree on
    the types and on these exchanges. It says nothing about walkie-agent-ros or
    walkie-ros, which must rebuild and run their own checks
    ([What other repositories must do](../architecture/database.md#what-other-repositories-must-do)).
    Remote CI has not run the gate.
