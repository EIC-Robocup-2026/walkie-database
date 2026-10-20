# Getting Started

This page gets the project running on your machine. Every command is meant to
be copied and pasted. If you want to know _how_ the project was built step by
step, read the [Setup Log](setup/index.md) after this.

## 1. Install Pixi

Pixi is the tool that installs ROS 2, SQLite and everything else for this
project. Install it once per computer:

```bash
curl -fsSL https://pixi.sh/install.sh | sh
```

Close and reopen your terminal, then check it works:

```bash
pixi --version
```

!!! note "Requirements"
    You need Linux on an x86_64 (normal Intel/AMD) computer. You do **not**
    need to install ROS 2 or SQLite yourself. Pixi does that for you.

## 2. Get the code

```bash
git clone https://github.com/EIC-Robocup-2026/walkie-database.git
cd walkie-database
pixi install
pixi run build-msgs
```

`pixi install` downloads everything listed in `pixi.toml` into a hidden
`.pixi/` folder inside the project. Nothing is installed system-wide.

`pixi run build-msgs` builds the node's message and service types, the
`walkie_database_msgs` package in `ros2_ws/`, with colcon. Every later
`pixi run` and `pixi shell` can then use them. Run it again after you
change a file in `ros2_ws/src/walkie_database_msgs/`.

## 3. Run the tests

```bash
pixi run test
```

The tests use throwaway databases and need no robot. They run on Fast DDS,
so they need no Zenoh router either. All tests should pass. If you skipped
`build-msgs`, they stop at once and tell you to run it.

## 4. Run the database node

Every `pixi run` uses the Zenoh middleware, like walkie-ros and the robot
([ADR-0014](architecture/decisions/0014-zenoh-default-middleware.md)). Under
Zenoh, nodes find each other only through a **Zenoh router**, one per
computer. On the robot, or next to walkie-ros's simulation, a router is
already running: use it. Otherwise, start one in its own terminal:

```bash title="Terminal 1: Zenoh router"
pixi run ros2 run rmw_zenoh_cpp rmw_zenohd
```

```bash title="Terminal 2: the database node"
pixi run db-node
```

The node creates `data/walkie.db` if it does not exist, and brings its
schema up to date. Press ++ctrl+c++ to stop it. Git ignores `data/`, so the
database is never committed.

Check that other nodes can see it:

```bash title="Terminal 3"
pixi run ros2 node list   # should show /walkie_database
```

!!! warning "Without a router, the node runs alone"
    The node still starts and logs `Unable to connect to a Zenoh router`,
    but no other node can see it. Start a router, and the node is found
    within a few seconds, without a restart.

!!! tip "Use the same domain in every terminal"
    Nodes on different `ROS_DOMAIN_ID`s never see each other. Use the
    domain of the nodes you want to reach: `export ROS_DOMAIN_ID=23` on the
    robot. For walkie-ros's simulation, use the domain it was started with:
    `42` if you followed walkie-agent-ros's Getting Started, or `0` if
    `ROS_DOMAIN_ID` was not set.

[Use the Zenoh middleware](setup/07-zenoh.md) explains the router, the
warnings Zenoh prints at start-up, and how to run one command on another
middleware.

To use another file, set the `db_path` parameter:

```bash
pixi run python -m walkie_database.node --ros-args -p db_path:=/tmp/test.db
```

On the robot, also set the `map_id` parameter to the map the robot is
localized in, for example `-p map_id:=arena-2026-10-19`. Requests that name
no map search that map, and they all fail while `map_id` is empty.

### Ask it for objects

