"""Index health: unused indexes, redundant indexes, and tables read mostly by
full scans, for one schema or the whole server.

:func:`index_report` returns the ``indexes-1.0`` envelope shared by
``myflames indexes`` and the workspace's Server > Indexes tab. It is a single
read, run only when the user asks: the Performance Schema tables it reads
cost time proportional to the number of tables and open table handles.

Sources, all raw tables (never the sys views, whose MariaDB versions differ):

* Index usage: ``performance_schema.table_io_waits_summary_by_index_usage``
  (COUNT_STAR = 0 is a candidate only on a known nonpartitioned table).
  On MariaDB or Percona Server with Performance Schema off and ``userstat=ON``,
  ``information_schema.INDEX_STATISTICS`` and ``TABLE_STATISTICS`` instead
  (an index absent from INDEX_STATISTICS on a table that has a
  TABLE_STATISTICS row was not read). With Performance Schema on and
  ``userstat=ON``, INDEX_STATISTICS is read as well: an index it shows read
  is kept even when its Performance Schema count is zero.
* Index definitions: ``information_schema.STATISTICS``. The redundancy rules
  are advisor.find_redundant_indexes, the same ones the per-plan advice uses.
* Foreign keys: ``information_schema.KEY_COLUMN_USAGE``.

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"; the review notes have every citation):

* COUNT_STAR counts handler row operations attributed to the index
  (M/storage/perfschema/pfs.cc:5386-5432). It needs the
  ``wait/io/table/sql/handler`` instrument and the ``global_instrumentation``
  consumer (on by default), an instrumented session (``setup_actors``) and an
  enabled ``setup_objects`` row; a table in a disabled schema still gets rows,
  all zero (M/storage/perfschema/pfs_instr_class.cc:2522-2531), so such tables
  are reported as not instrumented, never as unused.
* Partitioned-table range reads bypass the counted handler calls: MySQL's
  ha_innopart calls raw InnoDB index_read/index_next (M/storage/innobase/
  handler/ha_innopart.cc:1929-1942, 1970-1983; M/sql/handler.cc:3395,
  3573); MariaDB's ha_partition calls child range methods whose handlers
  have no Performance Schema handle (Ma/sql/ha_partition.cc:8045-8048,
  7881-7886, 9190-9192; Ma/sql/handler.cc:3946-3955). A zero count cannot
  establish non-use. Candidate tables' CREATE_OPTIONS are read in exact
  schema/table batches (MySQL DD: M/sql/dd/impl/system_views/tables.cc:55-59;
  MariaDB opens tables: Ma/sql/sql_show.cc:10078-10079). Partitioned and
  unknown tables are No data, with no DROP or visibility statements.
* Counts live in memory: they start at server start and restart for a table
  when its key count or key names change, or when it is renamed or rebuilt by
  copying (M/storage/perfschema/pfs_instr_class.cc:2435-2460, 2500-2512;
  M/sql/sql_table.cc:11729; Ma/.../pfs_instr_class.cc:1731-1741), or when
  someone truncates a summary table. An in-place ALTER that keeps the index
  names keeps the counts. A table not opened since startup has no rows at all
  (M/storage/perfschema/table_tiws_by_index_usage.cc:189-213).
* InnoDB checks foreign keys with an internal cursor, not the handler
  (M/storage/innobase/row/row0ins.cc:1416, 1595; Ma/.../row0ins.cc:1459,
  1630), so an index that backs a foreign key can show zero use while being
  essential. Those are marked "keep".
* MariaDB's sys.schema_unused_indexes excludes only PRIMARY, so it lists
  UNIQUE indexes; this report excludes PRIMARY and UNIQUE on both engines.
* Full scans: the ``INDEX_NAME IS NULL`` row counts rows read without an
  index (table scans and row-position reads, M/sql/handler.cc:3104-3225).
  SUM_TIMER_FETCH is its read time; SUM_TIMER_WAIT would include inserts.
* ``userstat`` (MariaDB, default OFF, Ma/sql/sys_vars.cc:7105-7110):
  TABLE_STATISTICS.ROWS_READ counts every row read, INDEX_STATISTICS only
  those read through each index (Ma/sql/handler.h:4439-4449,
  Ma/sql/handler.cc:4059-4173, 6437-6566), so the difference is the rows read
  without an index. It has no start time and counts reads only. Percona
  Server has the same variable and tables with the same columns (read live on
  8.0.46-37 and 8.4.11-11: INDEX_STATISTICS TABLE_SCHEMA, TABLE_NAME,
  INDEX_NAME, ROWS_READ; TABLE_STATISTICS adds ROWS_CHANGED and
  ROWS_CHANGED_X_INDEXES; MariaDB's columns are in
  Ma/plugin/userstat/index_stats.cc:5-9 and table_stats.cc:5-9). It counts
  every session, so it also sees reads by sessions setup_actors leaves out,
  and FLUSH INDEX_STATISTICS restarts it, not a table rebuild. Percona
  Server's user statistics do not support partitioned tables
  (https://docs.percona.com/percona-server/8.4/user-stats.html; read live on
  8.0.46-37 and 8.4.11-11: rows read through a partitioned table's secondary
  index were missing from INDEX_STATISTICS and from its TABLE_STATISTICS
  ROWS_READ). The same bounded partition check guards every usage source:
  a partitioned table's indexes are never called unused. A count above
  zero is still evidence of a read, but cannot establish complete coverage.
* MariaDB's information_schema.STATISTICS opens every table fully for the
  COLLATION and INDEX_TYPE columns (Ma/sql/sql_show.cc:10322, 10327,
  5072-5100), and that registers never-opened tables in Performance Schema
  with zero counts (Ma/sql/table_cache.cc:877), so usage is read first and a
  table with no row operations at all is "no data", not unused. A
  server-wide read is slow on servers with many tables; a schema filter
  keeps it to one schema.
* setup_objects is matched as (schema, table), then (schema, '%'), then
  ('%', '%'); no match means not instrumented
  (M/storage/perfschema/pfs_setup_object.cc:340-369).
* An index backs a foreign key when its leading key parts are the foreign
  key's columns in order with no prefix length, never FULLTEXT or SPATIAL
  (M/storage/innobase/dict/dict0dict.cc:3371-3372, 4777-4806).
"""
from .server_info import product_name
from .advisor import _index_text, find_redundant_indexes
from .connector import ConnectorError
from .perf_common import (Reader, _count_text, _duration_text, _number, _round, base_report, check_limit,
                          check_schema, check_schemas, consumer_note, consumer_state, ident, literal, lost_note, ms_text,
                          preflight, ps_off_text, reason, require_choice, ALL_SCHEMAS_TABLE_LIMIT, scope_sql, scope_text, table_text)

