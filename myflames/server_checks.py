"""Server checks no other view covers: replication status, account basics,
the query cache (MariaDB), binary log expiry, a memory scenario, dated
version support, engine caches and Galera/wsrep.

:func:`server_checks` returns the ``checks-1.0`` envelope the Full analysis
report (report.py) composes with the other views. It only reads: ``SHOW
GLOBAL VARIABLES`` / ``STATUS``, ``SHOW REPLICA STATUS`` (MySQL 8.0.22+;
``SHOW SLAVE STATUS`` before) or ``SHOW ALL SLAVES STATUS`` (MariaDB), and
``mysql.user`` (MariaDB: ``mysql.global_priv`` first, for the lock state).
The only additional table read is P_S internal memory; engine cache checks
reuse the Tables view's scoped inventory after collection. Memory and cache
semantics and lifecycle policy URLs are cited beside their computations.
Nothing is changed: every suggested statement is text for the user to run
after the db-operations confirmation, with why and a caveat.

Statuses follow health.py (``ok``, ``watch``, ``problem``, ``info``,
``unknown``); ``unknown`` when the data was not readable (privilege,
version), never a guess. The limits in :data:`RULES` are rules of thumb.

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"):

* Replication status needs SUPER or REPLICATION CLIENT on MySQL
  (M/sql/sql_show.cc:712-714), SLAVE MONITOR on MariaDB 11.0 and later
  (Ma/sql/sql_show.cc:9118, Ma/sql/privilege.h:542; MDEV-29668), and SLAVE
  MONITOR or SUPER on 10.11 (mariadb-10.11.16 sql/privilege.h:520). The REPLICA keywords
  exist from MySQL 8.0.22 and the SLAVE forms were removed in 8.4.0 (see the
  replication skill); MariaDB lists every connection with ``SHOW ALL SLAVES
  STATUS``. Columns are read by name, never by position, from the client's
  ``--xml`` output (``--batch --silent`` prints no column names).
  Seconds_Behind_Source / Seconds_Behind_Master is NULL while the applier
  is stopped and while the receiver is not connected (always on MariaDB,
  Ma/sql/slave.cc:3020; on MySQL once the applier has caught up,
  M/sql/rpl_replica.cc:3646-3649), 0 when the applier caught up with what the
  receiver has (even when the receiver is behind), and skewed by clock
  changes, so it is never the only lag signal. Group Replication members
  list their group_replication_applier and group_replication_recovery
  channels here, with a receiver that never runs (M/sql/rpl_replica.cc:3870);
  they are not asynchronous replicas and are not judged.
* ``read_only`` lets users with SUPER or CONNECTION_ADMIN write on MySQL
  unless ``super_read_only`` is on (M/sql/auth/sql_authorization.cc:
  1860-1890). MariaDB's ``read_only`` lets READ ONLY ADMIN write; it is a
  boolean up to 11.8 (mariadb-10.11.16 sql/sys_vars.cc:3169-3176), and from
  12.0.1 (MDEV-36425) OFF, ON, NO_LOCK or NO_LOCK_NO_ADMIN, the last of which
  stops READ ONLY ADMIN too (Ma/sql/sys_vars.cc:3295-3311). Replication
  threads are not affected.
* Accounts: an empty user name matches any user name (MySQL
  M/sql/auth/sql_authentication.cc:2314-2315; MariaDB find_user_or_anon,
  Ma/sql/sql_acl.cc:3424-3428). CREATE ROLE creates a locked account
  (M/sql/sql_admin.cc:2461-2470), so locked accounts are skipped. MariaDB's
  ``mysql.user`` is a view over ``mysql.global_priv`` without the lock state
  (Ma/scripts/mariadb_system_tables.sql:51-99); its ``mariadb.sys``
  account is created locked with an empty authentication string (line 47).
  The authentication string is compared with '' on the server, so a password
  hash never leaves it. An empty string means no password only for plugins
  that store a hash there and accept an empty password: MySQL's
  mysql_native_password, caching_sha2_password and sha256_password
  (M/sql/auth/sha2_password.cc:1149-1162, sql_authentication.cc:4789-4800),
  MariaDB's mysql_native_password, mysql_old_password and an empty plugin
  name (Ma/sql/sql_acl.cc:15415, 15525). MariaDB's ed25519 and parsec hash
  even an empty password, so an empty stored value lets nobody in
  (Ma/sql/sql_acl.cc:2443-2449, plugin/auth_ed25519/server_ed25519.c:56-63,
  plugin/auth_parsec/server_parsec.cc:171, 287). Other plugins (auth_socket,
  unix_socket, PAM, LDAP, Kerberos, ...) are not judged.
* Query cache (MariaDB): ``query_cache_type`` defaults to OFF and
  ``query_cache_size`` to 1 MiB (Ma/sql/sys_vars.cc:3488-3494, 3563-3570);
  every lookup and invalidation takes one global mutex
  (structure_guard_mutex, Ma/sql/sql_cache.cc:607-671), and a change to a
  table invalidates every cached result that read it (Query_cache::
  invalidate, 2232-2326). A cache hit skips parsing and execution, so it is
  not counted in Com_select (Ma/sql/sql_parse.cc:7889, 3741): the hit share
  is Qcache_hits / (Qcache_hits + Com_select). Counters: Ma/sql/mysqld.cc:
  7970-7974. SET GLOBAL query_cache_type = ON turns it on at runtime when
  query_cache_size is above 0 (Ma/sql/sys_vars.cc:3555, sql_cache.cc:1344),
  and a non-zero query_cache_size at startup turns the type ON
  (Ma/sql/mysqld.cc:5014-5020). MySQL 8.0 and later have no query cache (no
  query_cache_size in M/sql/sys_vars.cc).
* Binary log expiry: MySQL ``binlog_expire_logs_seconds`` defaults to
  2592000 (30 days) and ``binlog_expire_logs_auto_purge`` (8.0.29+) to ON
  (M/sql/sys_vars.cc:2196-2211); MySQL 8.0 also has the deprecated
  ``expire_logs_days``. MariaDB's ``expire_logs_days`` and
  ``binlog_expire_logs_seconds`` (10.6.1+) are linked and default to 0, never
  purge (Ma/sql/sys_vars.cc:1277-1299). Purges happen at startup and at
  binary log rotation. MariaDB 11.4+ ``max_binlog_total_size`` (alias
  ``binlog_space_limit``, the name Percona Server uses) purges by total size
  when above 0 (Ma/sql/sys_vars.cc:1302-1363), which also counts as automatic
  purging.
"""
import datetime

from .connector import ConnectorError
from .ps_instruments import BUILTIN_MEMORY
from .perf_common import Reader, _bytes_text as _bytes, _count_text, _duration_text, _number, base_report, preflight, reason

SCHEMA_VERSION = "checks-1.0"
CHECKS_TAG = "/*myflames:checks*/ "
STATUSES = ("ok", "watch", "problem", "info", "unknown")
VARIABLES = ("log_bin", "binlog_expire_logs_seconds", "expire_logs_days", "binlog_expire_logs_auto_purge",
             "max_binlog_total_size", "binlog_space_limit", "query_cache_type", "query_cache_size",
             "have_query_cache", "read_only", "super_read_only")
QCACHE_STATUS = ("Qcache_hits", "Qcache_inserts", "Qcache_not_cached", "Qcache_lowmem_prunes",
                 "Qcache_queries_in_cache", "Qcache_free_memory", "Com_select")