The node answers the `find_objects` service: the remembered objects with a
given label in one map, the most recently seen first
([The node's interface](architecture/database.md#the-nodes-interface)). A
new `walkie.db` has no objects yet, so try it on the sample house. This
writes it to `data/demo.db` and never touches `data/walkie.db`:

```bash
pixi run db-seed
```

Stop the node in terminal 2, then run it on the sample file, in the sample
house's map, `demo-house`:

```bash title="Terminal 2: the database node"
pixi run db-node --ros-args -p db_path:=data/demo.db -p map_id:=demo-house
```

Ask it for every cup:

```bash title="Terminal 3"
pixi run ros2 service call /walkie_database/find_objects walkie_database_msgs/srv/FindObjects "{label: cup}"
```

```text
response:
walkie_database_msgs.srv.FindObjects_Response(success=True, message='Found 2 objects.', objects=[walkie_database_msgs.msg.RememberedObject(id=4, label='cup', caption='a red mug with a handle on the counter', map_id='demo-house', frame_id='map', position=geometry_msgs.msg.Point(x=3.6, y=0.4, z=0.95), orientation=..., size=geometry_msgs.msg.Vector3(x=0.11, y=0.08, z=0.1), room='kitchen', n_detections=5, last_seen=...), walkie_database_msgs.msg.RememberedObject(id=3, label='cup', caption='a white ceramic cup on the table', ...)])
```

The sample kitchen has two cups, and the red mug comes first because it was
seen last. Add `map_id` to the request to search another map, and
`max_results` to get more than 10 objects. A map that is not in the file,
such as `"{map_id: arena, label: cup}"`, gives `success=False` and a message
that names it. The call reaches the node only through the Zenoh router in
terminal 1, like `ros2 node list`.

When you are done, press ++ctrl+c++ in terminal 2 to stop the node. Run
`pixi run db-node` again to go back to `data/walkie.db`.

## 5. Look inside the database

```bash
pixi run db-shell
```

This opens `data/walkie.db` in the `sqlite3` shell, read-only, with
sqlite-vec loaded. It is safe to use while the node runs. Some commands to
try:

| Command                                  | What it shows                          |
| ---------------------------------------- | -------------------------------------- |
| `.tables`                                | Every table, including index tables    |
| `.schema object`                         | How the `object` table is defined      |
| `PRAGMA user_version;`                   | The schema version (migration number)  |
| `SELECT id, label, room_id FROM object;` | Every object the robot remembers       |
| `.quit`                                  | Leave the shell                        |

!!! tip "Many more tables than you made"
    `.tables` lists names such as `object_clip_vit_b16_chunks` and
    `message_fts_data`. Virtual tables (vector, R*Tree and full-text
    indexes) store their data in these hidden "shadow" tables. Never write
    to them directly.

### In a web browser, with sample data

A new `walkie.db` has no rows yet. To see what a full one looks like, write
a sample house, its people and a conversation to a separate file. If you
already ran this in [Ask it for objects](#ask-it-for-objects), skip it:

```bash
pixi run db-seed
```

This writes `data/demo.db` and never touches `data/walkie.db`. Then open
every database in `data/` in your browser:

```bash
pixi run db-browse
```

Open <http://127.0.0.1:8001>, click `demo`, then a table. "Execute SQL" on
the database page runs any `SELECT`, vector search included. Nothing you do
there can change a file. Press ++ctrl+c++ to stop.

!!! tip "Restart it after a re-seed"
    `db-seed` replaces `demo.db` with a new file, but a running `db-browse`
    keeps showing the old one, and a `db-node` started on `demo.db` keeps
    answering from it. Stop it and run it again.

To browse the robot's database from a laptop, see
[On the robot](setup/06-sample-data-and-browser.md#on-the-robot).

## 6. Open the docs locally

```bash
pixi run docs
```

Open <http://127.0.0.1:8000> in your browser. The page reloads by itself when
you edit a file in `docs/`. Press ++ctrl+c++ to stop.

## Next

- [Setup Log](setup/index.md): how this repository was built, one step at a
  time.
- [The Database](architecture/database.md): every table and how they link.
- [Writing Docs](setup/02-documentation.md#writing-a-new-page): how to add
  your own page.
