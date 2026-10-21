"""Shared plumbing for the Performance workspace's live server views.

``myflames waits`` (waits.py), ``indexes`` (indexes.py), ``io`` (io_stats.py)
and ``config`` (config_review.py) all read a live MySQL or MariaDB server the
same way, so the parts they share live here:

* :class:`Reader` runs tagged, read-only SQL on one connection. Every
  statement carries a ``/*myflames:<view>*/`` prefix (kept with
  ``--comments``) so ``myflames top`` leaves it out.
* :func:`preflight` reads the server version, ``performance_schema`` and a
  few other variables with ``SHOW GLOBAL VARIABLES WHERE Variable_name IN
  (...)``, which never fails for a name the server does not have (``SELECT
  @@x`` does), plus ``Uptime`` and the ``Performance_schema_*_lost`` counters.
* :func:`sample_twice` takes two snapshots *interval* seconds apart, the way
  health.py does.

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"):

* ``performance_schema`` is ON by default on MySQL (M/sql/sys_vars.cc:570-574)
  and OFF on MariaDB (Ma/sql/sys_vars.cc:89-92); read-only on both. With it
  off, Performance Schema tables still exist and return no rows, not an error
  (M/storage/perfschema/ha_perfschema.cc:111-112, 1758-1762;
  Ma/storage/perfschema/ha_perfschema.cc:346-351), so every view checks the
  variable first instead of trusting an empty result.
* ``Performance_schema_*_lost`` above zero means some instruments, tables or
  index statistics could not be allocated, so counts are incomplete
  (M/storage/perfschema/ha_perfschema.cc:1555, 1566).
* Summary tables need SELECT on performance_schema
  (M/storage/perfschema/pfs_engine_table.cc:1041-1125).
"""
import datetime
import re
import time

from .connector import ConnectorError, MySQLConnection, unescape_mysql
from .server_info import detect_flavor, product_identity, server_version_tuple as _version_tuple

#: Schemas never reported as user data.
SYSTEM_SCHEMAS = ("mysql", "performance_schema", "information_schema", "sys")
PICO_PER_MS = 1e9
#: Over-a-window views (waits, I/O) sample this many seconds apart.
DEFAULT_WINDOW = 10
MIN_WINDOW, MAX_WINDOW = 2, 60
#: The variables every view's preflight reads.
PREFLIGHT_VARIABLES = ("version", "version_comment", "performance_schema", "userstat")

_LOST_SQL = ("SHOW GLOBAL STATUS WHERE Variable_name = 'Uptime' "
             "OR Variable_name LIKE 'Performance\\_schema\\_%\\_lost'")


def _number(value):
    """An int (exact, for 64-bit counters) or float from a status string, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number == int(number) else number


def _round(value, digits=2):
    if value is None:
        return None
    rounded = round(value, digits)
    return int(rounded) if rounded == int(rounded) and abs(rounded) < 1e15 else rounded


def _count_text(value):
    if value is None:
        return "?"
    if isinstance(value, float) and value != int(value):
        if abs(value) >= 100:
            return "{:,.0f}".format(value)
        return "{:,.1f}".format(value) if abs(value) >= 10 else "{:,.2f}".format(value)
    return "{:,}".format(int(value))


def _bytes_text(value):
    if value is None:
        return "?"
    for limit, suffix in ((1 << 40, "TiB"), (1 << 30, "GiB"), (1 << 20, "MiB"), (1 << 10, "KiB")):
        if abs(value) >= limit:
            return "%.1f %s" % (value / float(limit), suffix)
    return "%d B" % value


def _duration_text(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return "%d d %d h" % (days, hours)
    if hours:
        return "%d h %d min" % (hours, minutes)
    if minutes:
        return "%d min %d s" % (minutes, secs)
    return "%d s" % secs


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def reason(exc):
    """The server's message without the client wrapper."""
    return re.sub(r"^mysql client exited \d+:\s*", "", " ".join(str(exc).split()))


# Compatibility name used by discovery and locks; keep its narrower contract.
def _denied(exc):
    text = str(exc)
    return "command denied" in text or "Access denied" in text or "ERROR 1142" in text

# Both historical names use the same message normalization.
_reason = reason


def denied(exc):
    return _denied(exc) or "ERROR 1227" in str(exc)


def text_rows(conn, sql, tag):
    """Tagged batch rows with escaped cells decoded and literal NULL retained.

    Reader.rows has a separate, intentional contract: SQL NULL becomes None.
    """
    text = conn.run(tag + sql, extra_flags=["--comments"])
    return [[unescape_mysql(c) for c in line.split("\t")]
            for line in text.split("\n") if line]