# Allocation semantics: https://dev.mysql.com/doc/refman/8.4/en/memory-use.html
# and /internal-temporary-tables.html. These are configured capacities, not RSS.
GLOBAL_MEMORY = ("innodb_buffer_pool_size", "innodb_log_buffer_size", "key_buffer_size")
SESSION_MEMORY = ("sort_buffer_size", "join_buffer_size", "read_buffer_size", "read_rnd_buffer_size",
                  "thread_stack", "binlog_cache_size", "binlog_stmt_cache_size", "net_buffer_length")
MEMORY_VARIABLES = GLOBAL_MEMORY + SESSION_MEMORY + (
    "max_connections", "tmp_table_size", "max_heap_table_size", "internal_tmp_mem_storage_engine",
    "temptable_max_ram", "temptable_max_mmap", "aria_pagecache_buffer_size", "key_cache_block_size",
    "aria_block_size", "wsrep_on", "wsrep_provider")
VARIABLES += MEMORY_VARIABLES
CACHE_STATUS = ("Key_blocks_used", "Key_blocks_unused", "Key_read_requests", "Key_reads",
                "Aria_pagecache_blocks_used", "Aria_pagecache_blocks_unused",
                "Aria_pagecache_read_requests", "Aria_pagecache_reads")
WSREP_STATUS = ("wsrep_cluster_size", "wsrep_cluster_status", "wsrep_connected", "wsrep_ready",
                "wsrep_local_state", "wsrep_local_state_comment", "wsrep_flow_control_paused",
                "wsrep_local_cert_failures")
STATUS_VARIABLES = QCACHE_STATUS + CACHE_STATUS + WSREP_STATUS + ("Max_used_connections",)
STATUS_SQL = "SHOW GLOBAL STATUS WHERE Variable_name IN (%s)" % ", ".join(
    "'%s'" % n for n in STATUS_VARIABLES)
# Reuse Instruments' built-in prefix: these always-enabled instruments have
# no configurable FEATURE dependency. Only P_S's own buffers are included;
# summing all instruments would count the buffer pool
# and session allocations twice. Built-in instruments are always enabled:
# https://dev.mysql.com/doc/refman/8.4/en/performance-schema-memory-summary-tables.html
PS_MEMORY_SQL = ("SELECT SUM(CURRENT_NUMBER_OF_BYTES_USED) "
                 "FROM performance_schema.memory_summary_global_by_event_name "
                 "WHERE EVENT_NAME LIKE '%s%%'" % BUILTIN_MEMORY)

# Allocation evidence in both reviewed trees: network buffer at my_net_init
# (M/sql-common/net_serv.cc:159, Ma/sql/net_serv.cc:180), initial capacity in
# sql/sql_client.cc, and result buffer at sql/sql_connect.cc:629 / :1157.
# Both binlog caches open together on first use (M/sql/binlog.cc:1158-1160,
# :8372; Ma/sql/log.cc:7540-7556). MariaDB's non-mmap redo path allocates
# buf and flush_buf, each buf_size (Ma/storage/innobase/log/log0log.cc:358-368).
#
# Lifecycle data, never a wildcard saying every future major is supported.
# These EXISTING end dates come from VENDOR POLICY / release pages, not
# MYSQL_VERSION, VERSION or tag timestamps (which are not release/EOL dates).
# Reviewed against https://www.mysql.com/support/eol-notice.html (8.0: April 21)
# and https://dev.mysql.com/doc/refman/8.4/en/mysql-releases.html (Innovation
# ends at its successor; LTS 5+3 years). Successor GA dates: the respective
# https://dev.mysql.com/doc/relnotes/mysql/9.1/en/news-9-1-0.html through
# /9.7/en/news-9-7-0.html. Month-only LTS end dates below use the
# first day of the following month (also used for month-only MariaDB dates).
# MariaDB dates are Community binary
# maintenance, not Enterprise: https://mariadb.org/about/#maintenance-policy
# Short-term months: the respective
# https://mariadb.com/docs/release-notes/community-server/old-releases/11.0/what-is-mariadb-110
# through /11.2/what-is-mariadb-112 pages (June, August, November 2024).
VERSION_LAST_REVIEWED = "2026-10-15"
VERSION_SUPPORT = {
    "mysql": {
        (8, 0): ("maintenance", "2026-04-21"),
        (8, 4): ("LTS", "2032-05-01"),
        (9, 0): ("Innovation", "2024-10-15"),
        (9, 1): ("Innovation", "2025-01-21"),
        (9, 2): ("Innovation", "2025-04-15"),
        (9, 3): ("Innovation", "2025-07-22"),
        (9, 4): ("Innovation", "2025-10-21"),
        (9, 5): ("Innovation", "2026-01-20"),
        (9, 6): ("Innovation", "2026-04-21"),
        (9, 7): ("LTS", "2034-05-01"),
        (26, 7): ("unverified", None),
    },
    "mariadb": {
        (10, 6): ("LTS", "2026-07-06"),
        (10, 11): ("LTS", "2028-02-16"),
        (11, 4): ("LTS", "2029-05-29"),
        (11, 0): ("short-term", "2024-07-01"),
        (11, 1): ("short-term", "2024-09-01"),
        (11, 2): ("short-term", "2024-12-01"),
        (11, 8): ("LTS", "2028-06-04"),
        (11, 3): ("short-term", None),
        (11, 5): ("short-term", None),
        (11, 6): ("short-term", None),
        (11, 7): ("short-term", None),
        (12, 0): ("short-term", None),
        (12, 1): ("short-term", None),
        (12, 2): ("short-term", None),
        (12, 3): ("unverified", None),
        (13, 0): ("unverified", None),
    },
}
# "Current" is only a reviewed LTS line, never the latest version number.
# MySQL MYSQL_VERSION and mysql-9.7.0:MYSQL_VERSION explicitly say LTS.
# MariaDB's tree has no stable 12.3/13.0 tag, so do not nominate either.
VERSION_CURRENT = {"mysql": (9, 7), "mariadb": None}
VERSION_SUPPORT_NOTES = {
    "mysql": {
        (26, 7): "myflames has not been verified on this version yet; check its support period with the vendor.",
    },
    "mariadb": {
        (11, 3): "myflames has no verified maintenance end date for this series.",
        (11, 5): "myflames has no verified maintenance end date for this series.",
        (11, 6): "myflames has no verified maintenance end date for this series.",
        (11, 7): "myflames has no verified maintenance end date for this series.",
        (12, 0): "myflames has no verified maintenance end date for this series.",
        (12, 1): "myflames has no verified maintenance end date for this series.",
        (12, 2): "myflames has no verified maintenance end date for this series.",
        (12, 3): "myflames has not been verified on this version yet; check its support period with the vendor.",
        (13, 0): "myflames has not been verified on this version yet; check its support period with the vendor.",
    },
}
# Operational review horizons, explicitly rules of thumb, not vendor limits.
EOL_WATCH_DAYS = 180
CACHE_MISS_WATCH_PCT = 5
CACHE_MIN_REQUESTS = 1000
WSREP_FLOW_WATCH = 0.10
#: Replica lag (Seconds_Behind_Source) to watch at and to call a problem:
#: rules of thumb, not source limits.
REPLICA_LAG_S = (60, 600)
#: Plugins whose authentication string is a password hash and that accept
#: an empty password: an empty string means no password. (MariaDB's
#: ed25519 and parsec never store '' for a password, even an empty one.)
PASSWORD_PLUGINS = {
    "mysql": ("mysql_native_password", "caching_sha2_password", "sha256_password"),
    "mariadb": ("mysql_native_password", "mysql_old_password", ""),
}
#: Channels Group Replication manages itself (not asynchronous replication).
GROUP_REPLICATION_CHANNELS = ("group_replication_applier", "group_replication_recovery")
#: Accounts listed in a finding at most.
MAX_ACCOUNTS = 10
RULES = {
    "memory_budget": ("unknown without physical RAM or complete inputs; watch when the modeled connection "
                      "budget exceeds RAM; problem when configured global capacities alone exceed RAM"),
    "version_support": ("problem from the listed end of maintenance; watch within %d days before it; "
                        "unknown for an unlisted series or an unproved end date" % EOL_WATCH_DAYS),
    "engine_cache": ("watch at %.0f%% cache misses with at least %d read requests; unknown without readable "
                     "inventory and counters; skip efficiency when no engine tables are visible" %
                     (CACHE_MISS_WATCH_PCT, CACHE_MIN_REQUESTS)),
    "wsrep": ("problem when disconnected, not ready or non-Primary; watch while not Synced, for a singleton "
              "cluster, any certification failures, or flow-control paused >= %.0f%%" % (100 * WSREP_FLOW_WATCH)),
    "replication": ("problem when a replication thread is stopped or reports an error; watch while the receiver "
                    "is still connecting, or when the lag reaches %d s; problem at %d s" % REPLICA_LAG_S),
    "replica_read_only": "watch when a replica accepts writes (read_only=OFF)",
    "no_password": "problem when an unlocked account with a password plugin has an empty authentication string",
    "anonymous": "watch when an unlocked account has an empty user name",
    "root_any_host": "watch when 'root'@'%' exists and is not locked",
    "query_cache": "ok when off; watch when on (query_cache_type ON or DEMAND with query_cache_size above 0)",
    "binlog_expiry": ("watch when the binary log is on and nothing purges it automatically (no expiry period and no "
                      "total size limit)"),
}

