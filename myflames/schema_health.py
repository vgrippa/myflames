"""Table health: tables without a primary key or a usable unique key, and
InnoDB free space (fragmentation), for chosen schemas or every user schema.

:func:`table_report` returns the ``schema-2.0`` envelope shared by ``myflames
tables`` and the workspace's Server > Tables tab. It is read-only: it reads
information_schema once per run, and every statement it suggests is text for
the user to run (OPTIMIZE TABLE and ALTER TABLE are never sent).

Sources:

* ``information_schema.TABLES``: engine, TABLE_ROWS, DATA_LENGTH,
  INDEX_LENGTH, DATA_FREE, CREATE_OPTIONS (``partitioned``), for base tables
  and MariaDB's system-versioned tables (sequences and views are skipped;
  Ma/sql/sql_show.cc:5904-5921, M/sql/dd/impl/tables/tables.cc:69).
* ``information_schema.STATISTICS`` (``NON_UNIQUE = 0``): the primary key and
  every unique key with NULLABLE, SUB_PART, INDEX_TYPE and, on MySQL,
  EXPRESSION. Rows come in the server's key order (the order SHOW CREATE
  TABLE shows), checked live on MySQL 8.4 and 26.7 and MariaDB 11.4 and 13.0.
* ``information_schema.COLUMNS`` for ``my_row_id``, the surrogate names, and
  (MySQL) virtual generated columns.
* With PROCESS: MySQL ``INNODB_TABLESPACES`` (tablespace records read one by
  one, with AUTOEXTEND_SIZE; ``INNODB_TABLESPACES_BRIEF`` before 8.0.23;
  file-per-table tablespaces are named ``db/table``, M/storage/innobase/
  handler/ha_innodb.cc:14291-14293), plus SHOW CREATE TABLE to name a general
  tablespace when one exists. ``INNODB_TABLES`` is avoided: it loads every
  table into the InnoDB dictionary cache (M/storage/innobase/handler/
  i_s.cc:5510-5598). MariaDB ``INNODB_SYS_TABLES`` (space 0 is the system
  tablespace; it reads SYS_TABLES record by record,
  Ma/storage/innobase/handler/i_s.cc:4532-4580); MariaDB's
  INNODB_SYS_TABLESPACES lists only tablespaces opened since startup, and
  MariaDB has no general tablespaces (Ma/sql/sql_yacc.yy:5695-5696).

Verified against server source (mysql-server 9.7 "M/" with the 8.4 tags,
mariadb-server 13.0 "Ma/" with the 10.11, 11.4 and 11.8 tags):

* Clustered index: MySQL sorts keys UNIQUE first, then
  all-NOT-NULL, then PRIMARY, then without partial segments, then declaration
  order (M/sql/sql_table.cc:4077-4109), and promotes the first unique key
  whose parts are NOT NULL, whole columns and not virtual generated (a
  functional key part is a hidden virtual column) to the share's primary key
  (M/sql/dd_table_share.cc:241-262, 338-351); InnoDB clusters on it, or
  creates GEN_CLUST_INDEX (M/storage/innobase/handler/ha_innodb.cc:
  14033-14054, 12399-12410). MariaDB sorts long (hash) unique keys last and
  never promotes them, rejects nullable and prefix parts
  (Ma/sql/table.cc:2951-2983), and makes every generated column nullable
  (Ma/sql/sql_table.cc:2440-2442). DB_ROW_ID is 6 bytes (DATA_ROW_ID_LEN,
  M/storage/innobase/include/data0type.h:179, Ma/.../data0type.h:153).
* The row-ID counter: one global counter on MySQL, under the dict_sys mutex
  for every ID through 9.5 (8.4 included), atomic with the mutex every 256th
  ID from 9.6 (M/storage/innobase/include/dict0boot.ic:36-63); one atomic
  counter on MariaDB 10.6 to 10.11; per table on MariaDB 11.0 and later (MDEV-19506,
  Ma/storage/innobase/include/dict0mem.h:2449, btr/btr0cur.cc:465-476).
* Replica apply (row events): MySQL 8.3 removed
  replica_rows_search_algorithms; a primary key or unique NOT NULL key gives
  an index lookup, otherwise a hash of the event's rows, looked up through a
  non-unique index or with one table scan per event
  (M/sql/log_event.cc:8308-8373, 8795-8819, 9381-9400); events are capped by
  binlog_row_event_max_size, 8192 bytes (M/sql/sys_vars.cc:7201). MariaDB
  looks up each row: primary key, unique NOT NULL key, the most selective
  other key, else a scan until the first match
  (Ma/sql/log_event_server.cc:7977, 8413-8455, 8535, 8664).
* Group Replication counts keys through the share's primary key, so a
  promoted unique key passes (M/plugin/group_replication/.../
  rpl_handler.cc:685-696, observer_trans.cc:146-150). Galera certifies rows
  of key-less tables by a hash of the whole row (wsrep_certify_nonPK, default
  ON, Ma/sql/sys_vars.cc:6603-6606; Ma/storage/innobase/handler/
  ha_innodb.cc:10316-10335); wsrep_mode=REQUIRED_PRIMARY_KEY from 10.6
  (Ma/sql/wsrep_mysqld.cc:1552-1617).
* MySQL 8.0.30+ ``sql_generate_invisible_primary_key`` adds ``my_row_id
  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE`` as the PRIMARY KEY
  (M/sql/sql_gipk.cc:36, 198-230). ``show_gipk_in_create_table_and_
  information_schema`` (session, default ON, no privilege,
  M/sql/sys_vars.cc:7179-7186) hides it from COLUMNS and STATISTICS when OFF
  (M/sql/dd/info_schema/metadata.h:227-235), so this read turns it ON for its
  own session when the global value is OFF. COLUMNS.EXTRA then reads
  ``auto_increment INVISIBLE`` (M/sql/item_strfunc.cc:5705-5746).
* ``information_schema_stats_expiry`` (MySQL, session, default 86400, no
  privilege, M/sql/sys_vars.cc:2064-2069): the statistics columns come from
  mysql.table_stats until that many seconds pass; 0 asks the storage engine
  every time (M/sql/dd/info_schema/table_stats.cc:71-88; partitioned tables
  and read-only servers are never cached). ``fresh=True`` sends ``SET
  SESSION information_schema_stats_expiry = 0`` in the same client call as
  the read, so it lasts for that one connection. MariaDB has no such cache:
  every read calls handler::info (Ma/sql/sql_show.cc:6068-6071).
* TABLE_ROWS, DATA_LENGTH and INDEX_LENGTH are InnoDB's stat_n_rows,
  stat_clustered_index_size and stat_sum_of_other_index_sizes
  (M/storage/innobase/handler/ha_innodb.cc:17751-17758), recalculated after
  10% of the rows change (M/storage/innobase/row/row0mysql.cc:1111-1130).
  DATA_FREE is fsp_get_available_space_in_free_extents()
  (ha_innodb.cc:17364-17387): whole free extents plus the unused space above
  the free limit, minus a reserve of two extents and about 1%
  (M/storage/innobase/fsp/fsp0fsp.cc:3303-3337; extents are 1 MiB up to 16
  KiB pages, 2 MiB at 32 KiB, 4 MiB at 64 KiB, include/fsp0types.h:64-69), the
  whole tablespace's for a shared one. MySQL caches it with the other
  statistics (M/sql/dd/info_schema/table_stats.cc:187, 458-460); MariaDB
  reads it every time. A file grows by one extent up to 32 MiB, then by four
  (fsp0fsp.cc:1400-1425), or by AUTOEXTEND_SIZE (MySQL, up to 4 GiB,
  fsp0fsp.cc:1329-1342), whose unused tail is expected allocation. Free space alone is not a health warning. A partitioned table sums its
  partitions (M/storage/innobase/handler/ha_innopart.cc:3591-3621;
  Ma/sql/ha_partition.cc:8957-8972).
* OPTIMIZE TABLE on InnoDB is "recreate + analyze"
  (M/sql/sql_admin.cc:1390-1394, Ma/sql/sql_admin.cc:1287) unless
  innodb_optimize_fulltext_only is ON (M/storage/innobase/handler/
  ha_innodb.cc:18384-18393), the same rebuild
  as ALTER TABLE ... FORCE or ENGINE=InnoDB (M/sql/sql_table.cc:18024-18029,
  13050-13052); exclusive metadata locks at prepare and commit only
  (M/sql/sql_table.cc:14440-14470, 14612-14638); binary-logged unless
  NO_WRITE_TO_BINLOG or LOCAL (M/sql/sql_admin.cc:2048-2053), which need
  OPTIMIZE_LOCAL_TABLE on 8.4+ (:2018-2027). A FULLTEXT or SPATIAL index (on
  MariaDB also an index on a virtual column) blocks writes during the rebuild
  (M/.../handler0alter.cc:1325-1346, Ma/.../handler0alter.cc:2729-2757).
  MariaDB 10.11 with innodb_defragment=ON defragments instead of rebuilding
  (removed in 11.1). MariaDB 11.2+ copies other engines' tables online except
  when adding an AUTO_INCREMENT column (Ma/sql/sql_table.cc:10650-10732).
  MyISAM holds TL_READ until the copy ends (Ma/sql/sql_table.cc:10913-10916,
  13150-13153), so UPDATE and DELETE wait for the read lock; only concurrent
  inserts continue (Ma/mysys/thr_lock.c:866-869). MariaDB's
  system tablespace shrinks only with :autoshrink
  (Ma/storage/innobase/fsp/fsp0sysspace.cc:151-153); a MariaDB rebuild with
  innodb_file_per_table=ON moves a table out of the system tablespace
  (Ma/storage/innobase/handler/ha_innodb.cc:11962-11969).
* Adding an AUTO_INCREMENT column needs LOCK=SHARED on both engines
  (M/storage/innobase/handler/handler0alter.cc:1248-1255,
  Ma/.../handler0alter.cc:2525-2538); ADD PRIMARY KEY is an in-place rebuild
  that allows LOCK=NONE except with FULLTEXT or SPATIAL indexes, or (MySQL)
  NULL to NOT NULL outside strict mode (M/.../handler0alter.cc:1114-1120,
  1325-1347). Both checked live on MySQL 8.4 and MariaDB 11.4.
"""
import re
import time