#: The statement _server sends; discover.SHARED_METADATA_SQL lists it.
SERVER_STATE_SQL = "SELECT @@version, @@performance_schema"


def _server(conn, tag="/*myflames:discover*/ "):
    """Return (engine, version string, version tuple, performance_schema on)."""
    try:
        rows = text_rows(conn, SERVER_STATE_SQL, tag)
    except ConnectorError as exc:
        if "performance_schema" in str(exc) and "nknown system variable" in str(exc):
            version = conn.server_version()
            engine = "mariadb" if conn.is_mariadb() else "mysql"
            return engine, version, _version_tuple(version), False
        raise
    version = rows[0][0] if rows and rows[0] else conn.server_version()
    enabled = rows[0][1].strip().upper() in ("1", "ON") if rows and len(rows[0]) > 1 else False
    # Older connection adapters expose only server_version/is_mariadb.
    # The connector also supplies the comment, without changing this query's
    # established two-column result or its monitoring tag.
    comment_reader = getattr(conn, "server_version_comment", None)
    comment = (comment_reader() if callable(comment_reader) and detect_flavor(version) != "mariadb"
               else "")
    engine = detect_flavor(version, comment)
    return engine, version, _version_tuple(version), enabled


def literal(text):
    """Quote user text as a hex literal so no escaping rules apply."""
    return "CONVERT(X'%s' USING utf8mb4)" % text.encode("utf-8").hex()


def check_window(interval, low=MIN_WINDOW, high=MAX_WINDOW):
    if isinstance(interval, bool) or not isinstance(interval, int) or not low <= interval <= high:
        raise ValueError("Choose a sampling interval from %d to %d seconds." % (low, high))
    return interval


def check_schema(schema):
    """None, or a schema name a server can have (1 to 64 characters)."""
    if schema is None or schema == "":
        return None
    if not isinstance(schema, str) or len(schema) > 64 or not schema.strip() \
            or any(c in schema for c in "\x00\n\r"):
        raise ValueError("Choose a schema name of 1 to 64 characters.")
    return schema


def check_limit(limit, high=500):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= high:
        raise ValueError("Choose a limit from 1 to %d." % high)
    return limit


def ps_time_ms(value):
    """Picoseconds (Performance Schema timers) as milliseconds."""
    number = _number(value)
    return None if number is None else number / PICO_PER_MS


class Reader(object):
    """Runs tagged read-only SQL on one connection."""

    def __init__(self, conn, tag):
        self.conn = conn
        self.tag = tag

    def rows(self, sql, timeout=None):
        options = {} if timeout is None else {"timeout": timeout}
        text = self.conn.run(self.tag + sql, extra_flags=["--comments"], **options)
        out = []
        # Only \n separates rows (see connector.query_rows).
        for line in text.replace("\r\n", "\n").split("\n"):
            if line:
                out.append([None if cell == "NULL" else unescape_mysql(cell)
                            for cell in line.split("\t")])
        return out

    def pairs(self, sql):
        return {row[0]: (row[1] if len(row) > 1 else "") for row in self.rows(sql) if row}

    def named(self, sql):
        """Rows as dicts read by column name, for SHOW statements whose
        columns differ by engine and version (SHOW REPLICA STATUS). The
        connector prints no column names (--batch --silent), so the result
        is read as the client's XML output (--xml), which names every field
        and marks NULL with xsi:nil."""
        return named_rows(self.conn.run(self.tag + sql, extra_flags=["--comments", "--xml"]))

    def optional(self, sql, what, notes, timeout=None):
        """Rows, or None with a note when the server refuses (privileges, a
        missing table on this version). Connection failures still raise."""
        try:
            return self.rows(sql, timeout=timeout)
        except ConnectorError as exc:
            if "ERROR 2002" in str(exc) or "ERROR 2003" in str(exc) or "ERROR 2013" in str(exc) \
                    or "timed out" in str(exc):
                raise
            notes.append("Could not read %s: %s" % (what, reason(exc)))
            return None


_XML_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def named_rows(text):
    """``mysql --xml`` output as a list of ``{column: text or None}`` dicts."""
    import xml.etree.ElementTree as ElementTree
    if not (text or "").strip():
        return []
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("Unexpected XML from the client.")
    try:
        root = ElementTree.fromstring(text.strip())
    except ElementTree.ParseError as exc:
        raise ValueError("Could not read the client's XML output: %s" % exc)
    return [{field.get("name"): (None if field.get(_XML_NIL) == "true" else (field.text or ""))
             for field in row.findall("field")} for row in root.findall("row")]