# ---- reading (talks to the server) ---------------------------------------------------------


def _replica_sql(server):
    if server.engine == "mariadb":
        return "SHOW ALL SLAVES STATUS"
    if server.version_tuple >= (8, 0, 22):
        return "SHOW REPLICA STATUS"
    return "SHOW SLAVE STATUS"


def _named_rows(conn, sql):
    """Rows as dicts read by column name (perf_common.Reader.named: the
    client's --xml output, since --batch --silent prints no column names)."""
    return Reader(conn, CHECKS_TAG).named(sql)


def _fatal(exc):
    text = str(exc)
    return "ERROR 2002" in text or "ERROR 2003" in text or "ERROR 2013" in text or "timed out" in text


#: MySQL: mysql.user. MariaDB: mysql.global_priv has the lock state; the
#: mysql.user view (fallback) does not. The authentication string is only
#: compared with '' on the server. MariaDB's JSON_VALUE returns a JSON true
#: as '1' (measured on 11.4), so both spellings are accepted.
_ACCOUNT_SQL = {
    "mysql": "SELECT User, Host, plugin, authentication_string = '', account_locked, 'N' FROM mysql.user",
    "mariadb": ("SELECT User, Host, IFNULL(JSON_VALUE(Priv, '$.plugin'), ''), "
                "IFNULL(JSON_VALUE(Priv, '$.authentication_string'), '') = '', "
                "IF(IFNULL(JSON_VALUE(Priv, '$.account_locked'), '') IN ('1', 'true'), 'Y', 'N'), "
                "IF(IFNULL(JSON_VALUE(Priv, '$.is_role'), '') IN ('1', 'true'), 'Y', 'N') FROM mysql.global_priv"),
    "mariadb_view": "SELECT User, Host, plugin, authentication_string = '', NULL, is_role FROM mysql.user",
}


def server_checks(conn):
    """Return the ``checks-1.0`` report. Read-only; see the module docstring."""
    reader = Reader(conn, CHECKS_TAG)
    notes = []
    raw = {"status": {}, "replicas": None, "replica_error": None, "accounts": None, "accounts_error": None,
           "accounts_source": None}
    try:
        server = preflight(reader, VARIABLES)
        try:
            raw["status"] = reader.pairs(STATUS_SQL)
        except ConnectorError as exc:
            if _fatal(exc):
                raise
            raw["status_error"] = reason(exc)
        if server.ps_on:
            try:
                rows = reader.rows(PS_MEMORY_SQL)
                raw["ps_memory_bytes"] = _nonnegative(rows[0][0]) if rows and rows[0] else None
                if raw["ps_memory_bytes"] is None:
                    raw["ps_memory_error"] = "No P_S internal memory instruments were returned on this version."
            except ConnectorError as exc:
                if _fatal(exc):
                    raise
                raw["ps_memory_error"] = "P_S memory needs SELECT on performance_schema: " + reason(exc)
        else:
            raw["ps_memory_error"] = "Performance Schema is off; its memory was not measured."
        try:
            raw["replicas"] = _named_rows(conn, _replica_sql(server))
        except ConnectorError as exc:
            if _fatal(exc):
                raise
            raw["replica_error"] = reason(exc)
        except ValueError as exc:
            raw["replica_error"] = str(exc)
        sources = ("mariadb", "mariadb_view") if server.engine == "mariadb" else ("mysql",)
        for source in sources:
            try:
                raw["accounts"] = reader.rows(_ACCOUNT_SQL[source])
                raw["accounts_source"] = source
                raw["accounts_error"] = None
                break
            except ConnectorError as exc:
                if _fatal(exc):
                    raise
                raw["accounts_error"] = reason(exc)
    except ConnectorError as exc:
        raise ValueError("Could not read the server checks: " + reason(exc))
    return compute_checks(server, raw, notes)

# ---- arithmetic (pure) -------------------------------------------------------------------------


def _check(key, section, label, status, observation, rule=None, explain="", caveat=None, action=None,
           numbers=(), settings=(), sources=(), value=None, link=None):
    rule_text = RULES[rule] if rule else None
    if status == "unknown":
        why = observation or "Not readable."
    elif status == "info" or rule_text is None:
        why = observation
    else:
        why = (observation + ". " if observation else "") + "Rule of thumb: " + rule_text + "."
    return {"key": key, "section": section, "label": label, "explain": explain, "value": value,
            "status": status, "why": why, "rule": rule_text, "caveat": caveat, "action": action,
            "numbers": [n for n in numbers if n], "settings": [s for s in settings if s],
            "sources": list(sources), "link": link}


def _number_item(label, text, value=None):
    return {"label": label, "text": text, "value": value}


def _setting(server, name):
    if name not in server.variables:
        return None
    return {"name": name, "value": server.variables.get(name), "display": server.variables.get(name)}


def _on(value):
    return (value or "").upper() in ("ON", "1", "YES")


def account_name(user, host):
    """'user'@'host' as SQL text (quotes doubled)."""
    return "'%s'@'%s'" % ((user or "").replace("'", "''"), (host or "").replace("'", "''"))


def _first(row, *names):
    for name in names:
        if name in row:
            return row[name]
    return None


