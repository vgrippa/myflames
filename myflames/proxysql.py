# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""ProxySQL Admin: backends, hostgroups, query rules and query digests, and
reviewed changes to them.

:func:`read_status` returns the ``proxysql-1.0`` report (schema in
``docs/schemas/proxysql-1.0.json``) for a connection to ProxySQL's Admin
interface (port 6032 by default). It reads, never writes:

* identity: ``admin-version`` from ``global_variables`` and ``SELECT
  @@version``, which Admin answers with the same version (Admin_Handler.cpp
  ``SELECT @@version`` interception; the ``version`` admin variable exists
  in 2.0.0, 2.7.3 and 3.0). A MySQL or MariaDB server, even one with a
  ``mysql_servers`` table, has neither, so it is "not ProxySQL".
* the configuration in memory (``main.mysql_servers``, the monitor
  hostgroup tables, ``main.mysql_query_rules``), what ProxySQL runs
  (``runtime_*``), what is saved (``disk.*``), the connection pool
  (``stats_mysql_connection_pool``), rule hits (``stats_mysql_query_rules``),
  the top 100 digests by total time (``stats_mysql_query_digest``) and
  ``stats_mysql_global``.

Every table is read with ``SELECT *`` by column name from the client's XML
output (:func:`select`: SQL NULL apart from the text 'NULL'), so a column or
table a ProxySQL version lacks (2.x versus 3.x) is unknown, never a crash; a
table Admin refuses (a stats-only account) is noted and its part is
unknown. A table is taken as absent from this version only when its memory,
runtime and disk reads all answer "no such table"; any other unread copy
leaves what LOAD or SAVE would copy unknown, so that module offers no
change. The configuration is the source of every action, so without it no
action is offered. Routing health is judged by what runs
(``runtime_mysql_query_rules``), never by the configuration in memory.

ProxySQL's Galera, Group Replication, replication and Aurora monitors move
servers between the hostgroups of their configuration row (writer, backup
writer, reader, offline) by health, so a runtime row can sit in another
hostgroup than its configuration row (MySQL_HostGroups_Manager.cpp
``update_*_set_*``). A runtime row is matched to its configuration row by
hostgroup, host and port, and otherwise by host and port within the same
monitor's hostgroups. The Group Replication monitor keeps an OFFLINE_SOFT
configuration while it moves a server ("PRESERVE-OFFLINE_SOFT" in
``update_group_replication_set_*``; live on 3.0.11 the server went from
writer hostgroup 30 to reader hostgroup 32 still OFFLINE_SOFT). The Galera
monitor does not: ``update_galera_set_writer`` reads the configuration
without that filter, and live on 3.0.11 an OFFLINE_SOFT writer row was back
ONLINE in the backup writer and reader hostgroups within about 20 seconds,
so OFFLINE_SOFT is not offered for a server the Galera monitor manages.
OFFLINE_HARD removed the server from every runtime hostgroup under both
monitors (live), and a weight change reached every runtime copy (live,
Galera). Moving a configuration row out of a monitor's hostgroups does not
take the server out of them: the Group Replication monitor adds a group
member it does not monitor to its reader hostgroup with default settings
(MySQL_Monitor.cpp ``update_group_replication_add_autodiscovered``), and
live on 3.0.11 mf-ic-3 moved from 30 to 50 stayed ONLINE in reader hostgroup
32 (weight 1, use_ssl 0) as well as 50 for over 80 seconds. So Move to
hostgroup is offered only for a row in no monitor's hostgroups, and the
memory-versus-runtime comparison names a runtime row a monitor keeps for a
server it no longer has in memory. Runtime holds only active query rules
(ProxySQL_Admin.cpp loads ``mysql_query_rules WHERE active=1``), so an
inactive rule is expected to be absent from ``runtime_mysql_query_rules``.

Changes (:func:`available_actions`, :func:`plan_action`,
:func:`execute_action`; db-operations contract) edit the configuration in
memory and load it with ``LOAD MYSQL SERVERS TO RUNTIME`` or ``LOAD MYSQL
QUERY RULES TO RUNTIME`` in the same unit, so the change takes effect but is
not persisted. Saving is a separate reviewed step: ``SAVE MYSQL SERVERS TO
DISK`` or ``SAVE MYSQL QUERY RULES TO DISK`` copies the configuration in
memory (not runtime) to ProxySQL's disk database, which it loads at its next
start (ProxySQL_Admin.cpp: ``SAVE ... TO DISK`` is an alias of ``SAVE ...
FROM MEMORY``; Admin_Handler.cpp ``flush_GENERIC__from_to``
``memory_to_disk``). A LOAD or SAVE applies every table of its module, not
one row (ProxySQL_Admin.cpp ``mysql_servers_tablenames``,
``mysql_query_rules_tablenames``), so the review lists every difference
between memory and runtime (or disk) first, and a row change refuses while
other changes wait in memory.

