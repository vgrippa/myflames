"""Configuration review: a short list of server settings checked against
source-verified defaults and rules of thumb.

:func:`config_report` returns the ``config-1.0`` envelope shared by ``myflames
config`` and the workspace's Server > Config tab. It reads the running values
with ``SHOW GLOBAL VARIABLES WHERE Variable_name IN (...)`` (a removed or
engine-specific name is simply absent; ``SELECT @@x`` would fail), a few
``SHOW GLOBAL STATUS`` counters since startup, where each value came from
(``performance_schema.variables_info`` on MySQL, readable with Performance
Schema off; ``information_schema.SYSTEM_VARIABLES`` on MariaDB), and the
InnoDB tablespace sizes (needs PROCESS). Nothing is changed: suggested
statements are text for the user to run, each with why and a caveat. Changing
a setting is variables.py (``myflames config set``, the inspector's Change),
one confirmed statement at a time; each setting carries ``dynamic`` from
:func:`variables.static_dynamic` so the inspector can label it.

Statuses follow health.py: ``ok``, ``watch``, ``problem``, ``info`` (a fact
with no universal rule) and ``unknown``. A status is given only where a rule
is defensible; the rule is in ``rule``.

Defaults and behavior verified against server source (mysql-server 9.7 and
8.4 tags, mariadb-server 13.0 and the 10.11, 11.4, 11.8 tags; the review
notes have every citation):

==============================  ==========================  =========================
variable                        MySQL 8.4 / 9.x             MariaDB 10.11 to 13.0
==============================  ==========================  =========================
innodb_buffer_pool_size         128 MiB                     128 MiB
innodb_redo_log_capacity        100 MiB (8.0.30+)           does not exist
innodb_log_file_size            48 MiB, deprecated since    96 MiB, the redo size,
                                8.0.30, removed in 9.3      dynamic
innodb_flush_log_at_trx_commit  1 (0 to 2)                  1 (0 to 3; 1 and 3 sync at
                                                            prepare, 3 also at commit
                                                            with the binary log on)
sync_binlog                     1                           0
innodb_flush_method             O_DIRECT when supported     O_DIRECT; deprecated
                                                            from 11.0.1
max_connections                 151                         151
table_open_cache                4000                        2000
tmp_table_size                  16 MiB                      16 MiB, alias of
                                                            tmp_memory_table_size
max_heap_table_size             16 MiB                      16 MiB
internal_tmp_mem_storage_engine TempTable                   does not exist
sort_buffer_size                256 KiB                     2 MiB
join_buffer_size                256 KiB                     256 KiB
innodb_io_capacity              10000 (8.4.0+)              200
performance_schema              ON                          OFF
slow_query_log                  OFF                         OFF, alias of
                                                            log_slow_query
long_query_time                 10 s                        10 s, alias of
                                                            log_slow_query_time
==============================  ==========================  =========================

* MySQL can lower max_connections and table_open_cache at startup to fit
  open_files_limit (M/sql/mysqld.cc:10786-10809), so the running value is
  what is checked.
* MySQL TempTable limits each internal temporary table to tmp_table_size
  (M/storage/temptable/src/handler.cc:185) and all of them to
  temptable_max_ram; the MEMORY engine and MariaDB's HEAP use the smaller of
  tmp_table_size and max_heap_table_size (M/sql/sql_tmp_table.cc:2235-2241,
  Ma/sql/sql_select.cc:22662-22668). Configuration alone does not prove a
  spill, so the check uses Created_tmp_disk_tables.
* MariaDB counts a temporary table that moves to disk twice in
  Created_tmp_tables (see health.py), so its on-disk share is understated.
* ``Innodb_redo_log_capacity_resized`` is MySQL's redo capacity in use
  (8.0.30+, M/storage/innobase/log/log0files_capacity.cc:398); in 8.4 a legacy
  innodb_log_file_size x innodb_log_files_in_group setting can override
  innodb_redo_log_capacity when the latter is not set. 30/32 of it is only the
  raw ceiling: MySQL starts forced (sync) flushing near 0.79 of capacity and
  makes user writes wait near 0.84 (M/storage/innobase/log/
  log0files_capacity.cc:292-330, 475-499; log0chkp.cc:1012-1084, 1145-1162).
  MariaDB starts flushing at about 0.71 of innodb_log_file_size and forces it
  at about 0.81 (Ma/storage/innobase/log/log0log.cc:76-86; see health.py).
  The checkpoint age is shown against those points.
* MariaDB 10.11.12+, 11.4.6+ and 11.8.2+ cap online buffer pool growth at
  innodb_buffer_pool_size_max (0 means the startup size).
* FLUSH STATUS (MySQL, MariaDB 11.4 and older) resets Max_used_connections
  but not the summed counters used here (M/sql/mysqld.cc:13914-13939);
  MariaDB 11.5+ FLUSH GLOBAL STATUS resets those too
  (Ma/sql/mysqld.cc:9635-9648).
* MariaDB's log_slow_query sets the session sql_log_slow each connection
  copies when it connects (Ma/sql/sys_vars.cc:5693-5710, log.cc:1613), so a
  SET GLOBAL reaches new connections only. long_query_time is per session on
  both engines.
"""
from .advisor import buffer_pool_coverage, flush_log_durability
from .connector import ConnectorError
from .health import SEVERITY
from .perf_common import (Reader, _bytes_text, _count_text, _duration_text, _number, _round, base_report, denied,
                          preflight, reason, show_variables)
from .variables import static_dynamic

