# ADR-0009: Migrate the schema with numbered SQL files and user_version

<!-- --8<-- [start:body] -->
**Status:** Proposed · **Date:** 2026-10-19

**Context**

The schema will change, but the robot's file already holds memories, so it
cannot be deleted and recreated. Migration tools such as Alembic work
through an ORM that does not understand `vec0`, `rtree`, `geopoly` or
`fts5` tables, so their migrations here would be raw SQL anyway. SQLite
keeps an integer, `user_version`, in the file header, which nothing else
uses.

**Decision**

- Each schema change is one file in `src/walkie_database/migrations/`,
  named `NNNN_short_name.sql`, numbered from 0001 without gaps.
- `user_version` is the number of the last migration applied. `migrate()`
  applies the missing ones in order, each in its own transaction together
  with the new `user_version`.
- `migrate()` refuses a file whose `user_version` is newer than the newest
  migration it knows.
- A migration is never edited after it is merged.

**Consequences**

- A failed migration leaves the file exactly as it was, because SQLite
  undoes schema changes in a rolled-back transaction.
- Older code cannot open a file that newer code migrated, instead of
  misreading it.
- There are no down-migrations. To go back, restore a backup.
- Rebuilding a table that other tables reference needs foreign keys off,
  which a transaction does not allow. The first such migration must extend
  `migrate()`.

**See also:** [Write the schema and migrations](/setup/04-schema-and-migrations.md)
<!-- --8<-- [end:body] -->
