"""The Query editor's schema browser: tables of one schema, then the columns
and indexes of one table, read on demand (``catalog-1.0``), plus the routines,
triggers and events of one schema (loaded when the browser's group opens) and
the command palette's object search across schemas (``object-search-1.0``).

The schema list itself comes from :func:`myflames.perf_common.schema_catalog`
(``/api/live/schemas``, shared with the Server views' schema picker). This
module adds the levels below it, each one small read of
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
* ROUTINES, TRIGGERS and EVENTS carry the same columns on both engines
  (M/sql/dd/impl/system_views/routines.cc:41-44, triggers.cc:40-47,
  events.cc:38-62; Ma/sql/sql_show.cc:10253-10256, 10462-10481,
  10207-10231), and MySQL 5.7 has all three tables with these columns, so the
  group and search reads are identical everywhere. MariaDB's package routines
  (ROUTINE_TYPE 'PACKAGE' / 'PACKAGE BODY') are out of scope and filtered out.
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


def _count(value):
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _table_kind(value):
    """A TABLES.TABLE_TYPE as the workspace names it."""
    kind = (value or "").upper()
    if kind == "VIEW":
        return "view"
    if kind == "SYSTEM VIEW":
        return "system view"
    if kind == "SEQUENCE":
        return "sequence"
    return "table"


def tables_of(reader, schema, prefix=None, after=None, stats=False):
    """One name-ordered page and whether more exist; prefix is literal text.

    Binary keyset ordering keeps case-distinct names reachable on servers
    whose catalog collation otherwise compares them equal. With *stats* each
    table also carries ``engine``, ``rows`` (TABLE_ROWS: an estimate for
    InnoDB, cached per information_schema_stats_expiry on MySQL) and
    ``bytes`` (DATA_LENGTH + INDEX_LENGTH), None when the server has none
    (views). MariaDB opens each table of the schema to answer them.
    """
    where = "TABLE_SCHEMA = " + literal(schema)
    if prefix:
        # LEFT uses literal prefix semantics: '%' and '_' are ordinary characters.
        where += " AND LEFT(TABLE_NAME, %d) = %s" % (len(prefix), literal(prefix))
    if after:
        where += " AND BINARY TABLE_NAME > BINARY " + literal(after)
    columns = "TABLE_NAME, TABLE_TYPE" + (", ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH" if stats else "")
    rows = reader.named(
        "SELECT %s FROM information_schema.TABLES WHERE %s "
        "ORDER BY BINARY TABLE_NAME LIMIT %d" % (columns, where, MAX_TABLES + 1))
    tables = []
    for row in rows[:MAX_TABLES]:
        name = row.get("TABLE_NAME")
        if name is None:
            continue
        table = {"name": name, "type": _table_kind(row.get("TABLE_TYPE"))}
        if stats:
            data, index = _count(row.get("DATA_LENGTH")), _count(row.get("INDEX_LENGTH"))
            table.update(engine=row.get("ENGINE") or None, rows=_count(row.get("TABLE_ROWS")),
                         bytes=None if data is None and index is None else (data or 0) + (index or 0))
        tables.append(table)
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


def catalog_report(conn, schema, table=None, prefix=None, after=None, stats=False, group=None):
    """``catalog-1.0``: the tables of *schema* (with *stats*, also their
    engine, estimated rows and size), with *table* its columns and indexes,
    or with *group* the routines, triggers or events of *schema*."""
    schema = check_schema(schema)
    table = check_table(table)
    prefix, after = check_table(prefix), check_table(after)
    if schema is None:
        raise ValueError("Choose a schema to list its tables (the schema list is /api/live/schemas).")
    reader = Reader(conn, TAG)
    try:
        if group is not None:
            objects, truncated = objects_of(reader, schema, check_group(group))
            return {"schema_version": CATALOG_VERSION, "level": "objects", "schema": schema, "group": group,
                    "objects": objects, "truncated": truncated, "limit": MAX_OBJECTS}
        if table is None:
            tables, truncated = tables_of(reader, schema, prefix, after, stats=stats is True)
            return {"schema_version": CATALOG_VERSION, "level": "tables", "schema": schema, "tables": tables,
                    "truncated": truncated, "limit": MAX_TABLES, "prefix": prefix, "after": after,
                    "next_after": tables[-1]["name"] if truncated and tables else None, "stats": stats is True}
        columns = columns_of(reader, schema, table)
        if not columns:
            raise ValueError("No table %s.%s, or no privilege to see its columns." % (schema, table))
        return {"schema_version": CATALOG_VERSION, "level": "table", "schema": schema, "table": table,
                "columns": columns, "indexes": indexes_of(reader, schema, table)}
    except ConnectorError as exc:
        raise ValueError("Could not read %s: %s" % (schema if table is None else "%s.%s" % (schema, table),
                                                   reason(exc)))


# ---- Object groups (schema browser folders) and the palette's object search ----------

#: The groups a schema expands to in the schema browser (MariaDB's packages
#: are out of scope: only PROCEDURE and FUNCTION rows are listed).
OBJECT_GROUPS = ("routines", "triggers", "events")
#: Objects per group in one schema; a truncated group says so in its folder.
MAX_OBJECTS = 200
#: Matches the object search returns (one more row is read to flag the rest).
OBJECT_SEARCH_LIMIT = 50
#: The object search accepts 2 to this many characters (the palette asks from 2).
MAX_SEARCH = 128


def check_group(group):
    """One of OBJECT_GROUPS."""
    if group not in OBJECT_GROUPS:
        raise ValueError("Choose routines, triggers or events.")
    return group


def check_search(text):
    """The palette's object search text: 2 to 128 characters, one line."""
    if not isinstance(text, str):
        raise ValueError("Send the search text.")
    text = text.strip()
    if not 2 <= len(text) <= MAX_SEARCH or any(c in text for c in "\x00\n\r"):
        raise ValueError("Type 2 to %d characters to search for an object." % MAX_SEARCH)
    return text