class Server(object):
    """What the preflight learned: engine, version, variables, status."""

    def __init__(self, variables, status):
        self.variables = variables or {}
        self.status = status or {}
        self.version = self.variables.get("version") or ""
        comment = self.variables.get("version_comment") or ""
        identity = product_identity(self.version, comment)
        self.engine = identity["engine"]
        #: mysql, percona or mariadb; Percona Server keeps engine "mysql".
        self.flavor = identity["flavor"]
        #: The name to show: MySQL, Percona Server, Percona XtraDB Cluster or MariaDB.
        self.product = identity["product"]
        self.version_tuple = _version_tuple(self.version)
        self.ps_on = (self.variables.get("performance_schema") or "").upper() in ("ON", "1")
        self.uptime = _number(self.status.get("Uptime"))
        self.userstat = (self.variables.get("userstat") or "").upper() in ("ON", "1")

    @property
    def label(self):
        return self.product

    def var(self, name):
        return self.variables.get(name)

    def lost(self):
        """Names of ``Performance_schema_*_lost`` counters above zero."""
        return sorted(name for name, value in self.status.items()
                      if name.startswith("Performance_schema_") and name.endswith("_lost")
                      and (_number(value) or 0) > 0)

    def describe(self):
        return {"engine": self.engine, "flavor": self.flavor, "product": self.product,
                "version": self.version or None, "uptime_s": self.uptime,
                "performance_schema": self.ps_on}


def show_variables(reader, names):
    names = sorted(set(names))
    return reader.pairs("SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)"
                        % ", ".join("'%s'" % n for n in names))


def preflight(reader, extra_variables=()):
    """The server's engine, version, P_S state, Uptime and lost counters."""
    variables = show_variables(reader, PREFLIGHT_VARIABLES + tuple(extra_variables))
    status = reader.pairs(_LOST_SQL)
    return Server(variables, status)


def ps_off_text(server, what):
    """Why a Performance Schema view is empty, and how to turn it on."""
    if server.engine == "mariadb":
        return ("Performance Schema is off, so %s cannot be read. MariaDB ships with it off. "
                "To turn it on, add performance_schema=ON under [mysqld] in my.cnf and restart "
                "the server (it cannot be changed at runtime). It then uses some memory and a "
                "little CPU." % what)
    return ("Performance Schema is off (performance_schema=OFF), so %s cannot be read. Set "
            "performance_schema=ON under [mysqld] in my.cnf and restart the server (it cannot "
            "be changed at runtime)." % what)


def lost_note(server):
    lost = server.lost()
    if not lost:
        return None
    return ("Performance Schema could not record everything: %s above zero. Some rows are "
            "missing, so absent data does not prove there was no activity. Raise the matching "
            "performance_schema_max_* or *_size setting (needs a restart)." % ", ".join(lost))


def consumer_state(reader, notes):
    """{name: enabled} for the two consumers summary tables depend on."""
    rows = reader.optional("SELECT NAME, ENABLED FROM performance_schema.setup_consumers "
                           "WHERE NAME IN ('global_instrumentation', 'thread_instrumentation')",
                           "performance_schema.setup_consumers", notes)
    if rows is None:
        return None
    return {row[0]: (row[1] or "").upper() == "YES" for row in rows if row}


def consumer_note(consumers):
    if consumers is None:
        return None
    if consumers.get("global_instrumentation") is False:
        return ("The global_instrumentation consumer is off, so no summary table is collecting. "
                "To turn it on: UPDATE performance_schema.setup_consumers SET ENABLED = 'YES' "
                "WHERE NAME = 'global_instrumentation'; (until restart; add "
                "performance-schema-consumer-global-instrumentation=ON to my.cnf to keep it).")
    return None