Admin matches LOAD and SAVE commands on the whole text (Admin_Handler.cpp
``is_admin_command_or_alias`` compares the length and the words), so a
leading ``/*myflames:proxysql_op*/`` comment makes them a syntax error (seen
live on 3.0.11). They are sent exactly as shown and audited as sent; the
UPDATE before them, which Admin passes to SQLite, carries the tag.
"""
import hashlib
import json
import re

from .connector import ConnectorError
from .perf_common import named_rows, now

SCHEMA_VERSION = "proxysql-1.0"
ACTION_SCHEMA_VERSION = "proxysql-action-1.0"
ACTION_TAG = "/*myflames:proxysql_op*/ "
STATUSES = ("ONLINE", "SHUNNED", "OFFLINE_SOFT", "OFFLINE_HARD")
#: ProxySQL's own CHECK constraints (ProxySQL_Admin_Tables_Definitions.h,
#: mysql_servers V2_0_11): weight 0..10000000, hostgroup_id >= 0, port
#: 0..65535. Hostgroup ids are stored in an int.
WEIGHT_MAX = 10000000
HOSTGROUP_MAX = 2147483647
DIGEST_LIMIT = 100
#: A send waits this long for LOAD or SAVE (they lock the module in Admin).
ACTION_TIMEOUT = 60

#: Module -> (tables, primary key columns per table); ProxySQL_Admin.cpp
#: mysql_servers_tablenames and mysql_query_rules_tablenames.
MODULES = {
    "servers": (("mysql_servers", ("hostgroup_id", "hostname", "port")),
                ("mysql_replication_hostgroups", ("writer_hostgroup",)),
                ("mysql_group_replication_hostgroups", ("writer_hostgroup",)),
                ("mysql_galera_hostgroups", ("writer_hostgroup",)),
                ("mysql_aws_aurora_hostgroups", ("writer_hostgroup",)),
                ("mysql_hostgroup_attributes", ("hostgroup_id",)),
                ("mysql_servers_ssl_params", ("hostname", "port", "username"))),
    "query_rules": (("mysql_query_rules", ("rule_id",)),
                    ("mysql_query_rules_fast_routing", ("username", "schemaname", "flagIN"))),
}
LOAD = {"servers": "LOAD MYSQL SERVERS TO RUNTIME;", "query_rules": "LOAD MYSQL QUERY RULES TO RUNTIME;"}
SAVE = {"servers": "SAVE MYSQL SERVERS TO DISK;", "query_rules": "SAVE MYSQL QUERY RULES TO DISK;"}
MODULE_WORD = {"servers": "servers", "query_rules": "query rules"}
#: Monitor tables and the hostgroups each row names.
MONITORS = (
    ("galera", "mysql_galera_hostgroups", ("writer_hostgroup", "backup_writer_hostgroup", "reader_hostgroup",
                                           "offline_hostgroup")),
    ("group_replication", "mysql_group_replication_hostgroups", ("writer_hostgroup", "backup_writer_hostgroup",
                                                                 "reader_hostgroup", "offline_hostgroup")),
    ("replication", "mysql_replication_hostgroups", ("writer_hostgroup", "reader_hostgroup")),
    ("aws_aurora", "mysql_aws_aurora_hostgroups", ("writer_hostgroup", "reader_hostgroup")),
)
MONITOR_WORD = {"galera": "Galera", "group_replication": "Group Replication", "replication": "replication",
                "aws_aurora": "Aurora"}
ROLE_WORD = {"writer_hostgroup": "writer", "backup_writer_hostgroup": "backup writer",
             "reader_hostgroup": "reader", "offline_hostgroup": "offline"}
#: Config columns compared between a configuration row and its runtime rows
#: (status and hostgroup are compared apart: monitors and the pool set them).
SERVER_SETTINGS = ("gtid_port", "weight", "compression", "max_connections", "max_replication_lag", "use_ssl",
                   "max_latency_ms", "comment")
#: A host, IPv4 or IPv6 address, or socket path: never a quote or backslash.
_HOST = re.compile(r"^[A-Za-z0-9._:%/\[\]-]{1,255}$")
_GLOBAL_METRICS = ("ProxySQL_Uptime", "Active_Transactions", "Client_Connections_aborted",
                   "Client_Connections_connected", "Client_Connections_created", "Server_Connections_aborted",
                   "Server_Connections_connected", "Server_Connections_created", "Questions", "Slow_queries",
                   "ConnPool_get_conn_failure", "ConnPool_get_conn_immediate", "ConnPool_get_conn_success",
                   "MyHGM_myconnpoll_get", "MyHGM_myconnpoll_reset", "mysql_backend_buffers_bytes",
                   "mysql_frontend_buffers_bytes")


# ---- reading -------------------------------------------------------------------------------

def select(conn, sql):
    """Rows as dicts by column name, SQL NULL as None and every other value as
    its text. Read as the client's XML output (``--xml`` marks NULL with
    ``xsi:nil``; Admin sends SQLite NULL as a protocol NULL,
    MySQL_Session.cpp ``SQLite3_to_MySQL``), because batch output prints SQL
    NULL and the string 'NULL' alike. A missing table or column raises
    ConnectorError (Admin and SQLite refuse the query), and so does output
    that is not well-formed XML (a control character in a value)."""
    text = conn.run(sql, extra_flags=["--xml"])
    try:
        return named_rows(text)
    except ValueError:
        raise ConnectorError("a value the client could not print as XML (such as a control character)")


#: SQLite's error for a table this ProxySQL does not have, which Admin passes
#: on ("ProxySQL Admin Error: no such table: main.x", seen live on 3.0.11).
_NO_SUCH_TABLE = "no such table:"


class _Reads(object):
    """Admin reads for one report: unread tables become None plus a note.
    *absent* holds the labels whose read failed because the table does not
    exist, apart from every other failure (privileges, a lost connection)."""

    def __init__(self, conn):
        self.conn = conn
        self.statements = []
        self.missing = []
        self.absent = set()

    def table(self, sql, label=None):
        try:
            rows = select(self.conn, sql)
        except ConnectorError as exc:
            self.missing.append((label or sql, _short(exc)))
            if _NO_SUCH_TABLE in str(exc):
                self.absent.add(label or sql)
            return None
        self.statements.append(sql)
        return rows


def _short(exc):
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else "no answer"
    if ": " in text:
        text = text.split(": ", 2)[-1]
    return text[:160]


def identify(conn):
    """``(True, version)`` for ProxySQL Admin, ``(False, reason)`` otherwise.
    Connection failures raise ConnectorError."""
    try:
        rows = select(conn, "SELECT variable_value FROM global_variables WHERE variable_name = 'admin-version'")
    except ConnectorError as exc:
        from .reviewed_writes import client_error, error_number
        code = error_number(str(exc))
        # A client error (2000 to 2999), a refused login or no answer is a
        # connection failure; a server's SQL error means "not ProxySQL".
        if code is None or client_error(code) or "Access denied" in str(exc):
            raise
        return False, "the server has no ProxySQL admin variables (%s)" % _short(exc)
    version = (rows[0].get("variable_value") if rows else None) or None
    if not version:
        return False, "the server has no admin-version variable"
    try:
        echoed = conn.run("SELECT @@version").strip()
    except ConnectorError as exc:
        return False, "SELECT @@version failed (%s)" % _short(exc)
    if echoed != version:
        return False, "@@version (%s) is not the admin-version (%s)" % (echoed[:60], version[:60])
    return True, version


def _int(value):
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _key(hg, host, port):
    return "%s|%s|%s" % (hg, host, port)


def _row_key(row, columns):
    return tuple("" if row.get(c) is None else str(row.get(c)) for c in columns)


def _not_proxysql(message, version=None):
    return {
        "schema_version": SCHEMA_VERSION, "captured_at": now(), "available": False, "is_proxysql": False,
        "version": version, "uptime_s": None, "message": message, "status": "unknown", "status_why": [message],
        "config_readable": False, "servers": [], "hostgroups": [], "monitors": [], "query_rules": [],
        "query_digests": [], "pending": _empty_pending(), "fingerprints": {}, "metrics": {}, "summary": _summary([]),
        "actions": [], "notes": [], "statements": [], "routing": None,
    }


def _empty_pending():
    return {m: {"runtime": None, "disk": None} for m in MODULES}


def _summary(servers):
    out = {"backends": 0, "ONLINE": 0, "SHUNNED": 0, "OFFLINE_SOFT": 0, "OFFLINE_HARD": 0, "not_in_runtime": 0}
    for s in servers:
        if s.get("runtime_status") is None:
            out["not_in_runtime"] += 1
            continue
        out["backends"] += 1
        status = s["runtime_status"] if s["runtime_status"] in STATUSES else "SHUNNED"
        out[status] += 1
    return out


def read_status(conn):
    """The ``proxysql-1.0`` report. Read-only; see the module docstring."""
    ok, detail = identify(conn)
    if not ok:
        return _not_proxysql("This connection is not a ProxySQL Admin interface: %s. ProxySQL Admin listens on "
                             "port 6032 unless admin-mysql_ifaces says otherwise." % detail)
    reads = _Reads(conn)
    report = _not_proxysql(None, detail)
    report.update({"available": True, "is_proxysql": True, "message": None, "status_why": []})
    report["statements"] = ["SELECT admin-version", "SELECT @@version"]

    globals_rows = reads.table("SELECT * FROM stats_mysql_global", "stats_mysql_global") or []
    values = dict((r.get("Variable_Name"), r.get("Variable_Value")) for r in globals_rows)
    report["uptime_s"] = _int(values.get("ProxySQL_Uptime"))
    report["metrics"] = dict((name, _int(values.get(name))) for name in _GLOBAL_METRICS if name in values)

    config = {}
    runtime = {}
    disk = {}
    for module, tables in MODULES.items():
        for table, _ in tables:
            config[table] = reads.table("SELECT * FROM main.%s" % table, "main.%s" % table)
            runtime[table] = reads.table("SELECT * FROM main.runtime_%s" % table, "runtime_%s" % table)
            disk[table] = reads.table("SELECT * FROM disk.%s" % table, "disk.%s" % table)
    report["config_readable"] = config.get("mysql_servers") is not None and config.get("mysql_query_rules") is not None
    pool = reads.table("SELECT * FROM stats_mysql_connection_pool", "stats_mysql_connection_pool")
    hits = reads.table("SELECT * FROM stats_mysql_query_rules", "stats_mysql_query_rules")
    digests = reads.table("SELECT * FROM stats_mysql_query_digest ORDER BY sum_time DESC LIMIT %d" % DIGEST_LIMIT,
                          "stats_mysql_query_digest")
    # The cross-check of rules and digests (proxysql_digests.Routing): the
    # runtime rules and fast routing read above, the users' default hostgroups
    # (named columns, never the password) and the Admin variables it needs.
    from . import proxysql_digests
    variables = reads.table("SELECT variable_name, variable_value FROM runtime_global_variables WHERE "
                            "variable_name IN (%s)" % ", ".join("'%s'" % v for v in proxysql_digests.VARIABLES),
                            "runtime_global_variables")
    users = reads.table("SELECT username, active, default_hostgroup, transaction_persistent, frontend FROM "
                        "runtime_mysql_users", "runtime_mysql_users")
    routing = proxysql_digests.Routing(runtime.get("mysql_query_rules"), runtime.get("mysql_query_rules_fast_routing"),
                                       users, dict((r.get("variable_name"), r.get("variable_value"))
                                                   for r in variables or ()))

    monitors = _monitors(runtime, config)
    report["monitors"] = monitors
    report["servers"] = _servers(config.get("mysql_servers"), runtime.get("mysql_servers"), pool, monitors,
                                 report["notes"])
    report["hostgroups"] = _hostgroups(report["servers"], monitors)
    report["query_rules"] = _rules(config.get("mysql_query_rules"), runtime.get("mysql_query_rules"), hits)
    report["query_digests"] = _digests(digests)
    proxysql_digests.cross_check(report["query_digests"], report["query_rules"], routing)
    report["routing"] = routing.context()
    skipped = []
    # A table this ProxySQL does not have: every copy answered "no such
    # table". Any other failure, or a copy that did read, leaves what LOAD or
    # SAVE would copy unknown.
    absent = set(table for tables in MODULES.values() for table, _ in tables
                 if set(("main.%s" % table, "runtime_%s" % table, "disk.%s" % table)) <= reads.absent)
    report["fingerprints"] = dict(("%s_%s" % (module, copy), _fingerprint(module, tables))
                                  for module in MODULES
                                  for copy, tables in (("memory", config), ("runtime", runtime), ("disk", disk)))
    report["pending"] = {
        "servers": {"runtime": _servers_runtime_diff(config, runtime, monitors, skipped, absent),
                    "disk": _module_diff("servers", config, disk, "disk", skipped, absent)},
        "query_rules": {"runtime": _rules_runtime_diff(config, runtime, skipped, absent),
                        "disk": _module_diff("query_rules", config, disk, "disk", skipped, absent)},
    }
    for name in sorted(set(skipped)):
        report["notes"].append("%s could not be read in every copy, so what loading or saving its module would "
                               "change is unknown and that module offers no change." % name)
    report["summary"] = _summary(report["servers"])
    _judge(report, runtime.get("mysql_query_rules"), runtime.get("mysql_servers") is not None or pool is not None)
    for label, why in reads.missing:
        report["notes"].append("Not read: %s (%s)." % (label, why))
    if not report["config_readable"]:
        report["notes"].append("The configuration tables could not be read with this account (a stats-only "
                               "account reads the stats schema only), so no change is offered.")
    report["statements"] += reads.statements
    report["actions"] = available_actions(report)
    for server in report["servers"]:
        server["actions"] = available_actions(report, {"config": server.get("config_key")}) \
            if server.get("config_key") else []
        server["action_notes"] = []
        if server.get("monitor") == "galera" and server.get("config_key"):
            server["action_notes"].append(
                "OFFLINE_SOFT is not offered: the Galera monitor puts the server back in traffic within seconds "
                "(seen live on ProxySQL 3.0.11). OFFLINE_HARD or weight 0 takes it out.")
        if server.get("monitor") and server.get("config_key"):
            server["action_notes"].append(
                "Move to hostgroup is not offered: a row moved out of the %s monitor's hostgroups stays in its "
                "runtime hostgroups (seen live on ProxySQL 3.0.11)." % MONITOR_WORD[server["monitor"]])
    for rule in report["query_rules"]:
        rule["actions"] = available_actions(report, {"rule_id": rule["rule_id"]})
    return report


def _monitors(runtime, config):
    out = []
    for kind, table, columns in MONITORS:
        rows = runtime.get(table)
        source = "runtime"
        if rows is None:
            rows, source = config.get(table), "memory"
        for row in rows or ():
            item = {"kind": kind, "source": source, "active": _int(row.get("active")) != 0,
                    "max_writers": _int(row.get("max_writers")),
                    "writer_is_also_reader": _int(row.get("writer_is_also_reader")),
                    "max_transactions_behind": _int(row.get("max_transactions_behind")),
                    "comment": row.get("comment") or ""}
            for column in columns:
                item[column] = _int(row.get(column))
            out.append(item)
    return out


def _family(monitors, hg):
    """(monitor, role) for an active monitor naming hostgroup *hg*."""
    for monitor in monitors:
        if not monitor["active"]:
            continue
        for column in ROLE_WORD:
            if monitor.get(column) is not None and monitor.get(column) == hg:
                return monitor, column
    return None, None


def _family_groups(monitor):
    return set(monitor.get(c) for c in ROLE_WORD if monitor.get(c) is not None)


def _server_from(row):
    return {"hostgroup_id": _int(row.get("hostgroup_id")), "hostname": row.get("hostname") or "",
            "port": _int(row.get("port")), "status": (row.get("status") or "").upper() or None,
            "weight": _int(row.get("weight")), "max_connections": _int(row.get("max_connections")),
            "max_replication_lag": _int(row.get("max_replication_lag")), "use_ssl": _int(row.get("use_ssl")),
            "max_latency_ms": _int(row.get("max_latency_ms")), "compression": _int(row.get("compression")),
            "gtid_port": _int(row.get("gtid_port")), "comment": row.get("comment") or ""}


_POOL = (("ConnUsed", "conn_used"), ("ConnFree", "conn_free"), ("ConnOK", "conn_ok"), ("ConnERR", "conn_err"),
         ("MaxConnUsed", "max_conn_used"), ("Queries", "queries"), ("Bytes_data_sent", "bytes_sent"),
         ("Bytes_data_recv", "bytes_recv"), ("Latency_us", "latency_us"))


def _config_for(hg, host, port, config_rows, monitors):
    """The configuration row a runtime row comes from, or (None, reason)."""
    exact = [r for r in config_rows if (r["hostgroup_id"], r["hostname"], r["port"]) == (hg, host, port)]
    if exact:
        return exact[0], None
    monitor, _ = _family(monitors, hg)
    if monitor is None:
        return None, "no configuration row for hostgroup %s" % hg
    groups = _family_groups(monitor)
    same = [r for r in config_rows if r["hostname"] == host and r["port"] == port and r["hostgroup_id"] in groups]
    if len(same) == 1:
        return same[0], None
    if not same:
        return None, "no configuration row in the %s monitor's hostgroups" % MONITOR_WORD[monitor["kind"]]
    return None, "configured in hostgroups %s of the same monitor" % ", ".join(str(r["hostgroup_id"]) for r in same)


def _servers(config_rows, runtime_rows, pool_rows, monitors, notes):
    config = [_server_from(r) for r in config_rows or ()]
    runtime = [_server_from(r) for r in runtime_rows or ()]
    pool = {}
    for row in pool_rows or ():
        key = _key(_int(row.get("hostgroup")), row.get("srv_host") or "", _int(row.get("srv_port")))
        pool[key] = dict((name, _int(row.get(col))) for col, name in _POOL if col in row)
        pool[key]["status"] = (row.get("status") or "").upper() or None
    out = []
    used = set()
    if runtime_rows is None and pool_rows is not None:
        # Without runtime_mysql_servers the pool still names what runs.
        runtime = [{"hostgroup_id": _int(r.get("hostgroup")), "hostname": r.get("srv_host") or "",
                    "port": _int(r.get("srv_port")), "status": (r.get("status") or "").upper() or None}
                   for r in pool_rows]
    for rt in runtime:
        hg, host, port = rt["hostgroup_id"], rt["hostname"], rt["port"]
        row_key = _key(hg, host, port)
        cfg, why = _config_for(hg, host, port, config, monitors)
        monitor, role = _family(monitors, hg)
        stats = pool.get(row_key)
        # The pool names SHUNNED_REPLICATION_LAG apart from SHUNNED.
        live = (stats or {}).get("status") or rt.get("status")
        item = {"key": "rt|" + row_key, "hostgroup_id": hg, "hostname": host, "port": port,
                "runtime_status": live, "status_detail": None, "settings": dict((k, rt.get(k)) for k in SERVER_SETTINGS),
                "pool": stats, "config": cfg, "config_key": [cfg["hostgroup_id"], cfg["hostname"], cfg["port"]] if cfg else None,
                "config_note": why, "monitor": monitor["kind"] if monitor else None,
                "role": ROLE_WORD[role] if role else None, "tone": "ok", "why": []}
        if cfg is not None:
            used.add(_key(cfg["hostgroup_id"], cfg["hostname"], cfg["port"]))
        out.append(item)
    for cfg in config:
        k = _key(cfg["hostgroup_id"], cfg["hostname"], cfg["port"])
        if k in used:
            continue
        monitor, role = _family(monitors, cfg["hostgroup_id"])
        out.append({"key": "cfg|" + k, "hostgroup_id": cfg["hostgroup_id"], "hostname": cfg["hostname"],
                    "port": cfg["port"], "runtime_status": None, "status_detail": "configuration only",
                    "settings": dict((s, cfg.get(s)) for s in SERVER_SETTINGS), "pool": None, "config": cfg,
                    "config_key": [cfg["hostgroup_id"], cfg["hostname"], cfg["port"]], "config_note": None,
                    "monitor": monitor["kind"] if monitor else None, "role": ROLE_WORD[role] if role else None,
                    "tone": "unknown", "why": []})
    _server_tones(out, monitors)
    out.sort(key=lambda s: (s["hostgroup_id"] if s["hostgroup_id"] is not None else -1, s["hostname"],
                            s["port"] or 0))
    return out


def _server_tones(servers, monitors):
    online = set((s["hostgroup_id"], s["hostname"], s["port"]) for s in servers if s["runtime_status"] == "ONLINE")
    for s in servers:
        status = s["runtime_status"]
        monitor, role = _family(monitors, s["hostgroup_id"])
        if status is None and (s.get("config") or {}).get("status") == "OFFLINE_HARD":
            s["tone"] = "watch"
            s["why"].append("OFFLINE_HARD in the configuration: ProxySQL does not use it, and runtime has no row for "
                            "it.")
        elif status is None:
            s["tone"] = "unknown"
            s["why"].append("In the configuration but not in runtime: not loaded yet.")
        elif status == "ONLINE" and role == "offline_hostgroup":
            s["tone"] = "watch"
            s["why"].append("In the %s monitor's offline hostgroup: the monitor took it out of traffic."
                            % MONITOR_WORD[monitor["kind"]])
        elif status == "ONLINE":
            s["tone"] = "ok"
        elif status.startswith("SHUNNED") and role == "writer_hostgroup" and monitor.get("backup_writer_hostgroup") \
                is not None and (monitor["backup_writer_hostgroup"], s["hostname"], s["port"]) in online:
            s["tone"] = "ok"
            s["status_detail"] = "standby writer"
            s["why"].append("SHUNNED in writer hostgroup %d and ONLINE in backup writer hostgroup %d: the %s monitor "
                            "keeps the writers above max_writers (%s) there." % (
                                s["hostgroup_id"], monitor["backup_writer_hostgroup"], MONITOR_WORD[monitor["kind"]],
                                monitor.get("max_writers")))
        elif status.startswith("SHUNNED"):
            s["tone"] = "watch"
            s["why"].append("SHUNNED: ProxySQL stopped sending to it after connection errors or replication lag "
                            "(%s)." % status)
        elif status == "OFFLINE_SOFT":
            s["tone"] = "watch"
            s["why"].append("OFFLINE_SOFT: no new traffic; queries and transactions already on it finish.")
        else:
            s["tone"] = "watch"
            s["why"].append("%s: out of traffic." % status)


def _hostgroups(servers, monitors):
    groups = {}
    for s in servers:
        if s["runtime_status"] is None or s["hostgroup_id"] is None:
            continue
        hg = groups.setdefault(s["hostgroup_id"], {"id": s["hostgroup_id"], "monitor": None, "role": None,
                                                   "servers": 0, "status": dict((k, 0) for k in STATUSES),
                                                   "standby": 0, "queries": 0, "conn_used": 0, "conn_free": 0,
                                                   "conn_err": 0, "tone": "ok", "why": []})
        hg["servers"] += 1
        status = s["runtime_status"] if s["runtime_status"] in STATUSES else "SHUNNED"
        hg["status"][status] += 1
        if s.get("status_detail") == "standby writer":
            hg["standby"] += 1
        for name in ("queries", "conn_used", "conn_free", "conn_err"):
            hg[name] += (s.get("pool") or {}).get(name) or 0
    for monitor in monitors:
        for column, word in ROLE_WORD.items():
            hg = monitor.get(column)
            if hg is None:
                continue
            entry = groups.setdefault(hg, {"id": hg, "monitor": None, "role": None, "servers": 0,
                                           "status": dict((k, 0) for k in STATUSES), "standby": 0, "queries": 0,
                                           "conn_used": 0, "conn_free": 0, "conn_err": 0, "tone": "ok", "why": []})
            if monitor["active"]:
                entry["monitor"], entry["role"] = monitor["kind"], word
    for hg in groups.values():
        if hg["servers"] and not hg["status"]["ONLINE"] and hg["role"] != "offline":
            hg["tone"] = "problem"
            hg["why"].append("No server is ONLINE in hostgroup %d, so ProxySQL cannot send its traffic anywhere."
                             % hg["id"])
        elif hg["servers"] and hg["role"] == "offline":
            hg["tone"] = "watch"
            hg["why"].append("The %s monitor moved %d %s here, out of traffic." % (
                MONITOR_WORD[hg["monitor"]], hg["servers"], "server" if hg["servers"] == 1 else "servers"))
        elif hg["status"]["ONLINE"] + hg["standby"] < hg["servers"]:
            hg["tone"] = "watch"
            hg["why"].append("%d of %d servers in hostgroup %d are out of traffic." % (
                hg["servers"] - hg["status"]["ONLINE"] - hg["standby"], hg["servers"], hg["id"]))
    return sorted(groups.values(), key=lambda h: h["id"])


def _rules(config_rows, runtime_rows, hits_rows):
    hits = dict((_int(r.get("rule_id")), _int(r.get("hits"))) for r in hits_rows or ())
    runtime = dict((_int(r.get("rule_id")), r) for r in runtime_rows or ())
    out = []
    for row in config_rows or ():
        rid = _int(row.get("rule_id"))
        if rid is None:
            continue
        rt = runtime.get(rid)
        out.append({
            "rule_id": rid, "active": _int(row.get("active")) == 1, "username": row.get("username"),
            "schemaname": row.get("schemaname"), "match_digest": row.get("match_digest"),
            "match_pattern": row.get("match_pattern"), "negate_match_pattern": _int(row.get("negate_match_pattern")) == 1,
            "destination_hostgroup": _int(row.get("destination_hostgroup")), "apply": _int(row.get("apply")) == 1,
            "comment": row.get("comment") or "", "hits": hits.get(rid) if hits_rows is not None else None,
            # Runtime holds only active rules: one absent from it is not active there.
            "runtime_active": None if runtime_rows is None else (False if rt is None else _int(rt.get("active")) == 1),
            "fields": dict(row),
        })
    out.sort(key=lambda r: r["rule_id"])
    return out


def _digests(rows):
    from .proxysql_digests import digest_row
    return [digest_row(r) for r in rows or ()]


def _describe_row(table, key, row):
    if table == "mysql_servers":
        return "hostgroup %s, %s:%s" % key
    if table == "mysql_query_rules":
        return "rule %s" % key[0]
    return "%s %s" % (table, ", ".join(key))


def _table_diff(table, pk, left, right, left_word, right_word):
    """Lines naming each row that differs between two copies of *table*
    (left is the configuration in memory)."""
    if left is None or right is None:
        return None
    a = dict((_row_key(r, pk), r) for r in left)
    b = dict((_row_key(r, pk), r) for r in right)
    lines = []
    for key in sorted(set(a) | set(b)):
        what = _describe_row(table, key, a.get(key) or b.get(key))
        if key not in b:
            lines.append("%s: in %s, not in %s (%s)" % (what, left_word, right_word, _row_summary(table, a[key])))
        elif key not in a:
            lines.append("%s: in %s, not in %s (%s)" % (what, right_word, left_word, _row_summary(table, b[key])))
        else:
            changed = ["%s %s → %s" % (c, _shown(b[key].get(c)), _shown(a[key].get(c)))
                       for c in a[key] if c in b[key] and a[key].get(c) != b[key].get(c)]
            if changed:
                lines.append("%s: %s" % (what, ", ".join(changed)))
    return lines


#: Columns that say what an added or removed row does, per table.
_SUMMARY = {"mysql_servers": ("status", "weight", "max_connections", "use_ssl"),
            "mysql_query_rules": ("active", "match_digest", "match_pattern", "destination_hostgroup", "apply")}


def _row_summary(table, row):
    columns = _SUMMARY.get(table) or [c for c in row if row.get(c) not in (None, "")][:6]
    return ", ".join("%s %s" % (c, _shown(row.get(c))) for c in columns if c in row)


def _fingerprint(module, tables):
    """A hash of every row of *module* in one copy (memory, runtime or disk),
    or None when a table could not be read: a review binds it, so a change
    to any row between review and send refuses."""
    rows = []
    for table, _ in MODULES[module]:
        value = tables.get(table)
        rows.append([table, None if value is None else sorted(json.dumps(r, sort_keys=True) for r in value)])
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def _shown(value):
    """A value in a difference line: SQL NULL as NULL, the text 'NULL' (and
    the empty string) quoted, so the two never read alike."""
    if value is None:
        return "NULL"
    text = str(value)
    return "'%s'" % text if text == "" or text.upper() == "NULL" else text


def _module_diff(module, config, other, word, skipped, absent):
    """Every row of *module* that differs between memory and *other*
    (runtime or disk, named by *word*), or None when what LOAD or SAVE would
    copy is unknown: a table of the module, its main table included, could
    not be read in one of the copies (named in *skipped*). Only a secondary
    table in *absent* (no copy of it exists in this ProxySQL) is passed
    over."""
    lines = []
    for index, (table, pk) in enumerate(MODULES[module]):
        if index and table in absent:
            continue
        if config.get(table) is None or other.get(table) is None:
            skipped.append(table)
            return None
        lines.extend(_table_diff(table, pk, config.get(table), other.get(table), "memory", word))
    return lines


def _rules_runtime_diff(config, runtime, skipped, absent):
    """The query rules module, memory versus runtime: runtime holds only the
    active rules (ProxySQL_Admin.cpp ``WHERE active=1``)."""
    rules = config.get("mysql_query_rules")
    active = None if rules is None else [r for r in rules if _int(r.get("active")) == 1]
    lines = _module_diff("query_rules", dict(config, mysql_query_rules=active), runtime, "runtime", skipped, absent)
    if lines is None:
        return None
    # A rule still active in runtime but inactive in memory reads as that change.
    inactive = set(str(_int(r.get("rule_id"))) for r in rules or () if _int(r.get("active")) != 1)
    return ["rule %s: active 1 → 0" % line.split(":")[0].split()[-1]
            if ": in runtime, not in memory" in line and line.startswith("rule ")
            and line.split(":")[0].split()[-1] in inactive else line for line in lines]


def _servers_runtime_diff(config, runtime, monitors, skipped, absent):
    """mysql_servers memory versus runtime, allowing for what ProxySQL sets
    itself: SHUNNED (the pool), and the hostgroup of a server inside a
    monitor's hostgroups (the monitor). The other server tables of the
    module are compared row by row (see :func:`_module_diff` for *absent*)."""
    if config.get("mysql_servers") is None or runtime.get("mysql_servers") is None:
        skipped.append("mysql_servers")
        return None
    lines = []
    for table, pk in MODULES["servers"][1:]:
        if table in absent:
            continue
        if config.get(table) is None or runtime.get(table) is None:
            skipped.append(table)
            return None
        lines.extend(_table_diff(table, pk, config.get(table), runtime.get(table), "memory", "runtime"))
    cfg = [_server_from(r) for r in config["mysql_servers"]]
    rt = [_server_from(r) for r in runtime["mysql_servers"]]
    matched = set()
    for c in cfg:
        monitor, _ = _family(monitors, c["hostgroup_id"])
        groups = _family_groups(monitor) if monitor else set([c["hostgroup_id"]])
        if monitor:
            peers = [r for r in rt if r["hostname"] == c["hostname"] and r["port"] == c["port"]
                     and r["hostgroup_id"] in groups]
        else:
            peers = [r for r in rt if (r["hostgroup_id"], r["hostname"], r["port"]) ==
                     (c["hostgroup_id"], c["hostname"], c["port"])]
        galera = bool(monitor) and monitor["kind"] == "galera"
        what = "hostgroup %s, %s:%s" % (c["hostgroup_id"], c["hostname"], c["port"])
        if not peers:
            if c["status"] == "OFFLINE_HARD":
                continue
            lines.append("%s: in memory, not in runtime" % what)
            continue
        for r in peers:
            matched.add((r["hostgroup_id"], r["hostname"], r["port"]))
        changed = []
        for name in SERVER_SETTINGS:
            values = set(_shown(r.get(name)) for r in peers)
            if values != set([_shown(c.get(name))]):
                changed.append("%s %s → %s" % (name, "/".join(sorted(values)), _shown(c.get(name))))
        statuses = set("ONLINE" if (r["status"] or "").startswith("SHUNNED") else (r["status"] or "unknown")
                       for r in peers)
        if c["status"] in ("ONLINE", "SHUNNED"):
            if statuses - set(["ONLINE"]):
                changed.append("status %s → %s" % ("/".join(sorted(statuses)), c["status"]))
        elif galera and c["status"] == "OFFLINE_SOFT":
            # The Galera monitor puts an OFFLINE_SOFT row back in traffic itself.
            pass
        elif c["status"] not in statuses:
            changed.append("status %s → %s" % ("/".join(sorted(statuses)), c["status"]))
        if changed:
            lines.append("%s: %s" % (what, ", ".join(changed)))
    for r in rt:
        monitor, _ = _family(monitors, r["hostgroup_id"])
        if monitor:
            groups = _family_groups(monitor)
            if not any(c["hostname"] == r["hostname"] and c["port"] == r["port"] and c["hostgroup_id"] in groups
                       for c in cfg):
                lines.append("hostgroup %s, %s:%s: in runtime (the %s monitor's hostgroups), not in memory"
                             % (r["hostgroup_id"], r["hostname"], r["port"], MONITOR_WORD[monitor["kind"]]))
            continue
        if (r["hostgroup_id"], r["hostname"], r["port"]) in matched:
            continue
        if not any((c["hostgroup_id"], c["hostname"], c["port"]) == (r["hostgroup_id"], r["hostname"], r["port"])
                   for c in cfg):
            lines.append("hostgroup %s, %s:%s: in runtime, not in memory" % (r["hostgroup_id"], r["hostname"], r["port"]))
    return lines


def _judge(report, runtime_rules, runtime_read):
    """The overall status. Routing is judged by what ProxySQL runs: the rows
    of ``runtime_mysql_query_rules`` (*runtime_rules*) against the hostgroups
    of the runtime servers (*runtime_read*: ``runtime_mysql_servers`` or the
    pool was read), never the configuration in memory, which may not be
    loaded yet. Either one unread leaves routing unknown."""
    tone = "ok"
    why = []
    rank = {"ok": 0, "unknown": 1, "watch": 2, "problem": 3}
    for hg in report["hostgroups"]:
        if rank[hg["tone"]] > rank[tone]:
            tone = hg["tone"]
        why.extend(hg["why"])
    used = set(h["id"] for h in report["hostgroups"] if h["servers"])
    unread = [name for name, value in (("runtime_mysql_query_rules", runtime_rules),
                                       ("runtime_mysql_servers and stats_mysql_connection_pool", runtime_read or None))
              if value is None]
    if unread:
        if rank[tone] < rank["unknown"]:
            tone = "unknown"
        why.append("What ProxySQL runs could not be read (%s), so whether every active query rule sends to a "
                   "hostgroup with a server is unknown." % "; ".join(unread))
    else:
        routes = sorted(((_int(r.get("rule_id")), _int(r.get("destination_hostgroup"))) for r in runtime_rules
                         if _int(r.get("active")) == 1 and _int(r.get("rule_id")) is not None), key=lambda x: x[0])
        for rid, dest in routes:
            if dest is not None and dest not in used:
                tone = "problem"
                why.append("Active rule %d sends to hostgroup %d, which has no server in runtime." % (rid, dest))
    if not report["servers"]:
        tone = "unknown"
        why.append("No backend is configured or running.")
    report["status"] = tone
    report["status_why"] = why or ["Every hostgroup in use has an ONLINE server."]


# ---- actions -------------------------------------------------------------------------------

def _server_label(cfg):
    return "%s:%s" % (cfg["hostname"], cfg["port"])


def _config_row(report, target):
    key = (target or {}).get("config") if isinstance(target, dict) else None
    if not (isinstance(key, list) and len(key) == 3):
        return None
    for s in report.get("servers") or ():
        cfg = s.get("config")
        if cfg and [cfg["hostgroup_id"], cfg["hostname"], cfg["port"]] == key:
            return cfg
    return None


def _rule(report, target):
    rid = (target or {}).get("rule_id") if isinstance(target, dict) else None
    if isinstance(rid, bool) or not isinstance(rid, int):
        return None
    return next((r for r in report.get("query_rules") or () if r["rule_id"] == rid), None)


def _action(aid, label, danger, description, value=None):
    item = {"id": aid, "label": label, "danger": danger, "description": description}
    if value:
        item["value"] = value
    return item


def _compared(report, module, kind):
    """Whether memory was compared with *kind* (runtime or disk) for every
    table of *module*: otherwise what LOAD or SAVE would copy is unknown."""
    return ((report.get("pending") or {}).get(module) or {}).get(kind) is not None


def available_actions(report, target=None):
    """The changes offered for *target* ({"config": [hg, host, port]},
    {"rule_id": n}, or None for the place's own Load and Save actions).
    Nothing without a readable configuration, and nothing for a module whose
    memory-versus-runtime comparison (and, to save, memory versus disk) is
    unknown: its LOAD or SAVE would copy rows nobody reviewed."""
    if not report or not report.get("is_proxysql") or not report.get("config_readable"):
        return []
    if target is not None and not isinstance(target, dict):
        return []
    if target is None:
        out = []
        for module in MODULES:
            word = MODULE_WORD[module]
            if not _compared(report, module, "runtime"):
                continue
            out.append(_action("%s_load" % module, "Load %s to runtime…" % word, False,
                               "Loads the %s configuration in memory into runtime (%s)." % (word, LOAD[module][:-1])))
            if not _compared(report, module, "disk"):
                continue
            out.append(_action("%s_save" % module, "Save %s to disk…" % word, False,
                               "Saves the %s configuration in memory to ProxySQL's disk database (%s), which it "
                               "loads at its next start." % (word, SAVE[module][:-1])))
        return out
    if not _compared(report, "query_rules" if "rule_id" in target else "servers", "runtime"):
        return []
    if "rule_id" in target:
        rule = _rule(report, target)
        if rule is None:
            return []
        if rule["active"]:
            return [_action("rule_disable", "Disable rule…", True,
                            "Sets active = 0 on rule %d and loads the query rules to runtime: queries it matched go "
                            "to the next matching rule or the user's default hostgroup." % rule["rule_id"])]
        return [_action("rule_enable", "Enable rule…", False,
                        "Sets active = 1 on rule %d and loads the query rules to runtime: it routes the queries it "
                        "matches from now on." % rule["rule_id"])]
    cfg = _config_row(report, target)
    if cfg is None or not isinstance(cfg["hostname"], str) or not _HOST.match(cfg["hostname"]):
        return []
    out = []
    label = _server_label(cfg)
    monitor, _ = _family(report.get("monitors") or [], cfg["hostgroup_id"])
    for status, aid, text in (
            ("ONLINE", "server_online", "ProxySQL may send traffic to %s again." % label),
            ("OFFLINE_SOFT", "server_offline_soft", "No new traffic goes to %s; queries and transactions already "
                                                    "on it finish." % label),
            ("OFFLINE_HARD", "server_offline_hard", "ProxySQL stops using %s at once: free connections close and "
                                                    "a connection a client holds closes when it is used next." % label)):
        if status == "OFFLINE_SOFT" and monitor and monitor["kind"] == "galera":
            continue
        if cfg["status"] not in STATUSES:
            # Status not read (a column this version lacks): no undo to offer.
            continue
        if cfg["status"] != status:
            out.append(_action(aid, "Set %s…" % status, status == "OFFLINE_HARD", text))
    if cfg["weight"] is not None:
        out.append(_action("server_weight", "Change weight…", False,
                           "Sets the weight ProxySQL uses to share a hostgroup's traffic between its servers.",
                           {"label": "Weight", "min": 0, "max": WEIGHT_MAX, "current": cfg["weight"]}))
    if not monitor:
        out.append(_action("server_hostgroup", "Move to hostgroup…", True,
                           "Moves the configuration row of %s to another hostgroup." % label,
                           {"label": "Hostgroup", "min": 0, "max": HOSTGROUP_MAX, "current": cfg["hostgroup_id"]}))
    return out


def _quote(text):
    """SQLite text literal (Admin runs these on SQLite): only for values that
    passed :func:`_check_host` or are fixed words."""
    return "'%s'" % text.replace("'", "''")


def _check_host(cfg):
    host, port, hg = cfg["hostname"], cfg["port"], cfg["hostgroup_id"]
    if not isinstance(host, str) or not _HOST.match(host):
        raise ValueError("The host name %r has characters myflames does not put in SQL, so no change is offered "
                         "for it." % host)
    if not isinstance(port, int) or not 0 <= port <= 65535 or not isinstance(hg, int) or not 0 <= hg <= HOSTGROUP_MAX:
        raise ValueError("The server's hostgroup or port is not a valid number.")


def _where(cfg):
    _check_host(cfg)
    return "hostgroup_id = %d AND hostname = %s AND port = %d" % (cfg["hostgroup_id"], _quote(cfg["hostname"]),
                                                                    cfg["port"])


def _strict_int(value, low, high, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be a whole number." % what)
    if not low <= value <= high:
        raise ValueError("%s must be from %d to %d." % (what, low, high))
    return value


def _runtime_text(report, cfg):
    rows = [s for s in report["servers"] if s.get("config_key") == [cfg["hostgroup_id"], cfg["hostname"], cfg["port"]]
            and s["runtime_status"] is not None]
    if not rows:
        return "Not in runtime"
    return ", ".join("hostgroup %s %s" % (s["hostgroup_id"], s["runtime_status"]) for s in rows)


def _monitor_fact(report, hg):
    monitor, role = _family(report.get("monitors") or [], hg)
    if not monitor:
        return None, None
    groups = ", ".join("%s %s" % (ROLE_WORD[c], monitor[c]) for c in ROLE_WORD if monitor.get(c) is not None)
    return monitor, "%s monitor (%s)" % (MONITOR_WORD[monitor["kind"]], groups)


def _identity(report, *copies):
    """What a plan binds besides its rows: the version and the fingerprints of
    the module copies the statements depend on (never runtime servers, which
    the pool and the monitors change on their own)."""
    prints = report.get("fingerprints") or {}
    return {"proxysql_version": report.get("version"),
            "fingerprints": dict((name, prints.get(name)) for name in copies)}


def _pending_block(report, module, kind):
    pending = report["pending"][module][kind]
    if pending is None:
        raise ValueError("The %s %s copy could not be read, so the review cannot list what %s would change."
                         % (MODULE_WORD[module], "runtime" if kind == "runtime" else "disk",
                            "loading" if kind == "runtime" else "saving"))
    return list(pending)


def plan_action(report, action, target=None, value=None):
    """The reviewed plan for *action* on *target* from *report* (a fresh
    read). Raises ValueError when the action is not offered for that target
    now, or the value is not valid."""
    if not report or not report.get("is_proxysql"):
        raise ValueError("This connection is not a ProxySQL Admin interface.")
    if not isinstance(action, str):
        raise ValueError("Choose an action.")
    offered = [a["id"] for a in available_actions(report, target)]
    if action not in offered:
        raise ValueError("That change is not offered for this target now. Refresh and try again.")
    plan = {"schema_version": ACTION_SCHEMA_VERSION, "action": action, "target": target,
            "value": value if action in ("server_weight", "server_hostgroup") else None,
            "facts": [["ProxySQL", report.get("version") or "Unknown"]], "warnings": [], "typed_guard": None,
            "undo": None, "next": None, "verified": ["ProxySQL Admin identity (admin-version)"],
            "timeout_s": ACTION_TIMEOUT}
    if action.endswith("_load") or action.endswith("_save"):
        plan = _plan_module(report, plan, action)
    elif action.startswith("rule_"):
        plan = _plan_rule(report, plan, action, target)
    else:
        plan = _plan_server(report, plan, action, target, value)
    plan["checks"] = _checks(plan)
    return plan


def _checks(plan):
    """What to look at when a send got no answer (shown with Outcome unknown)."""
    action = plan["action"]
    audit = "The Audit log keeps this attempt as Outcome unknown; nothing is sent again."
    if action.endswith("_save"):
        return ["Refresh this place: the Configuration line says whether memory matches disk now.", audit]
    module = "query rules" if action.startswith(("rule_", "query_rules")) else "servers"
    load = LOAD["query_rules" if module == "query rules" else "servers"][:-1]
    if action.endswith("_load"):
        return ["Refresh this place: the Configuration line says whether memory matches runtime now.", audit]
    if action.startswith("rule_"):
        what = "Query rules shows rule %d as active or not in memory, and its inspector says whether runtime " \
               "runs it." % plan["target"]["rule_id"]
    else:
        cfg = plan["expect"]["config"]
        what = "Servers shows %s:%s as ProxySQL runs it, and its inspector shows the configuration row in " \
               "memory." % (cfg["hostname"], cfg["port"])
    return ["Refresh this place. %s" % what,
            "If memory changed and runtime did not, the UPDATE ran and %s did not: the Configuration line then "
            "says a change is not loaded to runtime. Load %s to runtime, or undo." % (load, module),
            audit]


def _plan_module(report, plan, action):
    module, step = action.rsplit("_", 1)
    word = MODULE_WORD[module]
    runtime = _pending_block(report, module, "runtime")
    if step == "load":
        statement = LOAD[module]
        plan.update(eyebrow="LOAD", title="Load the %s configuration to runtime?" % word,
                    confirm_label="Load %s to runtime" % word, target_text="ProxySQL runtime (%s)" % word,
                    description="ProxySQL starts using the %s configuration in memory now. It is not saved to disk."
                                % word)
        plan["changes"] = runtime
        if not runtime:
            plan["warnings"].append("myflames found no difference between memory and runtime, so loading changes "
                                    "nothing it can see.")
        if module == "servers":
            plan["warnings"].append("Servers in a monitor's hostgroups are compared by their settings only: the "
                                    "monitor sets their hostgroup and status in runtime.")
        copies = ["%s_memory" % module] + (["query_rules_runtime"] if module == "query_rules" else [])
        plan["expect"] = dict(_identity(report, *copies), pending_runtime=runtime)
        plan["undo"] = "There is no automatic undo: change the configuration back and load it again."
        plan["next"] = {"action": "%s_save" % module, "label": "Save %s to disk…" % word}
    else:
        statement = SAVE[module]
        disk = _pending_block(report, module, "disk")
        plan.update(eyebrow="SAVE", title="Save the %s configuration to disk?" % word,
                    confirm_label="Save %s to disk" % word, target_text="ProxySQL disk database (%s)" % word,
                    description="Copies the %s configuration in memory to the disk database, which ProxySQL loads "
                                "at its next start. Runtime does not change." % word)
        plan["changes"] = disk
        if not disk:
            plan["warnings"].append("Memory and disk already match for %s." % word)
        if runtime:
            plan["warnings"].append("Memory has %d %s not loaded to runtime; saving stores %s on disk although "
                                    "ProxySQL is not running %s." % (len(runtime), "change" if len(runtime) == 1
                                                                      else "changes", "it" if len(runtime) == 1
                                                                      else "them", "it" if len(runtime) == 1
                                                                      else "them"))
        plan["expect"] = dict(_identity(report, "%s_memory" % module, "%s_disk" % module), pending_runtime=runtime,
                              pending_disk=disk)
        plan["undo"] = "Cannot be undone: the disk copy before this save is replaced."
    plan["statements"] = [statement]
    plan["facts"].append(["Applies", "Every table of the %s module, not one row" % word])
    return plan


def _plan_rule(report, plan, action, target):
    rule = _rule(report, target)
    runtime = _pending_block(report, "query_rules", "runtime")
    if runtime:
        raise ValueError("Other query rule changes wait in memory, and LOAD MYSQL QUERY RULES TO RUNTIME would "
                         "apply them too: %s. Review them with Load query rules to runtime first."
                         % "; ".join(runtime[:5]))
    active = action == "rule_enable"
    verb = "Enable" if active else "Disable"
    sql = "UPDATE mysql_query_rules SET active = %d WHERE rule_id = %d;" % (1 if active else 0, rule["rule_id"])
    undo = "UPDATE mysql_query_rules SET active = %d WHERE rule_id = %d;" % (0 if active else 1, rule["rule_id"])
    match = rule["match_digest"] or rule["match_pattern"] or "every query"
    plan.update(eyebrow="UPDATE", title="%s query rule %d?" % (verb, rule["rule_id"]),
                confirm_label="%s rule %d" % (verb, rule["rule_id"]), target_text="Query rule %d" % rule["rule_id"],
                description=("Rule %d routes the queries it matches from now on." if active else
                             "Queries rule %d matched go to the next matching rule, or to the user's default "
                             "hostgroup.") % rule["rule_id"])
    plan["statements"] = [ACTION_TAG + sql, LOAD["query_rules"]]
    plan["facts"] += [["Rule", "%d: %s" % (rule["rule_id"], match)],
                      ["Destination hostgroup", "None" if rule["destination_hostgroup"] is None
                       else str(rule["destination_hostgroup"])],
                      ["Hits", "Not read" if rule["hits"] is None else "{:,}".format(rule["hits"])],
                      ["Active", "%s → %s" % ("Yes" if rule["active"] else "No", "Yes" if active else "No")]]
    plan["warnings"].append("Applied to runtime only. Save query rules to disk afterwards, or ProxySQL loads the "
                            "previous rules at its next start.")
    plan["expect"] = dict(_identity(report, "query_rules_memory", "query_rules_runtime"), rule=rule["fields"],
                          pending_runtime=runtime)
    plan["undo"] = "%s then %s" % (undo, LOAD["query_rules"])
    plan["verified"].append("rule %d unchanged since review" % rule["rule_id"])
    plan["next"] = {"action": "query_rules_save", "label": "Save query rules to disk…"}
    return plan


def _plan_server(report, plan, action, target, value):
    cfg = _config_row(report, target)
    where = _where(cfg)
    runtime = _pending_block(report, "servers", "runtime")
    if runtime:
        raise ValueError("Other server changes wait in memory, and LOAD MYSQL SERVERS TO RUNTIME would apply them "
                         "too: %s. Review them with Load servers to runtime first." % "; ".join(runtime[:5]))
    label = _server_label(cfg)
    monitor, monitor_text = _monitor_fact(report, cfg["hostgroup_id"])
    plan["facts"] += [["Configuration row", "hostgroup %d, %s" % (cfg["hostgroup_id"], label)],
                      ["Runtime", _runtime_text(report, cfg)]]
    if monitor_text:
        plan["facts"].append(["Managed by", monitor_text])
    online = [s for s in report["servers"] if s["runtime_status"] == "ONLINE"]
    if action in ("server_online", "server_offline_soft", "server_offline_hard"):
        status = {"server_online": "ONLINE", "server_offline_soft": "OFFLINE_SOFT",
                  "server_offline_hard": "OFFLINE_HARD"}[action]
        sql = "UPDATE mysql_servers SET status = '%s' WHERE %s;" % (status, where)
        undo = "UPDATE mysql_servers SET status = '%s' WHERE %s;" % (cfg["status"], where)
        plan.update(eyebrow="UPDATE", title="Set %s to %s?" % (label, status), confirm_label="Set %s" % status,
                    target_text="%s (hostgroup %d)" % (label, cfg["hostgroup_id"]),
                    description={"ONLINE": "ProxySQL may send traffic to %s again." % label,
                                 "OFFLINE_SOFT": "No new traffic goes to %s; queries and transactions already on it "
                                                 "finish." % label,
                                 "OFFLINE_HARD": "ProxySQL stops using %s at once: free connections close, and a "
                                                 "connection a client holds closes when it is used next." % label}[status])
        plan["facts"].append(["Status", "%s → %s" % (cfg["status"], status)])
        if status != "ONLINE":
            # Every runtime row of this server goes out of traffic: is it the
            # last ONLINE server of a hostgroup?
            mine = set(s["hostgroup_id"] for s in report["servers"] if s.get("config_key") ==
                       [cfg["hostgroup_id"], cfg["hostname"], cfg["port"]] and s["runtime_status"] == "ONLINE")
            last = sorted(hg for hg in mine if not [s for s in online if s["hostgroup_id"] == hg and
                                                     (s["hostname"], s["port"]) != (cfg["hostname"], cfg["port"])])
            if last:
                plan["warnings"].append("%s is the only ONLINE server in hostgroup %s: its traffic has nowhere to go "
                                        "until another server is ONLINE." % (label, ", ".join(str(h) for h in last)))
            if status == "OFFLINE_HARD" or last:
                plan["typed_guard"] = label
        if monitor and status == "OFFLINE_HARD":
            plan["warnings"].append("The %s monitor manages this server's hostgroups; check runtime after the "
                                    "change." % MONITOR_WORD[monitor["kind"]])
        if monitor and status == "OFFLINE_SOFT" and monitor["kind"] == "group_replication":
            plan["warnings"].append("The Group Replication monitor keeps OFFLINE_SOFT when it moves the server "
                                    "between its hostgroups.")
        elif monitor and status == "OFFLINE_SOFT":
            plan["warnings"].append("The %s monitor manages this server's hostgroups; check runtime after the "
                                    "change." % MONITOR_WORD[monitor["kind"]])
    elif action == "server_weight":
        weight = _strict_int(value, 0, WEIGHT_MAX, "Weight")
        if weight == cfg["weight"]:
            raise ValueError("The weight is already %d." % weight)
        sql = "UPDATE mysql_servers SET weight = %d WHERE %s;" % (weight, where)
        undo = "UPDATE mysql_servers SET weight = %d WHERE %s;" % (cfg["weight"], where)
        plan.update(eyebrow="UPDATE", title="Change the weight of %s to %d?" % (label, weight),
                    confirm_label="Set weight %d" % weight, target_text="%s (hostgroup %d)" % (label, cfg["hostgroup_id"]),
                    description="ProxySQL shares a hostgroup's traffic between its ONLINE servers by weight." +
                                (" Weight 0 sends it nothing while others have weight." if weight == 0 else ""))
        plan["facts"].append(["Weight", "%s → %d" % (cfg["weight"], weight)])
    else:
        hostgroup = _strict_int(value, 0, HOSTGROUP_MAX, "Hostgroup")
        if hostgroup == cfg["hostgroup_id"]:
            raise ValueError("The server is already in hostgroup %d." % hostgroup)
        if any(c.get("config") and [c["config"]["hostgroup_id"], c["config"]["hostname"], c["config"]["port"]] ==
               [hostgroup, cfg["hostname"], cfg["port"]] for c in report["servers"]):
            raise ValueError("%s is already configured in hostgroup %d." % (label, hostgroup))
        target_monitor, _ = _family(report.get("monitors") or [], hostgroup)
        if monitor:
            raise ValueError("The %s monitor manages hostgroup %d: a row moved out of its hostgroups stays in its "
                             "runtime hostgroups (seen live on ProxySQL 3.0.11), so nothing is offered. Change the "
                             "monitor's configuration, or set the server OFFLINE_HARD." % (
                                 MONITOR_WORD[monitor["kind"]], cfg["hostgroup_id"]))
        sql = "UPDATE mysql_servers SET hostgroup_id = %d WHERE %s;" % (hostgroup, where)
        moved = dict(cfg, hostgroup_id=hostgroup)
        undo = "UPDATE mysql_servers SET hostgroup_id = %d WHERE %s;" % (cfg["hostgroup_id"], _where(moved))
        plan.update(eyebrow="UPDATE", title="Move %s to hostgroup %d?" % (label, hostgroup),
                    confirm_label="Move to hostgroup %d" % hostgroup,
                    target_text="%s (hostgroup %d)" % (label, cfg["hostgroup_id"]),
                    description="Traffic routed to hostgroup %d stops reaching %s, and traffic for hostgroup %d "
                                "can reach it." % (cfg["hostgroup_id"], label, hostgroup))
        plan["facts"].append(["Hostgroup", "%d → %d" % (cfg["hostgroup_id"], hostgroup)])
        if target_monitor:
            plan["warnings"].append("Hostgroup %d belongs to the %s monitor, which then decides which of its "
                                    "hostgroups %s is in." % (hostgroup, MONITOR_WORD[target_monitor["kind"]], label))
        mine = [s for s in online if s["hostgroup_id"] == cfg["hostgroup_id"]]
        if mine and all((s["hostname"], s["port"]) == (cfg["hostname"], cfg["port"]) for s in mine):
            plan["warnings"].append("%s is the only ONLINE server in hostgroup %d." % (label, cfg["hostgroup_id"]))
            plan["typed_guard"] = label
    plan["statements"] = [ACTION_TAG + sql, LOAD["servers"]]
    plan["warnings"].append("Applied to runtime only. Save servers to disk afterwards, or ProxySQL loads the "
                            "previous configuration at its next start.")
    plan["expect"] = dict(_identity(report, "servers_memory"), config=cfg, pending_runtime=runtime)
    plan["undo"] = "%s then %s" % (undo, LOAD["servers"])
    plan["verified"].append("configuration row %s unchanged since review" % label)
    plan["next"] = {"action": "servers_save", "label": "Save servers to disk…"}
    return plan


def _after(report, plan):
    """What the result panel shows after a send: the target as ProxySQL has
    it now, and what is still not loaded or not saved."""
    action = plan["action"]
    module = "query_rules" if action.startswith("rule_") or action.startswith("query_rules") else "servers"
    out = {"pending_runtime": report["pending"][module]["runtime"], "pending_disk": report["pending"][module]["disk"]}
    if action.startswith("server_"):
        cfg = plan["expect"]["config"]
        host, port = cfg["hostname"], cfg["port"]
        hg = plan["value"] if action == "server_hostgroup" else cfg["hostgroup_id"]
        now_cfg = next((s["config"] for s in report["servers"] if s.get("config") and
                        (s["config"]["hostgroup_id"], s["config"]["hostname"], s["config"]["port"]) == (hg, host, port)),
                       None)
        out["config"] = now_cfg
        out["runtime"] = [{"hostgroup_id": s["hostgroup_id"], "status": s["runtime_status"],
                           "weight": s["settings"].get("weight")}
                          for s in report["servers"] if (s["hostname"], s["port"]) == (host, port)
                          and s["runtime_status"] is not None]
        monitor, _ = _family(report.get("monitors") or [], hg)
        out["monitor"] = monitor["kind"] if monitor else None
    elif action.startswith("rule_"):
        rid = plan["target"]["rule_id"]
        rule = next((r for r in report["query_rules"] if r["rule_id"] == rid), None)
        out["rule"] = None if rule is None else {"rule_id": rid, "active": rule["active"],
                                                 "runtime_active": rule["runtime_active"]}
    return out


def _done_text(plan, after):
    action = plan["action"]
    if action.endswith("_save"):
        left = (after or {}).get("pending_disk")
        return "Saved to disk." + ("" if not left else " %d differences remain between memory and disk." % len(left))
    if action.endswith("_load"):
        return "Loaded to runtime. Not saved to disk yet."
    text = "Changed and loaded to runtime. Not saved to disk yet: ProxySQL loads the previous configuration at its " \
           "next start unless you save."
    if (after or {}).get("monitor"):
        text += " The %s monitor may move the server between its hostgroups within seconds; Refresh shows where " \
                "it runs." % MONITOR_WORD[after["monitor"]]
    return text


def _partial_text(ran, failed, error):
    return ("The configuration in memory changed (%s), but %s failed: %s. ProxySQL still runs the previous "
            "configuration. Check the configuration, then load it to runtime or undo the change."
            % (ran[0][len(ACTION_TAG):] if ran and ran[0].startswith(ACTION_TAG) else "; ".join(ran), failed, error))


def _runtime_words(rows):
    if rows is None:
        return "Not read"
    if not rows:
        return "No row"
    return "; ".join("hostgroup %s %s%s" % (r["hostgroup_id"], r["status"], "" if r.get("weight") is None
                                            else ", weight %s" % r["weight"]) for r in rows)


def _count_words(lines, none, word):
    if lines is None:
        return "Not compared"
    return none if not lines else "%d %s" % (len(lines), word if len(lines) == 1 else word + "s")


def _effect(plan, before, after):
    """What changed: (what, before, now) rows from the reads before and after
    the send (plan["expect"] holds the reviewed configuration row or rule)."""
    action = plan["action"]
    rows = []
    if action.startswith("server_"):
        cfg = plan["expect"]["config"]
        now_cfg = after.get("config")
        name, label = {"server_weight": ("weight", "Weight in memory"),
                       "server_hostgroup": ("hostgroup_id", "Hostgroup in memory")}.get(action,
                                                                                    ("status", "Status in memory"))
        rows.append([label, _shown(cfg.get(name)), "Row not found" if now_cfg is None else _shown(now_cfg.get(name))])
        rows.append(["Runtime", _runtime_words(before.get("runtime")), _runtime_words(after.get("runtime"))])
    elif action.startswith("rule_"):
        rid = plan["target"]["rule_id"]
        b, a = before.get("rule") or {}, after.get("rule") or {}

        def yes(value):
            return "Not read" if value is None else ("Active" if value else "Not active")
        rows.append(["Rule %d in memory" % rid, yes(b.get("active")), yes(a.get("active"))])
        rows.append(["Rule %d in runtime" % rid, yes(b.get("runtime_active")), yes(a.get("runtime_active"))])
    if not action.endswith("_save"):
        rows.append(["Memory versus runtime", _count_words(before.get("pending_runtime"), "Match", "change"),
                     _count_words(after.get("pending_runtime"), "Match", "change")])
    rows.append(["Memory versus disk", _count_words(before.get("pending_disk"), "Match", "difference"),
                 _count_words(after.get("pending_disk"), "Match", "difference")])
    return rows


def execute_action(conn, reviewed, audit=None, typed=None, before_send=None, progress=None):
    """Send a reviewed plan (see :mod:`myflames.reviewed_writes`): read the
    target again, refuse with nothing sent when it changed, then send the
    statements as one unit and read the result back. *progress* follows the
    steps for the sheet (reviewed_writes.Progress)."""
    from . import reviewed_writes
    reviewed = reviewed_writes.check_reviewed(reviewed)
    if audit is not None:
        audit.server("proxysql", (reviewed.get("expect") or {}).get("proxysql_version"))
    state = {}

    def fresh():
        report = read_status(conn)
        if not report.get("is_proxysql"):
            raise ValueError("This connection is no longer a ProxySQL Admin interface.")
        plan = plan_action(report, reviewed["action"], reviewed.get("target"), reviewed.get("value"))
        state["before"] = _after(report, reviewed)
        return plan

    return reviewed_writes.execute(conn, reviewed, fresh, ACTION_SCHEMA_VERSION, audit=audit, typed=typed,
                                   before_send=before_send, read_after=lambda: _after(read_status(conn), reviewed),
                                   timeout=ACTION_TIMEOUT, partial_text=_partial_text, done_text=_done_text,
                                   progress=progress,
                                   effect=lambda plan, after: _effect(plan, state.get("before") or {}, after))
