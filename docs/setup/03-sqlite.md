# 3. Add SQLite and sqlite-vec

**Goal:** install a database that can search by meaning, by position and by
words, and open it the same safe way every time.

## Background

A **database** stores data in tables and answers questions about it in SQL.
**SQLite** is a database that lives in one file. There is no server to run:
the program opens the file directly. It already comes with Python.

Plain SQL can only find exact matches, such as `label = 'cup'`. Walkie also
needs to ask "what looks most like this?" and "what is near this point?".
SQLite answers those with **virtual tables**: tables whose searching is done
by an extension instead of by SQLite itself.

| Virtual table | What it searches                                  | Comes from          |
| ------------- | ------------------------------------------------- | ------------------- |
| `vec0`        | Vectors, by distance ("most similar")             | sqlite-vec          |
| `rtree`       | Boxes in 3D ("what overlaps this box")            | SQLite, built in    |
| `geopoly`     | Polygons in 2D ("which room is this point in")    | SQLite, built in    |
| `fts5`        | Words in text ("which messages mention a drink")  | SQLite, built in    |

An **embedding** is a list of numbers (a vector) that a model makes from an
image or a text. Similar things get vectors that point in similar
directions, so "find the most similar" becomes "find the nearest vector".
[sqlite-vec](https://alexgarcia.xyz/sqlite-vec/) adds that search to SQLite.

## Step 1: Install the packages

```bash
pixi add "python=3.12.*" "sqlite-vec-python>=0.1.9,<0.2" sqlite numpy pytest
```

| Package             | Why                                                                        |
| ------------------- | -------------------------------------------------------------------------- |
| `sqlite-vec-python` | The `sqlite_vec` Python module, and the extension itself (`libsqlite-vec`) |
| `sqlite`            | The `sqlite3` command-line shell, for looking inside the file              |
| `numpy`             | Vectors and point clouds, turned into bytes for the database               |
| `pytest`            | Runs the tests                                                             |

!!! info "Why `<0.2`?"
    sqlite-vec is not at version 1.0 yet, so a minor release may change its
    SQL. The upper limit means we move to 0.2 on purpose, after the tests
    pass, never by accident
    ([ADR-0007](../architecture/decisions/0007-sqlite-vec-one-table-per-model.md)).

## Step 2: Check the built-in extensions

R*Tree, Geopoly and FTS5 are optional parts of SQLite: each build of SQLite
decides whether to include them. The conda-forge build includes all three.
This checks it:

```bash
pixi run python -c "
import sqlite3
options = [row[0] for row in sqlite3.connect(':memory:').execute('PRAGMA compile_options')]
print([o for o in options if o in ('ENABLE_FTS5', 'ENABLE_RTREE', 'ENABLE_GEOPOLY')])
"
```

It should print all three names. `test_extensions_are_available` in
`tests/test_db.py` checks the same thing on every test run.

## Step 3: Open the database the same way every time

Every connection needs the same settings, so `src/walkie_database/db.py`
opens it in one place:

```python title="src/walkie_database/db.py"
conn = connect("data/walkie.db")
```

`connect()` does these things:

| Setting                  | What it does                                                                           |
| ------------------------ | -------------------------------------------------------------------------------------- |
| `sqlite_vec.load(conn)`  | Adds the `vec0` table type. Without it, vector tables cannot even be read.             |
| `PRAGMA foreign_keys`    | Turns on foreign keys. SQLite has them off by default, for every new connection.       |
| `PRAGMA journal_mode`    | WAL mode: other processes can read while one writes.                                   |
| `PRAGMA synchronous`     | `NORMAL`: faster writes. A power cut may lose the last commits but never breaks the file. |
| `PRAGMA busy_timeout`    | Wait up to 5 seconds for another process's lock instead of failing at once.            |
| `autocommit=True`        | No transaction starts unless we start one (see below).                                 |
| `readonly=True` (option) | Opens the file so that every write fails. For tools that only look.                    |

## Step 4: Write inside a transaction

A **transaction** is a group of changes that happens completely or not at
all. `transaction()` in `db.py` starts one and undoes everything if the
block raises an error:

```python
from walkie_database.db import connect, transaction

conn = connect("data/walkie.db")
with transaction(conn):
    conn.execute("INSERT INTO object ...")
    conn.execute("INSERT INTO object_box ...")
    conn.execute("INSERT INTO object_clip_vit_b16 ...")
```

The object and its two index rows are saved together, or not at all.

!!! danger "Every write to a `vec0` table needs a transaction"
    sqlite-vec checks some values only after it has started writing. For
    example, it rejects `NULL` in a metadata column, but by then it has
    already stored part of the row, and that row's id stays taken. Only a
    rollback removes it. `test_transaction_removes_half_written_vec0_row`
    shows this.

!!! info "Why `autocommit=True`?"
    By default, Python's `sqlite3` starts a transaction for you before
    `INSERT` and `UPDATE`, and you must remember to call `commit()`. That
    makes it hard to see where a transaction starts and ends. With
    `autocommit=True` nothing is implicit: a write outside `transaction()`
    is saved at once, and a write inside it is saved at the end of the
    block.

`transaction()` uses `BEGIN IMMEDIATE`, which takes the write lock at the
start. A plain `BEGIN` takes it only at the first write. If the transaction
read something before that write, and another process wrote in between, the
write fails at once with "database is locked". `busy_timeout` cannot help:
waiting would not change the fact that what the transaction read is out of
date.

## Step 5: A task to look inside

```toml title="pixi.toml"
[tasks.db-shell]
cmd = 'sqlite3 -readonly -cmd ".load $CONDA_PREFIX/lib/sqlite-vec/vec0" -cmd ".mode box" data/walkie.db'
```

- `-readonly` makes sure a look around never changes the file.
- `.load` adds sqlite-vec to the shell, the same as `connect()` does in
  Python.
- `.mode box` draws results as tables.

!!! warning "Quotes in Pixi tasks"
    Pixi's task shell does not expand `$CONDA_PREFIX` inside single quotes.
    The whole command is a TOML literal string (`'...'`), so the inner quotes
    can be double quotes, which do expand it.

See [Getting Started](../getting-started.md#5-look-inside-the-database) for
how to use it.