def _replication(server, raw):
    """One check for every channel (or none when this is not a replica),
    plus the writable-replica check."""
    mysql = server.engine == "mysql"
    form = _replica_sql(server)
    explain = ("Replication threads on this server: the receiver (IO thread) that copies the source's binary log "
               "and the applier (SQL thread) that runs it. Read with %s; nothing is started or stopped." % form)
    sources = [form]
    if raw.get("replicas") is None:
        need = "SUPER or REPLICATION CLIENT" if mysql else (
            "SLAVE MONITOR" if server.version_tuple >= (11, 0, 0) else "SLAVE MONITOR or SUPER")
        return [_check("replication", "replication", "Replication", "unknown",
                       "Could not read %s: %s. It needs the %s privilege." % (form, raw.get("replica_error") or "?",
                                                                              need),
                       explain=explain, sources=sources)]
    channels = [row for row in raw["replicas"]
                if (_first(row, "Channel_Name", "Connection_name") or "") not in GROUP_REPLICATION_CHANNELS]
    group = len(raw["replicas"]) - len(channels)
    if not channels:
        text = "This server is not an asynchronous replica (%s returned no channel)" % form
        if group:
            text = ("This server is a Group Replication member; its group_replication_applier and "
                    "group_replication_recovery channels are managed by the group and not judged here")
        return [_check("replication", "replication", "Replication", "info", text, explain=explain,
                       sources=sources, value="group member" if group else "not a replica")]
    numbers, worst, problems = [], "ok", []
    order = {"ok": 0, "unknown": 1, "watch": 2, "problem": 3}
    watch_lag, problem_lag = REPLICA_LAG_S
    for row in channels:
        name = _first(row, "Channel_Name", "Connection_name") or ""
        io = (_first(row, "Replica_IO_Running", "Slave_IO_Running") or "").strip()
        sql = (_first(row, "Replica_SQL_Running", "Slave_SQL_Running") or "").strip()
        lag = _number(_first(row, "Seconds_Behind_Source", "Seconds_Behind_Master"))
        io_errno = _number(row.get("Last_IO_Errno")) or 0
        sql_errno = _number(row.get("Last_SQL_Errno")) or 0
        label = "channel %s" % name if name else "default channel"
        status, parts = "ok", []
        if io != "Yes" or sql != "Yes":
            status = "problem" if (io == "No" or sql == "No") else "watch"
            parts.append("receiver %s, applier %s" % (io or "?", sql or "?"))
        if sql_errno:
            status = "problem"
            parts.append("applier error %d: %s" % (sql_errno, " ".join((row.get("Last_SQL_Error") or "").split())))
        if io_errno:
            status = "problem"
            parts.append("receiver error %d: %s" % (io_errno, " ".join((row.get("Last_IO_Error") or "").split())))
        if lag is None:
            parts.append("lag unknown (NULL)")
            # NULL with both threads running: the receiver is not connected
            # (or, on MySQL, not reading), so the lag cannot be told.
            if status == "ok":
                status = "unknown"
        else:
            parts.append("%s behind" % _duration_text(lag))
            if lag >= problem_lag:
                status = "problem"
            elif lag >= watch_lag and status == "ok":
                status = "watch"
        if status in ("watch", "problem", "unknown"):
            problems.append("%s: %s" % (label, "; ".join(parts)))
        numbers.append(_number_item(label.capitalize(), "; ".join(parts), lag))
        if order[status] > order[worst]:
            worst = status
    lag_name = "Seconds_Behind_Source" if mysql and form == "SHOW REPLICA STATUS" else "Seconds_Behind_Master"
    observation = ("%d replication channel%s; %s" % (len(channels), "" if len(channels) == 1 else "s",
                                                     "; ".join(problems))) if problems else \
        "%d replication channel%s, both threads running" % (len(channels), "" if len(channels) == 1 else "s")
    action = None
    if worst in ("watch", "problem"):
        action = {"text": "Read the error and the applier state in the Replication view or with %s; fix the cause "
                          "before restarting a thread. Skipping a failed transaction leaves this replica different "
                          "from its source." % form}
    out = [_check("replication", "replication", "Replication", worst, observation, "replication", explain=explain,
                  caveat=("%s is NULL while the applier is stopped and while the receiver is not connected (%s), "
                          "0 when the applier caught up with what the receiver has (even when the receiver is "
                          "behind the source), and skewed by a clock change on either server, so a 0 is not proof "
                          "that the replica is current." % (
                              lag_name, "always on MariaDB" if not mysql else
                              "on MySQL once the applier has caught up")),
                  action=action, numbers=numbers, sources=sources + [lag_name],
                  value="%d channel%s" % (len(channels), "" if len(channels) == 1 else "s"))]
    read_only = server.variables.get("read_only")
    if read_only is not None:
        settings = [_setting(server, "read_only"), _setting(server, "super_read_only")]
        if (read_only or "").upper() in ("OFF", "0"):
            out.append(_check(
                "replica_read_only", "replication", "Writable replica", "watch",
                "This server is a replica and read_only is OFF, so any user can write to it", "replica_read_only",
                explain="Writes made on a replica are not on its source; they make the replica differ and can "
                        "stop replication when the source later changes the same rows.",
                caveat=("Some setups write to a replica on purpose (a reporting schema, a multi-source "
                        "aggregator). %s" % (
                            "read_only=ON still lets users with SUPER or CONNECTION_ADMIN write; "
                            "super_read_only=ON stops them too." if mysql else
                            "read_only=ON still lets users with READ ONLY ADMIN write; read_only=NO_LOCK_NO_ADMIN "
                            "stops them too." if server.version_tuple >= (12, 0, 1) else
                            "read_only=ON still lets users with READ ONLY ADMIN write; before MariaDB 12.0 no "
                            "setting stops them, so do not grant it to applications.")),
                action={"text": "Make the replica read-only unless writes are intended; the setting is global "
                                "and needs the db-operations confirmation.",
                        "sql": "SET PERSIST super_read_only = ON;" if mysql else "SET GLOBAL read_only = ON;",
                        "config": "%s = ON  # under [mysqld]" % ("super_read_only" if mysql else "read_only")},
                settings=settings, sources=["read_only"] + (["super_read_only"] if mysql else []), value="OFF"))
        else:
            out.append(_check("replica_read_only", "replication", "Writable replica", "ok",
                              "read_only=%s" % read_only, "replica_read_only", settings=settings,
                              sources=["read_only"], value=read_only))
    return out