def objects_of(reader, schema, group):
    """[{name, ...}] of one group (routines, triggers or events) in one
    schema, name-ordered and bounded, and whether more exist. The columns are
    the same on every supported server (see the module docstring)."""
    if group == "routines":
        rows = reader.named(
            "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
            "WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION') "
            "ORDER BY ROUTINE_TYPE, BINARY ROUTINE_NAME LIMIT %d" % (literal(schema), MAX_OBJECTS + 1))
        objects = [{"name": row["ROUTINE_NAME"],
                    "kind": "procedure" if row.get("ROUTINE_TYPE") == "PROCEDURE" else "function"}
                   for row in rows[:MAX_OBJECTS] if row.get("ROUTINE_NAME")]
    elif group == "triggers":
        rows = reader.named(
            "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING "
            "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s "
            "ORDER BY BINARY TRIGGER_NAME LIMIT %d" % (literal(schema), MAX_OBJECTS + 1))
        objects = [{"name": row["TRIGGER_NAME"], "table": row.get("EVENT_OBJECT_TABLE"),
                    "event": (row.get("EVENT_MANIPULATION") or "").lower(),
                    "timing": (row.get("ACTION_TIMING") or "").lower()}
                   for row in rows[:MAX_OBJECTS] if row.get("TRIGGER_NAME")]
    else:
        rows = reader.named(
            "SELECT EVENT_NAME, STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s "
            "ORDER BY BINARY EVENT_NAME LIMIT %d" % (literal(schema), MAX_OBJECTS + 1))
        objects = [{"name": row["EVENT_NAME"], "status": (row.get("STATUS") or "").lower()}
                   for row in rows[:MAX_OBJECTS] if row.get("EVENT_NAME")]
    return objects, len(rows) > MAX_OBJECTS


def _like_pattern(text):
    """A literal substring pattern. ESCAPE '!' has one character with or
    without NO_BACKSLASH_ESCAPES on both engines; the pattern is hex-quoted.
    """
    return "%" + "".join("!" + c if c in "!%_" else c for c in text) + "%"


def _search_kind(value):
    kind = (value or "").upper()
    if kind == "PROCEDURE":
        return "procedure"
    if kind == "FUNCTION":
        return "function"
    if kind == "TRIGGER":
        return "trigger"
    if kind == "EVENT":
        return "event"
    return _table_kind(value)


