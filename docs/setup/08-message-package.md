# 8. Add the Message Package

**Goal:** give the node its first service, `find_objects`, with message and
service types of its own, built so that other repositories can build the
same ones.

## Background

A ROS 2 message or service type is defined in a `.msg` or `.srv` file.
`rosidl` turns it into Python and C++ code, and colcon runs `rosidl`. A
program can call the node only if it was built from the same definition.

`src/` holds the Python package, `walkie_database`, which pip installs.
colcon builds interface files only from an `ament_cmake` package, so the
types live in a second package, `walkie_database_msgs`, in `ros2_ws/src/`
([ADR-0015](../architecture/decisions/0015-messages-in-this-repository.md)).
walkie-vision keeps `walkie_vision_msgs` the same way.

## Step 1: Create the package

```text
ros2_ws/
└── src/
    └── walkie_database_msgs/
        ├── CMakeLists.txt
        ├── package.xml
        ├── msg/
        │   └── RememberedObject.msg
        └── srv/
            └── FindObjects.srv
```

`package.xml` declares the packages the types use, and joins the
`rosidl_interface_packages` group, which every interface package must:

```xml title="ros2_ws/src/walkie_database_msgs/package.xml (part)"
<buildtool_depend>ament_cmake</buildtool_depend>
<buildtool_depend>rosidl_default_generators</buildtool_depend>

<depend>builtin_interfaces</depend>
<depend>geometry_msgs</depend>

<exec_depend>rosidl_default_runtime</exec_depend>

<member_of_group>rosidl_interface_packages</member_of_group>
```

`CMakeLists.txt` lists every file. A new `.msg` or `.srv` file must be
added there too:

```cmake title="ros2_ws/src/walkie_database_msgs/CMakeLists.txt (part)"
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/RememberedObject.msg"
  "srv/FindObjects.srv"
  DEPENDENCIES builtin_interfaces geometry_msgs
)
```