def _accounts(server, raw):
    explain = ("Account basics from %s: accounts that accept a login without a password, anonymous accounts, and "
               "root allowed from any host. Only account names are read; password hashes never leave the server."
               % ("mysql.global_priv" if raw.get("accounts_source") == "mariadb" else "mysql.user"))
    sources = ["mysql.global_priv" if raw.get("accounts_source") == "mariadb" else "mysql.user"]
    keys = (("no_password", "Accounts without a password"), ("anonymous", "Anonymous accounts"),
            ("root_any_host", "root from any host"))
    if raw.get("accounts") is None:
        text = ("Could not read the accounts: %s. Reading them needs SELECT on mysql.user%s."
                % (raw.get("accounts_error") or "?", " (or mysql.global_priv)" if server.engine == "mariadb" else ""))
        return [_check(key, "security", label, "unknown", text, explain=explain, sources=sources)
                for key, label in keys]
    plugins = PASSWORD_PLUGINS[server.engine]
    lock_known = raw.get("accounts_source") != "mariadb_view"
    no_password, anonymous, root_any = [], [], []
    for row in raw["accounts"]:
        if len(row) < 6:
            continue
        user, host, plugin, empty, locked, role = row[:6]
        if (role or "N").upper() == "Y" or (locked or "N").upper() == "Y":
            continue
        if not lock_known and (user, host) == ("mariadb.sys", "localhost"):
            continue
        name = account_name(user, host)
        if (empty or "").strip() == "1" and (plugin or "").lower() in plugins:
            no_password.append(name)
        if user == "":
            anonymous.append(name)
        if user == "root" and host == "%":
            root_any.append(name)
    lock_note = "" if lock_known else (" The lock state is not readable without SELECT on mysql.global_priv, so "
                                       "locked accounts may be listed.")

    def listed(names):
        return [_number_item("Account", n) for n in names[:MAX_ACCOUNTS]] + (
            [_number_item("More", "%d more" % (len(names) - MAX_ACCOUNTS))] if len(names) > MAX_ACCOUNTS else [])

    first = no_password[0] if no_password else "'user'@'host'"
    out = [_check(
        "no_password", "security", "Accounts without a password", "problem" if no_password else "ok",
        ("%d account%s accept%s a login without a password: %s" % (
            len(no_password), "" if len(no_password) == 1 else "s", "s" if len(no_password) == 1 else "",
            ", ".join(no_password[:MAX_ACCOUNTS])) if no_password else "No unlocked account has an empty password")
        + lock_note, "no_password", explain=explain,
        caveat="Find the applications that use the account first: a new password or a lock breaks their logins.",
        action={"text": "Set a password, or lock the account if nothing uses it. Each statement needs the "
                        "db-operations confirmation.",
                "sql": "ALTER USER %s IDENTIFIED BY '<new password>';\n-- or: ALTER USER %s ACCOUNT LOCK;"
                       % (first, first)} if no_password else None,
        numbers=listed(no_password), sources=sources, value=str(len(no_password)))]
    out.append(_check(
        "anonymous", "security", "Anonymous accounts", "watch" if anonymous else "ok",
        ("%d anonymous account%s (empty user name), which accept%s any user name from %s: %s" % (
            len(anonymous), "" if len(anonymous) == 1 else "s", "s" if len(anonymous) == 1 else "",
            "its host" if len(anonymous) == 1 else "their hosts", ", ".join(anonymous[:MAX_ACCOUNTS]))
         if anonymous else "No unlocked anonymous account") + lock_note, "anonymous", explain=explain,
        caveat="Some old installations rely on an anonymous account for local tools; check before dropping it.",
        action={"text": "Drop anonymous accounts that nothing needs; it needs the db-operations confirmation.",
                "sql": "DROP USER %s;" % anonymous[0]} if anonymous else None,
        numbers=listed(anonymous), sources=sources, value=str(len(anonymous))))
    out.append(_check(
        "root_any_host", "security", "root from any host", "watch" if root_any else "ok",
        ("'root'@'%' exists and is not locked: the most privileged account accepts logins from every host, so "
         "only its password protects it" if root_any else "No unlocked 'root'@'%' account") + lock_note,
        "root_any_host", explain=explain,
        caveat=("Container images often create 'root'@'%' by default. Create root (or a named administrator) for "
                "the hosts administrators connect from and check that it works before dropping 'root'@'%', or "
                "you can lock yourself out."),
        action={"text": "Limit root to the hosts that administer the server; each statement needs the "
                        "db-operations confirmation.",
                "sql": "CREATE USER 'root'@'<admin host>' IDENTIFIED BY '<password>';\n"
                       "GRANT ALL ON *.* TO 'root'@'<admin host>' WITH GRANT OPTION;\n"
                       "-- after checking the new account works:\nDROP USER 'root'@'%';"} if root_any else None,
        numbers=listed(root_any), sources=sources, value=str(len(root_any))))
    return out


def _query_cache(server, raw):
    """MariaDB only (MySQL 8.0 and later have no query cache)."""
    if server.engine != "mariadb":
        return []
    settings = [_setting(server, n) for n in ("query_cache_type", "query_cache_size", "have_query_cache")]
    explain = ("The query cache keeps the result of a SELECT and returns it for the same statement text. Every "
               "lookup and every invalidation takes one global mutex, and any change to a table drops every cached "
               "result that read it.")
    sources = ["query_cache_type", "query_cache_size", "Qcache_hits", "Qcache_inserts", "Qcache_lowmem_prunes",
               "Com_select"]
    have = (server.variables.get("have_query_cache") or "YES").upper()
    kind = (server.variables.get("query_cache_type") or "").upper()
    size = _number(server.variables.get("query_cache_size"))
    if have == "NO":
        return [_check("query_cache", "query_cache", "Query cache", "info",
                       "This build has no query cache (have_query_cache=NO)", explain=explain, settings=settings,
                       sources=sources, value="not built in")]
    if not kind or size is None:
        return [_check("query_cache", "query_cache", "Query cache", "unknown",
                       "This server does not report query_cache_type and query_cache_size.", explain=explain,
                       settings=settings, sources=sources)]
    if kind in ("OFF", "0") or not size:
        return [_check("query_cache", "query_cache", "Query cache", "ok",
                       "Off (query_cache_type=%s, query_cache_size=%s)" % (kind, _count_text(size)), "query_cache",
                       explain=explain, settings=settings, sources=sources, value="off")]
    s = raw.get("status") or {}
    hits, inserts = _number(s.get("Qcache_hits")), _number(s.get("Qcache_inserts"))
    prunes, selects = _number(s.get("Qcache_lowmem_prunes")), _number(s.get("Com_select"))
    numbers = [_number_item("Hits since startup", _count_text(hits), hits),
               _number_item("Results added", _count_text(inserts), inserts),
               _number_item("Removed for lack of memory", _count_text(prunes), prunes)]
    share = None
    if hits is not None and selects is not None and hits + selects:
        share = 100.0 * hits / (hits + selects)
        numbers.append(_number_item("Hit share of SELECTs", "%.1f%%" % share, round(share, 1)))
    observation = "On (query_cache_type=%s, query_cache_size=%s)" % (kind, _count_text(size))
    if share is not None:
        observation += "; %.1f%% of SELECTs since startup were answered from it" % share
    return [_check(
        "query_cache", "query_cache", "Query cache", "watch", observation, "query_cache", explain=explain,
        caveat=("A read-mostly server with repeated identical SELECTs can gain from it; measure throughput with it "
                "off before deciding. Set both lines in my.cnf too: a non-zero query_cache_size at startup turns "
                "query_cache_type ON."),
        action={"text": "Turn it off unless measurements show it helps; it is a global setting and needs the "
                        "db-operations confirmation.",
                "sql": "SET GLOBAL query_cache_type = OFF;\nSET GLOBAL query_cache_size = 0;",
                "config": "query_cache_type = OFF\nquery_cache_size = 0  # under [mysqld]"},
        numbers=numbers, settings=settings, sources=sources, value="on")]