SCHEMA_VERSION = "indexes-1.0"
INDEXES_TAG = "/*myflames:indexes*/ "
DEFAULT_LIMIT = 200
MAX_BROWSE_ROWS = 100000
PARTITION_BATCH_SIZE = 100
#: A server younger than this has seen too little of its workload.
SHORT_UPTIME_S = 7 * 86400
#: A table counts as read mostly by full scans from this share of fetch time.
MOSTLY_SHARE = 0.5

UNUSED_CAVEAT = ("Make it invisible first and run a full business cycle (month-end jobs, reports, "
                 "replicas) before dropping it: an invisible index is still maintained on writes, but "
                 "the optimizer stops using it, and making it visible again is instant. Queries with "
                 "an index hint that names it fail while it is invisible.")


def _schemas(schema):
    """A schema name, a list of them, or None, as a tuple or None."""
    if isinstance(schema, str):
        return check_schemas([schema])
    return check_schemas(list(schema) if schema else None)


def _scope_sql(column, schema):
    return scope_sql(column, _schemas(schema))


def _statistics_sql(server, schema):
    extra = "IS_VISIBLE, EXPRESSION" if server.engine == "mysql" else "IGNORED"
    return ("SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, "
            "COLLATION, INDEX_TYPE, %s FROM information_schema.STATISTICS WHERE %s"
            % (extra, _scope_sql("TABLE_SCHEMA", schema)))


def _fk_sql(schema):
    return ("SELECT TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, ORDINAL_POSITION, "
            "REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_NAME IS NOT NULL AND %s"
            % _scope_sql("TABLE_SCHEMA", schema))


def _usage_sql(schema):
    return ("SELECT OBJECT_SCHEMA, OBJECT_NAME, INDEX_NAME, COUNT_STAR, COUNT_FETCH, SUM_TIMER_FETCH "
            "FROM performance_schema.table_io_waits_summary_by_index_usage "
            "WHERE OBJECT_TYPE = 'TABLE' AND %s" % _scope_sql("OBJECT_SCHEMA", schema))

# ---- sampling (talks to the server) ------------------------------------------


