# 6. Add Sample Data and a Browser

**Goal:** look at the database in a web browser, with a sample house to look
at before the robot has seen anything.

## Background

A new `data/walkie.db` has every table but no rows, so there is little to
learn from it. Two tasks fix that:

| Task                 | What it does                                                   |
| -------------------- | -------------------------------------------------------------- |
| `pixi run db-seed`   | Writes a made-up house, its people and a conversation to `data/demo.db` |
| `pixi run db-browse` | Shows every database in `data/` in a web browser, read-only    |

The browser is [Datasette](https://datasette.io): a small web server that
turns a SQLite file into pages you can click through, filter and query with
SQL. It cannot change the file.

## Step 1: Sample data in its own file

```bash
pixi run db-seed
```

```text
Wrote data/demo.db: 3 rooms, 12 objects, 4 people and 11 messages. Browse it with: pixi run db-browse
```

`src/walkie_database/seed.py` deletes `data/demo.db` and writes it again
from the newest schema, so it always matches the code.

!!! danger "Sample data never goes into `data/walkie.db`"
    The robot searches everything in its memory. A made-up guest or coke
    can in `walkie.db` would be found by real searches, and the robot would
    "remember" things that never happened. That is why the task always
    writes `demo.db` and takes no path.

| What         | In the sample                                                                         |
| ------------ | ------------------------------------------------------------------------------------- |
| Map          | `demo-house`, with a kitchen, a living room and a bedroom                              |
| Objects      | 5 pieces of furniture drawn on the map (`source = 'map'`), and 7 small objects merged from detections (`'perception'`), each with its point cloud and detections |
| People       | Robin, a team member kept across sessions; guests Charlie and Jane; and a guest who has not said their name yet |
| Conversation | 11 messages from one Receptionist run                                                  |

!!! info "Random vectors that still make sense"
    No model runs here, so the vectors are random. But every object with
    the same label gets a vector close to the same random direction, and so
    do the faces of one person. Searches therefore behave as with a real
    model: the nearest object to a cup is the other cup, and a face is
    nearest to the other faces of the same person. Unrelated things are
    about 1 apart in cosine distance. `tests/test_seed.py` checks this.

The point clouds are points on the surface of each object's box, so they
show the right shape and size in RViz.

## Step 2: Datasette, in its own environment

```toml title="pixi.toml"
[feature.browse.dependencies]
python = "3.12.*"
datasette = ">=0.65.5,<0.66"
libsqlite-vec = ">=0.1.9,<0.2"

[environments]
browse = { features = ["browse"], no-default-feature = true }
```

A Pixi **environment** is a separate set of installed packages, built from
one or more **features**. `no-default-feature = true` leaves out the
project's main dependencies, ROS 2 included.

| Choice                  | Why                                                                          |
| ----------------------- | ---------------------------------------------------------------------------- |
| Its own environment     | Datasette's web server packages never reach the robot's environment, so updating Datasette cannot change what the node runs on. |
| `<0.66`                 | Datasette 1.0 (in alpha) changes how plugins and settings work. Move to it on purpose. |
| `libsqlite-vec`         | Only the loadable `vec0` library. Datasette loads it itself, so the Python module (`sqlite-vec-python`) is not needed. |

The first `pixi run db-browse` installs the environment, so there is no
separate install step.

!!! info "Why not DB Browser for SQLite?"
    It is a good desktop app, but conda-forge does not have it, and Ubuntu's
    package uses the system SQLite (3.45.1 on Ubuntu 24.04), which is built
    without Geopoly, so it cannot open `room_shape`. It also opens files
    for writing by default. Datasette comes from conda-forge with Pixi's
    SQLite, has every extension we use, and is read-only. It also works
    from a laptop when the database is on the robot (see
    [Step 5](#step-5-use-it)).

## Step 3: The task

```toml title="pixi.toml"
[feature.browse.tasks.db-browse]
cmd = 'datasette serve data/*.db --load-extension "$CONDA_PREFIX/lib/sqlite-vec/vec0" --plugins-dir datasette-plugins'
```

| Part                          | What it does                                                              |
| ----------------------------- | ------------------------------------------------------------------------- |
| `data/*.db`                   | Pixi's task shell expands this to every database file in `data/`. Each one becomes its own database in Datasette: `walkie`, `demo`, and a `backup` if there is one. |
| `--load-extension .../vec0`   | Adds sqlite-vec. Without it, every page that touches a vector table fails with `no such module: vec0`. |
| `--plugins-dir`               | Loads every `.py` file in `datasette-plugins/` as a plugin (Step 4).      |

!!! warning "An empty `data/` folder"
    When no file matches `data/*.db`, the pattern stays as it is and
    Datasette stops with `Path 'data/*.db' does not exist`. Run
    `pixi run db-seed` or `pixi run db-node` first.

## Step 4: Hide the index tables

Each virtual table keeps its data in ordinary tables named after it, its
**shadow tables**: `object_box_node`,
`face_arcface_w600k_r50_vector_chunks00`, and so on. The schema makes 11
tables of its own, and its virtual tables add 35 more. Datasette 0.65 hides
only those of full-text tables, so the list of tables is mostly noise.

On 2026-10-19,
[migration 0002](04-schema-and-migrations.md#migration-0002-the-weights-in-the-name)
renamed `face_arcface` to `face_arcface_w600k_r50` and `appearance_osnet` to
`appearance_osnet_x1_0_msmt17`. This page uses the new names. A `demo.db`
written before then still has the old ones: run `pixi run db-seed` again.

`datasette-plugins/hide_shadow_tables.py` hides them. It asks SQLite with
`PRAGMA table_list`, which reports each table's type, and hides:

- every table SQLite reports as `shadow`,
- every table whose name starts with a virtual table's name and `_`, because
  sqlite-vec does not report its `*_vector_chunksNN` tables as shadow
  tables, and
- `sqlite_sequence`, SQLite's own counter table.

Hidden tables still open by URL, and from SQL. The "36 hidden tables" link
at the bottom of the database page lists them.

!!! warning "Restart Datasette when files change"
    The plugin reads the table list when Datasette starts, so restart it
    after a migration adds a virtual table. Restart it after
    `pixi run db-seed` too: the seed deletes `demo.db` and writes a new
    file, but a running Datasette keeps reading the old, deleted one.

## Step 5: Use it

```bash
pixi run db-browse
```

Open <http://127.0.0.1:8001>. Press ++ctrl+c++ to stop. It is safe to leave
open while the node runs, and it shows new rows as the node writes them.

| To                                  | Do this                                                         |
| ----------------------------------- | --------------------------------------------------------------- |
| See a table                         | Click `demo`, then a table, such as `object`                    |
| Follow a foreign key                | Click a value in a column such as `room_id`                     |
| Filter rows                         | Use the filter boxes above the table, or add `?label=cup` to the URL |
| Run a query                         | Click "Execute SQL" on the database page. Only `SELECT` is allowed. |
| See a vector as numbers             | `SELECT vec_to_json(embedding) FROM face_arcface_w600k_r50 LIMIT 1` |
| See a room's outline                | `SELECT rowid, geopoly_json(_shape) FROM room_shape`            |
| Get JSON for a script               | Add `.json` to a page's URL, for example `/demo/object.json?_shape=array` |

Every [example query](../architecture/database.md#example-queries) works
here, vector search included.

### On the robot

Datasette listens only on `127.0.0.1`, so nothing else on the network can
reach it. To browse the robot's memory from a laptop, forward the port over
SSH:

```bash
ssh -L 8001:localhost:8001 <user>@<robot address>
```

`-L 8001:localhost:8001` sends connections to port 8001 on the laptop
through SSH to port 8001 on the robot. Run `pixi run db-browse` in that SSH
session, then open <http://127.0.0.1:8001> on the laptop.

!!! danger "Do not use `--host 0.0.0.0`"
    It makes Datasette reachable from the whole network, with no password.
    At a competition, anyone on the arena Wi-Fi could read every guest's
    name and conversation.

!!! note "Why `demo.db` is 8.5 MB"
    Most of it is empty space. Each vector table reserves room for 1,024
    vectors (about 2 MB for 512 numbers each) as soon as it has its first
    row. The sample has four vector tables with rows, so 8 MB is reserved
    space. Everything else, point clouds included, is about 0.5 MB.
