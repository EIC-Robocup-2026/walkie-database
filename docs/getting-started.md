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
```

`pixi install` downloads everything listed in `pixi.toml` into a hidden
`.pixi/` folder inside the project. Nothing is installed system-wide.

## 3. Run the tests

```bash
pixi run test
```

The tests use throwaway databases and need no robot. All tests should pass.

## 4. Run the database node

```bash
pixi run db-node
```

The node creates `data/walkie.db` if it does not exist, and brings its
schema up to date. Press ++ctrl+c++ to stop it. Git ignores `data/`, so the
database is never committed.

To use another file, set the `db_path` parameter:

```bash
pixi run python -m walkie_database.node --ros-args -p db_path:=/tmp/test.db
```

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