Every field is commented in its file.
[The node's interface](../architecture/database.md#the-nodes-interface)
describes them.

## Step 2: Build it with colcon

The build tools come from conda, so no system ROS is needed:

```toml title="pixi.toml"
[dependencies]
colcon-common-extensions = "*"
ros-jazzy-ament-cmake = "*"
ros-jazzy-rosidl-default-generators = "*"
c-compiler = "*"
cxx-compiler = "*"
cmake = "*"
pkg-config = "*"
make = "*"

[tasks.build-msgs]
cmd = "colcon build --symlink-install --cmake-args -DPython3_EXECUTABLE=$CONDA_PREFIX/bin/python"
cwd = "ros2_ws"
```

```bash
pixi run build-msgs
```

| Part                    | Why                                                                                          |
| ----------------------- | -------------------------------------------------------------------------------------------- |
| The build tools         | `ros-base` pulls in `ament_cmake`, `rosidl_default_generators`, CMake and pkg-config only indirectly, so they are listed. Without the compilers and make, CMake falls back to whatever the system has, such as an apt ROS install. |
| `cwd = "ros2_ws"`       | colcon writes `build/`, `install/` and `log/` next to `src/`, so they stay inside `ros2_ws/`. Git ignores all three. |
| `-DPython3_EXECUTABLE`  | A user-level `~/.colcon/defaults.yaml`, often set up for an apt ROS install, could otherwise point CMake at the system Python. |
| `--symlink-install`     | The same command as walkie-agent-ros's `build-ros2` and walkie-vision's `build-msgs`.          |

!!! note "colcon changes the Python environment"
    `colcon-core` pins setuptools below 80, so `pixi install` moved it from
    84.0.0 to 79.0.1. `colcon-common-extensions` also adds `pytest-cov`,
    `pytest-repeat`, `pytest-rerunfailures` and `coverage`. pytest loads
    their plugins, as the `plugins:` line at the start of
    `pixi run test` shows, but they do nothing unless their options are
    passed.

## Step 3: Use the build in every `pixi run`

```toml title="pixi.toml"
[activation]
scripts = ["scripts/activate.sh"]
```

```bash title="scripts/activate.sh"
if [[ -f "${PIXI_PROJECT_ROOT}/ros2_ws/install/local_setup.sh" ]]; then
    source "${PIXI_PROJECT_ROOT}/ros2_ws/install/local_setup.sh"
fi
```

Pixi runs the script when it activates the environment, so every
`pixi run` and `pixi shell` after a build can import `walkie_database_msgs`.
The shell that ran the build cannot: open a new `pixi shell` after the first
build.

Pixi 0.80 does not show the script's output, so a missing build would
otherwise only show up as an `ImportError`. Instead, `tests/conftest.py`
stops the tests at once with:

```text
walkie_database_msgs is not built: run `pixi run build-msgs` once, then run the tests again
```

`test` does not depend on `build-msgs`. Pixi computes the environment
before it runs a dependency, so the first test run after a build would still
miss the build. CI runs `build-msgs` as a step of its own, before the tests.

## Step 4: Answer `find_objects`

`DatabaseNode` gets a `map_id` parameter, the map the robot is using, and
serves `~/find_objects`, which resolves to `/walkie_database/find_objects`.
Each call first checks that the map is in the `map` table, then runs one
query:

```sql
SELECT o.id, o.label, o.caption, o.map_id, m.frame_id,
       o.center_x, o.center_y, o.center_z, o.qx, o.qy, o.qz, o.qw,
       o.size_x, o.size_y, o.size_z, r.name AS room, o.n_detections, o.last_seen
FROM object AS o
JOIN map AS m ON m.id = o.map_id
LEFT JOIN room AS r ON r.id = o.room_id
WHERE o.map_id = :map_id
  AND (:label = '' OR o.label = :label COLLATE NOCASE)
ORDER BY o.last_seen DESC
LIMIT :limit
```

| Part                 | Why                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------ |
| The map check        | Without it, a typo in a map id finds nothing and succeeds, like a map with no objects.     |
| `o.map_id = :map_id` | Keeps every search inside one map ([ADR-0008](../architecture/decisions/0008-coordinates-in-a-named-map.md)). |
| `COLLATE NOCASE`     | Ignores the case of ASCII letters, so `Cup` finds `cup`.                                    |
| `:limit`             | The request's `max_results` plus one: 10 when it is 0, and never more than 100, because the node answers one call at a time. The extra row only shows whether objects were left out. Then the message ends in `(more not shown).` |

The node reads the `map_id` parameter on every request, not once at
start-up, so `ros2 param set /walkie_database map_id <id>` takes effect at
once.

rclpy does not check integer ranges when it builds a message. A `last_seen`
that `builtin_interfaces/Time` cannot hold, such as a time after 2038 or one
in milliseconds, would fail only when the node sends the response, and that
would stop the node. So the node checks each row first, and such a row
fails the search with a message instead. `last_seen` is split with
`math.floor`, not `int`, so `nanosec` is never negative.

`tests/test_node.py` calls `find_objects` directly, without a ROS client, on
the sample data from [step 6](06-sample-data-and-browser.md). It also checks
that the service has the name and type that walkie-agent-ros calls. To call it
over ROS 2, see
[Getting Started](../getting-started.md#ask-it-for-objects).

## How other repositories build the package

walkie-agent-ros, and later other repositories, clone this repository with
vcstool into their `ros2_ws/src/`, pinned to a commit:

```yaml title="ros2_ws.repos (in the other repository)"
repositories:
  ros2_ws/src/walkie-database:
    type: git
    url: https://github.com/EIC-Robocup-2026/walkie-database.git
    version: <commit>
```

They build only the message package, with the same build tools as in
Step 2:

```bash
colcon build --symlink-install --packages-select walkie_database_msgs
```

## Changing a message or service

A change to a field changes the type's hash (`RIHS01_...`). Under
rmw_zenoh_cpp 0.2.11, a client built from the old definition still finds
the service, but its requests are never answered, and nothing reports an
error. A change to comments only keeps the hash.

1. Edit the `.msg` or `.srv` file, and its comments.
2. Update the node and `tests/test_node.py`.
3. Run `pixi run build-msgs`, then `pixi run test`.
4. Bump the pinned commit in every repository that uses the package,
   rebuild it there, and restart the node and its callers.
