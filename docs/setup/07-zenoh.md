# 7. Use the Zenoh Middleware

**Goal:** make every `pixi run` use the same ROS 2 middleware as walkie-ros
and the robot, so the database node and the robot's nodes see each other.

## Background

ROS 2 sends messages through a **middleware** (an RMW implementation),
chosen at start-up by the `RMW_IMPLEMENTATION` variable. Two nodes on
different middlewares never see each other, and nothing reports an error.

| Middleware               | How nodes find each other                                  | Who uses it                                   |
| ------------------------ | ---------------------------------------------------------- | --------------------------------------------- |
| `rmw_fastrtps_cpp`       | Directly, over multicast. ROS 2's default.                 | The tests ([Step 5](05-ros-node.md#step-3-test-on-fast-dds)) |
| `rmw_zenoh_cpp`          | Through a **Zenoh router**, one per computer, on port 7447 | walkie-ros, the robot, walkie-agent-ros, and now `pixi run` |

The router introduces nodes to each other. Nodes on the same computer then
send their messages to each other directly. With rmw_zenoh's default config,
routers on different computers do not connect, so a node reaches only the
nodes on its own computer. Reaching another computer needs a router
configured to connect to it, as walkie-ros's
`scripts/bringup/connect_walkie_zenoh_router.sh` does.

## Step 1: Install rmw_zenoh_cpp

```bash
pixi add "ros-jazzy-rmw-zenoh-cpp>=0.2.11,<0.3"
```

This also installs the router, `rmw_zenohd`. The lower bound matters:
0.2.10 can block forever in `rmw_wait`, and a node stops running its
callbacks ([ros2/rmw_zenoh#1032](https://github.com/ros2/rmw_zenoh/issues/1032)).

RoboStack's 0.2.11 is built with the same ROS stack as our `ros-base`, so
Pixi adds five packages and changes no other. walkie-agent-ros cannot do
this and builds 0.2.11 from source, because its rai-core rejects the
OpenCV 5 that RoboStack's 0.2.11 stack needs.

Check the installed version:

```bash
pixi list ros-jazzy-rmw-zenoh-cpp
```

```text
Name                     Version  Build        Size      Kind   Source
ros-jazzy-rmw-zenoh-cpp  0.2.11   hb0f4dca_25  1.99 KiB  conda  https://prefix.dev/robostack-jazzy
```

## Step 2: Make it the default

```toml title="pixi.toml"
[activation.env]
RMW_IMPLEMENTATION = "rmw_zenoh_cpp"

[feature.docs.activation.env]
RMW_IMPLEMENTATION = "rmw_zenoh_cpp"

[feature.browse.activation.env]
RMW_IMPLEMENTATION = "rmw_zenoh_cpp"
```

Pixi sets every variable in `[activation.env]` for each `pixi run` and
`pixi shell` ([ADR-0014](../architecture/decisions/0014-zenoh-default-middleware.md)).
Which value wins, from weakest to strongest:

| Where `RMW_IMPLEMENTATION` is set           | Result                                                    |
| ------------------------------------------- | --------------------------------------------------------- |
| `export` in your terminal                   | Ignored. `[activation.env]` replaces it.                  |
| `[activation.env]` in `pixi.toml`           | Used by every task and command.                           |
| A task's `env` in `pixi.toml`               | Replaces `[activation.env]` for that task.                |
| `pixi run env RMW_IMPLEMENTATION=... <cmd>` | Replaces both, for that one command.                      |

To run one command on Fast DDS, for example:

```bash
pixi run env RMW_IMPLEMENTATION=rmw_fastrtps_cpp ros2 topic list
```

`env` is the ordinary Linux command: it sets the variable, then runs the
rest of the line, after Pixi has set its own.

!!! note "Why three tables"
    `[activation.env]` belongs to the default feature, which the `docs` and
    `browse` environments leave out (`no-default-feature = true`). They have
    no ROS, but their own features set the same value, so
    `pixi run -e docs` and `pixi run -e browse` agree with every other
    `pixi run`.

## Step 3: Run a router

There is no Zenoh config in this repository. Every node connects to the
router at `tcp/localhost:7447`, rmw_zenoh's default. Use whichever router
already runs on the computer:

- **On the robot**, walkie-ros starts one.
- **With walkie-ros's simulation**, use the router you started for it.
- **On a laptop with neither**, start one in its own terminal:

```bash
pixi run ros2 run rmw_zenoh_cpp rmw_zenohd
```

Start only one router per computer. A second one cannot listen on port 7447.
Press ++ctrl+c++ to stop it.

!!! warning "Without a router, the node runs alone"
    The node still starts and opens the database, but logs:

    ```text
    [WARN] [rmw_zenoh_cpp]: Unable to connect to a Zenoh router. Have you started a router with `ros2 run rmw_zenoh_cpp rmw_zenohd`?
    ```

    No other node can see it, and `ros2 node list` shows nothing. Start a
    router, and the node is found within a few seconds, without a restart.

## Step 4: Check it

With the router running, start the node, then list the nodes from another
terminal:

```bash
pixi run db-node
```

```bash
pixi run ros2 node list
```

```text
/walkie_database
```

Every terminal must use the same `ROS_DOMAIN_ID`: 23 on the robot, and 0
when it is not set. For walkie-ros's simulation, use the domain it was
started with: 42 if you followed walkie-agent-ros's Getting Started. Zenoh
keeps domains apart even through one router.

!!! note "Two warnings at every start"
    Every node and `ros2` command on Zenoh prints two `WARN ... zenoh_config`
    lines about deprecated settings. They come from rmw_zenoh's own default
    config and are harmless.

## The tests stay on Fast DDS

`tests/conftest.py` sets `RMW_IMPLEMENTATION=rmw_fastrtps_cpp` inside the
test process, which replaces the Zenoh default. It sets it before anything
imports rclpy: importing rclpy loads the middleware, and a change after that
has no effect. So `pixi run test` needs no router, on a laptop or in CI.
The cost is that a problem that only Zenoh has does not show up in the
tests. It shows up when the node runs with a router.
