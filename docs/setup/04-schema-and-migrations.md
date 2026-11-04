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
| `src/walkie_database/migrations/0003_one_open_session_and_unique_ids.sql` | Allows at most one open session, and keeps session and person ids from being given twice ([below](#migration-0003-one-open-session)) |
| `src/walkie_database/migrations/0004_object_lifecycle.sql` | Adds the object lifecycle fields, run identity and an object id counter ([below](#migration-0004-object-lifecycle-fields)) |
| `src/walkie_database/migrations/0005_object_vector_filters.sql` | Adds state and layer to the object vector table's metadata ([below](#migration-0005-object-vector-filters)) |
| `src/walkie_database/migrations/0006_object_context.sql` | Adds room keys, surfaces, the catalogue, relations, the task's log and retried writes ([below](#migration-0006-object-context)) |
| `src/walkie_database/migrate.py`             | Finds the migrations and applies the missing ones |
| `src/walkie_database/objects.py`             | Reserves object ids, writes new object rows, tombstones and vectors, and searches vectors |
| `src/walkie_database/object_context.py`      | Records and reads what migration 0006 adds       |
| `tests/test_migrate.py`                      | Tests for applying, refusing and rolling back    |
| `tests/test_schema.py`                       | Tests for what each table and index promises     |
| `tests/test_migration_upgrades.py`           | Tests that upgrade a populated database from version 3 |
| `tests/test_objects.py`                      | Tests for object ids, new rows and tombstones    |
| `tests/test_object_context.py`               | Tests for rooms, surfaces, relations, the log, forgetting and retried writes |

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
- **Name the columns in every insert into a vector table.** A migration can add
  metadata columns, and an insert by position then fails.

## Add a migration

1. Create the next file in `src/walkie_database/migrations/`, such as
   `NNNN_message_vectors.sql` with the next number. Numbers must run 1, 2, 3,
   ... without gaps, and the name uses only lowercase letters, digits and `_`.
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

## Migration 0003: one open session

Added on 2026-10-26, with the people and session services
([9. Add the people and session services](09-people-services.md)). The
open session is the `session` row whose `ended_at` is NULL: the session the
agent is in
([ADR-0017](../architecture/decisions/0017-one-open-session.md)).
`0003_one_open_session_and_unique_ids.sql` makes two changes.

**At most one session is open.** A partial unique index allows one row with
`ended_at` NULL:

```sql
CREATE UNIQUE INDEX session_one_open ON session ((ended_at IS NULL))
WHERE ended_at IS NULL;
```

Every open row indexes the same value, `1`, so a second one fails with
`UNIQUE constraint failed: index 'session_one_open'`. Ended rows are not in
the index. A plain unique index on `ended_at` would not work, because NULLs
are all different in a unique index. Before it builds the index, the
migration ends every open session but the newest, at its own start time.
Their people stay.

**Session and person ids are never given twice.** Without `AUTOINCREMENT`,
SQLite gives a new row the largest id in its table plus one, so forgetting
the newest session would hand its id to the next one. Adding
`AUTOINCREMENT` means rebuilding `session` and `person`, which the warning
above rules out. So a new table, `last_id`, holds the largest id ever given
in each, and two triggers keep it up to date on every insert. The node
gives a new session or person `last_id + 1`. No table is rebuilt, so
`migrate()` needs no change.

`test_migration_0003_closes_all_but_the_newest_open_session` builds a
database at version 2 with three open sessions, migrates it, and checks
that only the newest is still open, that every person stays, and that
`last_id` starts at the largest ids. `test_only_one_session_can_be_open`
and `test_last_id_keeps_the_largest_id_ever_given` check the index and the
triggers.

## Migration 0004: object lifecycle fields

Added on 2026-11-05. `0004_object_lifecycle.sql` gives every table the columns
object memory will write, without any of its logic
([Object fields](../architecture/database.md#object-fields)). All of it is
`ALTER TABLE ... ADD COLUMN` and new tables, with one exception.

**`last_id` is created again.** Object ids need a counter, and the table's
`CHECK` allowed only `session` and `person`. A `CHECK` cannot change, so the
migration stages the counters in a temporary table, drops the two triggers and
the table, and creates both again. Nothing has a foreign key to `last_id`, so the
[warning above](#add-a-migration) about `DROP TABLE` does not apply. The new
counter starts at the largest object id.

**Old objects are not promoted.** An object with `source = 'map'` becomes a
`present` anchor. Every other old object stays a `candidate`, because nothing has
confirmed it, and `last_verified` stays empty.

**Some rules are triggers.** A `CHECK` cannot read a JSON array, so the column's
`CHECK` covers the shape of `color_shares` and two triggers cover its twelve
values. A trigger also removes the `object_box` row when `geometry_valid` goes
to 0.

`tests/test_migration_upgrades.py` builds a populated database at version 3 and
upgrades it. It checks the backfill, every old row and both old counters. It also
runs the migration twice, and runs a copy that fails at its last statement, which
must leave version 3 unchanged. `tests/test_objects.py` checks the order of the
reservation's `PRAGMA` and `COMMIT`, and that an id is not given again after a
delete, a restart or an unused reservation.

## Migration 0005: object vector filters

Added on 2026-11-05. A search must filter by state and layer before it ranks,
or a similar object that is gone can take the place of the one you asked for.
vec0 filters only on its own metadata columns, and sqlite-vec 0.1 cannot add one.
So `0005_object_vector_filters.sql` rebuilds `object_clip_vit_b16`:

1. Copy every vector into a temporary ordinary table.
2. Drop `object_delete`, `object_room_update` and the `vec0` table. Dropping a
   table does not drop a trigger that writes to it.
3. Create the `vec0` table again, under the same name, with `state` and `layer`.
4. Put each vector back with the metadata of its `object` row, and drop the
   temporary table.
5. Create the triggers again, and add `object_state_layer_update` and
   `object_map_fixed`.

It never renames a `vec0` table or touches its shadow tables. A vector with no
`object` row is left behind, as in migration 0002. The vector bytes do not change.
`room_clip_vit_b16` is not touched.

!!! warning "A positional insert stops working"
    `INSERT INTO object_clip_vit_b16 VALUES (?, ?, ?, ?)` now fails, because the
    table has six columns. Every insert in this repository names its columns.

`tests/test_migration_upgrades.py` upgrades a populated database and compares the
vector bytes. It also runs a copy of the migration that fails at its last
statement, and checks that the old table, its triggers and its rows all still work
on the same connection and after a restart. `tests/test_objects.py` checks that a
near-copy of the query in the wrong room, map, state or layer cannot hide the
object asked for, and that each change to a row reaches its vector.

## Migration 0006: object context

Added on 2026-11-05. `0006_object_context.sql` adds what the object services will
read, and no logic that fills it. It adds `room.key`, `object.enclosed` and
`object.catalogue_id` with `ALTER TABLE ... ADD COLUMN`, so old rows keep their values
and the new columns stay empty. Every other change is a new table
([Rooms, surfaces and relations](../architecture/database.md#rooms-surfaces-and-relations),
[The task's log](../architecture/database.md#the-tasks-log)).

**Why triggers check relations.** A `CHECK` cannot read another row, so triggers
check that a relation stays on one map and that its surface belongs to its anchor.
`ALTER TABLE ... ADD COLUMN` can add a `REFERENCES` only when the default is NULL,
which is why `catalogue_id` has none.

**Why `session_forget_events` runs before the delete.** Foreign key actions run after
the `session` row is deleted. By then a person's row is gone and `person_id` is NULL,
so the event can no longer be told apart from one that named nobody. A `BEFORE DELETE`
trigger still sees the links, and it deletes those events first.

!!! warning "Never pass a caller's text to `MATCH`"
    `cup OR plate` is query syntax there, and a stray `"` is an error. `find_events`
    quotes each word for this reason.

`tests/test_migration_upgrades.py` upgrades a populated database, and runs a copy of
the migration that fails at its last statement, on the same connection and after a
restart. It also checks that an upgraded database and a new one end with the same
schema. `tests/test_object_context.py` covers the cascades, the relation rules, the
word index after an insert, an update and a delete, and what forgetting a session
takes with it, including a test that shows the foreign key alone would keep the text.