SCHEMA_VERSION = "config-1.0"
CONFIG_TAG = "/*myflames:config*/ "
STATUSES = ("ok", "watch", "problem", "info", "unknown")
GROUPS = (
    ("memory", "Memory"),
    ("durability", "Durability"),
    ("redo", "Redo log"),
    ("connections", "Connections and caches"),
    ("observability", "Observability"),
    ("server", "Server facts"),
)
VARIABLES = (
    "innodb_buffer_pool_size", "innodb_buffer_pool_size_max", "innodb_dedicated_server",
    "innodb_redo_log_capacity", "innodb_log_file_size",
    "innodb_log_files_in_group", "innodb_flush_log_at_trx_commit", "sync_binlog", "log_bin",
    "innodb_flush_method", "innodb_data_file_buffering", "innodb_log_file_buffering", "max_connections",
    "table_open_cache", "open_files_limit", "tmp_table_size", "tmp_memory_table_size", "max_heap_table_size",
    "tmp_disk_table_size", "internal_tmp_mem_storage_engine", "temptable_max_ram", "temptable_max_mmap",
    "temptable_use_mmap", "sort_buffer_size", "join_buffer_size", "innodb_io_capacity", "innodb_io_capacity_max",
    "slow_query_log", "log_slow_query", "long_query_time", "log_slow_query_time", "log_output",
    "thread_handling", "thread_pool_size",
)
STATUS = ("Uptime", "Max_used_connections", "Connection_errors_max_connections", "Opened_tables", "Open_tables",
          "Created_tmp_tables", "Created_tmp_disk_tables", "Innodb_os_log_written", "Innodb_redo_log_capacity_resized",
          "Innodb_redo_log_current_lsn", "Innodb_redo_log_checkpoint_lsn", "Innodb_checkpoint_age")
MIB = 1 << 20
#: Engine defaults (source-verified; see the module docstring).
DEFAULTS = {
    "mysql": {"innodb_buffer_pool_size": 128 * MIB, "innodb_redo_log_capacity": 100 * MIB,
              "innodb_log_file_size": 48 * MIB, "innodb_flush_log_at_trx_commit": "1", "sync_binlog": "1",
              "innodb_flush_method": "O_DIRECT when supported, else fsync", "max_connections": "151",
              "table_open_cache": "4000", "tmp_table_size": 16 * MIB, "max_heap_table_size": 16 * MIB,
              "internal_tmp_mem_storage_engine": "TempTable", "sort_buffer_size": 256 * 1024,
              "join_buffer_size": 256 * 1024, "innodb_io_capacity": "10000", "performance_schema": "ON",
              "slow_query_log": "OFF", "long_query_time": "10", "innodb_dedicated_server": "OFF",
              "thread_handling": "one-thread-per-connection"},
    "mariadb": {"innodb_buffer_pool_size": 128 * MIB, "innodb_log_file_size": 96 * MIB,
                "innodb_flush_log_at_trx_commit": "1", "sync_binlog": "0", "innodb_flush_method": "O_DIRECT",
                "max_connections": "151", "table_open_cache": "2000", "tmp_table_size": 16 * MIB,
                "tmp_memory_table_size": 16 * MIB, "max_heap_table_size": 16 * MIB, "sort_buffer_size": 2 * MIB,
                "join_buffer_size": 256 * 1024, "innodb_io_capacity": "200", "performance_schema": "OFF",
                "slow_query_log": "OFF", "log_slow_query": "OFF", "long_query_time": "10",
                "log_slow_query_time": "10", "thread_handling": "one-thread-per-connection"},
}
BYTE_VARIABLES = ("innodb_buffer_pool_size", "innodb_redo_log_capacity", "innodb_log_file_size", "tmp_table_size",
                  "tmp_memory_table_size", "max_heap_table_size", "tmp_disk_table_size", "temptable_max_ram",
                  "temptable_max_mmap", "sort_buffer_size", "join_buffer_size")