def index_report(conn, schema=None, limit=DEFAULT_LIMIT, schemas=None, require_choice_above=ALL_SCHEMAS_TABLE_LIMIT):
    """Return the ``indexes-1.0`` report. Read-only; see the module docstring.

    *schema* (one name) or *schemas* (a list) limits every read to those
    schemas (recommended); neither reads every schema except the system ones.
    The selected scope is counted before expensive metadata reads and is
    refused above *require_choice_above* (default 2,000). Pass None only for
    an explicit all-schemas override.
    """
    chosen = list(check_schemas(schemas) or ())
    single = check_schema(schema)
    if single and single not in chosen:
        chosen.insert(0, single)
    schema = check_schemas(chosen)
    limit = check_limit(limit, 5000) if limit is not None else None
    reader = Reader(conn, INDEXES_TAG)
    notes = []
    raw = {"statistics": [], "foreign_keys": [], "usage": None, "instrument": None, "consumers": None,
           "actors": None, "objects": None, "index_stats": None, "table_stats": None, "partitioned": None}
    try:
        server = preflight(reader)
        require_choice(reader, server, schema, require_choice_above, "Index health")
        # Usage first: on MariaDB, reading information_schema.STATISTICS opens
        # every table, which registers never-opened tables in
        # Performance Schema with zero counts.
        if server.ps_on:
            raw["instrument"] = reader.optional(
                "SELECT NAME, ENABLED FROM performance_schema.setup_instruments "
                "WHERE NAME = 'wait/io/table/sql/handler'", "performance_schema.setup_instruments", notes)
            raw["consumers"] = consumer_state(reader, notes)
            raw["actors"] = reader.optional("SELECT HOST, USER, ROLE, ENABLED FROM performance_schema.setup_actors",
                                            "performance_schema.setup_actors", notes)
            raw["objects"] = reader.optional(
                "SELECT OBJECT_SCHEMA, OBJECT_NAME, ENABLED FROM performance_schema.setup_objects "
                "WHERE OBJECT_TYPE = 'TABLE'", "performance_schema.setup_objects", notes)
            raw["usage"] = reader.optional(_usage_sql(schema),
                                           "performance_schema.table_io_waits_summary_by_index_usage", notes)
        if server.userstat:
            # The only source with Performance Schema off; evidence beside it when on.
            where = _scope_sql("TABLE_SCHEMA", schema)
            raw["index_stats"] = reader.optional(
                "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, ROWS_READ FROM information_schema.INDEX_STATISTICS "
                "WHERE " + where, "information_schema.INDEX_STATISTICS", notes)
            raw["table_stats"] = reader.optional(
                "SELECT TABLE_SCHEMA, TABLE_NAME, ROWS_READ FROM information_schema.TABLE_STATISTICS WHERE " + where,
                "information_schema.TABLE_STATISTICS", notes)
        raw["statistics"] = reader.rows(_statistics_sql(server, schema))
        raw["foreign_keys"] = reader.optional(_fk_sql(schema), "information_schema.KEY_COLUMN_USAGE", notes) or []
        # Reuse the partition metadata read for every engine, after usage and
        # definitions. On MariaDB CREATE_OPTIONS opens tables, so restrict it
        # to tables that actually have zero-use candidates, in bounded batches.
        tables = _tables(raw["statistics"], server.engine)
        source, instrument_off = _usage_source(server, raw)
        candidates, _, _ = _usage(server, raw, tables, {}, source,
                                  source is not None and not instrument_off, check_partitioning=False)
        by_schema = {}
        for key in sorted({(u["schema"], u["table"]) for u in candidates}):
            by_schema.setdefault(key[0], []).append(key)
        raw["partitioned"] = []
        for keys in by_schema.values():
            for start in range(0, len(keys), PARTITION_BATCH_SIZE):
                rows = reader.optional(_partition_sql(keys[start:start + PARTITION_BATCH_SIZE]),
                                       "information_schema.TABLES partitioning", notes)
                # Keep successful batches; an absent or unreadable table stays
                # unknown, including a table hidden by metadata privileges.
                raw["partitioned"].extend(rows or [])
    except ConnectorError as exc:
        raise ValueError("Could not read index health: " + reason(exc))
    return compute_indexes(server, raw, schema, limit, notes)

# ---- arithmetic (pure) ----------------------------------------------------------


def _tables(statistics, engine):
    """{(schema, table): {name: index}} in the advisor's parsed-index shape."""
    tables = {}
    for row in statistics:
        if len(row) < 10 or not row[0] or not row[1] or not row[2]:
            continue
        key = (row[0], row[1])
        indexes = tables.setdefault(key, {})
        name = row[2]
        index = indexes.get(name)
        if index is None:
            unique = (row[3] or "1").strip() == "0"
            itype = (row[8] or "").upper()
            if name == "PRIMARY":
                kind = "PRIMARY KEY"
            elif itype == "FULLTEXT":
                kind = "FULLTEXT KEY"
            elif itype == "SPATIAL":
                kind = "SPATIAL KEY"
            elif itype not in ("BTREE", "HASH"):
                kind = "OTHER"
            else:
                kind = "UNIQUE KEY" if unique else "KEY"
            if engine == "mysql":
                visible = (row[9] or "YES").upper() != "NO"
            else:
                visible = (row[9] or "NO").upper() != "YES"
            index = {"name": name, "kind": kind, "unique": unique, "type": itype,
                     "using": "HASH" if itype == "HASH" else None, "visible": visible, "_parts": {}}
            indexes[name] = index
        seq = _number(row[4]) or 0
        expression = row[10] if len(row) > 10 else None
        index["_parts"][seq] = {"name": row[5] or expression or "?", "length": _number(row[6]),
                                "desc": (row[7] or "").upper() == "D",
                                "expression": row[5] is None or bool(expression)}
    for indexes in tables.values():
        for index in indexes.values():
            index["parts"] = [index["_parts"][k] for k in sorted(index["_parts"])]
            index["columns"] = [p["name"] for p in index["parts"]]
            del index["_parts"]
    return tables


def _ordered(indexes):
    """PRIMARY, then unique, then the rest, by name: a stable table order
    (information_schema has no index position)."""
    return sorted(indexes.values(), key=lambda i: (0 if i["kind"] == "PRIMARY KEY" else 1 if i["unique"] else 2,
                                                   i["name"].lower()))


def _foreign_keys(rows):
    """[{schema, table, name, columns, ref_schema, ref_table, ref_columns}]."""
    found = {}
    for row in rows or []:
        if len(row) < 8 or not row[2]:
            continue
        key = (row[0], row[1], row[2])
        fk = found.setdefault(key, {"schema": row[0], "table": row[1], "name": row[2], "ref_schema": row[5],
                                    "ref_table": row[6], "_cols": {}})
        fk["_cols"][_number(row[4]) or 0] = (row[3], row[7])
    out = []
    for fk in found.values():
        pairs = [fk["_cols"][k] for k in sorted(fk["_cols"])]
        fk["columns"] = [p[0] for p in pairs]
        fk["ref_columns"] = [p[1] for p in pairs]
        del fk["_cols"]
        out.append(fk)
    return out