from .connector import ConnectorError
from .health import SEVERITY
from .perf_common import (Reader, _bytes_text, _count_text, _duration_text, _number, _round, base_report, check_limit,
                          check_schemas, denied, ident, preflight, reason, scope_sql, scope_text, table_text,
                          TABLE_TYPES_SQL, ALL_SCHEMAS_TABLE_LIMIT, require_choice)

SCHEMA_VERSION = "schema-2.0"
TABLES_TAG = "/*myflames:tables*/ "
DEFAULT_LIMIT = 200
MAX_BROWSE_ROWS = 100000
MIB = 1 << 20
GIB = 1 << 30
# DATA_FREE describes reusable allocation, not exhaustion or wasted space.
# Capacity warnings require a measured file limit or filesystem headroom.
GIPK_COLUMN = "my_row_id"
#: Names tried, in order, for a new surrogate key column.
SURROGATE_NAMES = ("id", "row_id", "pk_id")
#: At most this many SHOW CREATE TABLE reads name the general tablespace of
#: tables that are not file-per-table (MySQL, only when one exists).
MAX_SHOW_CREATE = 50
METADATA_DEADLINE_S = 30.0
VARIABLES = ("information_schema_stats_expiry", "show_gipk_in_create_table_and_information_schema",
             "sql_generate_invisible_primary_key", "sql_require_primary_key", "innodb_force_primary_key",
             "innodb_file_per_table", "innodb_defragment", "innodb_optimize_fulltext_only",
             "lower_case_table_names", "wsrep_on", "group_replication_group_name")
STATUS_ORDER = {"problem": 0, "watch": 1, "ok": 2, "info": 2, "shared": 3, "unknown": 4}
RULES = {
    "no_usable_key": "problem: no PRIMARY KEY and no UNIQUE key on NOT NULL columns",
    "not_null_unique": "watch: no PRIMARY KEY, but a UNIQUE key on NOT NULL columns stands in for it",
    "free": ("Informational: DATA_FREE is reusable space inside the tablespace. Its amount or percentage "
             "alone does not establish a problem or a need to rebuild. File capacity and filesystem "
             "headroom are not measured by this report."),
}

# ---- sampling (talks to the server) ------------------------------------------


def _on(value):
    return (value or "").upper() in ("ON", "1")


def _tables_sql(schemas):
    return ("SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, DATA_FREE, "
            "CREATE_OPTIONS FROM information_schema.TABLES WHERE %s AND %s"
            % (TABLE_TYPES_SQL, scope_sql("TABLE_SCHEMA", schemas)))


def _keys_sql(server, schemas):
    expression = "EXPRESSION" if server.engine == "mysql" else "NULL"
    return ("SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, NULLABLE, "
            "INDEX_TYPE, %s FROM information_schema.STATISTICS WHERE NON_UNIQUE = 0 AND %s"
            % (expression, scope_sql("TABLE_SCHEMA", schemas)))


def _columns_sql(server, schemas):
    """my_row_id and the surrogate names; on MySQL also virtual generated
    columns, which can never stand in for a primary key (MariaDB reports
    every generated column as nullable)."""
    names = "COLUMN_NAME IN (%s)" % ", ".join("'%s'" % n for n in (GIPK_COLUMN,) + SURROGATE_NAMES)
    if server.engine == "mysql":
        names = "(%s OR EXTRA LIKE '%%VIRTUAL GENERATED%%')" % names
    return ("SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, EXTRA FROM information_schema.COLUMNS WHERE %s AND %s"
            % (names, scope_sql("TABLE_SCHEMA", schemas)))


#: MySQL: INNODB_TABLESPACES reads the dictionary's tablespace records one by
#: one (M/storage/innobase/handler/i_s.cc:7051-7105) and has AUTOEXTEND_SIZE
#: (8.0.23+; the BRIEF view, used as the fallback, does not);
#: information_schema.INNODB_TABLES would load every table into the InnoDB
#: dictionary cache. MariaDB: SYS_TABLES, read record by record without
#: opening tables (INNODB_SYS_TABLESPACES lists only tablespaces opened since
#: startup).
_SPACES_SQL = {
    "mysql": "SELECT NAME, SPACE_TYPE, AUTOEXTEND_SIZE FROM information_schema.INNODB_TABLESPACES "
             "WHERE SPACE_TYPE IN ('Single', 'General')",
    "mysql_brief": "SELECT NAME, SPACE_TYPE, 0 FROM information_schema.INNODB_TABLESPACES_BRIEF "
                   "WHERE SPACE_TYPE IN ('Single', 'General')",
    "mariadb": "SELECT NAME, IF(SPACE = 0, 'System', 'Single'), 0 FROM information_schema.INNODB_SYS_TABLES",
}