def _search_sql(pattern):
    """One UNION over the four catalog tables, ordered and bounded.
    The explicit escape character is independent of the session SQL mode.
    """
    like = " LIKE %s ESCAPE '!'" % pattern
    union = (
        "(SELECT TABLE_SCHEMA AS obj_schema, TABLE_NAME AS obj_name, TABLE_TYPE AS obj_kind "
        "FROM information_schema.TABLES WHERE TABLE_NAME%s) UNION ALL "
        "(SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
        "WHERE ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION') AND ROUTINE_NAME%s) UNION ALL "
        "(SELECT TRIGGER_SCHEMA, TRIGGER_NAME, 'TRIGGER' FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_NAME%s) UNION ALL "
        "(SELECT EVENT_SCHEMA, EVENT_NAME, 'EVENT' FROM information_schema.EVENTS "
        "WHERE EVENT_NAME%s)" % (like, like, like, like))
    return ("SELECT obj_schema, obj_name, obj_kind FROM (%s) found "
            "ORDER BY BINARY obj_name, BINARY obj_schema, obj_kind LIMIT %d"
            % (union, OBJECT_SEARCH_LIMIT + 1))


def object_search(conn, text):
    """``object-search-1.0``: tables, views, routines, triggers and events
    whose name holds *text*, in every schema the account can see. One
    information_schema read (a UNION of the four catalog tables), bounded to
    OBJECT_SEARCH_LIMIT matches; the endpoint gives it 2 seconds."""
    text = check_search(text)
    reader = Reader(conn, TAG)
    try:
        rows = reader.named(_search_sql(literal(_like_pattern(text))))
    except ConnectorError as exc:
        raise ValueError("Could not search objects: %s" % reason(exc))
    objects = [{"schema": row.get("obj_schema"), "name": row.get("obj_name"),
                "kind": _search_kind(row.get("obj_kind"))}
               for row in rows[:OBJECT_SEARCH_LIMIT] if row.get("obj_schema") and row.get("obj_name")]
    return {"schema_version": "object-search-1.0", "text": text, "objects": objects,
            "truncated": len(rows) > OBJECT_SEARCH_LIMIT, "limit": OBJECT_SEARCH_LIMIT}


# ---- Table structure (Server > Tables) ---------------------------------------------------------

STRUCTURE_VERSION = "table-structure-1.0"


def _yes(value):
    """True, False, or None when the server has no such column."""
    return None if value is None else (value or "").upper() in ("YES", "1")


def structure_filename(schema, table):
    """``<schema>.<table>.sql`` with characters a file name cannot hold replaced."""
    clean = lambda text: "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in text)
    return "%s.%s.sql" % (clean(schema), clean(table))