def _starts_with(index, columns, primary=None):
    """True when the index's leading key parts are *columns*, in order, with
    no prefix length (InnoDB's rule). *primary* lists the PRIMARY KEY parts
    InnoDB appends to a secondary index, when they count for this side."""
    if index["kind"] not in ("KEY", "UNIQUE KEY", "PRIMARY KEY"):
        return False  # FULLTEXT, SPATIAL and HASH indexes never back a foreign key
    parts = list(index["parts"])
    if primary and index["kind"] != "PRIMARY KEY":
        names = {(p["name"] or "").lower() for p in parts}
        parts += [p for p in primary if (p["name"] or "").lower() not in names]
    if len(parts) < len(columns):
        return False
    for part, column in zip(parts, columns):
        if part["expression"] or part["length"] or (part["name"] or "").lower() != (column or "").lower():
            return False
    return True


def _fk_backing(tables, fks, engine="mysql"):
    """{(schema, table, index): [description]} for indexes that can back a FK.

    InnoDB counts the primary key columns it appends to a secondary index:
    MySQL on the parent side (M/sql/sql_table.cc:6470-6497), MariaDB on both
    (Ma/storage/innobase/dict/dict0dict.cc:4225-4260)."""
    backing = {}
    for fk in fks:
        sides = (((fk["schema"], fk["table"]), fk["columns"], engine == "mariadb",
                  "child side of %s (references %s.%s)" % (fk["name"], fk["ref_schema"], fk["ref_table"])),
                 ((fk["ref_schema"], fk["ref_table"]), fk["ref_columns"], True,
                  "parent side of %s (from %s.%s)" % (fk["name"], fk["schema"], fk["table"])))
        for key, columns, with_primary, text in sides:
            indexes = tables.get(key) or {}
            primary = indexes.get("PRIMARY", {}).get("parts") if with_primary else None
            for index in indexes.values():
                if _starts_with(index, columns, primary):
                    backing.setdefault((key[0], key[1], index["name"]), []).append(text)
    return backing


def _object_enabled(objects, schema, table):
    """setup_objects decides per table. The server tries (schema, table), then
    (schema, '%'), then ('%', '%'); a ('%', table) row is never consulted
    (M/storage/perfschema/pfs_setup_object.cc:340-369). No match means not
    instrumented. *objects* is the {(schema, table): enabled} lookup that
    _usage builds once per report (None when setup_objects was not read)."""
    if objects is None:
        return True
    for key in ((schema, table), (schema, "%"), ("%", "%")):
        if key in objects:
            return objects[key]
    return False


def _actors_note(actors):
    if actors is None:
        return None
    rows = [r for r in actors if len(r) >= 4]
    default = [r for r in rows if r[0] == "%" and r[1] == "%" and (r[3] or "").upper() == "YES"]
    if len(rows) == 1 and default:
        return None
    return ("performance_schema.setup_actors is not the default (all users instrumented): %s. Sessions "
            "it does not instrument leave no index usage, so an index only they use looks unused."
            % "; ".join("%s@%s %s" % (r[1], r[0], "on" if (r[3] or "").upper() == "YES" else "off")
                        for r in rows) if rows else
            "performance_schema.setup_actors is empty, so no session is instrumented and every index "
            "looks unused. Restore the default row: INSERT INTO performance_schema.setup_actors "
            "(HOST, USER, ROLE, ENABLED, HISTORY) VALUES ('%', '%', '%', 'YES', 'YES');")


def _sql(server, schema, table, index):
    target = "%s.%s" % (ident(schema), ident(table))
    word = "IGNORED" if server.engine == "mariadb" else "INVISIBLE"
    return ("ALTER TABLE %s ALTER INDEX %s %s;" % (target, ident(index), word),
            "ALTER TABLE %s DROP INDEX %s;" % (target, ident(index)))


def _columns_text(index):
    """Key parts as the advisor prints them: ``a, b(10), c DESC``."""
    out = []
    for part in index["parts"]:
        text = part["name"] or "?"
        if part.get("length"):
            text += "(%d)" % part["length"]
        if part.get("desc"):
            text += " DESC"
        out.append(text)
    return ", ".join(out)


def _partition_sql(keys):
    """Read both plain and partitioned candidates; missing rows are unknown.

    Exact schema/table predicates let MariaDB prune its table-opening work.
    No LIKE patterns: names containing percent or underscore remain literal.
    """
    if not keys or len({s for s, _ in keys}) != 1:
        raise ValueError("Partition metadata requires candidate tables in one schema")
    return ("SELECT TABLE_SCHEMA, TABLE_NAME, CREATE_OPTIONS FROM information_schema.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA = %s AND (" % literal(keys[0][0])
            + " OR ".join("TABLE_NAME = %s" % literal(t) for _, t in keys) + ")")


def _partitioning(raw):
    """Known candidate table states only; absent or NULL metadata is unknown."""
    return {(r[0], r[1]): "partitioned" in r[2].lower()
            for r in raw.get("partitioned") or []
            if len(r) >= 3 and r[0] and r[1] and r[2] is not None}


def _usage_source(server, raw):
    source = None
    if server.ps_on and raw.get("usage") is not None:
        source = "performance_schema"
    elif raw.get("index_stats") is not None and raw.get("table_stats") is not None:
        source = "userstat"
    instrument_off = False
    if source == "performance_schema" and raw.get("instrument") is not None:
        instrument_off = not any((r[1] or "").upper() == "YES" for r in raw["instrument"] if len(r) > 1)
    consumers = raw.get("consumers")
    if consumers and consumers.get("global_instrumentation") is False:
        instrument_off = True
    return source, instrument_off