def table_report(conn, schemas=None, fresh=False, limit=DEFAULT_LIMIT,
                 require_choice_above=ALL_SCHEMAS_TABLE_LIMIT):
    """Return the ``schema-2.0`` report. Read-only; see the module docstring.

    *schemas* (a list of names) limits the read; None reads every schema
    except mysql, sys, performance_schema and information_schema. *fresh*
    reads MySQL's table statistics from the storage engine instead of the
    information_schema cache, for this connection only. The selected scope
    is capped at *require_choice_above* (default 2,000); None is an explicit
    override. SHOW CREATE TABLE is bounded by MAX_SHOW_CREATE and one shared
    METADATA_DEADLINE_S deadline; unresolved spaces are unknown.
    """
    schemas = check_schemas(schemas)
    limit = check_limit(limit, 5000) if limit is not None else None
    if not isinstance(fresh, bool):
        raise ValueError("Pass fresh as true or false.")
    reader = Reader(conn, TABLES_TAG)
    notes = []
    raw = {"tables": [], "keys": [], "columns": None, "spaces": None, "show_create": None,
           "fresh_applied": False, "gipk_session": False}
    try:
        server = preflight(reader, VARIABLES)
        require_choice(reader, server, schemas, require_choice_above, "Table health")
        mysql = server.engine == "mysql"
        sql = _tables_sql(schemas)
        if fresh and mysql and server.var("information_schema_stats_expiry") is not None:
            # A session variable: it lasts for this one client call.
            sql = "SET SESSION information_schema_stats_expiry = 0; " + TABLES_TAG + sql
            raw["fresh_applied"] = True
        raw["tables"] = reader.rows(sql)
        prefix = ""
        if mysql and (server.var("show_gipk_in_create_table_and_information_schema") or "").upper() in ("OFF", "0"):
            prefix = "SET SESSION show_gipk_in_create_table_and_information_schema = ON; " + TABLES_TAG
            raw["gipk_session"] = True
        raw["keys"] = reader.rows(prefix + _keys_sql(server, schemas))
        tables = _table_rows(raw["tables"])
        primary = {(r[0], r[1]) for r in raw["keys"] if len(r) >= 3 and r[2] == "PRIMARY"}
        gipk_candidates = any(len(r) >= 5 and r[2] == "PRIMARY" and (r[4] or "").lower() == GIPK_COLUMN
                              for r in raw["keys"])
        if gipk_candidates or any(key not in primary for key in tables):
            raw["columns"] = reader.optional(prefix + _columns_sql(server, schemas), "information_schema.COLUMNS",
                                             notes)
        with_free = [key for key in sorted(tables) if tables[key]["innodb"] and (tables[key]["free"] or 0) > 0]
        if with_free:
            raw["spaces"] = _optional_process(reader, _SPACES_SQL[server.engine], notes,
                                              _SPACES_SQL["mysql_brief"] if mysql else None)
            if mysql and raw["spaces"] and any(len(r) >= 2 and r[1] == "General" for r in raw["spaces"]):
                # Which general tablespace: only SHOW CREATE TABLE says, per table.
                singles = _single_keys(raw["spaces"], server)
                raw["show_create"] = {}
                deadline = time.monotonic() + METADATA_DEADLINE_S
                candidates = [k for k in with_free if not _lookup(singles, k, _fold(server))]
                raw["spaces_unresolved"] = set(candidates)
                if len(candidates) > MAX_SHOW_CREATE:
                    notes.append("Tablespace lookup capped at %d SHOW CREATE TABLE reads; unresolved spaces "
                                 "are unknown." % MAX_SHOW_CREATE)
                for key in candidates[:MAX_SHOW_CREATE]:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        notes.append("Tablespace metadata deadline reached; unresolved tablespaces are unknown.")
                        break
                    try:
                        rows = reader.optional("SHOW CREATE TABLE %s.%s" % (ident(key[0]), ident(key[1])),
                                               "SHOW CREATE TABLE %s.%s" % key, notes, timeout=remaining)
                    except ConnectorError as exc:
                        if "timed out" not in str(exc).lower():
                            raise
                        notes.append("Tablespace metadata deadline reached; unresolved tablespaces are unknown.")
                        break
                    raw["show_create"]["%s.%s" % key] = _tablespace_clause(rows)
                    if rows:
                        raw["spaces_unresolved"].discard(key)
    except ConnectorError as exc:
        raise ValueError("Could not read table health: " + reason(exc))
    return compute_tables(server, raw, schemas, fresh, limit, notes)


def _fold(server):
    return (server.var("lower_case_table_names") or "0") != "0"


def _tablespace_clause(rows):
    """The TABLESPACE name in SHOW CREATE TABLE output, '' when none."""
    for row in rows or []:
        if len(row) >= 2 and row[1]:
            match = re.search(r"TABLESPACE `((?:[^`]|``)+)`", row[1])
            return match.group(1).replace("``", "`") if match else ""
    return ""


def _optional_process(reader, sql, notes, fallback=None):
    """InnoDB information_schema tables need PROCESS; without it the
    tablespace of each table is unknown, which is reported, not fatal.
    *fallback* is tried when a column is missing (an older version)."""
    try:
        return reader.rows(sql)
    except ConnectorError as exc:
        if fallback and "Unknown column" in str(exc):
            return _optional_process(reader, fallback, notes)
        if not denied(exc) and "Unknown table" not in str(exc):
            raise
        notes.append("Could not read which tablespace each table is in (%s). It needs the PROCESS privilege; "
                     "until then free space is shown but not judged." % reason(exc))
        return None

# ---- arithmetic (pure) ----------------------------------------------------------


def _table_rows(rows):
    tables = {}
    for row in rows or []:
        if len(row) < 8 or not row[0] or not row[1]:
            continue
        engine = row[2] or ""
        tables[(row[0], row[1])] = {
            "schema": row[0], "table": row[1], "engine": engine or None,
            "innodb": engine.lower() == "innodb", "rows": _number(row[3]), "data": _number(row[4]),
            "index": _number(row[5]), "free": _number(row[6]),
            "partitioned": "partitioned" in (row[7] or "").lower(),
        }
    return tables


def _unique_keys(rows):
    """{(schema, table): {index: [part, ...]}} with parts in key order."""
    keys = {}
    for row in rows or []:
        if len(row) < 9 or not row[0] or not row[1] or not row[2]:
            continue
        parts = keys.setdefault((row[0], row[1]), {}).setdefault(row[2], {})
        parts[int(_number(row[3]) or 0)] = {
            "column": row[4], "sub_part": _number(row[5]), "nullable": (row[6] or "").upper() == "YES",
            "type": (row[7] or "").upper(), "expression": row[8],
        }
    return {table: {name: [parts[k] for k in sorted(parts)] for name, parts in indexes.items()}
            for table, indexes in keys.items()}


def _columns(rows):
    """{(schema, table): {lower column name: EXTRA}}."""
    out = {}
    for row in rows or []:
        if len(row) >= 4 and row[0] and row[1] and row[2]:
            out.setdefault((row[0], row[1]), {})[row[2].lower()] = row[3] or ""
    return out


