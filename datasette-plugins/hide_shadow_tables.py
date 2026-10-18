"""Hide the internal tables behind virtual tables in Datasette's table lists.

Every vec0, rtree, geopoly and fts5 table keeps its data in shadow tables
named after it, such as ``object_box_node``. Datasette 0.65 hides only those
of FTS tables, which leaves about 40 internal tables next to the 11 real
ones. They stay reachable by URL and from SQL queries.

A table is hidden when SQLite reports it as a shadow table, or when its name
starts with a virtual table's name and ``_``: sqlite-vec does not declare
its ``*_vector_chunksNN`` tables as shadow tables. A real table must not be
named that way, or it is hidden too.

The lists are read at startup. Restart Datasette after a migration adds a
virtual table.
"""

from datasette import hookimpl

HIDDEN_TABLES = r"""
SELECT t.name FROM pragma_table_list AS t
WHERE t.schema = 'main' AND (
    t.type = 'shadow'
    OR t.name = 'sqlite_sequence'
    OR (t.type = 'table' AND EXISTS (
        SELECT 1 FROM pragma_table_list AS v
        WHERE v.schema = 'main' AND v.type = 'virtual'
          AND t.name LIKE replace(v.name, '_', '\_') || '\_%' ESCAPE '\'
    ))
)
"""

_hidden: dict[str, list[str]] = {}


@hookimpl
def startup(datasette):
    async def find_hidden_tables():
        for name, db in datasette.databases.items():
            if db.path is None:  # in-memory databases, including Datasette's own
                continue
            _hidden[name] = [row[0] for row in await db.execute(HIDDEN_TABLES)]

    return find_hidden_tables


@hookimpl
def get_metadata(datasette, key, database, table):
    return {
        "databases": {
            name: {"tables": {table_name: {"hidden": True} for table_name in tables}}
            for name, tables in _hidden.items()
        }
    }