def compute_indexes(server, raw, schema=None, limit=DEFAULT_LIMIT, notes=None):
    """The ``indexes-1.0`` report from the raw rows. Pure. *schema* is a name,
    a list of names, or None (every user schema)."""
    notes = list(notes or [])
    schema = _schemas(schema)
    report = base_report(SCHEMA_VERSION, server)
    tables = _tables(raw.get("statistics") or [], server.engine)
    fks = _foreign_keys(raw.get("foreign_keys"))
    backing = _fk_backing(tables, fks, server.engine)
    source, instrument_off = _usage_source(server, raw)
    report.update({
        "scope": {"schema": schema[0] if schema and len(schema) == 1 else None,
                  "schemas": list(schema) if schema else None, "server_wide": schema is None},
        "source": source, "available": source is not None and not instrument_off,
        # userstat=ON (MariaDB, Percona Server): the source with Performance
        # Schema off, evidence for unused indexes with it on.
        "userstat": {"enabled": bool(server.userstat),
                     "read": raw.get("index_stats") is not None and raw.get("table_stats") is not None,
                     "role": "source" if source == "userstat" else "evidence"
                     if source == "performance_schema" and raw.get("index_stats") is not None else None},
        "unused": [], "redundant": [], "full_scans": [], "no_data": [],
        "foreign_keys": len(fks), "notes": notes,
    })
    unused, full_scans, no_data = _usage(server, raw, tables, backing, source, report["available"])
    blocked = {(n["schema"], n["table"]): n["reason_text"] for n in no_data
               if n["reason"] in ("partitioned", "partitioning_unknown")}
    redundant = []
    for (s, t) in sorted(tables):
        for index, other, why in find_redundant_indexes(_ordered(tables[(s, t)])):
            test, drop = _sql(server, s, t, index["name"])
            fk = backing.get((s, t, index["name"]), [])
            text = ("%s %s %s, so %s serves every lookup %s serves. Each extra index slows writes and uses "
                    "buffer pool memory." % (_index_text(index), why, _index_text(other), other["name"],
                                             index["name"]))
            if fk:
                text += (" It backs a foreign key (%s); %s starts with the same columns, so it can take "
                         "over." % ("; ".join(fk), other["name"]))
            redundant.append({
                "schema": s, "table": t, "index": index["name"], "columns": _columns_text(index),
                "unique": index["unique"], "visible": index["visible"], "dominant": other["name"],
                "dominant_columns": _columns_text(other), "reason": why, "foreign_keys": fk,
                "why": text, "test_sql": test, "drop_sql": drop,
                "caveat": ("Check for FORCE INDEX or USE INDEX hints that name it. " + UNUSED_CAVEAT),
            })
            if (s, t) in blocked:
                redundant[-1].update(test_sql="", drop_sql="", caveat="",
                                     action_blocked_reason=blocked[(s, t)])
    report["summary"] = {
        "tables": len(tables), "indexes": sum(len(v) for v in tables.values()),
        "unused": len(unused), "unused_candidates": sum(1 for u in unused if u["verdict"] == "candidate"),
        "keep_foreign_key": sum(1 for u in unused if u.get("keep_reason") == "foreign_key"),
        "keep_userstat": sum(1 for u in unused if u.get("keep_reason") == "userstat"),
        "redundant": len(redundant), "full_scans": len(full_scans),
        "mostly_full_scans": sum(1 for f in full_scans if f["mostly"]), "no_data": len(no_data),
    }
    inventory = [{"schema": schema_name, "table": table_name, "index": index["name"],
                  "columns": _columns_text(index), "unique": index["unique"],
                  "type": index["type"], "visible": index["visible"]}
                 for (schema_name, table_name), entries in sorted(tables.items())
                 for index in _ordered(entries)]
    for row in inventory:
        if (row["schema"], row["table"]) in blocked:
            row["action_blocked_reason"] = blocked[(row["schema"], row["table"])]
    # Workspace browsing keeps every collected row. CLI callers retain their limit.
    if limit is None:
        limit = max(len(inventory), len(unused), len(redundant), len(full_scans), len(no_data))
        if limit > MAX_BROWSE_ROWS:
            raise ValueError("Index results exceed 100,000 rows. Choose fewer databases.")
    report["indexes"] = inventory[:limit]
    report["more"] = {"indexes": max(0, len(inventory) - limit), "unused": max(0, len(unused) - limit), "redundant": max(0, len(redundant) - limit),
                      "full_scans": max(0, len(full_scans) - limit), "no_data": max(0, len(no_data) - limit)}
    report["unused"] = unused[:limit]
    report["redundant"] = redundant[:limit]
    report["full_scans"] = full_scans[:limit]
    report["no_data"] = no_data[:limit]
    _notes(server, raw, report, schema, source, instrument_off, notes,
           any(n["reason"] == "partitioning_unknown" for n in no_data))
    return report


def _userstat_reads(raw):
    """{(schema, table, index): rows read} from INDEX_STATISTICS, or None when not read."""
    if raw.get("index_stats") is None:
        return None
    return {(r[0], r[1], r[2]): _number(r[3]) or 0 for r in raw["index_stats"] if len(r) >= 4 and r[0] and r[1]}


