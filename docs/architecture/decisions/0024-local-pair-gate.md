# ADR-0024: Check the vision and database pair locally, each side in its own environment

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-11-05

**Context**

`walkie_database_msgs` nests `walkie_vision_msgs`, from the walkie-vision commit that
`ros2_ws.repos` pins
([ADR-0019](/architecture/decisions/0019-object-service-contracts.md)). A type that
differs between the two sides changes its hash
([ADR-0015](/architecture/decisions/0015-messages-in-this-repository.md)), and the
mismatch shows only at run time, when the peers meet.

Remote CI cannot check a pair before it is pushed. This repository's CI clones the
pin from GitHub, and walkie-vision's CI cannot fetch a database commit at all. Each
repository has its own Pixi environment and lock, and the robot runs each in its own
environment.

We considered:

- **Push both sides, then let CI find out.** A broken pair reaches GitHub, and
  walkie-vision's CI still never sees the database's types.
- **One test in one environment.** It cannot show that each side imports its own
  build, and it would need a third environment and lock.
- **Source the database's install in vision's environment.** Its Python modules
  were built by another interpreter against other libraries. They can import and
  still misbehave, and the import would pass for the wrong reason.
- **Compare the hashes in the generated files' comments.** The installed
  `share/<package>/{msg,srv}/*.json` files hold the `type_hashes` that ROS uses,
  nested types included.
- **A negative control with a wrong-typed node on the graph.** It could disturb the
  other peers, and other people's ROS processes run on the same computers.

**Decision**

- **One standard-library tool.** `tools/pair_check.py` runs with the host's
  `python3`. It takes both worktrees, the full commit each must be at, a domain and
  a report path. `tools/contract_peer.py` holds the roles it starts.
- **Each peer in its own environment.** Every peer runs with
  `pixi run --locked --clean-env` in its own repository. The gate sets Fast DDS,
  localhost only, the domain, no GPU and offline models after activation.
- **A client overlay instead of sourcing.** Vision's peers use
  `walkie_database_msgs` built by vision's interpreter against vision's build of
  `walkie_vision_msgs`. The overlay lives in the report's folder, outside Git.
- **Checks before the wire.** The gate first requires clean worktrees at the given
  commits, the pin and its clone at the vision commit, and the builds. It then
  probes each side's import paths, compares every installed type hash, and checks
  that two negative controls are caught: an altered nested hash, and a vision probe
  that imports the database's own build. A failure here stops the gate, so no peer
  with a wrong type joins the domain.
- **Fakes on the wire.** The peers exchange observations from vision's fake
  publisher, `LocalizeObjects` with a moving camera, vision's services with fake
  providers, and the database's reads and event log on a store restored from
  synthetic fixtures. No model, camera, GPU, Zenoh router or `ros2` command takes
  part.
- **Processes are owned by PID.** Each runs in its own process group, and is stopped
  with `SIGINT`, then `SIGTERM`, then `SIGKILL` to that group, each with a time
  limit. Nothing is found or killed by name.
- **The exit status is the result.** It is 0 only when every check passed and
  every process stopped. The JSON report keeps every check, the inventories, the
  import evidence and every process's output.
- **It gates type edits.** A change to either package's types is pushed only with a
  passing run on both commits. Once both are on GitHub, this repository's CI should
  run the same tool against its pin.

**Consequences**

- A type edit needs a commit on each side, a re-pin and a run of a few minutes,
  most of it the overlay's build.
- The report names this repository's own commit, so it is kept outside Git. The
  handoff of a pair records it.
- The gate depends on vision's fakes, and on the node's parameters. A change to
  either can make the gate fail. It cannot make it pass.
- The gate does not check that its domain is unused. Pick one that nobody else uses.
- A pass covers these two repositories only. walkie-agent-ros and walkie-ros must
  rebuild against the same commits and run their own checks.
- Remote CI does not run the gate yet.

**See also:** [Setup step 14](/setup/14-pair-check.md),
[Checking the pair](/architecture/database.md#checking-the-pair)
<!-- --8<-- [end:body] -->