def _binlog_expiry(server):
    mysql = server.engine == "mysql"
    names = ["log_bin", "binlog_expire_logs_seconds", "expire_logs_days"] + (
        ["binlog_expire_logs_auto_purge"] if mysql else []) + [
        n for n in ("max_binlog_total_size", "binlog_space_limit") if n in server.variables]
    settings = [_setting(server, n) for n in names]
    explain = ("Binary logs are kept until they expire or someone purges them; expiry runs at startup and when the "
               "server rotates to a new binary log.")
    if not _on(server.variables.get("log_bin")):
        return _check("binlog_expiry", "redo", "Binary log expiry", "info",
                      "The binary log is off (log_bin=%s)" % (server.variables.get("log_bin") or "?"),
                      explain=explain, settings=settings, sources=names, value="binary log off")
    seconds = _number(server.variables.get("binlog_expire_logs_seconds")) or 0
    days = _number(server.variables.get("expire_logs_days")) or 0
    auto = server.variables.get("binlog_expire_logs_auto_purge")
    caveat = ("Replicas and point-in-time recovery need the binary logs since their last position or backup: keep "
              "them longer than a replica may be down and longer than your backup interval. A job that runs PURGE "
              "BINARY LOGS also counts.")
    if mysql and auto is not None and not _on(auto):
        return _check("binlog_expiry", "redo", "Binary log expiry", "watch",
                      "binlog_expire_logs_auto_purge is OFF, so the server never purges binary logs itself",
                      "binlog_expiry", explain=explain, caveat=caveat,
                      action={"text": "Turn automatic purging on, or check that a job purges the logs.",
                              "sql": "SET PERSIST binlog_expire_logs_auto_purge = ON;"},
                      settings=settings, sources=names, value="auto purge off")
    period = seconds or days * 86400
    size = _number(server.variables.get("max_binlog_total_size")) or _number(
        server.variables.get("binlog_space_limit")) or 0
    if size and not period:
        return _check("binlog_expiry", "redo", "Binary log expiry", "ok",
                      "Binary logs are purged when together they exceed %s (%s)" % (
                          _bytes(size), "max_binlog_total_size" if "max_binlog_total_size" in server.variables
                          else "binlog_space_limit"), "binlog_expiry", explain=explain, caveat=caveat,
                      settings=settings, sources=names, value="by size, %s" % _bytes(size))
    if not period:
        return _check(
            "binlog_expiry", "redo", "Binary log expiry", "watch",
            "The binary log is on and does not expire (binlog_expire_logs_seconds and expire_logs_days are 0), so "
            "it grows until the disk fills unless something purges it", "binlog_expiry", explain=explain,
            caveat=caveat,
            action={"text": "Set an expiry that covers your replicas and backups; the setting is global and needs the "
                            "db-operations confirmation.",
                    "sql": ("SET PERSIST binlog_expire_logs_seconds = <seconds>;" if mysql else
                            "SET GLOBAL binlog_expire_logs_seconds = <seconds>;"),
                    "config": "binlog_expire_logs_seconds = <seconds>  # under [mysqld]"},
            settings=settings, sources=names, value="never")
    text = "Binary logs expire after %s" % _duration_text(period)
    if size:
        text += ", or sooner when together they exceed %s" % _bytes(size)
    return _check("binlog_expiry", "redo", "Binary log expiry", "ok", text, "binlog_expiry", explain=explain,
                  settings=settings, sources=names, value=_duration_text(period))


def _nonnegative(value):
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _evidence(label, value, byte_size=False):
    text = "unknown" if value is None else _bytes(value) if byte_size else str(value)
    return _number_item(label, text, value)


def _memory_budget(server, raw):
    """A configured per-session scenario, NOT a hard bound.

    MySQL memory-use and internal-temporary-tables manual sections cited at
    MEMORY_VARIABLES explain demand allocation, multiple join/sort buffers,
    TempTable's shared RAM cap and MEMORY's min(tmp, heap) per-table cap.
    MariaDB: https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/system-variables/server-system-variables
    (tmp_table_size/max_heap_table_size). No host-local RAM lookup: the SQL
    server may be remote or containerized. physical_memory_bytes is optional
    independently supplied evidence for the pure computation only.
    """
    variables = server.variables
    global_names = list(GLOBAL_MEMORY)
    if server.engine == "mariadb":
        global_names += ["query_cache_size", "aria_pagecache_buffer_size"]
    binlog_names = ("binlog_cache_size", "binlog_stmt_cache_size")
    log_bin = (variables.get("log_bin") or "").upper()
    # Count both caches only with logging ON. An unknown state leaves a
    # partial subtotal; it must never be treated as a complete OFF scenario.
    session_names = [n for n in SESSION_MEMORY if n not in binlog_names or log_bin in ("ON", "1", "YES")]
    missing = []
    if log_bin not in ("ON", "1", "YES", "OFF", "0", "NO"):
        missing.append("log_bin (binary-log cache allocation unknown)")
    numbers = []

    def read(name):
        value = _nonnegative(variables.get(name))
        if value is None:
            missing.append(name)
        numbers.append(_evidence(name, value, True))
        return value or 0

    global_bytes = sum(read(n) for n in global_names)
    # MariaDB allocates two redo buffers on its non-mmap path. This is a
    # configured scenario: mmap can bypass them (see caveat), not measured RSS.
    if server.engine == "mariadb":
        global_bytes += _nonnegative(variables.get("innodb_log_buffer_size")) or 0
    session_bytes = sum(read(n) for n in session_names)
    # Both engines allocate a network buffer AND a result buffer at connect.
    session_bytes += _nonnegative(variables.get("net_buffer_length")) or 0
    temp_engine = (variables.get("internal_tmp_mem_storage_engine") or "").upper()
    if server.engine == "mariadb" or temp_engine == "MEMORY":
        tmp, heap = read("tmp_table_size"), read("max_heap_table_size")
        temp_bytes = min(tmp, heap) if all(_nonnegative(variables.get(n)) is not None
                                          for n in ("tmp_table_size", "max_heap_table_size")) else None
        session_bytes += temp_bytes or 0
        numbers.append(_evidence("One internal MEMORY table per connection", temp_bytes, True))
    elif temp_engine == "TEMPTABLE" and server.version_tuple >= (8, 0, 16):
        global_bytes += read("temptable_max_ram")
        # These are evidence, not additional global RAM allocations.
        numbers += [_evidence(n, _nonnegative(variables.get(n)), True)
                    for n in ("tmp_table_size", "max_heap_table_size", "temptable_max_mmap")]
    else:
        missing.append("internal_tmp_mem_storage_engine / supported temporary-table model")
    ps_bytes = _nonnegative(raw.get("ps_memory_bytes"))
    if ps_bytes is None:
        missing.append(raw.get("ps_memory_error") or "P_S internal memory is not readable")
    else:
        global_bytes += ps_bytes
    max_connections = _nonnegative(variables.get("max_connections"))
    peak_connections = _nonnegative((raw.get("status") or {}).get("Max_used_connections"))
    if max_connections is None:
        missing.append("max_connections")
    if peak_connections is None:
        missing.append(raw.get("status_error") or "Max_used_connections")
    ram = _nonnegative(raw.get("physical_memory_bytes"))
    if ram == 0:
        ram = None
    maximum = None if max_connections is None else global_bytes + session_bytes * max_connections
    peak = None if peak_connections is None else global_bytes + session_bytes * peak_connections
    partial = " (partial)" if missing else ""
    numbers += [_evidence("P_S internal memory bytes", ps_bytes, True),
                _evidence("Known global capacity bytes" + partial, global_bytes, True),
                _evidence("Per-connection scenario bytes" + partial, session_bytes, True),
                _evidence("max_connections", max_connections), _evidence("Max_used_connections", peak_connections),
                _evidence("Budget at max_connections" + partial, maximum, True),
                _evidence("Budget at Max_used_connections" + partial, peak, True),
                _evidence("Physical memory bytes", ram, True)]
    status = "unknown"
    observation = "Computed configured global capacities plus per-connection scenario times each connection count"
    if missing:
        observation += "; incomplete inputs: " + ", ".join(missing)
    if ram is None:
        observation += "; physical RAM is unknown over SQL (never inferred from buffer settings)"
    elif not missing:
        status = "problem" if global_bytes > ram else "watch" if max(maximum, peak) > ram else "ok"
        observation += "; compared with independently supplied physical RAM"
    caveat = ("Both net_buffer_length buffers (network and result) are allocated at connect; thread_stack at "
              "thread creation. Other session buffers are allocated on demand, some per table or per join. "
              "Both binlog caches are created together on a session's first binlog write; this scenario counts "
              "binlog_cache_size and binlog_stmt_cache_size only with log_bin ON; unknown logging leaves a partial subtotal. "
              "MariaDB counts two redo buffers of innodb_log_buffer_size; memory-mapped redo can bypass these "
              "allocations. The scenario can overstate use when sessions do not need all buffers together. "
              "It uses one sort/join/read buffer per connection and one MEMORY temporary table, not a hard upper bound: "
              "joins and queries can allocate several buffers/tables; network buffers grow. "
              "TempTable thread-local blocks, allocator overhead, other engines, the OS, other processes "
              "and container limits are not included; mmap limits are file-backed capacity, not added as RAM. "
              "P_S includes only its own internal buffers to avoid double counting other buffers. "
              "Global defaults may differ from existing session settings; a peak connection count is not measured "
              "peak memory. A budget below RAM does not establish safety.")
    return _check("memory_budget", "memory", "Total memory budget", status, observation, "memory_budget",
                  caveat=caveat, numbers=numbers,
                  action={"text": "Compare with server/container memory and measured peak use before changing limits."},
                  sources=global_names + list(SESSION_MEMORY) + ["log_bin", "Max_used_connections", "memory_summary_global_by_event_name"],
                  value="partial budget" if missing else "modeled budget")