#: Rules of thumb behind each status (shown with it).
LARGE_SESSION_BUFFER = 16 * MIB
CONNECTIONS_PEAK_PCT = 80.0
OPENED_TABLES_PER_S = 1.0
TABLE_CACHE_FULL = 0.95
TMP_DISK_PCT = 25.0
MIN_TMP_TABLES = 100
REDO_HOURS = 1.0
MIN_UPTIME_S = 3600
RULES = {
    "buffer_pool": ("watch when the buffer pool is less than half the InnoDB tablespace files; a pool smaller "
                    "than the data is normal when the hot data fits, so check the hit ratio in Health"),
    "durability": "ok at 1 (and MariaDB's 3), which make each commit durable; watch at 0 or 2",
    "sync_binlog": "ok at 1 with the binary log on; watch at 0 or above 1",
    "redo": ("watch when the redo capacity holds less than %g hour of redo at the average rate since startup "
             "(judged after %d minutes of uptime; peaks are higher than the average)" % (REDO_HOURS, MIN_UPTIME_S // 60)),
    "connections": ("watch when the peak since startup reached %g%% of max_connections; problem when clients were "
                    "refused (Connection_errors_max_connections above zero)" % CONNECTIONS_PEAK_PCT),
    "table_cache": ("watch when the cache is at least %g%% full and tables were opened at %g per second or more on "
                    "average since startup" % (100 * TABLE_CACHE_FULL, OPENED_TABLES_PER_S)),
    "tmp_tables": ("watch when %g%% or more of internal temporary tables since startup went to disk, with at least "
                   "%d of them" % (TMP_DISK_PCT, MIN_TMP_TABLES)),
    "session_buffer": "watch when the global value is %s or more" % _bytes_text(LARGE_SESSION_BUFFER),
    "performance_schema": "watch when off: Top statements, Waits, I/O and unused indexes need it",
    "slow_log": "watch when off; info when on with long_query_time at 10 s or more",
}

# ---- sampling (talks to the server) ------------------------------------------


def config_report(conn):
    """Return the ``config-1.0`` report. Read-only; see the module docstring."""
    reader = Reader(conn, CONFIG_TAG)
    notes = []
    raw = {"sources": None, "tablespaces": None}
    try:
        server = preflight(reader, VARIABLES)
        status = reader.pairs("SHOW GLOBAL STATUS WHERE Variable_name IN (%s)" % ", ".join("'%s'" % n for n in STATUS))
        names = sorted(set(VARIABLES + ("performance_schema",)))
        if server.engine == "mysql":
            raw["sources"] = reader.optional(
                "SELECT VARIABLE_NAME, VARIABLE_SOURCE, NULL FROM performance_schema.variables_info "
                "WHERE VARIABLE_NAME IN (%s)" % ", ".join("'%s'" % n for n in names),
                "performance_schema.variables_info", notes)
            tablespaces = ("SELECT COALESCE(SUM(ALLOCATED_SIZE), 0), COUNT(*) FROM information_schema.INNODB_TABLESPACES "
                           "WHERE SPACE_TYPE IN ('Single', 'General')")
        else:
            raw["sources"] = reader.optional(
                "SELECT LOWER(VARIABLE_NAME), GLOBAL_VALUE_ORIGIN, DEFAULT_VALUE FROM information_schema.SYSTEM_VARIABLES "
                "WHERE VARIABLE_NAME IN (%s)" % ", ".join("'%s'" % n.upper() for n in names),
                "information_schema.SYSTEM_VARIABLES", notes)
            tablespaces = ("SELECT COALESCE(SUM(ALLOCATED_SIZE), 0), COUNT(*) FROM "
                           "information_schema.INNODB_SYS_TABLESPACES WHERE SPACE <> 0 AND NAME NOT LIKE 'innodb\\_undo%' "
                           "AND NAME <> 'innodb_temporary'")
        try:
            rows = reader.rows(tablespaces)
            raw["tablespaces"] = _number(rows[0][0]) if rows and rows[0] else None
        except ConnectorError as exc:
            if not denied(exc) and "Unknown table" not in str(exc):
                raise
            raw["tablespaces_error"] = reason(exc)
    except ConnectorError as exc:
        raise ValueError("Could not read the configuration: " + reason(exc))
    return compute_config(server, status, raw, notes)

# ---- arithmetic (pure) ----------------------------------------------------------


def _display(name, value):
    if value is None:
        return None
    if name in BYTE_VARIABLES:
        number = _number(value)
        if number is not None:
            return _bytes_text(number) if number < (1 << 62) else "unlimited"
    if name == "long_query_time" or name == "log_slow_query_time":
        number = _number(value)
        if number is not None:
            return "%g s" % _round(number, 3)
    return str(value)


class _Config(object):
    def __init__(self, server, status, raw):
        self.server = server
        self.engine = server.engine
        self.v = server.variables
        self.s = status or {}
        self.raw = raw or {}
        self.sources = {}
        for row in self.raw.get("sources") or []:
            if row and row[0]:
                self.sources[row[0].lower()] = (row[1] if len(row) > 1 else None, row[2] if len(row) > 2 else None)
        self.uptime = _number(self.s.get("Uptime")) or server.uptime

    def num(self, name):
        return _number(self.v.get(name))

    def stat(self, name):
        return _number(self.s.get(name))

    def on(self, name):
        return (self.v.get(name) or "").upper() in ("ON", "1")

    def setting(self, name, alias=None):
        """{name, value, display, default, set_by} for one variable (None if absent)."""
        if name not in self.v:
            return None
        default = DEFAULTS[self.engine].get(name)
        source, live_default = self.sources.get(name, (None, None))
        if live_default is not None and self.engine == "mariadb":
            default = live_default
        out = {"name": name, "value": self.v.get(name), "display": _display(name, self.v.get(name)),
               "default": _display(name, default) if default is not None else None,
               "set_by": _set_by(source),
               # Whether it can change while the server runs (variables.py: source-verified
               # map, None when unknown). The Change sheet reads the server before acting.
               "dynamic": static_dynamic(self.engine, name)}
        if alias:
            out["alias"] = alias
        return out


def _set_by(source):
    if not source:
        return None
    text = source.upper()
    if text in ("COMPILED", "COMPILE-TIME"):
        return "default"
    if text == "AUTO":
        return "sized automatically"
    return {"GLOBAL": "my.cnf", "SERVER": "my.cnf", "EXPLICIT": "my.cnf", "EXTRA": "my.cnf", "USER": "my.cnf",
            "LOGIN": "my.cnf", "CONFIG": "my.cnf", "PERSISTED": "SET PERSIST", "DYNAMIC": "SET GLOBAL",
            "SQL": "SET GLOBAL", "COMMAND_LINE": "command line", "COMMAND-LINE": "command line",
            "ENVIRONMENT": "environment"}.get(text, source.lower())


def _check(key, group, label, explain, status, observation, rule=None, caveat="", action=None, settings=(),
           sources=(), value=None):
    rule_text = RULES[rule] if rule else None
    if status == "unknown":
        why = observation or "Not enough data."
    elif status == "info":
        why = (observation + ". " if observation else "") + "No rule: compare with your hardware and workload."
    else:
        why = (observation + ". " if observation else "") + "Rule of thumb: " + rule_text + "."
    return {"key": key, "group": group, "label": label, "explain": explain, "value": value, "status": status,
            "why": why, "rule": rule_text, "caveat": caveat, "action": action,
            "settings": [s for s in settings if s], "sources": list(sources)}


def _buffer_pool(c):
    bp = c.num("innodb_buffer_pool_size")
    data = c.raw.get("tablespaces")
    settings = [c.setting("innodb_buffer_pool_size"), c.setting("innodb_dedicated_server"),
                c.setting("innodb_buffer_pool_size_max")]
    explain = ("The memory InnoDB uses to cache data and index pages. Pages not in it are read from disk. "
               "Compared here with the size of the InnoDB tablespace files on disk (free space inside them "
               "included), because a pool much smaller than the data cannot hold much of it.")
    sources = ["innodb_buffer_pool_size", "information_schema.%s" % (
        "INNODB_TABLESPACES" if c.engine == "mysql" else "INNODB_SYS_TABLESPACES")]
    if bp is None:
        return _check("buffer_pool", "memory", "Buffer pool size", explain, "unknown",
                      "This server does not report innodb_buffer_pool_size.", settings=settings, sources=sources)
    if data is None:
        error = c.raw.get("tablespaces_error")
        return _check("buffer_pool", "memory", "Buffer pool size", explain, "unknown",
                      "%s; the InnoDB data size needs the PROCESS privilege%s" % (
                          _bytes_text(bp), " (%s)" % error if error else ""),
                      settings=settings, sources=sources, value=_bytes_text(bp))
    severity = buffer_pool_coverage(bp, data)
    observation = "%s for %s of InnoDB tablespace files" % (_bytes_text(bp), _bytes_text(data))
    if severity:
        observation = "innodb_buffer_pool_size (%s) is %s the InnoDB tablespace files (%s)" % (
            _bytes_text(bp), severity, _bytes_text(data))
    action = None
    if severity:
        resize = "It can be resized online."
        if c.engine == "mariadb" and "innodb_buffer_pool_size_max" in c.v:
            cap = c.num("innodb_buffer_pool_size_max") or 0
            resize = ("innodb_buffer_pool_size_max is 0, which caps it at the startup size, so growing it needs a "
                      "restart." if cap <= bp else
                      "It can be resized online up to innodb_buffer_pool_size_max (%s); beyond that needs a "
                      "restart." % _bytes_text(cap))
        action = {"sql": "SET GLOBAL innodb_buffer_pool_size = <bytes>;",
                  "config": "innodb_buffer_pool_size = <size>  # under [mysqld]",
                  "text": "Raise it within the memory the server can spare, if Health shows disk reads and a "
                          "falling hit ratio. " + resize}
    return _check("buffer_pool", "memory", "Buffer pool size", explain, "watch" if severity else "ok", observation,
                  "buffer_pool", caveat=("The data on disk includes cold rows and free space; what matters is the "
                                         "hot set. Leave room for connections, the OS and other processes: a "
                                         "pool that makes the server swap is far worse than disk reads."),
                  action=action, settings=settings, sources=sources, value=_bytes_text(bp))


def _session_buffer(c, name, label, what):
    value = c.num(name)
    setting = c.setting(name)
    explain = ("Memory each session can allocate %s. It is a per-session limit, so a large global value is "
               "multiplied by the number of sessions doing that at once." % what)
    if value is None:
        return _check(name, "memory", label, explain, "unknown", "This server does not report %s." % name,
                      settings=[setting], sources=[name])
    default = DEFAULTS[c.engine].get(name)
    large = value >= LARGE_SESSION_BUFFER
    observation = "%s (engine default %s)" % (_bytes_text(value), _bytes_text(default) if default else "?")
    action = None
    if large:
        action = {"sql": "SET GLOBAL %s = %d;" % (name, default or 262144),
                  "text": "Return the global value to the default and raise it per session for the queries that "
                          "need it: SET SESSION %s = <bytes>;" % name}
    return _check(name, "memory", label, explain, "watch" if large else "ok", observation, "session_buffer",
                  caveat="A global change applies to new sessions only. Check first which queries sort or join "
                         "large sets (Health shows sort merge passes).",
                  action=action, settings=[setting], sources=[name], value=_bytes_text(value))


def tmp_table_limit(engine, variables):
    """(sentence, variable names): where internal temporary tables live in
    memory and the limit before they move to disk. Shared with health.py, so
    the Config and Health tabs explain the engines the same way (see the
    module docstring for the sources)."""
    def num(name):
        return _number(variables.get(name))
    if engine == "mysql":
        storage = variables.get("internal_tmp_mem_storage_engine") or "TempTable"
        if storage.lower() == "temptable":
            limit = ("TempTable keeps each internal temporary table in memory up to tmp_table_size (%s), and all of "
                     "them up to temptable_max_ram (%s) together" % (
                         _display("tmp_table_size", variables.get("tmp_table_size")) or "?",
                         _display("temptable_max_ram", variables.get("temptable_max_ram")) or "?"))
            names = ["internal_tmp_mem_storage_engine", "tmp_table_size", "temptable_max_ram", "temptable_max_mmap"]
        else:
            limit = ("The MEMORY engine keeps each internal temporary table in memory up to the smaller of "
                     "tmp_table_size and max_heap_table_size (%s)" % _bytes_text(min(
                         num("tmp_table_size") or 0, num("max_heap_table_size") or 0)))
            names = ["internal_tmp_mem_storage_engine", "tmp_table_size", "max_heap_table_size"]
    else:
        size = num("tmp_memory_table_size") or num("tmp_table_size") or 0
        limit = ("MariaDB keeps each internal temporary table in memory up to the smaller of tmp_memory_table_size "
                 "(tmp_table_size is its alias) and max_heap_table_size (%s); larger ones, and ones with BLOB or "
                 "TEXT columns, go to Aria tables on disk" % _bytes_text(min(size, num("max_heap_table_size") or 0)))
        names = ["tmp_memory_table_size", "max_heap_table_size", "tmp_disk_table_size"]
    return limit, names


def redo_capacity(engine, variables, status=None):
    """(bytes, source name) of the redo log capacity in use, or (None, None).

    MySQL 8.0.30+ reports the capacity in use as Innodb_redo_log_capacity_resized
    (a legacy innodb_log_file_size x innodb_log_files_in_group setting
    overrides innodb_redo_log_capacity when the latter is not set, and SHOW
    VARIABLES still shows the default; M/storage/innobase/handler/
    ha_innodb.cc innodb_redo_log_capacity_init at mysql-8.4.0). Before 8.0.30
    the capacity is the two legacy variables (2 files by default). MariaDB
    10.5+ has one redo file of innodb_log_file_size. Shared with health.py.
    """
    status = status or {}
    if engine == "mysql":
        resized = _number(status.get("Innodb_redo_log_capacity_resized"))
        if resized:
            return resized, "Innodb_redo_log_capacity_resized"
        capacity = _number(variables.get("innodb_redo_log_capacity"))
        if capacity:
            return capacity, "innodb_redo_log_capacity"
        size = _number(variables.get("innodb_log_file_size"))
        if size:
            return size * (_number(variables.get("innodb_log_files_in_group")) or 2), \
                "innodb_log_file_size x innodb_log_files_in_group"
        return None, None
    size = _number(variables.get("innodb_log_file_size"))
    return (size, "innodb_log_file_size") if size else (None, None)


def _tmp_tables(c):
    created, disk = c.stat("Created_tmp_tables"), c.stat("Created_tmp_disk_tables")
    limit, names = tmp_table_limit(c.engine, c.v)
    settings = [c.setting(n, alias="tmp_table_size" if n == "tmp_memory_table_size" else None) for n in names]
    explain = ("Internal temporary tables (GROUP BY, DISTINCT, UNION, derived tables) start in memory and move "
               "to disk when they outgrow the limit. %s." % limit)
    if c.engine == "mariadb":
        explain += " MariaDB counts a table that moves to disk twice in Created_tmp_tables, so the share is understated."
    sources = names + ["Created_tmp_tables", "Created_tmp_disk_tables"]
    if created is None or disk is None:
        return _check("tmp_tables", "memory", "Temporary tables", explain, "unknown",
                      "This server does not report Created_tmp_tables.", settings=settings, sources=sources)
    pct = 100.0 * disk / created if created else 0.0
    observation = "%s of %s internal temporary tables since startup went to disk (%.1f%%)" % (
        _count_text(disk), _count_text(created), pct)
    status = "watch" if created >= MIN_TMP_TABLES and pct >= TMP_DISK_PCT else "ok"
    action = None
    if status == "watch":
        action = {"text": "Find the statements in Top statements with the temporary-table-on-disk flag first; an "
                          "index that avoids the temporary table beats a bigger limit. Then raise the per-table "
                          "limit for the sessions that need it (SET SESSION tmp_table_size = <bytes>%s)." % (
                              "; max_heap_table_size too" if c.engine == "mariadb" or "max_heap_table_size" in names
                              else "")}
    return _check("tmp_tables", "memory", "Temporary tables", explain, status, observation, "tmp_tables",
                  caveat=("Each concurrent temporary table can use up to the limit, so a large global value risks "
                          "memory pressure. Tables too big for any sensible limit go to disk regardless."),
                  action=action, settings=settings, sources=sources, value="%.1f%% on disk" % pct)


def _durability(c):
    value = c.num("innodb_flush_log_at_trx_commit")
    setting = c.setting("innodb_flush_log_at_trx_commit")
    explain = ("When InnoDB writes and syncs the redo log for a commit. 1 writes and syncs at every commit, so "
               "a committed transaction survives a crash or power loss." + (
                   " On MariaDB with the binary log on, 1 syncs at prepare and relies on the binary log at "
                   "commit; 3 syncs at both." if c.engine == "mariadb" else ""))
    if value is None:
        return _check("flush_log", "durability", "Redo flush at commit", explain, "unknown",
                      "This server does not report innodb_flush_log_at_trx_commit.", settings=[setting],
                      sources=["innodb_flush_log_at_trx_commit"])
    loss = flush_log_durability(value)
    if loss is None:
        return _check("flush_log", "durability", "Redo flush at commit", explain, "ok",
                      "innodb_flush_log_at_trx_commit=%d" % value, "durability", settings=[setting],
                      sources=["innodb_flush_log_at_trx_commit"], value=str(value))
    return _check("flush_log", "durability", "Redo flush at commit", explain, "watch",
                  "innodb_flush_log_at_trx_commit=%d: %s" % (value, loss), "durability",
                  caveat=("At 1 each commit waits for a redo sync, so commit latency depends on the disk's sync "
                          "speed. Keep 0 or 2 only for data you can rebuild."),
                  action={"sql": "SET GLOBAL innodb_flush_log_at_trx_commit = 1;",
                          "config": "innodb_flush_log_at_trx_commit = 1  # under [mysqld]"},
                  settings=[setting], sources=["innodb_flush_log_at_trx_commit"], value=str(value))


def _sync_binlog(c):
    setting = c.setting("sync_binlog")
    explain = ("How often the binary log is synced to disk. At 1 it is synced at each commit (group), so after "
               "an OS crash or power loss the binary log has every committed transaction and replicas stay "
               "consistent. %s" % ("MySQL's default is 1." if c.engine == "mysql" else "MariaDB's default is 0."))
    if not c.on("log_bin"):
        return _check("sync_binlog", "durability", "Binary log sync", explain, "info",
                      "The binary log is off (log_bin=OFF), so sync_binlog has no effect",
                      settings=[setting, c.setting("log_bin")], sources=["sync_binlog", "log_bin"],
                      value=c.v.get("sync_binlog"))
    value = c.num("sync_binlog")
    if value is None:
        return _check("sync_binlog", "durability", "Binary log sync", explain, "unknown",
                      "This server does not report sync_binlog.", settings=[setting], sources=["sync_binlog"])
    if value == 1:
        return _check("sync_binlog", "durability", "Binary log sync", explain, "ok", "sync_binlog=1",
                      "sync_binlog", settings=[setting], sources=["sync_binlog", "log_bin"], value="1")
    observation = ("sync_binlog=0: the OS decides when the binary log reaches disk, so an OS crash or power loss "
                   "can lose committed transactions from it" if value == 0 else
                   "sync_binlog=%d: the binary log is synced every %d commit groups, so an OS crash or power loss "
                   "can lose up to %d groups from it" % (value, value, value - 1))
    return _check("sync_binlog", "durability", "Binary log sync", explain, "watch", observation, "sync_binlog",
                  caveat="Each commit group then waits for a binary log sync as well as the redo sync.",
                  action={"sql": "SET GLOBAL sync_binlog = 1;", "config": "sync_binlog = 1  # under [mysqld]"},
                  settings=[setting, c.setting("log_bin")], sources=["sync_binlog", "log_bin"], value=str(value))


def _flush_method(c):
    names = ["innodb_flush_method"]
    if c.engine == "mariadb":
        names += ["innodb_data_file_buffering", "innodb_log_file_buffering"]
    settings = [c.setting(n) for n in names]
    if c.engine == "mariadb" and c.server.version_tuple >= (11, 0, 1):
        explain = ("How InnoDB opens its files. It is deprecated in MariaDB 11.0.1 and later, replaced by "
                   "innodb_data_file_buffering, innodb_log_file_buffering and the *_write_through settings; do "
                   "not change it on this version.")
    else:
        explain = ("How InnoDB opens and syncs its data files. O_DIRECT bypasses the OS file cache, so data is "
                   "not cached twice." + (" MySQL uses O_DIRECT by default when the file system supports it."
                                          if c.engine == "mysql" else ""))
    return _check("flush_method", "durability", "File flush method", explain, "info",
                  "innodb_flush_method=%s" % (c.v.get("innodb_flush_method") or "?"), settings=settings,
                  sources=names, value=c.v.get("innodb_flush_method"))


def _redo(c):
    capacity = redo_capacity(c.engine, c.v, c.s)[0]
    if c.engine == "mysql":
        names =["innodb_redo_log_capacity", "innodb_log_file_size", "innodb_log_files_in_group"]
        sources = names + ["Innodb_redo_log_capacity_resized", "Innodb_os_log_written"]
        current, checkpoint = c.stat("Innodb_redo_log_current_lsn"), c.stat("Innodb_redo_log_checkpoint_lsn")
        age = current - checkpoint if current is not None and checkpoint is not None else None
    else:
        names = ["innodb_log_file_size"]
        sources = names + ["Innodb_os_log_written", "Innodb_checkpoint_age"]
        age = c.stat("Innodb_checkpoint_age")
    settings = [c.setting(n) for n in names if n in c.v]
    explain = ("The redo log holds changes not yet written to the data files. When it fills, InnoDB must "
               "flush dirty pages before more writes can proceed, so a small redo log for the write rate causes "
               "flushing bursts. A larger one means a longer crash recovery.")
    written = c.stat("Innodb_os_log_written")
    if not capacity:
        return _check("redo", "redo", "Redo log size", explain, "unknown", "This server does not report the redo size.",
                      settings=settings, sources=sources)
    value = _bytes_text(capacity)
    detail = ""
    if age is not None:
        detail = "; the checkpoint age is %s now" % _bytes_text(age)
        if c.engine == "mysql":
            detail += (" (%.0f%% of capacity; MySQL starts forced flushing near 79%% and makes writes "
                       "wait near 84%%)" % (100.0 * age / capacity))
        else:
            detail += (" (%.0f%% of the log size; MariaDB starts flushing near 71%% and forces it near "
                       "81%%)" % (100.0 * age / capacity))
    if written is None or not c.uptime or c.uptime < MIN_UPTIME_S:
        return _check("redo", "redo", "Redo log size", explain, "info",
                      "Redo capacity %s%s; too little uptime to judge the write rate" % (value, detail),
                      settings=settings, sources=sources, value=value)
    rate = float(written) / c.uptime
    hours = capacity / (rate * 3600) if rate > 0 else None
    observation = "Redo capacity %s; about %s/s of redo on average since startup" % (value, _bytes_text(rate))
    if hours is not None:
        observation += ", so it holds about %s at that rate" % _duration_text(hours * 3600)
    observation += detail
    status = "watch" if hours is not None and hours < REDO_HOURS else "ok"
    action = None
    if status == "watch":
        target = int(rate * 3600 * REDO_HOURS)
        target = ((target + 256 * MIB - 1) // (256 * MIB)) * 256 * MIB
        if c.engine == "mysql":
            action = {"sql": "SET GLOBAL innodb_redo_log_capacity = %d;" % target,
                      "config": "innodb_redo_log_capacity = %d  # under [mysqld]" % target,
                      "text": "About an hour of redo at the average rate, rounded up to 256 MiB (%s). It resizes "
                              "online." % _bytes_text(target)}
        else:
            action = {"sql": "SET GLOBAL innodb_log_file_size = %d;" % target,
                      "config": "innodb_log_file_size = %d  # under [mysqld]" % target,
                      "text": "About an hour of redo at the average rate, rounded up to 256 MiB (%s)." % _bytes_text(target)}
    note = ""
    if c.engine == "mysql":
        note = " MySQL's Innodb_os_log_written re-counts rewrites of partly filled blocks, so the rate is a little high."
    return _check("redo", "redo", "Redo log size", explain + note, status, observation, "redo",
                  caveat="Peaks write faster than the average, and a larger redo log makes crash recovery longer "
                         "and uses more disk.", action=action, settings=settings, sources=sources, value=value)


def _connections(c):
    limit, peak = c.num("max_connections"), c.stat("Max_used_connections")
    refused = c.stat("Connection_errors_max_connections")
    settings = [c.setting("max_connections")]
    explain = ("The most client connections the server accepts (one more is kept for an administrator). "
               "Compared with the peak since startup or the last status reset.")
    sources = ["max_connections", "Max_used_connections", "Connection_errors_max_connections"]
    if limit is None or peak is None:
        return _check("max_connections", "connections", "Connection limit", explain, "unknown",
                      "This server does not report max_connections or Max_used_connections.", settings=settings,
                      sources=sources)
    pct = 100.0 * peak / limit if limit else 0
    observation = "Peak %s of max_connections %s (%.0f%%)" % (_count_text(peak), _count_text(limit), pct)
    status = "watch" if pct >= CONNECTIONS_PEAK_PCT else "ok"
    if refused:
        status = "problem"
        observation += "; %s connection attempts were refused at the limit" % _count_text(refused)
    action = None
    if status != "ok":
        action = {"sql": "SET GLOBAL max_connections = <n>;",
                  "text": "Raise the limit, or cap connections in the application with a pool."}
    return _check("max_connections", "connections", "Connection limit", explain, status, observation,
                  "connections", caveat=("Every connection can allocate its own sort, join and temporary-table "
                                         "memory, so a higher limit raises the worst-case memory. "
                                         + ("MySQL may lower max_connections at startup to fit open_files_limit."
                                            if c.engine == "mysql" else "")).strip(),
                  action=action, settings=settings, sources=sources, value=_count_text(limit))


def _table_cache(c):
    cache, open_now, opened = c.num("table_open_cache"), c.stat("Open_tables"), c.stat("Opened_tables")
    settings = [c.setting("table_open_cache")]
    explain = ("How many open table instances the server keeps for reuse. When it is full, a table not in it "
               "must be opened again (Opened_tables).")
    sources = ["table_open_cache", "Open_tables", "Opened_tables"]
    if cache is None or open_now is None or opened is None or not c.uptime:
        return _check("table_open_cache", "connections", "Table cache", explain, "unknown",
                      "This server does not report table_open_cache, Open_tables or Opened_tables.",
                      settings=settings, sources=sources)
    rate = float(opened) / c.uptime
    full = open_now >= TABLE_CACHE_FULL * cache
    observation = "%s of %s in use; %s tables opened per second on average since startup" % (
        _count_text(open_now), _count_text(cache), _count_text(_round(rate, 2)))
    status = "watch" if full and rate >= OPENED_TABLES_PER_S else "ok"
    action = None
    if status == "watch":
        action = {"sql": "SET GLOBAL table_open_cache = <n>;",
                  "text": "Raise it so the tables in use fit, then watch Tables opened in Health."}
    return _check("table_open_cache", "connections", "Table cache", explain, status, observation, "table_cache",
                  caveat=("Each open table uses a file descriptor and memory; the server needs open_files_limit to "
                          "cover it." + (" MySQL may lower table_open_cache at startup to fit open_files_limit."
                                         if c.engine == "mysql" else "")),
                  action=action, settings=settings, sources=sources, value=_count_text(cache))


def _performance_schema(c):
    on = c.server.ps_on
    explain = ("Performance Schema records statement digests, waits, and table and file I/O. myflames' Top "
               "statements, Waits, I/O and unused-index views need it. It is read-only at runtime.")
    if on:
        return _check("performance_schema", "observability", "Performance Schema", explain, "ok", "ON",
                      "performance_schema", settings=[c.setting("performance_schema") or _ps_setting(c)],
                      sources=["performance_schema"], value="ON")
    return _check("performance_schema", "observability", "Performance Schema", explain, "watch",
                  "performance_schema=OFF" + (" (MariaDB's default)" if c.engine == "mariadb" else ""),
                  "performance_schema",
                  caveat="Turning it on needs a restart and uses some memory and a little CPU per statement.",
                  action={"config": "performance_schema = ON  # under [mysqld], then restart"},
                  settings=[c.setting("performance_schema") or _ps_setting(c)], sources=["performance_schema"],
                  value="OFF")


def _ps_setting(c):
    return {"name": "performance_schema", "value": c.v.get("performance_schema"),
            "display": c.v.get("performance_schema"), "default": DEFAULTS[c.engine]["performance_schema"],
            "set_by": _set_by(c.sources.get("performance_schema", (None, None))[0]), "dynamic": False}


def _slow_log(c):
    name = "log_slow_query" if c.engine == "mariadb" and "log_slow_query" in c.v else "slow_query_log"
    time_name = "log_slow_query_time" if c.engine == "mariadb" and "log_slow_query_time" in c.v else "long_query_time"
    on = c.on(name)
    seconds = _number(c.v.get(time_name))
    settings = [c.setting(name, alias="slow_query_log" if name != "slow_query_log" else None),
                c.setting(time_name, alias="long_query_time" if time_name != "long_query_time" else None),
                c.setting("log_output")]
    sources = [name, time_name, "log_output"]
    explain = "The slow query log records statements that ran longer than %s." % time_name
    if not on:
        return _check("slow_log", "observability", "Slow query log", explain, "watch",
                      "%s=OFF: slow statements are not logged" % name, "slow_log",
                      caveat=("A low threshold on a busy server writes a lot of log; log_output=TABLE costs more "
                              "than FILE. A new %s applies to new connections only%s." % (
                                  time_name, ", and so does turning the log on: each MariaDB connection copies "
                                  "log_slow_query when it connects, so pooled connections keep the old setting"
                                  if c.engine == "mariadb" else "")),
                      action={"sql": "SET GLOBAL %s = ON; SET GLOBAL %s = 1;" % (name, time_name),
                              "config": "%s = ON\n%s = 1  # under [mysqld]" % (name, time_name)},
                      settings=settings, sources=sources, value="OFF")
    if seconds is not None and seconds >= 10:
        return _check("slow_log", "observability", "Slow query log", explain, "info",
                      "On, but only statements over %s are logged" % _display(time_name, c.v.get(time_name)),
                      settings=settings, sources=sources, value="ON")
    return _check("slow_log", "observability", "Slow query log", explain, "ok",
                  "On, logging statements over %s" % (_display(time_name, c.v.get(time_name)) or "?"), "slow_log",
                  settings=settings, sources=sources, value="ON")


def _facts(c):
    out = []
    io = c.setting("innodb_io_capacity")
    if io:
        out.append(_check("io_capacity", "server", "InnoDB I/O capacity",
                          "The background I/O rate InnoDB aims for when flushing pages. The engines' defaults "
                          "differ a lot: MySQL 8.4 uses 10000, MariaDB 200.", "info",
                          "innodb_io_capacity=%s" % io["value"],
                          settings=[io, c.setting("innodb_io_capacity_max")],
                          sources=["innodb_io_capacity", "innodb_io_capacity_max"], value=io["value"]))
    if c.engine == "mysql" and "innodb_dedicated_server" in c.v:
        out.append(_check("dedicated_server", "server", "Dedicated server mode",
                          "With innodb_dedicated_server=ON, MySQL sizes the buffer pool from the machine's memory "
                          "and, when innodb_redo_log_capacity is not set, the redo log from its CPU count.", "info",
                          "innodb_dedicated_server=%s" % c.v.get("innodb_dedicated_server"),
                          settings=[c.setting("innodb_dedicated_server")], sources=["innodb_dedicated_server"],
                          value=c.v.get("innodb_dedicated_server")))
    handling = c.v.get("thread_handling")
    if handling:
        names = ["thread_handling"] + (["thread_pool_size"] if c.engine == "mariadb" else [])
        text = "How the server runs connections: a thread per connection, or a thread pool."
        if c.engine == "mariadb":
            text += " thread_pool_size matters only with pool-of-threads."
        out.append(_check("thread_handling", "server", "Thread handling", text, "info",
                          "thread_handling=%s" % handling, settings=[c.setting(n) for n in names], sources=names,
                          value=handling))
    return out


def compute_config(server, status, raw, notes=None):
    """The ``config-1.0`` report. Pure."""
    notes = list(notes or [])
    c = _Config(server, status, raw)
    checks = [_buffer_pool(c), _session_buffer(c, "sort_buffer_size", "Sort buffer", "for each sort"),
              _session_buffer(c, "join_buffer_size", "Join buffer", "for each join that cannot use an index"),
              _tmp_tables(c), _durability(c), _sync_binlog(c), _flush_method(c), _redo(c), _connections(c),
              _table_cache(c), _performance_schema(c), _slow_log(c)] + _facts(c)
    summary = {"status": "ok", "problem": 0, "watch": 0, "ok": 0, "info": 0, "unknown": 0}
    for check in checks:
        summary[check["status"]] += 1
    if summary["problem"]:
        summary["status"] = "problem"
    elif summary["watch"]:
        summary["status"] = "watch"
    if c.engine == "mysql" and "innodb_log_file_size" in c.v and c.sources.get("innodb_log_file_size", ("",))[0] \
            not in (None, "COMPILED") and c.sources.get("innodb_redo_log_capacity", ("",))[0] == "COMPILED":
        notes.append("innodb_log_file_size is set explicitly; it is deprecated since MySQL 8.0.30 and removed in "
                     "9.3. Set innodb_redo_log_capacity instead.")
    if c.uptime is not None and c.uptime < MIN_UPTIME_S:
        notes.append("The server started %s ago, so the counters since startup are still small."
                     % _duration_text(c.uptime))
    notes.append("Counters run from server start. The connection peak (Max_used_connections) also restarts with "
                 "FLUSH STATUS on MySQL and MariaDB 11.4 or older, and with FLUSH GLOBAL STATUS on MariaDB 11.5 "
                 "and later, which also resets the temporary-table and tables-opened counts.")
    notes.append("Statuses are rules of thumb, not verdicts; each check says which rule it uses. The review never "
                 "changes a setting: read a suggestion's caveat first, then change it yourself or with Change "
                 "(myflames config set), which shows the exact statement and asks before sending it.")
    report = base_report(SCHEMA_VERSION, server)
    report.update({"summary": summary, "groups": [{"key": k, "label": l} for k, l in GROUPS], "checks": checks,
                   "notes": notes})
    return report


def at_or_worse(report, level):
    floor = SEVERITY[level]
    return [c for c in report["checks"] if SEVERITY.get(c["status"], -1) >= floor]

# ---- text output for the CLI -------------------------------------------------------

_MARK = {"ok": "ok", "watch": "WATCH", "problem": "PROBLEM", "info": "-", "unknown": "?"}


def format_text(report):
    head = "%s %s" % ("MariaDB" if report["engine"] == "mariadb" else "MySQL", report["server_version"] or "?")
    if report["uptime_s"] is not None:
        head += ", up %s" % _duration_text(report["uptime_s"])
    s = report["summary"]
    lines = [head, "Status: %s (%d problem, %d watch, %d ok)" % (
        s["status"].upper() if s["status"] != "ok" else "ok", s["problem"], s["watch"], s["ok"]), ""]
    labels = dict(GROUPS)
    rows = [(c["group"], _MARK[c["status"]], c["label"], c["value"] or "—",
             ", ".join("%s=%s" % (x["name"], x["display"]) for x in c["settings"][:2])) for c in report["checks"]]
    widths = [max(len(r[i]) for r in rows) for i in range(1, 4)]
    group = None
    for g, mark, label, value, detail in rows:
        if g != group:
            if group is not None:
                lines.append("")
            lines.append(labels.get(g, g).upper())
            group = g
        lines.append(("  %s  %s  %s  %s" % (mark.ljust(widths[0]), label.ljust(widths[1]), value.ljust(widths[2]),
                                            detail)).rstrip())
    flagged = [c for c in report["checks"] if c["status"] in ("watch", "problem")]
    if flagged:
        lines += ["", "WHY"]
        for c in flagged:
            lines.append("  %s %s: %s" % (_MARK[c["status"]], c["label"], c["why"]))
            if c["action"] and c["action"].get("sql"):
                lines.append("    suggested: %s" % c["action"]["sql"])
            if c["caveat"]:
                lines.append("    caveat: %s" % c["caveat"])
    return "\n".join(lines) + "\n"