def _why_not_usable(parts, engine, columns=None):
    """None when the unique key can stand in for a primary key, else why not
    (M/sql/dd_table_share.cc:241-262, 338-351; Ma/sql/table.cc:2951-2983)."""
    columns = columns or {}
    for part in parts:
        if part["column"] is None or part["expression"]:
            return "a functional key part"
        if "VIRTUAL GENERATED" in columns.get(part["column"].lower(), "").upper():
            return "%s is a virtual generated column" % part["column"]
        if part["sub_part"]:
            return "a prefix of %s (%d)" % (part["column"], part["sub_part"])
        if part["type"] == "HASH" and (engine or "").lower() != "memory":
            return "a hash (long) unique key"
        if part["type"] not in ("BTREE", "HASH"):
            return "a %s index" % part["type"].lower()
    nullable = [p["column"] for p in parts if p["nullable"]]
    if nullable:
        return "%s allow%s NULL" % (", ".join(nullable), "" if len(nullable) > 1 else "s")
    return None


def _columns_text(parts):
    out = []
    for p in parts:
        text = p["column"] or ("(%s)" % p["expression"] if p["expression"] else "?")
        if p["sub_part"]:
            text += "(%d)" % p["sub_part"]
        out.append(text)
    return ", ".join(out)


def _split_space_name(name, decode):
    """``db/table`` (a file-per-table tablespace or InnoDB table name) as
    (schema, table), or None for temporary and internal names. MariaDB
    writes names in the filename encoding ('@' plus four hex digits is one
    character, M/strings/ctype-utf8.cc); MySQL's tablespace names are
    already decoded. Partitions (#p# on MySQL, #P# on MariaDB, with #sp# for
    subpartitions) map to their table."""
    if not name or "/" not in name:
        return None
    out = []
    for piece in name.split("/", 1):
        piece = re.split(r"#[pP]#", piece, maxsplit=1)[0]
        if piece.startswith("#") or not piece:
            return None
        if decode:
            text, i = [], 0
            while i < len(piece):
                if piece[i] == "@":
                    code = piece[i + 1:i + 5]
                    if len(code) != 4 or not re.match(r"^[0-9a-fA-F]{4}$", code):
                        return None
                    text.append(chr(int(code, 16)))
                    i += 5
                else:
                    text.append(piece[i])
                    i += 1
            piece = "".join(text)
        out.append(piece)
    return out[0], out[1]


def _autoextend(rows, server):
    """{(schema, table): bytes} of AUTOEXTEND_SIZE (MySQL), summed over
    partitions: a file grows by up to that much at once, and the unused tail
    counts in DATA_FREE (M/storage/innobase/fsp/fsp0fsp.cc:1329-1342)."""
    out = {}
    for row in rows or []:
        size = _number(row[2]) if len(row) >= 3 else None
        key = _split_space_name(row[0], server.engine == "mariadb") if size else None
        if key is not None:
            out[key] = out.get(key, 0) + size
    return out


def _single_keys(rows, server):
    """{(schema, table): 'Single' or 'System'} from the tablespace rows."""
    out = {}
    for row in rows or []:
        if len(row) < 2 or row[1] not in ("Single", "System"):
            continue
        keys = [_split_space_name(row[0], True)]
        if server.engine == "mysql":
            keys.append(_split_space_name(row[0], False))
        for key in keys:
            if key is None:
                continue
            # A partitioned table is in a shared tablespace if any partition is.
            if out.get(key) != "System":
                out[key] = row[1]
    return out


def _lookup(spaces, key, fold):
    if key in spaces:
        return spaces[key]
    if fold:
        lowered = (key[0].lower(), key[1].lower())
        for other, value in spaces.items():
            if (other[0].lower(), other[1].lower()) == lowered:
                return value
    return None


def _tablespace(server, raw, key, fold, singles):
    """(kind, name) for an InnoDB table: ('file_per_table', None),
    ('system', 'innodb_system'), ('general', name), ('shared', None) when a
    shared tablespace could not be named, or None when unknown."""
    if raw.get("spaces") is None:
        return None
    found = _lookup(singles, key, fold)
    if found == "Single":
        return "file_per_table", None
    if found == "System":
        return "system", "innodb_system"
    if server.engine == "mariadb":
        # SYS_TABLES lists every InnoDB table; a miss is a name it could not decode.
        return None
    if not any(len(r) >= 2 and r[1] == "General" for r in raw["spaces"]):
        return "system", "innodb_system"
    if key in raw.get("spaces_unresolved", ()):
        return None
    clause = (raw.get("show_create") or {}).get("%s.%s" % key)
    if clause is None:
        return "shared", None
    if clause in ("", "innodb_system"):
        return "system", "innodb_system"
    if clause == "innodb_file_per_table":
        return None
    return "general", clause


def compute_tables(server, raw, schemas=None, fresh=False, limit=DEFAULT_LIMIT, notes=None):
    """The ``schema-2.0`` report from the raw rows. Pure."""
    notes = list(notes or [])
    schemas = check_schemas(list(schemas) if schemas else None)
    tables = _table_rows(raw.get("tables"))
    keys = _unique_keys(raw.get("keys"))
    columns = _columns(raw.get("columns"))
    fold = _fold(server)
    missing, gipk = [], []
    for key in sorted(tables):
        t = tables[key]
        indexes = keys.get(key, {})
        if "PRIMARY" in indexes:
            parts = indexes["PRIMARY"]
            extra = columns.get(key, {}).get(GIPK_COLUMN, "").lower()
            if (len(parts) == 1 and (parts[0]["column"] or "").lower() == GIPK_COLUMN and "invisible" in extra
                    and "auto_increment" in extra):
                gipk.append({"schema": t["schema"], "table": t["table"], "engine": t["engine"],
                             "rows": t["rows"], "size_bytes": _size(t)})
            continue
        missing.append(_missing_entry(server, t, indexes, columns.get(key, {})))
    missing.sort(key=lambda m: (STATUS_ORDER[m["status"]], -(m["size_bytes"] or 0), m["schema"], m["table"]))
    fragmentation, shared = _fragmentation(server, raw, tables, fold)
    innodb = sum(1 for t in tables.values() if t["innodb"])
    summary = {
        "tables": len(tables), "innodb": innodb, "other_engines": len(tables) - innodb,
        "no_usable_key": sum(1 for m in missing if m["kind"] == "no_usable_key"),
        "not_null_unique": sum(1 for m in missing if m["kind"] == "not_null_unique"),
        "gipk": len(gipk),
        "free_problem": sum(1 for f in fragmentation if f["status"] == "problem"),
        "free_watch": sum(1 for f in fragmentation if f["status"] == "watch"),
        "free_info": sum(1 for f in fragmentation if f["status"] == "info"),
        "shared": sum(1 for f in fragmentation if f["status"] == "shared"),
        "unknown_tablespace": sum(1 for f in fragmentation if f["status"] == "unknown"),
    }
    # Storage engine inventory of the tables read (no extra SQL): the Full
    # analysis report judges non-InnoDB engines from it.
    engines = {}
    for t in tables.values():
        name = t["engine"] or "unknown"
        entry = engines.setdefault(name, {"engine": name, "tables": 0, "bytes": 0})
        entry["tables"] += 1
        entry["bytes"] += _size(t) or 0
    summary["engines"] = sorted(engines.values(), key=lambda e: (-e["tables"], e["engine"].lower()))
    statuses = [m["status"] for m in missing] + [f["status"] for f in fragmentation]
    summary["status"] = "problem" if "problem" in statuses else "watch" if "watch" in statuses else "ok"
    report = base_report(SCHEMA_VERSION, server)
    if limit is None:
        limit = max(len(tables), len(missing), len(gipk), len(fragmentation))
        if limit > MAX_BROWSE_ROWS:
            raise ValueError("Table results exceed 100,000 rows. Choose fewer databases.")
    inventory = [{"schema": t["schema"], "table": t["table"], "engine": t["engine"],
                  "rows": t["rows"], "data_bytes": t["data"], "index_bytes": t["index"],
                  "size_bytes": _size(t), "partitioned": t["partitioned"]}
                 for _, t in sorted(tables.items())]
    expiry = _number(server.var("information_schema_stats_expiry"))
    report.update({
        "scope": {"schemas": list(schemas) if schemas else None, "all_user_schemas": schemas is None,
                  "excluded": ["mysql", "sys", "performance_schema", "information_schema"] if schemas is None else []},
        "statistics": {
            "fresh_requested": bool(fresh), "fresh": bool(raw.get("fresh_applied")) or server.engine == "mariadb"
            or expiry == 0, "cache_expiry_s": expiry if server.engine == "mysql" else None,
            "cached": server.engine == "mysql" and not raw.get("fresh_applied") and expiry != 0,
        },
        "tablespaces_known": raw.get("spaces") is not None,
        "tablespace_collection": {"show_create_limit": MAX_SHOW_CREATE, "deadline_s": METADATA_DEADLINE_S,
                                  "unresolved": len(raw.get("spaces_unresolved", ()))},
        "summary": summary,
        "thresholds": {},
        "rules": dict(RULES),
        "why": why_texts(server),
        "tables": inventory[:limit],
        "missing_primary_keys": missing[:limit],
        "generated_primary_keys": gipk[:limit],
        "fragmentation": fragmentation[:limit],
        "shared_tablespaces": shared,
        "more": {"tables": max(0, len(inventory) - limit), "missing_primary_keys": max(0, len(missing) - limit),
                 "generated_primary_keys": max(0, len(gipk) - limit),
                 "fragmentation": max(0, len(fragmentation) - limit)},
        "notes": notes,
    })
    _notes(server, raw, report, schemas, fresh, notes)
    return report


