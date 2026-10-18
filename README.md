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
pixi run test
```

Run the node. It creates `data/walkie.db` and brings its schema up to date:

```bash
pixi run db-node
```

Look inside the file, read-only, while the node runs:

```bash
pixi run db-shell
```

## Tasks

| Command               | What it does                                                        |
| --------------------- | ------------------------------------------------------------------- |
| `pixi run db-node`    | Run the database node. It owns and writes `data/walkie.db`.         |
| `pixi run db-shell`   | Open `data/walkie.db` read-only in `sqlite3`, with sqlite-vec loaded. |
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
| `tests/`                            | Tests for the connection, migrations, schema and node     |
| `data/`                             | The database file. Not in Git.                            |
| `docs/`                             | The documentation site (MkDocs)                           |
