# ADR-0027: Tune the object memory by replaying a recording offline, through the node's own code

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-12

**Context**

Merging cannot be re-run from the database, so tuning needs a recording
([ADR-0011](/architecture/decisions/0011-merge-clouds-in-memory.md)). Playing a
bag with `ros2 bag play` runs at the bag's speed. Each run needs a router, a
node, TF and a fresh store, and it scores one setting. Tuning the matching
([ADR-0025](/architecture/decisions/0025-merge-by-overlap-and-label.md)) took
dozens of settings.

**Decision**

`tools/replay_memory.py` (`pixi run memory-replay <bag>`) reads the bag in order
with `rosbag2_py` and runs the node's steps, in one process with no ROS graph:

1. It checks each array with `ingress.check_observation`, against the latest
   `/clock` sample.
2. It looks the array up in TF at its exact stamp, once 50 ms more of the bag has
   been read. That is the node's lookup timeout.
3. It turns the array into map-frame detections with
   `object_intake.map_detections`, the function the node calls.
4. It merges them with `ObjectMemory.add`.
5. After every merge period of `/clock`, it runs `maintain` and
   `object_memory_store.save` into a scratch store, as the node's timer does.

`--set NAME=VALUE` overrides a `MergeConfig` field. `--seed` starts from a copy
of a seed store, with its anchors loaded as the node loads them after
`db-restore`. `--truth` scores the confirmed objects against a JSON file of
known boxes in the map frame. `--json` writes the summary and every track.

**Consequences**

- One run of the 2600 arrays of `chula-drive-01` takes 60 to 80 s, about 18 s of
  it reading the bag. A sweep runs several settings in parallel processes.
- No ROS graph, node or real store is touched. The tool writes only a scratch
  store in a temporary folder, and the `--json` file. A `--seed` store is only
  read.
- The result is the best the node could do on that recording. The node's
  subscription keeps one array and can drop arrays when it falls behind, and the
  tool never drops one. On the drive, the live node and the replay agreed: 41
  objects and 1 relation, with the settings of the time.
- A truth file is made by hand for each world. `tools/truth/chula_pat14_6.json`
  is the Chula world's, with boxes measured from its MuJoCo model.
- The bag must hold the detection topic, `/tf`, `/tf_static` and `/clock`.

**See also:** [Replay a recording offline](/setup/15-scene-graph-demo.md#step-4-replay-the-recording-offline)
<!-- --8<-- [end:body] -->
