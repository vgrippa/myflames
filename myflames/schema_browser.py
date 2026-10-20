"""The Query editor's schema browser: tables of one schema, then the columns
and indexes of one table, read on demand (``catalog-1.0``).

The schema list itself comes from :func:`myflames.perf_common.schema_catalog`
(``/api/live/schemas``, shared with the Server views' schema picker). This
module adds the two levels below it, each one small read of
``information_schema`` tagged ``/*myflames:editor*/`` so Top statements leaves
it out. It only reads; names are passed as hex literals
(:func:`perf_common.literal`), never spliced into the SQL.

Engine notes (mysql-server 9.7 "M/", mariadb-server 13.0 "Ma/"):

* MySQL answers information_schema.TABLES, COLUMNS and STATISTICS from the
  data dictionary (M/sql/dd/impl/system_views/); MariaDB opens each table's
  definition for TABLE_TYPE and the column details, but only for the one
  schema named in the WHERE clause (get_lookup_field_values,
  Ma/sql/sql_show.cc:4451; the same lookup perf_common.scope_sql relies on),
  so every read here names its schema with ``=``.
* STATISTICS has an EXPRESSION column for functional key parts on MySQL 8.0.13+
  (COLUMN_NAME is NULL there) and none on MariaDB, so the index read selects
  every column and reads them by name.
"""
from .connector import ConnectorError
from .perf_common import Reader, check_schema, literal, reason

CATALOG_VERSION = "catalog-1.0"
TAG = "/*myflames:editor*/ "
#: Table names per page; truncated pages expose a next_after cursor.
MAX_TABLES = 200


def check_table(name):
    """None, or a table name a server can have (1 to 64 characters)."""
    if name is None or name == "":
        return None
    if not isinstance(name, str) or len(name) > 64 or not name.strip() or any(c in name for c in "\x00\n\r"):
        raise ValueError("Choose a table name of 1 to 64 characters.")
    return name


def _flag(value):
    return (value or "").upper() in ("YES", "1")


def tables_of(reader, schema, prefix=None, after=None):
    """One name-ordered page and whether more exist; prefix is literal text.

    Binary keyset ordering keeps case-distinct names reachable on servers
    whose catalog collation otherwise compares them equal.
    """
    where = "TABLE_SCHEMA = " + literal(schema)
    if prefix:
        # LEFT uses literal prefix semantics: '%' and '_' are ordinary characters.
        where += " AND LEFT(TABLE_NAME, %d) = %s" % (len(prefix), literal(prefix))
    if after:
        where += " AND BINARY TABLE_NAME > BINARY " + literal(after)
    rows = reader.named(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE %s "
        "ORDER BY BINARY TABLE_NAME LIMIT %d" % (where, MAX_TABLES + 1))
    tables = []
    for row in rows[:MAX_TABLES]:
        name = row.get("TABLE_NAME")
        if name is None:
            continue
        kind = (row.get("TABLE_TYPE") or "").upper()
        tables.append({"name": name, "type": "view" if kind == "VIEW" else
                       "system view" if kind == "SYSTEM VIEW" else
                       "sequence" if kind == "SEQUENCE" else "table"})
    return tables, len(rows) > MAX_TABLES


def columns_of(reader, schema, table):
    """[{name, type, nullable, key, default, extra, generated}] in column order."""
    rows = reader.named(
        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA, GENERATION_EXPRESSION "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION"
        % (literal(schema), literal(table)))
    out = []
    for row in rows:
        if row.get("COLUMN_NAME") is None:
            continue
        out.append({"name": row["COLUMN_NAME"], "type": row.get("COLUMN_TYPE") or "",
                    "nullable": _flag(row.get("IS_NULLABLE")), "key": row.get("COLUMN_KEY") or "",
                    "default": row.get("COLUMN_DEFAULT"), "extra": row.get("EXTRA") or "",
                    "generated": bool(row.get("GENERATION_EXPRESSION"))})
    return out


def indexes_of(reader, schema, table):
    """[{name, unique, primary, type, columns: [{name, expression, sub_part}]}], PRIMARY first."""
    rows = reader.named(
        "SELECT * FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME, SEQ_IN_INDEX" % (literal(schema), literal(table)))
    indexes, order = {}, []
    for row in rows:
        name = row.get("INDEX_NAME")
        if name is None:
            continue
        if name not in indexes:
            indexes[name] = {"name": name, "unique": (row.get("NON_UNIQUE") or "1") == "0",
                             "primary": name == "PRIMARY", "type": row.get("INDEX_TYPE") or "", "columns": []}
            order.append(name)
        sub_part = row.get("SUB_PART")
        indexes[name]["columns"].append({"name": row.get("COLUMN_NAME"), "expression": row.get("EXPRESSION"),
                                         "sub_part": int(sub_part) if sub_part and sub_part.isdigit() else None})
    return [indexes[name] for name in order]


def catalog_report(conn, schema, table=None, prefix=None, after=None):
    """``catalog-1.0``: the tables of *schema*, or with *table* its columns and indexes."""
    schema = check_schema(schema)
    table = check_table(table)
    prefix, after = check_table(prefix), check_table(after)
    if schema is None:
        raise ValueError("Choose a schema to list its tables (the schema list is /api/live/schemas).")
    reader = Reader(conn, TAG)
    try:
        if table is None:
            tables, truncated = tables_of(reader, schema, prefix, after)
            return {"schema_version": CATALOG_VERSION, "level": "tables", "schema": schema, "tables": tables,
                    "truncated": truncated, "limit": MAX_TABLES, "prefix": prefix, "after": after,
                    "next_after": tables[-1]["name"] if truncated and tables else None}
        columns = columns_of(reader, schema, table)
        if not columns:
            raise ValueError("No table %s.%s, or no privilege to see its columns." % (schema, table))
        return {"schema_version": CATALOG_VERSION, "level": "table", "schema": schema, "table": table,
                "columns": columns, "indexes": indexes_of(reader, schema, table)}
    except ConnectorError as exc:
        raise ValueError("Could not read %s: %s" % (schema if table is None else "%s.%s" % (schema, table),
                                                   reason(exc)))