def _usage(server, raw, tables, backing, source, available, check_partitioning=True):
    unused, full_scans, no_data = [], [], []
    if not available:
        return unused, full_scans, no_data
    # With Performance Schema as the source, user statistics are evidence beside it.
    userstat = _userstat_reads(raw) if source == "performance_schema" else None
    partitioning = _partitioning(raw)
    objects = None
    if source == "performance_schema":
        if raw.get("objects") is not None:
            objects = {}
            for row in raw["objects"]:
                if len(row) >= 3:
                    objects.setdefault((row[0], row[1]), (row[2] or "").upper() == "YES")
        usage = {}
        for row in raw["usage"]:
            if len(row) < 6 or not row[0] or not row[1]:
                continue
            usage.setdefault((row[0], row[1]), {})[row[2]] = (
                _number(row[3]) or 0, _number(row[4]) or 0, _number(row[5]) or 0)
        since = "server start or the last change to this table's indexes"
        evidence = "COUNT_STAR = 0 in performance_schema.table_io_waits_summary_by_index_usage"
    else:
        usage = {}
        for row in raw["table_stats"]:
            if len(row) >= 3 and row[0] and row[1]:
                usage.setdefault((row[0], row[1]), {})[None] = (_number(row[2]) or 0, None, None)
        for row in raw["index_stats"]:
            if len(row) >= 4 and row[0] and row[1] and (row[0], row[1]) in usage:
                usage[(row[0], row[1])][row[2]] = (_number(row[3]) or 0, None, None)
        since = "userstat was turned on, the server started, or the last FLUSH INDEX_STATISTICS"
        evidence = "no row in information_schema.INDEX_STATISTICS"
    for (s, t) in sorted(tables):
        rows = usage.get((s, t))
        count = len(tables[(s, t)])
        if source == "performance_schema" and not _object_enabled(objects, s, t):
            no_data.append({"schema": s, "table": t, "indexes": count, "reason": "not_instrumented"})
            continue
        if not rows:
            no_data.append({"schema": s, "table": t, "indexes": count,
                            "reason": "not_opened" if source == "performance_schema" else "not_read"})
            continue
        if source == "performance_schema" and not any(v[0] for v in rows.values()):
            # Opened (perhaps only to read its definition) but no row was read
            # or written: the whole table is idle, not just its indexes.
            no_data.append({"schema": s, "table": t, "indexes": count, "reason": "no_activity"})
            continue
        candidates = [index for index in _ordered(tables[(s, t)])
                      if index["kind"] != "PRIMARY KEY" and not index["unique"]
                      and (index["name"] in rows and not rows[index["name"]][0]
                           if source == "performance_schema" else index["name"] not in rows)]
        if check_partitioning and (partitioning.get((s, t)) is True or
                                   (candidates and partitioning.get((s, t)) is not False)):
            why = "partitioned" if partitioning.get((s, t)) else "partitioning_unknown"
            no_data.append({"schema": s, "table": t, "indexes": count,
                            "reason": why, "reason_text": NO_DATA_TEXT[why]})
            continue
        for index in candidates:
            fk = backing.get((s, t, index["name"]), [])
            test, drop = _sql(server, s, t, index["name"])
            # Absent from INDEX_STATISTICS: no row read through it in its window,
            # unknown for a table user statistics do not count.
            read = None
            if userstat is not None:
                read = userstat.get((s, t, index["name"]))
                if read is None and partitioning.get((s, t)) is False:
                    read = 0
            seen = bool(read)
            entry = {"schema": s, "table": t, "index": index["name"], "columns": _columns_text(index),
                     "visible": index["visible"], "foreign_keys": fk,
                     "verdict": "keep" if fk or seen else "candidate",
                     "keep_reason": "foreign_key" if fk else "userstat" if seen else None,
                     "userstat_rows_read": read}
            if fk:
                entry["why"] = ("Shows no use since %s, but it backs a foreign key (%s). InnoDB checks foreign "
                                "keys without going through the counters, so keep it." % (since, "; ".join(fk)))
            elif seen:
                entry["why"] = ("Performance Schema shows no use since %s, but user statistics "
                                "(information_schema.INDEX_STATISTICS) count %s rows read through it since userstat "
                                "was turned on or the last FLUSH INDEX_STATISTICS, so it is in use. User statistics "
                                "count every session, including sessions setup_actors does not instrument, and keep "
                                "their counts when the table is rebuilt; keep it."
                                % (since, _count_text(read)))
            else:
                entry["why"] = "No use since %s (%s)." % (since, evidence)
                if not index["visible"]:
                    entry["why"] += (" It is already %s, so the optimizer ignores it; drop it when your test "
                                     "period is over." % ("ignored" if server.engine == "mariadb" else "invisible"))
                entry["test_sql"], entry["drop_sql"] = test, drop
                entry["caveat"] = UNUSED_CAVEAT
            unused.append(entry)
        if source == "performance_schema":
            no_index = rows.get(None)
            if no_index and no_index[1] > 0:
                with_rows = sum(v[1] for k, v in rows.items() if k is not None)
                with_ps = sum(v[2] for k, v in rows.items() if k is not None)
                share_rows = float(no_index[1]) / (no_index[1] + with_rows)
                share_time = float(no_index[2]) / (no_index[2] + with_ps) if (no_index[2] + with_ps) else None
                full_scans.append({
                    "schema": s, "table": t, "rows_without_index": no_index[1], "rows_with_index": with_rows,
                    "share_rows": _round(share_rows, 4), "fetch_ms_without_index": _round(no_index[2] / 1e9, 3),
                    "fetch_ms_with_index": _round(with_ps / 1e9, 3),
                    "share_time": _round(share_time, 4) if share_time is not None else None,
                    "mostly": (share_time if share_time is not None else share_rows) >= MOSTLY_SHARE,
                })
        else:
            # The former Percona partition lookup also guarded this subtraction.
            # Candidate-only metadata leaves other tables unresolved; incomplete
            # user statistics must not turn their missing reads into full scans.
            if server.flavor == "percona" and partitioning.get((s, t)) is not False:
                continue
            table_rows = (rows.get(None) or (0,))[0]
            with_rows = sum(v[0] for k, v in rows.items() if k is not None)
            without = max(0, table_rows - with_rows)
            if without > 0:
                share_rows = float(without) / table_rows if table_rows else 0
                full_scans.append({
                    "schema": s, "table": t, "rows_without_index": without, "rows_with_index": with_rows,
                    "share_rows": _round(share_rows, 4), "fetch_ms_without_index": None,
                    "fetch_ms_with_index": None, "share_time": None, "mostly": share_rows >= MOSTLY_SHARE,
                })
    full_scans.sort(key=lambda f: (-(f["fetch_ms_without_index"] or 0), -f["rows_without_index"],
                                   f["schema"], f["table"]))
    for f in full_scans:
        f["why"] = _full_scan_why(f)
    unused.sort(key=lambda u: (u["verdict"] != "candidate", u["schema"], u["table"], u["index"]))
    return unused, full_scans, no_data