def _size(t):
    if t["data"] is None and t["index"] is None:
        return None
    return (t["data"] or 0) + (t["index"] or 0)


def row_id_text(server):
    """How the hidden row ID is assigned on this version (module docstring)."""
    v = server.version_tuple or (0, 0, 0)
    if server.engine == "mariadb":
        if v >= (11, 0, 0):
            return ("MariaDB 11.0 and later keep this counter per table, so tables do not contend for it; it is "
                    "rebuilt from the largest row ID when the table is loaded.")
        return "It comes from one atomic counter shared by every table that has no key."
    if v >= (9, 6, 0):
        return ("It comes from one counter shared by every table that has no key (atomic since MySQL 9.6, with a "
                "dictionary mutex taken every 256 IDs).")
    return ("It comes from one counter shared by every table that has no key, and each new ID takes InnoDB's "
            "global dictionary mutex, so inserts into all such tables contend for it.")


def _missing_entry(server, t, indexes, columns):
    target = "%s.%s" % (ident(t["schema"]), ident(t["table"]))
    uniques = []
    # information_schema lists keys in the server's key order (the order
    # SHOW CREATE TABLE shows), where unique NOT NULL keys come first; InnoDB
    # clusters on the first of them.
    for name in indexes:
        parts = indexes[name]
        problem = _why_not_usable(parts, t["engine"], columns)
        uniques.append({"index": name, "columns": _columns_text(parts), "usable": problem is None,
                        "why_not": problem, "_parts": parts})
    usable = [u for u in uniques if u["usable"]]
    innodb = t["innodb"]
    entry = {"schema": t["schema"], "table": t["table"], "engine": t["engine"], "rows": t["rows"],
             "size_bytes": _size(t), "partitioned": t["partitioned"],
             "kind": "not_null_unique" if usable else "no_usable_key",
             "status": "watch" if usable else "problem",
             "unique_keys": [{k: v for k, v in u.items() if k != "_parts"} for u in uniques]}
    checks, fixes = [], []
    online = ", ALGORITHM=INPLACE, LOCK=NONE" if innodb else ""
    if usable:
        best = usable[0]
        cols = ", ".join(ident(p["column"]) for p in best["_parts"])
        if innodb:
            entry["clustered_on"] = best["index"]
            entry["why"] = (
                "InnoDB clusters the rows on %s (%s), %sthe first unique key on NOT NULL columns, which acts as the "
                "primary key. Replicas and tools that need a unique key can use it, but some checks look for a "
                "PRIMARY KEY only. Declaring it as the primary key makes that explicit."
                % (best["index"], best["columns"], "" if len(usable) == 1 else "in key order "))
        else:
            entry["clustered_on"] = None
            entry["why"] = ("%s has no clustered index. The unique key %s (%s) on NOT NULL columns identifies each "
                            "row, so replicas can find rows by it; declaring it as the primary key makes that "
                            "explicit." % (t["engine"] or "This engine", best["index"], best["columns"]))
        fixes.append({"label": "Declare %s as the primary key" % best["index"],
                      "sql": "ALTER TABLE %s ADD PRIMARY KEY (%s), DROP INDEX %s%s;"
                             % (target, cols, ident(best["index"]), online)})
    else:
        entry["clustered_on"] = "hidden row ID" if innodb else None
        plain = [u for u in uniques if u["why_not"] and u["why_not"].endswith(("allows NULL", "allow NULL"))]
        if innodb:
            text = ("InnoDB clusters the rows on a hidden 6-byte row ID (DB_ROW_ID) that queries and tools "
                    "cannot use. " + row_id_text(server))
        else:
            text = "%s has no primary key and no unique key on NOT NULL columns." % (t["engine"] or "The table")
        if uniques:
            text += " Unique keys that cannot stand in: %s." % "; ".join(
                "%s (%s): %s" % (u["index"], u["columns"], u["why_not"]) for u in uniques)
        text += (" Without a key, a replica applying row-based changes has to search the table to find the "
                 "changed rows (see Why a primary key matters).")
        entry["why"] = text
        if plain:
            best = plain[0]
            nullable = [p["column"] for p in best["_parts"] if p["nullable"]]
            checks.append({"label": "Must return 0 (rows with NULL in %s)" % ", ".join(nullable),
                           "sql": "SELECT COUNT(*) FROM %s WHERE %s;" % (
                               target, " OR ".join("%s IS NULL" % ident(c) for c in nullable))})
            fixes.append({"label": "Make %s the primary key (its columns become NOT NULL)" % best["index"],
                          "sql": "ALTER TABLE %s ADD PRIMARY KEY (%s), DROP INDEX %s%s;"
                                 % (target, ", ".join(ident(p["column"]) for p in best["_parts"]),
                                    ident(best["index"]), online)})
        else:
            checks.append({"label": "Template: must return no rows for the columns you choose",
                           "sql": "SELECT col1, col2, COUNT(*) FROM %s GROUP BY col1, col2 HAVING COUNT(*) > 1 "
                                  "LIMIT 10;" % target, "template": True})
            fixes.append({"label": "Template: a primary key on columns that identify a row",
                          "sql": "ALTER TABLE %s ADD PRIMARY KEY (col1, col2)%s;" % (target, online),
                          "template": True})
        name = next((n for n in SURROGATE_NAMES if n not in columns), "row_pk")
        fixes.append({"label": "Or add an invisible surrogate key (SELECT * and INSERT without a column list "
                               "keep working)",
                      "sql": "ALTER TABLE %s ADD COLUMN %s BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE "
                             "PRIMARY KEY FIRST%s;" % (target, ident(name),
                                                      ", ALGORITHM=INPLACE, LOCK=SHARED" if innodb else "")})
    entry["checks"] = checks
    entry["fixes"] = fixes
    caveat = ("Adding a primary key rebuilds the table: it takes time and free disk space for a new copy of the "
              "table, and replicas run the same ALTER after it finishes here, so they lag while they rebuild. ")
    if innodb:
        caveat += ("ALGORITHM=INPLACE, LOCK=NONE makes the server refuse instead of blocking writes when it cannot "
                   "do it online, for example with a FULLTEXT or SPATIAL index%s. Adding an AUTO_INCREMENT column "
                   "always blocks writes (LOCK=SHARED). "
                   % (", or when columns must become NOT NULL outside strict SQL mode"
                      if server.engine == "mysql" else ""))
    elif server.engine == "mariadb" and (server.version_tuple or (0,)) >= (11, 2, 0):
        if (t["engine"] or "").lower() == "myisam":
            caveat += ("For a MyISAM table the copy holds a table read lock, so UPDATE and DELETE wait for the "
                       "whole copy; only concurrent inserts continue. Adding an AUTO_INCREMENT column makes "
                       "inserts wait too. ")
        else:
            caveat += ("%s cannot change the table in place, so the table is copied. Writes continue during the copy "
                       "and are applied at the end, except when the ALTER adds an AUTO_INCREMENT column; then writes "
                       "wait for the whole copy. " % (t["engine"] or "This engine"))
    else:
        caveat += "%s has no online ALTER: the table is copied and writes wait for the whole copy. " % (
            t["engine"] or "This engine")
    caveat += ("The ALTER waits for transactions that use the table, and queries on it queue behind that wait. Try "
               "it on a copy or a replica first.")
    if t["partitioned"]:
        caveat += (" This table is partitioned: every unique key, the primary key included, must contain all "
                   "columns of the partitioning function.")
    entry["caveat"] = caveat
    return entry


