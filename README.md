# Walkie Database

Long-term memory for Walkie, the EIC RoboCup 2026 team's robot. One ROS 2
node keeps conversations, people, objects and rooms in one SQLite file, and
searches them by meaning (vector search with
[sqlite-vec](https://alexgarcia.xyz/sqlite-vec/)), by position and by words.
[Pixi](https://pixi.sh) installs all of it, ROS 2 Jazzy included.

**Documentation:** <https://eic-robocup-2026.github.io/walkie-database/>

- [Getting Started](https://eic-robocup-2026.github.io/walkie-database/getting-started/):
  install and run the node, step by step.
- [Setup Log](https://eic-robocup-2026.github.io/walkie-database/setup/):
  how this repository was built, with explanations.
- [Architecture](https://eic-robocup-2026.github.io/walkie-database/architecture/):
  the tables, the search indexes, and the decisions behind them.

## Quick start

```bash
curl -fsSL https://pixi.sh/install.sh | sh  # once per computer, then reopen the terminal
git clone https://github.com/EIC-Robocup-2026/walkie-database.git
cd walkie-database
pixi install
pixi run build-msgs   # builds walkie_database_msgs in ros2_ws/
pixi run test
```

Run the node. It creates `data/walkie.db` and brings its schema up to date:

```bash
pixi run db-node
```

Every `pixi run` uses the Zenoh middleware, like walkie-ros and the robot.
Other nodes see this one only through a Zenoh router on the same computer.
The robot and walkie-ros's simulation already run one. Elsewhere, start it
first, in its own terminal:

```bash
pixi run ros2 run rmw_zenoh_cpp rmw_zenohd
```

Look inside the file, read-only, while the node runs:

```bash
pixi run db-shell
```

Or fill a separate file with a sample house, and browse it in a web browser
at <http://127.0.0.1:8001>:

```bash
pixi run db-seed
pixi run db-browse
```

## Tasks

| Command               | What it does                                                        |
| --------------------- | ------------------------------------------------------------------- |
| `pixi run build-msgs` | Build the node's messages and services, `walkie_database_msgs` in `ros2_ws/`, with colcon. |
| `pixi run db-node`    | Run the database node. It owns and writes `data/walkie.db`.         |
| `pixi run db-shell`   | Open `data/walkie.db` read-only in `sqlite3`, with sqlite-vec loaded. |
| `pixi run db-seed`    | Write a sample house, people and conversation to `data/demo.db`. Never touches `walkie.db`. |
| `pixi run db-browse`  | Browse every database in `data/` read-only at <http://127.0.0.1:8001>. |
| `pixi run test`       | Run the tests. They use throwaway databases and need no robot.      |
| `pixi run docs`       | Serve the documentation at <http://127.0.0.1:8000>.                 |
| `pixi run docs-build` | Build the documentation, failing on any warning.                    |

`pixi task list` shows every task.

## Repository layout

| Path                                | What it holds                                             |
| ----------------------------------- | --------------------------------------------------------- |
| `src/walkie_database/node.py`       | The ROS 2 node that owns the database                     |
| `src/walkie_database/db.py`         | Opens the file with the same settings every time          |
| `src/walkie_database/migrate.py`    | Applies the schema migrations                             |
| `src/walkie_database/migrations/`   | The schema, as numbered SQL files                         |
| `src/walkie_database/seed.py`       | The sample data that `db-seed` writes                     |
| `ros2_ws/src/walkie_database_msgs/` | The node's messages and services, built by `build-msgs`   |
| `scripts/activate.sh`               | Adds the `build-msgs` output to every `pixi run`          |
| `datasette-plugins/`                | Datasette plugins for `db-browse`                         |
| `tests/`                            | Tests for the connection, migrations, schema, node and sample data |
| `data/`                             | The database files. Not in Git.                           |
| `docs/`                             | The documentation site (MkDocs)                           |