def _version_support(server, as_of=None):
    """Dated, explicit series table; unknown series never inherit a lifecycle."""
    as_of = as_of or datetime.date.today()
    row = VERSION_SUPPORT.get(server.engine, {}).get(server.version_tuple[:2])
    numbers = [_number_item("Lifecycle last reviewed", VERSION_LAST_REVIEWED, VERSION_LAST_REVIEWED),
               _number_item("Evaluation date", as_of.isoformat(), as_of.isoformat())]
    caveat = ("Static series-level maintenance dates, not a check of installed patches or vulnerabilities. "
              "MySQL dates include Extended Support, exclude Sustaining Support; MariaDB dates describe "
              "Community binary maintenance, not Enterprise contracts. Current means the reviewed current LTS line, "
              "not the latest patch or Innovation release. Fork/distributor support can differ. "
              "Listed end dates come from vendor policy/release pages, not server source or tag timestamps. "
              "Month-only dates use the first day after the final maintenance month. "
              "Verify vendor policy before scheduling an upgrade; this table can become stale.")
    if row is None:
        return _check("version_support", "observability", "Version support", "unknown",
                      "No reviewed lifecycle entry for %s %s" % (server.engine, server.version or "unknown"),
                      "version_support", caveat=caveat, numbers=numbers, value="unknown")
    track, end_text = row
    if end_text is None:
        note = VERSION_SUPPORT_NOTES.get(server.engine, {}).get(server.version_tuple[:2],
                                                                  "No proved maintenance end date.")
        return _check("version_support", "observability", "Version support", "unknown",
                      "%s %s: support unknown (%s). %s" % (server.engine, server.version, track, note),
                      "version_support", caveat=caveat, numbers=numbers, value="unknown",
                      sources=["version", "version_comment"],
                      action={"text": "Verify the release maturity and maintenance period with the vendor."})
    end = datetime.date(*[int(n) for n in end_text.split("-")])
    remaining = (end - as_of).days
    classification = "past end of life" if remaining <= 0 else (
        "current" if server.version_tuple[:2] == VERSION_CURRENT[server.engine] else "supported")
    status = "problem" if remaining <= 0 else "watch" if remaining <= EOL_WATCH_DAYS else "ok"
    numbers += [_number_item("Maintenance ends", end_text, end_text),
                _evidence("Days until maintenance ends", remaining)]
    return _check("version_support", "observability", "Version support", status,
                  "%s %s: %s (%s), maintenance ends %s" %
                  (server.engine, server.version, classification, track, end_text), "version_support",
                  caveat=caveat, numbers=numbers, value=classification,
                  action={"text": "Check the vendor lifecycle and test an upgrade to a maintained series."}
                  if status in ("watch", "problem") else None, sources=["version", "version_comment"])