def _fragmentation(server, raw, tables, fold):
    rows, shared = [], {}
    singles = _single_keys(raw.get("spaces"), server)
    autoextend = _autoextend(raw.get("spaces"), server)
    for key in sorted(tables):
        t = tables[key]
        if not t["innodb"]:
            continue
        free = t["free"]
        used = _size(t)
        complete = free is not None and t["data"] is not None and t["index"] is not None
        total = (used or 0) + (free or 0)
        pct = (100.0 * free / total if total else 0.0) if complete else None
        entry = {"schema": t["schema"], "table": t["table"], "rows": t["rows"], "data_bytes": t["data"],
                 "index_bytes": t["index"], "free_bytes": free, "free_pct": _round(pct, 1),
                 "partitioned": t["partitioned"], "tablespace": None, "tablespace_name": None}
        found = _tablespace(server, raw, key, fold, singles) if free else None
        if free is None:
            entry["status"] = "unknown"
        elif not free:
            entry["status"] = "ok"
        elif found is None:
            entry["status"] = "unknown"
        elif found[0] == "file_per_table":
            entry["tablespace"] = "file_per_table"
            grow = _lookup(autoextend, key, fold) or 0
            if grow:
                entry["autoextend_bytes"] = grow
            entry["status"] = "info"
        else:
            kind, name = found
            entry["tablespace"] = kind
            entry["tablespace_name"] = name
            entry["status"] = "shared"
            label = name or "shared"
            item = shared.setdefault(label, {"name": name, "type": kind, "free_bytes": 0, "tables": []})
            item["free_bytes"] = max(item["free_bytes"], free)
            item["tables"].append("%s.%s" % key)
        entry["why"] = _free_why(entry)
        # This is an optional operation, never a recommendation based on DATA_FREE.
        if entry["status"] == "info":
            target = "%s.%s" % (ident(t["schema"]), ident(t["table"]))
            entry["fix_sql"] = "OPTIMIZE TABLE %s;" % target
            entry["online_sql"] = "ALTER TABLE %s FORCE, ALGORITHM=INPLACE, LOCK=NONE;" % target
            entry["caveat"] = optimize_caveat(server)
        rows.append(entry)
    rows.sort(key=lambda f: (STATUS_ORDER[f["status"]], -(f["free_bytes"] or 0), f["schema"], f["table"]))
    shared_list = sorted(shared.values(), key=lambda s: (s["type"] != "system", s["name"] or ""))
    for item in shared_list:
        where = ("the %s tablespace %s" % (item["type"], item["name"]) if item["name"]
                 else "a shared tablespace (system or general; more than %d tables to name)" % MAX_SHOW_CREATE)
        item["why"] = ("Up to %s free in %s, which holds %d %s in scope. DATA_FREE shows that tablespace's free "
                       "space for each table in it, so it is not per table. %s"
                       % (_bytes_text(item["free_bytes"]), where, len(item["tables"]),
                          "table" if len(item["tables"]) == 1 else "tables", _shared_rebuild_text(server)))
    return rows, shared_list


def _shared_rebuild_text(server):
    if server.engine == "mariadb":
        text = ("With innodb_file_per_table=ON (the default), rebuilding a table moves it into its own file; the "
                "pages it leaves are reused inside the system tablespace")
        if (server.version_tuple or (0,)) >= (11, 2, 0):
            return text + (", which shrinks only when innodb_data_file_path has :autoshrink (MariaDB 11.2 and "
                           "later).")
        return text + ", and the file does not shrink."
    return "Rebuilding a table there frees pages inside the tablespace for reuse; the file does not shrink."


def _free_why(f, server=None):
    if f["status"] == "shared":
        return ("In %s: DATA_FREE is that whole tablespace's free space, not this table's."
                % ("the %s tablespace %s" % (f["tablespace"], f["tablespace_name"]) if f["tablespace_name"]
                   else "a shared tablespace"))
    if f["free_bytes"] is None:
        return "DATA_FREE is unavailable. Reusable space and its percentage are unknown."
    if f["status"] == "unknown":
        return "The tablespace of this table could not be read, so its free space is not judged."
    if not f["free_bytes"]:
        return ("DATA_FREE is 0: no free space beyond what InnoDB holds back (2 extents plus about 1% of the file). "
                "This does not establish that the tablespace or disk is full.")
    text = "%s reusable space" % _bytes_text(f["free_bytes"])
    if f["free_pct"] is None:
        text += "; its percentage is unknown because data or index bytes are unavailable"
    else:
        text += ", about %.0f%% of reported data, index and free bytes" % f["free_pct"]
    if f.get("autoextend_bytes"):
        text += (" (AUTOEXTEND_SIZE %s: allocation grows in steps, which can leave unused space)"
                 % _bytes_text(f["autoextend_bytes"]))
    return text + ". " + RULES["free"]