def _full_scan_why(f):
    text = "%s rows read without an index, %s through indexes" % (
        _count_text(f["rows_without_index"]), _count_text(f["rows_with_index"]))
    if f["fetch_ms_without_index"] is not None:
        text += "; %s of read time without an index" % ms_text(f["fetch_ms_without_index"])
        if f["share_time"] is not None:
            text += " (%.0f%% of this table's read time)" % (100 * f["share_time"])
    text += (". Rows read without an index are table scans and row-position reads (for example after a "
             "sort). A scan of a small lookup table is normal; look at the time and at the statements in "
             "Top statements for this schema.")
    return text


def _notes(server, raw, report, schema, source, instrument_off, notes, partitioning_unknown=False):
    uptime = server.uptime
    if source is None:
        if (server.engine == "mariadb" or server.flavor == "percona") and not server.ps_on:
            notes.insert(0, ps_off_text(server, "index usage") + (
                " Alternatively, turn on user statistics at runtime with SET GLOBAL userstat = ON; it "
                "counts reads through each index from that moment (a small overhead on every "
                "statement), and this report then uses it."))
        elif not server.ps_on:
            notes.insert(0, ps_off_text(server, "index usage"))
        else:
            notes.insert(0, "Index usage could not be read (see the note above), so unused indexes and full "
                            "scans are not shown. Redundant indexes need only information_schema.")
    elif instrument_off:
        notes.insert(0, "The wait/io/table/sql/handler instrument or the global_instrumentation consumer is "
                        "off, so no index usage is counted. Turn it on with: UPDATE "
                        "performance_schema.setup_instruments SET ENABLED = 'YES' WHERE NAME = "
                        "'wait/io/table/sql/handler'; (until restart). Unused indexes are not shown.")
    if source == "performance_schema":
        notes.append("Unused means no row read, written or looked up through the index since the server started "
                     "(%s ago), or since an index on that table was added, dropped or renamed, or the table was "
                     "renamed or rebuilt by copying, any of which restarts its counts. An index used only by a "
                     "weekly or monthly job looks unused until the job runs."
                     % (_duration_text(uptime) if uptime is not None else "?"))
        notes.append("Tables with no row read or written since the counts started (never opened, or opened "
                     "only to read their definition) are listed under No data, never as unused. A TRUNCATE "
                     "of a Performance Schema summary table also resets the counts.")
        note = _actors_note(raw.get("actors"))
        if note:
            notes.append(note)
        note = consumer_note(raw.get("consumers"))
        if note and not instrument_off:
            notes.append(note)
        note = lost_note(server)
        if note:
            notes.append(note)
        if any(n["reason"] == "not_instrumented" for n in report["no_data"]):
            notes.append("performance_schema.setup_objects turns table instrumentation off for some tables in "
                         "scope; their indexes are listed under No data.")
        if raw.get("index_stats") is not None:
            notes.append("User statistics are on (userstat=ON), so an index that Performance Schema shows unused "
                         "but information_schema.INDEX_STATISTICS shows read is marked keep. They count from when "
                         "userstat was turned on or the last FLUSH INDEX_STATISTICS.")
    elif source == "userstat":
        notes.append("Using %s user statistics (userstat=ON) because Performance Schema is off. They count "
                     "reads only, from when userstat was turned on or the last FLUSH INDEX_STATISTICS; there is "
                     "no start time to show. Tables with no reads or changes since then are under No data."
                     % server.product)
    if source == "userstat" and server.flavor == "percona":
        states = _partitioning(raw)
        if any(len(r) >= 2 and states.get((r[0], r[1])) is not False for r in raw.get("table_stats") or []):
            notes.append("Full-scan shares from Percona user statistics are shown only for known plain tables. "
                         "Partitioning is checked only for tables with unused-index candidates.")
    if any(_partitioning(raw).values()):
        notes.append("Partitioned tables with unused-index candidates are under No data: Performance Schema "
                     "does not count every read through their indexes, and user statistics do not establish "
                     "that those indexes are unused.")
    if partitioning_unknown:
        notes.append("Partitioning could not be established for some candidate tables (information_schema.TABLES "
                     "was unreadable or omitted them). Their indexes are under No data, never unused; "
                     "DROP and visibility SQL are not offered.")
    if uptime is not None and uptime < SHORT_UPTIME_S and source == "performance_schema":
        notes.append("The server has been up only %s, so infrequent jobs may not have run yet."
                     % _duration_text(uptime))
    notes.append("PRIMARY and UNIQUE indexes are never listed as unused: they enforce constraints even when "
                 "no query reads them. Indexes that back a foreign key are marked keep: InnoDB checks foreign "
                 "keys without going through the usage counters.")
    if schema:
        notes.append("Foreign keys declared in other schemas that reference %s are not checked; a non-unique "
                     "parent index they use could look unused." % ", ".join(schema))
    notes.append("Counts are per server: reads on replicas are not included.")
    if schema is None and server.engine == "mariadb":
        notes.append("This read covered every schema. MariaDB opens every table for "
                     "information_schema.STATISTICS, which is slow on servers with many tables; choose a "
                     "schema next time.")