def engine_cache_checks(check_report, tables_report=None, gate=None):
    """Judge collected cache counters using Tables' existing scoped inventory.

    Called only after the parallel views finish, so no duplicate I_S scan.
    Absence means 'no visible tables in this scope', never 'no engine on the
    whole server'. I_S hides objects without privileges. Aria also services
    internal disk temp tables, even without a visible user Aria table.
    """
    evidence = check_report.get("cache_evidence") or {}
    variables, status = evidence.get("variables") or {}, evidence.get("status") or {}
    engines = (tables_report or {}).get("summary", {}).get("engines")
    counts = None if engines is None else {e["engine"].lower(): e["tables"] for e in engines}
    if (tables_report or {}).get("available") is False or (counts is not None and "unknown" in counts):
        counts = None
        gate = gate or "The table inventory or its engine names were not readable"
    results = []
    for engine, key, prefix, buffer_name, block_name in (
            ("myisam", "myisam_cache", "Key", "key_buffer_size", "key_cache_block_size"),
            ("aria", "aria_cache", "Aria_pagecache", "aria_pagecache_buffer_size", "aria_block_size")):
        if engine == "aria" and check_report.get("engine") != "mariadb":
            continue
        count = None if counts is None else counts.get(engine, 0)
        names = (["Key_blocks_used", "Key_blocks_unused", "Key_read_requests", "Key_reads"] if engine == "myisam"
                 else [prefix + suffix for suffix in ("_blocks_used", "_blocks_unused", "_read_requests", "_reads")])
        used, unused, requests, reads = [_nonnegative(status.get(n)) for n in names]
        capacity, block_size = [_nonnegative(variables.get(n)) for n in (buffer_name, block_name)]
        numbers = [_evidence("Visible %s tables" % engine, count), _evidence(buffer_name, capacity, True),
                   _evidence(block_name, block_size, True)] + [_evidence(n, _nonnegative(status.get(n))) for n in names]
        # Key_blocks_used and Aria_pagecache_blocks_used are HIGH WATER marks;
        # Ma/storage/maria/ma_pagecache.h:159: maximum concurrently used blocks.
        # used+unused is not
        # necessarily a contemporaneous capacity. Never derive occupancy from it.
        # https://dev.mysql.com/doc/refman/8.4/en/server-status-variables.html
        # Aria: https://mariadb.com/docs/server/server-usage/storage-engines/aria/aria-status-variables
        caveat = ("Inventory is limited to readable tables in the selected schemas; invisible tables and other "
                  "schemas may use this server-wide cache. Counters cover startup/reset history, including warmup; "
                  "a miss ratio alone does not prove undersizing. Named/segmented key caches are not totaled. "
                  "Key_blocks_used and Aria_pagecache_blocks_used are high-water marks, not current occupancy. Aria also caches internal disk "
                  "temporary tables, even when no user Aria tables are visible. Measure a representative interval "
                  "before considering a global cache-size change.")
        result_status = "unknown"
        ratio = None
        if counts is None:
            observation = "Engine inventory unavailable: " + (gate or "Tables was skipped, failed or lacks inventory")
        elif count == 0:
            result_status = "ok"
            observation = "No visible %s tables in the inventory scope; cache efficiency was not judged" % engine
        elif evidence.get("status_error"):
            observation = "Global cache counters are not readable: " + evidence["status_error"]
        elif any(v is None for v in (capacity, block_size, used, unused, requests, reads)) or reads > requests:
            observation = "Cache counters/settings missing or inconsistent on this server"
        elif requests == 0:
            observation = "No cache read requests; a miss rate cannot be calculated"
        else:
            ratio = 100.0 * reads / requests
            result_status = "watch" if requests >= CACHE_MIN_REQUESTS and ratio >= CACHE_MISS_WATCH_PCT else "ok"
            observation = "%s cache: %s physical reads / %s requests (%.2f%% misses)" % (
                engine, reads, requests, ratio)
        numbers.append(_evidence("Read miss percent", ratio))
        # Show capacity vs unused block bytes; metadata means buffer_size /
        # block_size is NOT an exact usable block count.
        numbers.append(_evidence("Unused block bytes", None if unused is None or block_size is None
                                 else unused * block_size, True))
        results.append(_check(key, "caches", "%s cache" % ("MyISAM key" if engine == "myisam" else "Aria page"),
                              result_status, observation, "engine_cache", caveat=caveat, numbers=numbers,
                              sources=names + [buffer_name, block_name, "Tables engine inventory"],
                              action={"text": "Sample cache reads after warmup and check the memory budget before resizing."}
                              if result_status == "watch" else None, value="not judged" if ratio is None
                              else "%.2f%% misses" % ratio))
    return results


def _wsrep(server, raw):
    """Provider-gated, read-only state. No expected cluster size is guessed.

    https://mariadb.com/docs/galera-cluster/reference/galera-cluster-status-variables
    describes Primary, connected/ready and the provider-reported paused
    fraction. Provider counter semantics are not verified by this server tree.
    FLUSH STATUS calls provider.reset_status (Ma/sql/mysqld.cc:9653).
    Synced=4: https://mariadb.com/docs/galera-cluster/galera-cluster-quickstart-guides/mariadb-galera-cluster-usage-guide
    Cert failures report rollbacks since provider start or last FLUSH STATUS,
    subject to provider semantics, not a current error rate.
    The 10% flow-control threshold and singleton watch are rules of thumb.
    """
    variables, status = server.variables, raw.get("status") or {}
    if "wsrep_on" not in variables and "wsrep_provider" not in variables and not any(n in status for n in WSREP_STATUS):
        return []
    provider = variables.get("wsrep_provider")
    # Plain MariaDB exposes wsrep variables too. Explicitly disabled Galera
    # is outside this check's scope, even if status is stale or unreadable.
    if (variables.get("wsrep_on") or "").upper() in ("OFF", "NO", "0") or (
            provider is not None and provider.strip().lower() in ("", "none")):
        return []
    caveat = ("A singleton may be intentional; expected membership is not known. Flow-control paused is a fraction "
              "reported over the provider's reset period, not this report's sampling window. Certification failures "
              "report transaction rollbacks since provider start or last FLUSH STATUS, not a current failure rate; "
              "applications may retry them. These counter meanings/reset periods are provider-dependent; "
              "FLUSH STATUS requests a provider status reset, whose implementation is outside the reviewed server tree. "
              "A single node's view does not establish the health of the entire cluster.")
    numbers = [_number_item(n, str(status.get(n, "unknown")), _number(status.get(n))) for n in WSREP_STATUS]
    size = _nonnegative(status.get("wsrep_cluster_size"))
    state = _nonnegative(status.get("wsrep_local_state"))
    paused = _nonnegative(status.get("wsrep_flow_control_paused"))
    failures = _nonnegative(status.get("wsrep_local_cert_failures"))
    required = ("wsrep_cluster_status", "wsrep_connected", "wsrep_ready")
    missing = any(not status.get(n) for n in required) or any(v is None for v in (size, state, paused, failures))
    fault = (status.get("wsrep_cluster_status", "Primary").lower() != "primary" or
             any(n in status and (status[n] or "").upper() in ("OFF", "NO", "0")
                 for n in ("wsrep_connected", "wsrep_ready")))
    invalid = paused is not None and paused > 1
    if raw.get("status_error") or missing or invalid:
        check_status = "unknown"
        observation = "wsrep status incomplete: " + (raw.get("status_error") or "missing or invalid wsrep counters")
    elif fault:
        check_status = "problem"
        observation = "wsrep is disconnected, not ready or outside the Primary component"
    elif state != 4 or size < 2 or paused >= WSREP_FLOW_WATCH or failures > 0:
        check_status = "watch"
        observation = "Review wsrep state, membership, flow control and certification rollbacks"
    else:
        check_status = "ok"
        observation = "wsrep is Primary, connected, ready and Synced"
    return [_check("wsrep", "replication", "Galera / wsrep", check_status, observation, "wsrep", caveat=caveat,
                   numbers=numbers, sources=list(WSREP_STATUS) + ["wsrep_provider", "wsrep_on"], value=check_status,
                   action={"text": "Inspect peer state, expected membership and a fresh workload interval; no recovery is applied."}
                   if check_status in ("watch", "problem") else None)]


def compute_checks(server, raw, notes=None, as_of=None):
    """The ``checks-1.0`` report from the raw rows. Pure."""
    notes = list(notes or [])
    checks = (_replication(server, raw) + _accounts(server, raw) + _query_cache(server, raw) +
              [_binlog_expiry(server), _memory_budget(server, raw), _version_support(server, as_of)] + _wsrep(server, raw))
    summary = {"status": "ok", "problem": 0, "watch": 0, "ok": 0, "info": 0, "unknown": 0}
    for check in checks:
        summary[check["status"]] += 1
    if summary["problem"]:
        summary["status"] = "problem"
    elif summary["watch"]:
        summary["status"] = "watch"
    if server.engine == "mysql":
        notes.append("MySQL 8.0 and later have no query cache, so there is nothing to check for it.")
    report = base_report(SCHEMA_VERSION, server)
    report.update({"summary": summary, "checks": checks, "rules": dict(RULES), "notes": notes,
                   "cache_evidence": {"variables": {n: server.variables.get(n) for n in
                                      ("key_buffer_size", "key_cache_block_size", "aria_pagecache_buffer_size", "aria_block_size")},
                                      "status": {n: (raw.get("status") or {}).get(n) for n in CACHE_STATUS},
                                      "status_error": raw.get("status_error")}})
    return report