def table_structure(conn, schema, table):
    """``table-structure-1.0``: one table's columns (with character set,
    collation and comment), indexes, foreign keys and its exact SHOW CREATE
    TABLE, read from information_schema and SHOW CREATE TABLE only.

    Every read selects ``*`` and reads columns by name, so a column one
    server lacks (STATISTICS.EXPRESSION before MySQL 8.0.13, IS_VISIBLE before
    8.0, MariaDB's IGNORED before 10.6) is None, never an error. A SHOW CREATE
    TABLE the account may not run keeps the rest, with its reason.
    """
    schema = check_schema(schema)
    table = check_table(table)
    if schema is None or table is None:
        raise ValueError("Choose a table.")
    reader = Reader(conn, TAG)
    where = "TABLE_SCHEMA = %s AND TABLE_NAME = %s" % (literal(schema), literal(table))
    try:
        found = reader.named("SELECT * FROM information_schema.TABLES WHERE %s" % where)
        if not found:
            raise ValueError("No table %s.%s, or no privilege to see it." % (schema, table))
        info = found[0]
        column_rows = reader.named("SELECT * FROM information_schema.COLUMNS WHERE %s ORDER BY ORDINAL_POSITION" % where)
        index_rows = reader.named("SELECT * FROM information_schema.STATISTICS WHERE %s "
                                  "ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME, SEQ_IN_INDEX" % where)
        key_rows = reader.named(
            "SELECT * FROM information_schema.KEY_COLUMN_USAGE WHERE %s AND REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION" % where)
        rule_rows = reader.named(
            "SELECT * FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = %s AND TABLE_NAME = %s"
            % (literal(schema), literal(table))) if key_rows else []
    except ConnectorError as exc:
        raise ValueError("Could not read %s.%s: %s" % (schema, table, reason(exc)))
    columns = []
    for row in column_rows:
        if row.get("COLUMN_NAME") is None:
            continue
        columns.append({
            "name": row["COLUMN_NAME"], "position": _count(row.get("ORDINAL_POSITION")),
            "type": row.get("COLUMN_TYPE") or "", "nullable": _flag(row.get("IS_NULLABLE")),
            "default": row.get("COLUMN_DEFAULT"), "extra": row.get("EXTRA") or "",
            "comment": row.get("COLUMN_COMMENT") or "", "character_set": row.get("CHARACTER_SET_NAME"),
            "collation": row.get("COLLATION_NAME"), "key": row.get("COLUMN_KEY") or "",
            "generated": row.get("GENERATION_EXPRESSION") or None})
    indexes, order = {}, []
    for row in index_rows:
        name = row.get("INDEX_NAME")
        if name is None:
            continue
        if name not in indexes:
            ignored = _yes(row.get("IGNORED"))
            indexes[name] = {"name": name, "primary": name == "PRIMARY", "unique": (row.get("NON_UNIQUE") or "1") == "0",
                             "type": row.get("INDEX_TYPE") or "", "comment": row.get("INDEX_COMMENT") or "",
                             "visible": _yes(row.get("IS_VISIBLE")) if "IS_VISIBLE" in row
                             else (None if ignored is None else not ignored), "columns": []}
            order.append(name)
        sub_part = row.get("SUB_PART")
        indexes[name]["columns"].append({
            "name": row.get("COLUMN_NAME"), "expression": row.get("EXPRESSION"),
            "sub_part": int(sub_part) if sub_part and sub_part.isdigit() else None,
            "order": {"A": "asc", "D": "desc"}.get(row.get("COLLATION") or "")})
    rules = {row.get("CONSTRAINT_NAME"): row for row in rule_rows}
    keys, key_order = {}, []
    for row in key_rows:
        name = row.get("CONSTRAINT_NAME")
        if name is None:
            continue
        if name not in keys:
            rule = rules.get(name, {})
            keys[name] = {"name": name, "columns": [], "referenced_schema": row.get("REFERENCED_TABLE_SCHEMA"),
                          "referenced_table": row.get("REFERENCED_TABLE_NAME"), "referenced_columns": [],
                          "on_update": rule.get("UPDATE_RULE"), "on_delete": rule.get("DELETE_RULE")}
            key_order.append(name)
        keys[name]["columns"].append(row.get("COLUMN_NAME"))
        keys[name]["referenced_columns"].append(row.get("REFERENCED_COLUMN_NAME"))
    create, create_error = None, None
    try:
        shown = reader.named("SHOW CREATE TABLE %s.%s" % (_quote(schema), _quote(table)))
        if shown:
            create = shown[0].get("Create Table") or shown[0].get("Create View")
        if not create:
            create_error = "The server returned no definition."
    except ConnectorError as exc:
        if any(code in str(exc) for code in ("ERROR 2002", "ERROR 2003", "ERROR 2013")):
            raise ValueError("Could not read %s.%s: %s" % (schema, table, reason(exc)))
        create_error = "SHOW CREATE TABLE failed: %s" % reason(exc)
    kind = (info.get("TABLE_TYPE") or "").upper()
    comment = info.get("TABLE_COMMENT") or ""
    if kind == "VIEW" and comment == "VIEW":
        # Both servers fill a valid view's TABLE_COMMENT with the word VIEW; a
        # broken view gets the error text instead, which is kept (mysql-server
        # sql/item_strfunc.cc:4848-4869, mariadb-server sql/sql_show.cc:6172-6180).
        comment = ""
    return {
        "schema_version": STRUCTURE_VERSION, "schema": schema, "table": table,
        "type": "view" if kind == "VIEW" else "system view" if kind == "SYSTEM VIEW" else
                "sequence" if kind == "SEQUENCE" else "table",
        "engine": info.get("ENGINE") or None, "collation": info.get("TABLE_COLLATION") or None,
        "comment": comment,
        "columns": columns, "indexes": [indexes[n] for n in order], "foreign_keys": [keys[n] for n in key_order],
        "create_table": create, "create_error": create_error, "filename": structure_filename(schema, table),
    }


def _quote(name):
    return "`" + name.replace("`", "``") + "`"
