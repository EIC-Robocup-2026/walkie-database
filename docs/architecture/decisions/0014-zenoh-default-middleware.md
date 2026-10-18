# ADR-0014: Use Zenoh as the default ROS 2 middleware

<!-- --8<-- [start:body] -->
**Status:** Accepted · **Date:** 2026-10-19

**Context**

walkie-ros, the robot and walkie-agent-ros run on the Zenoh middleware,
`rmw_zenoh_cpp`. Each computer runs one Zenoh router, `rmw_zenohd`, on port
7447. The database node used whatever middleware the calling shell set, and
Fast DDS when it set none. Fast DDS and Zenoh do not talk to each other, and
a mismatch raises no error: the nodes simply never see each other.

`rmw_zenoh_cpp` 0.2.10 can block forever in `rmw_wait`
([ros2/rmw_zenoh#1032](https://github.com/ros2/rmw_zenoh/issues/1032)), so a
node stops running its callbacks. 0.2.11 fixes it
([#1040](https://github.com/ros2/rmw_zenoh/pull/1040)). walkie-agent-ros
builds 0.2.11 from source, because rai-core rejects the OpenCV 5 that
RoboStack's 0.2.11 needs (its ADR-0032). This environment has no rai-core.
RoboStack's 0.2.11 build, `hb0f4dca_25`, belongs to the same stack build as
our `ros-base` (`_25`), and is the build in walkie-ros's `pixi.lock`.

**Decision**

- `pixi.toml` depends on `ros-jazzy-rmw-zenoh-cpp` `>=0.2.11,<0.3` from
  RoboStack. Adding it put five new packages in `pixi.lock` and changed no
  other package. It also provides the router, `rmw_zenohd`.
- `[activation.env]` in `pixi.toml` sets
  `RMW_IMPLEMENTATION=rmw_zenoh_cpp` for every `pixi run` and `pixi shell`.
  It replaces a value exported in the calling shell. A task's `env`, or
  `pixi run env RMW_IMPLEMENTATION=... <command>`, replaces it.
- The `docs` and `browse` environments leave out the default feature, and
  with it `[activation.env]`. `[feature.docs.activation.env]` and
  `[feature.browse.activation.env]` set the same value. They have no ROS,
  but every environment then agrees, as in walkie-agent-ros (its ADR-0033).
- There is no Zenoh config in this repository. Nodes connect to the router at
  `tcp/localhost:7447`, rmw_zenoh's default: the robot's router, the one
  walkie-ros's simulation uses, or one started with
  `pixi run ros2 run rmw_zenoh_cpp rmw_zenohd`.
- Tests stay on Fast DDS. `tests/conftest.py` sets `rmw_fastrtps_cpp` before
  anything imports rclpy, because rcl loads the middleware at import. So
  `pixi run test` needs no router, as in CI.

**Consequences**

- The node reaches walkie-ros and the agent without any export in the
  terminal. `ROS_DOMAIN_ID` must still match theirs: 23 on the robot. For
  walkie-ros's simulation, it is the domain the simulation was started with:
  42 in walkie-agent-ros's Getting Started, or 0 if `ROS_DOMAIN_ID` was not
  set.
- Without a router, the node still starts, about half a second later, and
  logs `Unable to connect to a Zenoh router`. No other node can see it until
  a router starts. It is then found without a restart.
- Exporting another middleware in the shell no longer changes what
  `pixi run` uses. Use `pixi run env RMW_IMPLEMENTATION=rmw_fastrtps_cpp
  <command>` for one command on Fast DDS.
- Every node and `ros2` command on Zenoh prints two `zenoh_config` warnings
  about deprecated settings. They come from rmw_zenoh's own default config
  and are harmless.
- The version must match walkie-ros's. Move the upper bound only together
  with walkie-ros, after the tests pass.
- A Zenoh-only problem does not show up in `pixi run test`. It shows up only
  when the node runs with a router.

**See also:** [Use the Zenoh middleware](/setup/07-zenoh.md),
[Getting Started](/getting-started.md#4-run-the-database-node),
[rmw_zenoh](https://github.com/ros2/rmw_zenoh)
<!-- --8<-- [end:body] -->