# ---- text output for the CLI -------------------------------------------------------


# Why a table is under No data, for the text output (Indexes.tsx NO_DATA).
NO_DATA_TEXT = {
    "not_opened": "not opened since the counts started",
    "no_activity": "no rows read or written",
    "not_instrumented": "not instrumented (setup_objects)",
    "not_read": "no reads since userstat started",
    "partitioned": "partitioned, and Performance Schema does not count every read through a partitioned table's indexes",
    "partitioning_unknown": "partitioning could not be read, so index use cannot be judged",
    "userstat_unknown": "partitioned tables could not be listed, and Percona Server's user statistics do not count them",
}


def format_text(report):
    head = "%s %s" % (product_name(report), report["server_version"] or "?")
    if report["uptime_s"] is not None:
        head += ", up %s" % _duration_text(report["uptime_s"])
    scope = report["scope"]
    chosen = scope.get("schemas") or ([scope["schema"]] if scope.get("schema") else None)
    head += ", %s" % (scope_text(chosen) if chosen else "every schema")
    s = report["summary"]
    lines = [head, "%d tables, %d indexes; usage from %s" % (
        s["tables"], s["indexes"], {"performance_schema": "Performance Schema",
                                    "userstat": "%s user statistics (userstat)" % product_name(report)}.get(
            report["source"], "nothing (see notes)")), ""]
    lines.append("UNUSED INDEXES")
    if not report["available"]:
        lines.append("  Not available (see notes).")
    elif not report["unused"]:
        lines.append("  None.")
    else:
        rows = [["%s.%s" % (u["schema"], u["table"]), u["index"], u["columns"],
                 "candidate" if u["verdict"] != "keep" else "keep: read per user statistics"
                 if u.get("keep_reason") == "userstat" else "keep: backs a foreign key"] for u in report["unused"]]
        lines.extend("  " + l for l in table_text(["TABLE", "INDEX", "COLUMNS", "VERDICT"], rows).split("\n"))
    lines.append("")
    lines.append("REDUNDANT INDEXES")
    if not report["redundant"]:
        lines.append("  None.")
    for r in report["redundant"]:
        lines.append("  %s.%s: %s (%s) %s %s (%s)" % (r["schema"], r["table"], r["index"], r["columns"],
                                                     r["reason"], r["dominant"], r["dominant_columns"]))
        if r.get("action_blocked_reason"):
            lines.append("    " + r["action_blocked_reason"])
        elif r["test_sql"]:
            lines.append("    test: %s" % r["test_sql"])
    lines.append("")
    lines.append("TABLES READ WITHOUT AN INDEX")
    if not report["available"]:
        lines.append("  Not available (see notes).")
    elif not report["full_scans"]:
        lines.append("  None.")
    else:
        rows = [["%s.%s" % (f["schema"], f["table"]), _count_text(f["rows_without_index"]),
                 _count_text(f["rows_with_index"]), "%.0f%%" % (100 * f["share_rows"]),
                 ms_text(f["fetch_ms_without_index"]), "yes" if f["mostly"] else ""] for f in report["full_scans"]]
        lines.extend("  " + l for l in table_text(
            ["TABLE", ">NO INDEX", ">INDEXED", ">SHARE", ">READ TIME", "MOSTLY"], rows).split("\n"))
        from shlex import quote
        first = report["full_scans"][0]
        lines.append("  Statements: myflames top --order no_index --table %s" %
                     quote("%s.%s" % (first["schema"], first["table"])))
    if report["no_data"]:
        lines.append("")
        lines.append("NO DATA (never listed as unused: there is nothing to judge)")
        groups = {}
        for n in report["no_data"]:
            why = n.get("reason_text") or NO_DATA_TEXT.get(n["reason"], n["reason"])
            groups.setdefault(why, []).append("%s.%s" % (n["schema"], n["table"]))
        for why, names in groups.items():
            lines.append("  %s: %s" % (why, ", ".join(names)))
    return "\n".join(lines) + "\n"
