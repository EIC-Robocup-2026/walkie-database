# 5. Run It as a ROS 2 Node

**Goal:** make one ROS 2 node the owner of the database file, so the agent,
perception, RViz and MoveIt all reach it over ROS 2.

## Background

A **ROS 2 node** is one program on the ROS network. Nodes talk through
**topics** (a stream of messages) and **services** (a request and a reply).
The robot's other software already speaks ROS 2, so the database does too
([ADR-0005](../architecture/decisions/0005-one-node-owns-the-database.md)).

## Step 1: Install ROS 2

```bash
pixi add "ros-jazzy-ros-base>=0.11,<0.12"
```

`ros-base` is ROS 2 without graphical tools: the core libraries, `rclpy`
(ROS 2 for Python), common message types and the `ros2` command. Python is
3.12 because RoboStack builds Jazzy for 3.12.

## Step 2: The node

`src/walkie_database/node.py` holds `DatabaseNode`. For now it only:

1. reads the `db_path` parameter (default `data/walkie.db`),
2. opens the file with `connect()`, and
3. runs `migrate()`, so the schema is always up to date before anything
   reads it.

The topics and services that store and search memory come in later steps,
starting with [object memory](../architecture/object-memory.md).

```bash
pixi run db-node
```

```text
[INFO] [walkie_database]: Opened data/walkie.db at schema version 1
```

!!! warning "One thread per connection"
    A Python `sqlite3` connection refuses to be used from a thread other than
    the one that opened it. The node uses ROS 2's default single-threaded
    executor, so every callback runs on the same thread. If you ever switch
    to a `MultiThreadedExecutor`, give each thread its own connection.

## Step 3: Test on Fast DDS

The tests create real ROS 2 nodes. `tests/conftest.py` sets three things
before ROS starts, the same way walkie-agent-ros does:

| Setting                                    | Why                                                                 |
| ------------------------------------------ | ------------------------------------------------------------------- |
| `RMW_IMPLEMENTATION=rmw_fastrtps_cpp`      | Under Zenoh, nodes find each other only through a router. Fast DDS needs none. |
| `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`  | Test traffic never leaves this computer.                            |
| A random domain ID from 100 to 200         | Tests never join the robot's or the simulator's domain.             |

```bash
pixi run test
```

Since [step 7](07-zenoh.md), `pixi.toml` puts every other `pixi run` on
Zenoh. The tests still run on Fast DDS, because `conftest.py` sets the
variable inside the test process, after Pixi has set it and before anything
imports rclpy.