def sample_twice(take, interval, sleep=None, clock=None):
    """Two snapshots *interval* seconds apart: ``(first, second)``.

    *take(clock)* returns a dict with ``t`` (the monotonic clock when its
    result arrived) and ``taken_at``. Waits only for the time left after the
    first snapshot, as health.py does.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    first = take(clock)
    remaining = interval - (clock() - first["t"])
    if remaining > 0:
        sleep(remaining)
    second = take(clock)
    return first, second


def elapsed(first, second):
    if not first or not second:
        return None
    value = (second.get("t") or 0) - (first.get("t") or 0)
    return value if value > 0 else None


def restarted(first, second):
    a, b = _number((first or {}).get("uptime")), _number((second or {}).get("uptime"))
    return a is not None and b is not None and b < a


def identity_fields(engine, version, version_comment=None):
    """``flavor`` and ``server_product`` for a report built from engine and
    version alone; the comment decides Percona (see server_info)."""
    identity = product_identity(version, version_comment or "")
    if engine == "mariadb" and identity["engine"] != "mariadb":
        identity = {"flavor": "mariadb", "product": "MariaDB"}
    return {"flavor": identity["flavor"], "server_product": identity["product"]}


def conn_identity(conn, engine, version):
    """identity_fields from a connection's cached ``@@version_comment``.

    The connector caches the comment once read (``_server`` reads it), so
    this normally sends nothing; connection doubles without the reader are
    MySQL or MariaDB by engine."""
    comment = None
    if engine != "mariadb":
        reader = getattr(conn, "server_version_comment", None)
        if callable(reader):
            try:
                comment = reader()
            except ConnectorError:
                comment = None
    return identity_fields(engine, version, comment)


def base_report(schema_version, server, taken_at=None):
    return {
        "schema_version": schema_version,
        "engine": server.engine,
        "flavor": getattr(server, "flavor", "mariadb" if server.engine == "mariadb" else "mysql"),
        "server_product": getattr(server, "product", "MariaDB" if server.engine == "mariadb" else "MySQL"),
        "server_version": server.version or None,
        "taken_at": taken_at or now(),
        "uptime_s": server.uptime,
        "performance_schema": server.ps_on,
    }


def ms_text(ms):
    """Milliseconds as "1.23 s", "45.6 ms", "120 µs", or "—"."""
    if ms is None:
        return "—"
    if ms >= 1000:
        return "%.2f s" % (ms / 1000.0)
    if ms >= 1:
        return "%.1f ms" % ms
    if ms >= 0.001:
        return "%.0f µs" % (ms * 1000)
    if ms > 0:
        return "%.0f ns" % (ms * 1e6)
    return "0"


def table_text(headers, rows):
    """Plain text table; headers starting with '>' are right-aligned."""
    right = [h.startswith(">") for h in headers]
    headers = [h.lstrip(">") for h in headers]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            if right[i]:
                out.append(cell.rjust(widths[i]))
            else:
                out.append(cell if i == len(cells) - 1 else cell.ljust(widths[i]))
        return "  ".join(out).rstrip()
    return "\n".join([line(headers)] + [line(r) for r in rows])


def ident(name):
    """Backquote an identifier for copy-paste SQL."""
    return "`" + (name or "").replace("`", "``") + "`"

# ---- schema selection (shared by Indexes and Tables) ------------------------------

#: Above this many base tables in the selected scope, Indexes and Tables
#: require a narrower scope or an explicit override: 2,000 is MySQL's default
#: table_definition_cache, see :data:`ALL_SCHEMAS_NOTE`.
ALL_SCHEMAS_TABLE_LIMIT = 2000
#: The most schemas one read may name (the picker's selection).
MAX_SCHEMAS = 200
CATALOG_VERSION = "schemas-1.0"
#: Base tables, plus MariaDB's system-versioned tables (sequences and views
#: are not tables to judge).
TABLE_TYPES_SQL = "TABLE_TYPE IN ('BASE TABLE', 'SYSTEM VERSIONED')"
def all_schemas_note(limit=ALL_SCHEMAS_TABLE_LIMIT, engine=None):
    """Why a read of every user schema needs a choice above *limit* tables.
    MySQL's table_definition_cache defaults to min(400 + table_open_cache / 2,
    2000) with table_open_cache 4000 (M/sql/mysqld.cc:10820, sys_vars.h:103);
    the Performance Schema cost is in indexes.py."""
    note = ("The default limit is %s tables in the selected scope. Above that, choose fewer schemas or pass "
            "all_schemas / --all-schemas explicitly: the read costs "
            "time per table (Performance Schema walks every table and open handle for each index row, and MariaDB "
            "opens every table for information_schema.STATISTICS); 2,000 matches MySQL's default "
            "table_definition_cache." % "{:,}".format(limit))
    if engine == "mariadb":
        note += " On MariaDB, this read opens every table in scope and can evict cached table definitions."
    return note


ALL_SCHEMAS_NOTE = all_schemas_note()


def is_system_schema(name):
    """mysql, sys, performance_schema, information_schema (any case), and the
    ``#mysql50#`` names MariaDB gives datadir directories it cannot decode
    (lost+found, for example; filename_to_tablename, Ma/sql/sql_table.cc:446-448)."""
    return (name or "").lower() in SYSTEM_SCHEMAS or (name or "").startswith("#mysql50#")


def _table_count_sql(server, group, scope=None):
    """Tables per schema (or in total). MySQL counts base tables from the data
    dictionary without opening any (M/sql/dd/impl/system_views/tables.cc:36-46).
    On MariaDB, TABLE_TYPE needs each table's .frm, but selecting only name
    columns skips the open (Ma/sql/sql_show.cc:5650, 10059), so the count
    there includes views and sequences."""
    where = [] if server.engine == "mariadb" else [TABLE_TYPES_SQL]
    if scope:
        where.append(scope)
    return "SELECT %sCOUNT(*) FROM information_schema.TABLES%s%s" % (
        "TABLE_SCHEMA, " if group else "", " WHERE " + " AND ".join(where) if where else "",
        " GROUP BY TABLE_SCHEMA" if group else "")


def check_schemas(schemas):
    """None (every user schema), or a tuple of distinct valid schema names.

    Accepts a list or tuple; an empty one means every user schema."""
    if schemas is None:
        return None
    if isinstance(schemas, str) or not isinstance(schemas, (list, tuple)):
        raise ValueError("Choose schemas as a list of names.")
    out = []
    for name in schemas:
        name = check_schema(name)
        if name and name not in out:
            out.append(name)
    if len(out) > MAX_SCHEMAS:
        raise ValueError("Choose at most %d schemas." % MAX_SCHEMAS)
    return tuple(out) or None


def scope_sql(column, schemas):
    """``column = x`` for one schema (MariaDB then reads only that schema's
    directory), ``column IN (...)`` for several (MariaDB still checks the name
    before opening a table, Ma/sql/sql_show.cc:5639), else every non-system
    schema."""
    if schemas and len(schemas) == 1:
        return "%s = %s" % (column, literal(schemas[0]))
    if schemas:
        return "%s IN (%s)" % (column, ", ".join(literal(s) for s in schemas))
    return "%s NOT IN (%s)" % (column, ", ".join("'%s'" % s for s in SYSTEM_SCHEMAS))


def scope_text(schemas):
    if not schemas:
        return "every user schema"
    if len(schemas) == 1:
        return "schema %s" % schemas[0]
    if len(schemas) <= 4:
        return "schemas %s" % ", ".join(schemas)
    return "%d schemas" % len(schemas)


def list_schemas(reader, server):
    """[{name, tables, system}] for every schema the account can see, with its
    table count (see _table_count_sql). information_schema.SCHEMATA lists
    schemas without tables too. No table is opened on either engine."""
    names = [row[0] for row in reader.rows("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA") if row and row[0]]
    counts = {}
    for row in reader.rows(_table_count_sql(server, True)):
        if len(row) >= 2 and row[0]:
            counts[row[0]] = int(_number(row[1]) or 0)
            if row[0] not in names:
                names.append(row[0])
    out = [{"name": n, "tables": counts.get(n, 0), "system": is_system_schema(n)} for n in names]
    out.sort(key=lambda s: (s["system"], s["name"].lower()))
    return out


def user_table_count(catalog, schemas=None):
    chosen = set(schemas or ())
    return sum(s["tables"] for s in catalog if (s["name"] in chosen if chosen else not s["system"]))


def schema_catalog(conn, tag="/*myflames:schemas*/ "):
    """The ``schemas-1.0`` list behind the workspace's schema picker."""
    reader = Reader(conn, tag)
    try:
        server = preflight(reader)
        catalog = list_schemas(reader, server)
    except ConnectorError as exc:
        raise ValueError("Could not list schemas: " + reason(exc))
    report = base_report(CATALOG_VERSION, server)
    report.update({"schemas": catalog, "user_tables": user_table_count(catalog),
                   "all_limit": ALL_SCHEMAS_TABLE_LIMIT, "all_note": all_schemas_note(engine=server.engine),
                   "system": list(SYSTEM_SCHEMAS)})
    return report


def require_choice(reader, server, schemas, limit=ALL_SCHEMAS_TABLE_LIMIT, what="this view"):
    """Gate the selected scope before costly metadata reads; None opts out."""
    if limit is None:
        return None
    rows = reader.rows(_table_count_sql(server, False, scope_sql("TABLE_SCHEMA", schemas)))
    total = int(_number(rows[0][0]) or 0) if rows and rows[0] else 0
    if total > limit:
        raise ValueError("The selected scope (%s) has %s tables, more than %s, so %s needs %s or an explicit "
                         "all_schemas / --all-schemas override. %s" %
                         (scope_text(schemas), "{:,}".format(total), "{:,}".format(limit), what,
                          "fewer schemas" if schemas else "a choice of schemas", all_schemas_note(limit, server.engine)))
    return total
