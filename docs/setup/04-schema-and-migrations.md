# 4. Write the Schema and Migrations

**Goal:** create the tables, and a safe way to change them after the robot
already has data.

## Background

The **schema** is the list of tables and their columns. It will change: a
new kind of memory, a new embedding model, a new column. But the robot's
database file already holds data in the old shape, so we cannot just
delete it and start again.

A **migration** is one SQL file that changes the schema one step, for
example "add a table" or "add a column". Each database file remembers which
step it has reached. When the code starts, it applies only the steps the
file is missing.

## Where things are

| Path                                         | What it is                                       |
| -------------------------------------------- | ------------------------------------------------ |
| `src/walkie_database/migrations/0001_initial.sql` | The first schema: every table, index and trigger |
| `src/walkie_database/migrations/0002_name_vector_tables_by_weights.sql` | Renames the face and appearance vector tables after their weights ([below](#migration-0002-the-weights-in-the-name)) |
| `src/walkie_database/migrate.py`             | Finds the migrations and applies the missing ones |
| `tests/test_migrate.py`                      | Tests for applying, refusing and rolling back    |
| `tests/test_schema.py`                       | Tests for what each table and index promises     |

The tables themselves are explained in
[The Database](../architecture/database.md).

## How the version is stored

SQLite keeps a number called `user_version` in the file's header. It is 0
in a new file, and SQLite never changes it by itself. We use it as "the
number of the last migration applied":

```sql
PRAGMA user_version;        -- read it
PRAGMA user_version = 1;    -- set it
```

`migrate(conn)` then does this:

1. Read `user_version`, for example 0.
2. Find the migration files: `0001_initial.sql`, `0002_...sql`, and so on.
3. Apply every file with a bigger number, in order. Each file runs in its
   own transaction, together with the line that sets `user_version` to its
   number. If a file fails, its changes and the version change are undone
   together.
4. Refuse to start if `user_version` is **bigger** than the newest file. That
   file was migrated by newer code, and older code could damage it.

!!! info "Why not an ORM?"
    Tools such as SQLAlchemy and Alembic can write migrations for you. They
    do not understand `vec0`, `rtree`, `geopoly` or `fts5` tables, so every
    migration here would need raw SQL anyway. Plain SQL files are less to
    learn, and you can read them directly
    ([ADR-0009](../architecture/decisions/0009-sql-migrations-user-version.md)).

## The schema's rules

`0001_initial.sql` starts with these rules. Follow them in every migration:

- **Times** are Unix time in seconds (`REAL`).
- **Coordinates** are metres in the frame of the row's map
  ([ADR-0008](../architecture/decisions/0008-coordinates-in-a-named-map.md)).
- **Tables are `STRICT`.** A plain SQLite table stores the text `'one'` in a
  `REAL` column without complaint. A `STRICT` table refuses it.
- **An index shares ids with its table.** `object_box` row 10 and
  `object_clip_vit_b16` row 10 both belong to `object` row 10.
- **Triggers clean up the indexes.** Virtual tables cannot have foreign keys,
  so a trigger deletes index rows when their owner goes. Triggers also run
  for rows that `ON DELETE CASCADE` removes, so deleting a map removes every
  object and index row in it.
- **Vector tables are named after their model and its weights**, such as
  `face_arcface_w600k_r50`: ArcFace R50 trained on WebFace600K
  ([ADR-0007](../architecture/decisions/0007-sqlite-vec-one-table-per-model.md)).
  Two sets of weights for the same network make vectors that cannot be
  compared. `0001_initial.sql` named every vector table after the network
  only. [Migration 0002](#migration-0002-the-weights-in-the-name) added the
  weights to the face and appearance tables. The CLIP tables,
  `room_clip_vit_b16` and `object_clip_vit_b16`, still name only the
  network.
- **No `NULL` in vector-table metadata.** sqlite-vec rejects it, so `0` means
  "none": an object in no room has `room_id = 0` in `object_clip_vit_b16`.

## Add a migration

1. Create the next file in `src/walkie_database/migrations/`, for example
   `0003_message_vectors.sql`. Numbers must run 1, 2, 3, ... without gaps,
   and the name uses only lowercase letters, digits and `_`.
2. Write the SQL. Do not write `BEGIN`, `COMMIT` or `PRAGMA user_version`:
   `migrate()` adds them.
3. Add tests to `tests/test_schema.py` for what the change promises.
4. Run `pixi run test`. Every test starts from an empty database and
   applies every migration, so this also checks that they apply in order.

!!! danger "Never edit a migration after it is merged"
    A robot that already applied `0001_initial.sql` will never run it again,
    so an edit to it never reaches that robot. Its database and a new one
    would then have different schemas under the same version number. Write
    a new migration instead.

!!! tip "Changing a table"
    SQLite's `ALTER TABLE` can add, rename and drop a column, but it cannot
    change a column's type or constraints. For that, create the new table,
    copy the rows across with `INSERT INTO new SELECT ... FROM old`, drop the
    old table and rename the new one, all in the same migration. The
    [SQLite documentation](https://www.sqlite.org/lang_altertable.html#otheralter)
    lists the exact steps.

!!! warning "A `vec0` table cannot be renamed"
    sqlite-vec 0.1 does not rename a `vec0` table's shadow tables. After
    `ALTER TABLE ... RENAME`, the table fails with
    `no such table: main.<new name>_rowids`. Create the new table under its
    final name and copy the rows into it instead, as
    [migration 0002](#migration-0002-the-weights-in-the-name) does.

!!! danger "Rebuilding a table that others point to"
    With foreign keys on, `DROP TABLE object` first deletes every row of
    `object`, and `ON DELETE CASCADE` then deletes every `object_cloud` and
    `object_observation` row that points to it. The SQLite steps above say
    to turn foreign keys off first, but `PRAGMA foreign_keys = OFF` does
    nothing inside a transaction, and `migrate()` runs every migration
    inside one. The first migration that rebuilds such a table must also
    change `migrate()` so that it runs with foreign keys off, and checks
    them with `PRAGMA foreign_key_check` before it commits.

## Migration 0002: the weights in the name

Added on 2026-10-19. A caller picks a vector table by its name, and the
database checks only the name and the length of the vector
([ADR-0006](../architecture/decisions/0006-callers-send-vectors.md)).
OSNet x1.0 makes 512 numbers whatever its weights, so `appearance_osnet`
would take vectors from any OSNet x1.0 checkpoint, and a search would mix
them without an error. `0002_name_vector_tables_by_weights.sql` puts the
weights in the names of the face and appearance tables:

| Before             | After                          | Weights                                                       | Rows    |
| ------------------ | ------------------------------ | ------------------------------------------------------------- | ------- |
| `face_arcface`     | `face_arcface_w600k_r50`       | ArcFace R50 trained on WebFace600K, from InsightFace's `buffalo_l` | Copied  |
| `appearance_osnet` | `appearance_osnet_x1_0_msmt17` | OSNet x1.0 trained for person re-identification on MSMT17     | Dropped |

**The face vectors are copied** because the model stays the same. They came
from `buffalo_l`'s ArcFace R50, so they compare with every new face vector.
Only the table's name changes. Each copy takes its `person_id` from its
`face` row, because no trigger updates the copy in the vector table when a
face moves to another person.

**The appearance vectors are dropped** because the model changes. They came
from OSNet x1.0 with ImageNet weights. ImageNet trains a network to name the
object in a photo, not to tell one person from another, so these vectors
never answered "who is wearing these clothes?" well. They also cannot be
compared with vectors from the re-identification weights, so copying them
would mix two kinds of vector in one table. The `appearance` rows stay. A
caller can make new vectors from their `crop_path` images, where there is
one.

[ADR-0007](../architecture/decisions/0007-sqlite-vec-one-table-per-model.md)
keeps an old table until every row has a vector from the new model, so that
searches keep working during the change. The ImageNet vectors were not worth
keeping, so `appearance_osnet` goes at once.

The migration also:

- creates each table again under its new name, because a `vec0` table
  cannot be renamed (see the warning above), and
- drops and creates again the `face_delete` and `appearance_delete`
  triggers. Dropping a table does not drop a trigger that writes to it. The
  old trigger would stay, and every later `DELETE FROM face` would fail with
  `no such table: main.face_arcface`.

`test_migration_0002_copies_face_vectors_and_drops_appearance_vectors` in
`tests/test_schema.py` builds a database at version 1, adds faces and an
appearance, migrates it to version 2 and checks each of these.