def optimize_caveat(server):
    """The OPTIMIZE TABLE caveat for this engine."""
    if server.engine == "mysql":
        return OPTIMIZE_CAVEAT
    return (OPTIMIZE_CAVEAT.replace(" (MySQL 8.4 and later then need the OPTIMIZE_LOCAL_TABLE privilege)", "")
            .replace("With a FULLTEXT or SPATIAL index the rebuild", "With a FULLTEXT or SPATIAL index, or an index "
                     "on a virtual column, the rebuild"))


OPTIMIZE_CAVEAT = (
    "Optional maintenance; review and confirm before executing. For InnoDB, OPTIMIZE TABLE rebuilds the table (the same rebuild as ALTER TABLE ... "
    "FORCE or ALTER TABLE ... ENGINE=InnoDB) and then analyzes it. The rebuild lets writes continue, with "
    "exclusive metadata locks at the start and the end; those wait for transactions that use the table, and "
    "queries on it queue behind them. With a FULLTEXT or SPATIAL index the rebuild blocks writes. It writes a "
    "new copy of the table, so it needs free disk space for one, plus temporary sort files in innodb_tmpdir and "
    "an online log of up to innodb_online_alter_log_max_size (128 MiB by default) for the writes made meanwhile. "
    "It is written to the binary log, so replicas rebuild too; "
    "OPTIMIZE NO_WRITE_TO_BINLOG TABLE keeps it on this server (MySQL 8.4 and later then need the "
    "OPTIMIZE_LOCAL_TABLE privilege). If the table will grow back, the free space would be reused anyway and a "
    "rebuild gains little.")


def why_texts(server):
    """Why a primary key matters, for this engine. Shown once per report."""
    mariadb = server.engine == "mariadb"
    return [
        {"key": "replication", "title": "Replicas find each changed row by its key",
         "text": ("With row-based replication each UPDATE or DELETE reaches the replica as the changed rows, and "
                  "the replica has to find each one. With a primary key or a unique key on NOT NULL columns that "
                  "is one index lookup. Without one, MariaDB uses the most selective other index if there is one, "
                  "or else reads the table from the start until it finds the row, for every changed row: deleting "
                  "10,000 rows can mean 10,000 table scans, and the replica falls behind."
                  if mariadb else
                  "With row-based replication (the default) each UPDATE or DELETE reaches the replica as the "
                  "changed rows, and the replica has to find each one. With a primary key or a unique key on NOT "
                  "NULL columns that is one index lookup per row. Without one, MySQL hashes the rows of each "
                  "event and looks them up through another index if there is one, or else makes up to one full "
                  "table scan per event. An event holds about 8 KiB of rows (binlog_row_event_max_size), so a "
                  "large DELETE means many full scans and the replica falls behind.")},
        {"key": "clustering", "title": "InnoDB stores rows in primary key order",
         "text": ("InnoDB keeps each table as a B-tree ordered by the primary key, and every secondary index "
                  "stores the primary key to reach the row. Without a primary key it uses the first unique key on "
                  "NOT NULL columns (whole columns, not a prefix, %s). Without one it adds a hidden 6-byte row ID."
                  % ("a generated column or a long hash key" if mariadb else
                     "a functional part or a virtual generated column"))},
        {"key": "row_id", "title": "The hidden row ID",
         "text": "Queries and tools cannot see or use the hidden row ID to find, page through or deduplicate "
                 "rows. " + row_id_text(server)},
        {"key": "clusters", "title": ("Galera needs a primary key" if mariadb else
                                      "Group Replication needs a primary key"),
         "text": ("Galera certifies each change by its key. For a table without a primary key it hashes the "
                  "whole row instead (wsrep_certify_nonPK, on by default). MariaDB's documentation asks for a "
                  "primary key on every table in a Galera cluster, and wsrep_mode=REQUIRED_PRIMARY_KEY (10.6 and "
                  "later) refuses to create tables without one."
                  if mariadb else
                  "Group Replication refuses writes to a table without a primary key or a unique key on NOT NULL "
                  "columns: the client sees \"The table does not comply with the requirements by an external "
                  "plugin.\"")},
        {"key": "tools", "title": "Online schema change tools need a key",
         "text": ("pt-online-schema-change and gh-ost copy rows in chunks by a primary or unique key; per their "
                  "documentation they refuse a table without one, so the server's own online ALTER is the only "
                  "online option left.")},
    ]


def _notes(server, raw, report, schemas, fresh, notes):
    stats = report["statistics"]
    if server.engine == "mysql":
        expiry = stats["cache_expiry_s"]
        if raw.get("fresh_applied"):
            notes.append("Fresh numbers: information_schema_stats_expiry was set to 0 for myflames' own connection "
                         "only (SET SESSION; the server setting is unchanged), so InnoDB was asked for every table "
                         "in scope. That takes longer on servers with many tables.")
        elif expiry:
            notes.append("Row counts, sizes and free space come from a cache MySQL keeps for up to %s "
                         "(information_schema_stats_expiry; partitioned tables are never cached), so they can be "
                         "that old. Fresh numbers (--fresh) reads them from InnoDB now, for myflames' own "
                         "connection only." % _duration_text(expiry))
    elif fresh:
        notes.append("MariaDB has no information_schema statistics cache: every read asks InnoDB, so Fresh "
                     "numbers changes nothing here.")
    notes.append("TABLE_ROWS, DATA_LENGTH and INDEX_LENGTH are InnoDB estimates from its table statistics, "
                 "recalculated in the background after about 10%% of the rows change (persistent statistics, the "
                 "default) or by ANALYZE TABLE. DATA_FREE is %s. It counts whole free extents and the unused "
                 "space at the end of the file (an extent is 1 MiB with 4 to 16 KiB pages, 2 MiB with 32 KiB, "
                 "4 MiB with 64 KiB), minus a reserve of 2 extents plus about 1%%; free pages in partly used "
                 "extents and half-empty pages after scattered deletes are not in it, so it understates."
                 % ("read from the tablespace every time" if server.engine == "mariadb" or raw.get("fresh_applied")
                    or stats.get("cache_expiry_s") == 0 else
                    "read with the other statistics, so it comes from the same cache unless Fresh numbers is on"))
    if raw.get("gipk_session"):
        notes.append("show_gipk_in_create_table_and_information_schema is OFF on this server; myflames turned it "
                     "ON for its own connection so generated invisible primary keys are visible.")
    if _on(server.var("sql_generate_invisible_primary_key")):
        notes.append("sql_generate_invisible_primary_key is ON: new InnoDB tables created without a primary key get "
                     "one (my_row_id)%s. Existing tables are not changed."
                     % (" (since 9.7, not when the table declares a unique key on NOT NULL columns)"
                        if (server.version_tuple or (0,)) >= (9, 7, 0) else ""))
    if _on(server.var("sql_require_primary_key")):
        notes.append("sql_require_primary_key is ON: the server refuses to create a table without a primary key%s."
                     % (" (since 9.7 a unique key on NOT NULL columns also satisfies it)"
                        if (server.version_tuple or (0,)) >= (9, 7, 0) else ""))
    if _on(server.var("innodb_force_primary_key")):
        notes.append("innodb_force_primary_key is ON: InnoDB refuses to create a table without a primary key or a "
                     "unique key on NOT NULL columns.")
    flagged = report["summary"]["free_info"]
    if _on(server.var("innodb_optimize_fulltext_only")) and flagged:
        notes.append("innodb_optimize_fulltext_only is ON: OPTIMIZE TABLE then only optimizes FULLTEXT indexes and "
                     "does not rebuild the table. To rebuild, use the ALTER TABLE ... FORCE statement instead.")
    if _on(server.var("innodb_defragment")) and flagged:
        notes.append("innodb_defragment is ON: OPTIMIZE TABLE then defragments indexes instead of rebuilding the "
                     "table. To rebuild, use the ALTER TABLE ... FORCE statement instead.")
    if _on(server.var("wsrep_on")):
        notes.append("This server is a Galera node (wsrep_on=ON): tables without a primary key are the ones to fix "
                     "first.")
    if server.var("group_replication_group_name"):
        notes.append("Group Replication is configured on this server (group_replication_group_name is set): it "
                     "refuses writes to tables without a primary key or a unique key on NOT NULL columns.")
    if not report["summary"]["tables"]:
        notes.insert(0, "No base tables in %s that this account can see." % scope_text(schemas))
    from .perf_common import ALL_SCHEMAS_TABLE_LIMIT
    if report["summary"]["tables"] > ALL_SCHEMAS_TABLE_LIMIT and (server.engine == "mariadb"
                                                                  or raw.get("fresh_applied")):
        notes.append("This read covered %s tables and %s each of them, which takes a while on servers with many "
                     "tables; choose databases to read fewer."
                     % ("{:,}".format(report["summary"]["tables"]),
                        "MariaDB opened" if server.engine == "mariadb" else "Fresh numbers asked InnoDB about"))
    other = report["summary"]["other_engines"]
    if other:
        notes.append("%d %s not InnoDB: listed under missing primary keys when they lack one, never under free "
                     "space (DATA_FREE means something else for other engines)."
                     % (other, "table is" if other == 1 else "tables are"))
    if report["summary"]["unknown_tablespace"] and report["tablespaces_known"]:
        notes.append("Some tablespaces could not be resolved (metadata lookup limits, unavailable definitions, "
                     "names myflames cannot decode, or tables created during the read); their free space "
                     "is not judged.")
    if schemas is None:
        notes.append("Read every schema except mysql, sys, performance_schema and information_schema.")


def at_or_worse(report, level):
    """Rows (missing keys and free space) at *level* ('watch' or 'problem') or worse."""
    floor = SEVERITY[level]
    out = []
    for m in report["missing_primary_keys"]:
        if SEVERITY.get(m["status"], -1) >= floor:
            out.append({"status": m["status"], "label": "%s.%s" % (m["schema"], m["table"]),
                        "why": "no primary key" + ("" if m["kind"] == "no_usable_key"
                                                   else " (a unique NOT NULL key stands in)")})
    for f in report["fragmentation"]:
        if SEVERITY.get(f["status"], -1) >= floor:
            out.append({"status": f["status"], "label": "%s.%s" % (f["schema"], f["table"]),
                        "why": "%s free (%.0f%%)" % (_bytes_text(f["free_bytes"] or 0), f["free_pct"])})
    return out

# ---- text output for the CLI -------------------------------------------------------

_MARK = {"ok": "ok", "info": "info", "watch": "WATCH", "problem": "PROBLEM", "shared": "shared", "unknown": "?"}


def _rows_text(value):
    return "—" if value is None else "~" + _count_text(value)


def format_text(report):
    head = "%s %s" % ("MariaDB" if report["engine"] == "mariadb" else "MySQL", report["server_version"] or "?")
    if report["uptime_s"] is not None:
        head += ", up %s" % _duration_text(report["uptime_s"])
    head += ", %s" % scope_text(report["scope"]["schemas"])
    s = report["summary"]
    stats = report["statistics"]
    if report["engine"] == "mysql":
        head += "; statistics %s" % ("fresh" if stats["fresh"] else "cached up to %s (--fresh reads them now)"
                                     % _duration_text(stats["cache_expiry_s"] or 0))
    lines = [head, "%d tables (%d InnoDB); %d without a usable key, %d with only a unique NOT NULL key; free space: "
             "%d informational observations" % (s["tables"], s["innodb"], s["no_usable_key"], s["not_null_unique"],
                                       s["free_info"]), ""]
    lines.append("TABLES WITHOUT A PRIMARY KEY")
    if not report["missing_primary_keys"]:
        lines.append("  None.")
    else:
        rows = [[_MARK[m["status"]], "%s.%s" % (m["schema"], m["table"]), m["engine"] or "?", _rows_text(m["rows"]),
                 _bytes_text(m["size_bytes"]) if m["size_bytes"] is not None else "—",
                 (m["clustered_on"] or "") if m["kind"] == "no_usable_key" else
                 "unique NOT NULL: " + ", ".join(u["index"] for u in m["unique_keys"] if u["usable"])]
                for m in report["missing_primary_keys"]]
        lines.extend("  " + l for l in table_text(
            ["STATUS", "TABLE", "ENGINE", ">ROWS", ">SIZE", "CLUSTERED ON / KEY"], rows).split("\n"))
        if report["more"]["missing_primary_keys"]:
            lines.append("  ... %d more" % report["more"]["missing_primary_keys"])
    if report["generated_primary_keys"]:
        lines.append("  Generated invisible primary key (my_row_id), fine: %s" % ", ".join(
            "%s.%s" % (g["schema"], g["table"]) for g in report["generated_primary_keys"]))
    lines.append("")
    lines.append("FREE SPACE (InnoDB, file-per-table tablespaces)")
    flagged = [f for f in report["fragmentation"] if f["status"] == "info"]
    lines.append("  " + RULES["free"])
    if not flagged:
        lines.append("  No file-per-table reusable space reported.")
    else:
        rows = [[_MARK[f["status"]], "%s.%s" % (f["schema"], f["table"]), _rows_text(f["rows"]),
                 _bytes_text((f["data_bytes"] or 0) + (f["index_bytes"] or 0)), _bytes_text(f["free_bytes"] or 0),
                 ("%.0f%%" % f["free_pct"] if f["free_pct"] is not None else "unknown")] for f in flagged]
        lines.extend("  " + l for l in table_text(
            ["STATUS", "TABLE", ">ROWS", ">DATA+INDEX", ">FREE", ">FREE %"], rows).split("\n"))
    if report["shared_tablespaces"]:
        lines.append("")
        lines.append("SHARED TABLESPACES (free space is the whole tablespace's, not per table)")
        for item in report["shared_tablespaces"]:
            lines.append("  %s (%s): %s free; %s" % (item["name"], item["type"], _bytes_text(item["free_bytes"]),
                                                    ", ".join(item["tables"][:10])
                                                    + (" and %d more" % (len(item["tables"]) - 10)
                                                       if len(item["tables"]) > 10 else "")))
    statements = [(m, next(x for x in m["fixes"] if not x.get("template")))
                  for m in report["missing_primary_keys"] if m["fixes"]]
    statements += [(f, {"sql": f["fix_sql"]}) for f in flagged]
    if statements:
        lines += ["", "STATEMENTS TO REVIEW (never run by myflames; --json has the checks and caveats)"]
        for item, fix in statements:
            lines.append("  %s" % fix["sql"])
    return "\n".join(lines) + "\n"
