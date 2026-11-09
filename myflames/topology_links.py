# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only multi-tier topology discovery across ProxySQL, Readyset and MySQL/MariaDB.

Each topology place (ProxySQL, Readyset, Replication, Cluster) reads its own
connected server first. When saved workspace connections are passed with
``open_profile`` (from :func:`myflames.replication.profile_opener`), this module
probes candidate saved connections under strict bounds (single-flight per
profile ID, keyed by the store's settings + secret-reference fingerprint, at
most ``PROFILE_READ_LIMIT`` opens and ``PROFILE_BUDGET_S`` wall-clock seconds
per request, ``QUERY_TIMEOUT_S`` per query, never prompting for a password) so
multi-tier topologies such as ``ProxySQL -> Readyset -> MySQL/MariaDB``
(standalone, async primary/replicas, Galera, PXC, Group Replication, InnoDB
Cluster) can render verified cross-tier edges and ``Use <connection>`` actions
without guessing endpoints.
"""
import collections
import json
import re
import socket
import sqlite3
import threading
import time

from . import dbops
from .connector import ConnectorError
from .perf_common import Reader, named_rows, reason
from .server_info import is_readyset_comment, product_identity

TOPOLOGY_TAG = "/*myflames:topology*/ "
CACHE_SIZE = 64
ATTEMPT_LIMIT = 128
PROFILE_READ_LIMIT = 6
PROFILE_BUDGET_S = 5.0
QUERY_TIMEOUT_S = 2

_LOCAL_NAMES = frozenset(("127.0.0.1", "localhost", "::1"))
_READYSET_META_RE = re.compile(r"readyset-meta:\s*(\{.*\})", re.I | re.S)

_DB_VARS_SQL = (
    "SHOW GLOBAL VARIABLES WHERE Variable_name IN ("
    "'version','version_comment','hostname','port','report_host','report_port',"
    "'server_id','server_uuid','read_only','super_read_only',"
    "'wsrep_on','wsrep_cluster_name','wsrep_cluster_address','wsrep_node_name','wsrep_node_incoming_address',"
    "'group_replication_group_name','gtid_mode')"
)
_WSREP_STATUS_SQL = (
    "SHOW GLOBAL STATUS WHERE Variable_name IN ("
    "'wsrep_cluster_size','wsrep_cluster_status','wsrep_local_state_comment')"
)
_WSREP_MEMBERS_SQL = "SELECT node_uuid, node_name, node_incoming_address FROM mysql.wsrep_cluster_members"
_GR_MEMBERS_SQL = (
    "SELECT MEMBER_ID, MEMBER_HOST, MEMBER_PORT, MEMBER_STATE, MEMBER_ROLE "
    "FROM performance_schema.replication_group_members"
)
_INNODB_CLUSTER_SQL = (
    "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
    "WHERE SCHEMA_NAME = 'mysql_innodb_cluster_metadata' LIMIT 1"
)
_LIVE_ATTRS_SQL = (
    "SELECT DISTINCT t.PROCESSLIST_HOST, a.ATTR_NAME, a.ATTR_VALUE "
    "FROM performance_schema.session_connect_attrs a "
    "JOIN performance_schema.threads t ON t.THREAD_ID = a.THREAD_ID "
    "WHERE a.ATTR_NAME IN ('_program_name', 'program_name', '_client_name') "
    "AND (a.ATTR_VALUE LIKE 'READYSET%' OR LOWER(a.ATTR_VALUE) LIKE '%proxysql%') "
    "LIMIT 50"
)
_HEARTBEAT_TABLE_SQL = (
    "SELECT 1 FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = '_readyset' AND TABLE_NAME = '_readyset_heartbeat' LIMIT 1"
)

_cache_lock = threading.Lock()
_probe_cache = collections.OrderedDict()
_probe_pending = {}
_probe_attempts = set()


def _reset_cache_for_tests():
    with _cache_lock:
        _probe_cache.clear()
        _probe_pending.clear()
        _probe_attempts.clear()


def _int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value):
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _host_key(host):
    if not isinstance(host, str):
        return ""
    h = host.strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def is_loopback(host):
    h = _host_key(host)
    return h in _LOCAL_NAMES or h.startswith("127.")


def split_host_port(value, default_port=None):
    """Parse ``host:port`` or ``[ipv6]:port`` into ``(host, port)``."""
    raw = _text(value)
    if not raw:
        return None, default_port
    if raw.startswith("["):
        end = raw.find("]")
        if end > 0:
            host = raw[1:end]
            rest = raw[end + 1:]
            if rest.startswith(":"):
                return host, _int(rest[1:]) or default_port
            return host, default_port
    if raw.count(":") == 1:
        host, port_s = raw.rsplit(":", 1)
        port = _int(port_s)
        if port is not None:
            return host.strip() or None, port
    return raw, default_port


def format_endpoint(host, port):
    h = _text(host) or "?"
    if ":" in h and not h.startswith("["):
        h = "[%s]" % h
    return "%s:%s" % (h, port) if port else h


def parse_readyset_version(rows):
    """Extract Readyset release version from ``SHOW READYSET VERSION`` rows.

    Real Readyset returns two-column ``(name, value)`` rows where row 0 is
    ``("release version", "stable-261022")``; single-column mocks return
    ``[{"version": "26.1.0"}]``.
    """
    if not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict) or not first:
        return None
    if len(first) >= 2 or ("name" in first and "value" in first):
        kv = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            vals = list(r.values())
            if len(vals) >= 2 and vals[0]:
                kv[str(vals[0]).strip().lower()] = vals[1]
        for key in ("release version", "release-version", "release_version", "version"):
            v = _text(kv.get(key))
            if v:
                return v
        vals0 = list(first.values())
        return _text(vals0[1]) if len(vals0) >= 2 else None
    return _text(next(iter(first.values())))


def _profile_name(profile):
    return profile.get("name") or profile.get("id") or "connection"


def _profile_port(profile, default=3306):
    return _int(profile.get("port")) or default


def _usable_profiles(profiles, skip_profile_id=None):
    out = []
    for p in profiles or ():
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if skip_profile_id and pid and pid == skip_profile_id:
            continue
        if p.get("remember_password") or p.get("has_saved_password"):
            out.append(p)
    return out


def _evict_locked():
    for failures in (False, True):
        for key in [k for k, e in _probe_cache.items()
                    if e["done"].is_set() and (e["data"] is None) == failures]:
            if len(_probe_cache) <= CACHE_SIZE:
                return
            del _probe_cache[key]


def _claim_probe(profile, open_profile, reads, remaining):
    from .replication import _settings_fingerprint
    name = _profile_name(profile)
    profile_id = profile.get("id") or name
    with _cache_lock:
        pending = _probe_pending.get(profile_id)
        if pending is not None and not pending["done"].is_set():
            return pending, False, True
        prepare = getattr(open_profile, "prepare", None)
        if prepare is not None:
            fingerprint, connect = prepare(profile)
        else:
            fingerprint, connect = _settings_fingerprint(profile), (lambda: open_profile(profile))
        key = (profile_id, fingerprint)
        entry = _probe_cache.get(key)
        if entry is not None:
            _probe_cache.move_to_end(key)
            return entry, False, False
        if key in _probe_attempts:
            raise ValueError("Saved connection %s was already checked earlier." % name)
        if len(_probe_attempts) >= ATTEMPT_LIMIT:
            raise ValueError("Topology discovery reached its per-process connection check limit.")
        if reads[0] >= PROFILE_READ_LIMIT:
            raise ValueError("More than %d saved connections were candidates; the rest were not opened."
                             % PROFILE_READ_LIMIT)
        if remaining() < QUERY_TIMEOUT_S:
            raise ValueError("Topology discovery spent its %g s budget before checking %s."
                             % (PROFILE_BUDGET_S, name))
        for old in [k for k in _probe_cache if k[0] == key[0]]:
            del _probe_cache[old]
        entry = {"done": threading.Event(), "data": None, "problem": None, "kept": True,
                 "key": key, "connect": connect}
        _probe_attempts.add(key)
        _probe_pending[profile_id] = entry
        _probe_cache[key] = entry
        _evict_locked()
        return entry, True, False


def _named_rows(conn, sql):
    text = conn.run(sql, extra_flags=["--comments", "--xml"], timeout=QUERY_TIMEOUT_S)
    try:
        return named_rows(text)
    except ValueError:
        raise ConnectorError("a value the client could not print as XML")


def _probe_connection(conn, profile):
    """Read a lightweight, bounded identity + topology summary from *conn*."""
    reader = Reader(conn, TOPOLOGY_TAG)
    base = {
        "profile_id": profile.get("id"),
        "profile_name": _profile_name(profile),
        "profile_host": _text(profile.get("host")) or "127.0.0.1",
        "profile_port": _profile_port(profile),
        "ssh_host": _text(profile.get("ssh_host")),
    }

    # 1. Check Readyset first: single-column SELECT @@version_comment works on
    # MySQL, MariaDB, Percona, Readyset Adapter (3307) and Readyset Admin (6036).
    comment = ""
    try:
        crows = _named_rows(conn, "SELECT @@version_comment")
        comment = (next(iter(crows[0].values())) if crows and crows[0] else "") or ""
    except ConnectorError:
        comment = ""

    if is_readyset_comment(comment):
        version = None
        try:
            vrows = _named_rows(conn, "SHOW READYSET VERSION")
            version = parse_readyset_version(vrows)
        except ConnectorError:
            version = None
        db_name = (getattr(conn, "database", None) or _text(profile.get("database")) or "").strip()
        port_role = "adapter"
        if not db_name:
            try:
                _named_rows(conn, "SELECT 1 FROM replication_status LIMIT 0")
                port_role = "admin"
            except ConnectorError:
                port_role = "adapter"
        elif db_name.lower() == "readyset":
            if _profile_port(profile) == 6036:
                port_role = "admin"
            else:
                try:
                    drows = _named_rows(conn, "SHOW DATABASES")
                    names = {
                        (next(iter(r.values())) or "").strip().lower()
                        for r in drows if r
                    } - {""}
                    if names and names <= {"information_schema", "public", "readyset"}:
                        port_role = "admin"
                except ConnectorError:
                    pass
        status_map = {}
        try:
            srows = _named_rows(conn, "SHOW READYSET STATUS")
            for r in srows:
                vals = list(r.values())
                if len(vals) >= 2 and vals[0]:
                    status_map[vals[0]] = vals[1]
        except ConnectorError:
            pass
        upstreams = []
        for prefix in ("readyset.", ""):
            try:
                urows = _named_rows(
                    conn,
                    "SELECT host, port, database, user, weight, purpose, active_cdc, health, connections "
                    "FROM %supstream_servers" % prefix,
                )
                upstreams = urows
                break
            except ConnectorError:
                continue
        cache_count = None
        try:
            crows = _named_rows(conn, "SHOW CACHES")
            cache_count = len(crows)
        except ConnectorError:
            cache_count = None
        base.update({
            "kind": "readyset",
            "product": "Readyset",
            "port_role": port_role,
            "version": version,
            "status_text": _text(status_map.get("Status")) or "Online",
            "replication_status": _text(status_map.get("Replication Status")),
            "cdc_server": _text(status_map.get("CDC Server")),
            "database_connection": _text(status_map.get("Database Connection")),
            "controller_status": _text(status_map.get("Readyset Controller Status")),
            "upstreams": upstreams,
            "cache_count": cache_count,
        })
        return base

    # 2. Check ProxySQL Admin: global_variables admin-version echoed by @@version.
    # Never use Reader here: ProxySQL Admin intercepts bare `SELECT @@version`
    # before SQLite, whereas a leading `/*myflames:topology*/` comment falls
    # through to SQLite and fails on `@@version`.
    try:
        prows = _named_rows(conn, "SELECT variable_value FROM global_variables WHERE variable_name = 'admin-version'")
        admin_ver = _text(prows[0].get("variable_value")) if prows else None
    except ConnectorError:
        admin_ver = None
    if admin_ver:
        echoed_ver = None
        try:
            erows = _named_rows(conn, "SELECT @@version")
            echoed_ver = _text(next(iter(erows[0].values()))) if erows and erows[0] else None
        except ConnectorError:
            try:
                echoed_ver = _text(conn.run("SELECT @@version", timeout=QUERY_TIMEOUT_S))
            except ConnectorError:
                echoed_ver = None
        if echoed_ver == admin_ver:
            servers = []
            for tbl in ("runtime_mysql_servers", "mysql_servers"):
                try:
                    servers = _named_rows(
                        conn,
                        "SELECT hostgroup_id, hostname, port, status, weight, comment FROM %s" % tbl,
                    )
                    break
                except ConnectorError:
                    continue
            rules = []
            for tbl in ("runtime_mysql_query_rules", "mysql_query_rules"):
                try:
                    rules = _named_rows(
                        conn,
                        "SELECT rule_id, active, destination_hostgroup, mirror_hostgroup, comment "
                        "FROM %s WHERE active = 1 LIMIT 200" % tbl,
                    )
                    break
                except ConnectorError:
                    continue
            base.update({
                "kind": "proxysql",
                "product": "ProxySQL",
                "version": admin_ver,
                "servers": servers,
                "query_rules": rules,
            })
            return base

    # 3. Standard MySQL / Percona / MariaDB server.
    vrows = reader.rows(_DB_VARS_SQL, timeout=QUERY_TIMEOUT_S)
    variables = dict((r[0], r[1] if len(r) > 1 else "") for r in vrows if r)
    version = _text(variables.get("version"))
    if not version:
        v_fallback = reader.rows("SELECT @@version", timeout=QUERY_TIMEOUT_S)
        version = _text(v_fallback[0][0]) if v_fallback and v_fallback[0] else None
    vcomment = _text(variables.get("version_comment")) or comment
    ident = product_identity(version or "", vcomment or "")

    cluster_kind = "none"
    cluster_name = None
    cluster_members = []
    wsrep_on = (_text(variables.get("wsrep_on")) or "").upper() == "ON"
    gr_group = _text(variables.get("group_replication_group_name"))
    if wsrep_on:
        cluster_kind = "pxc" if "percona xtradb cluster" in (vcomment or "").lower() else "galera"
        cluster_name = _text(variables.get("wsrep_cluster_name"))
        try:
            mrows = reader.rows(_WSREP_MEMBERS_SQL, timeout=QUERY_TIMEOUT_S)
            for r in mrows:
                if len(r) >= 3:
                    h, p = split_host_port(r[2], 3306)
                    cluster_members.append({
                        "uuid": _text(r[0]), "name": _text(r[1]), "host": h, "port": p,
                        "address": _text(r[2]),
                    })
        except ConnectorError:
            pass
    elif gr_group:
        cluster_kind = "group_replication"
        cluster_name = gr_group
        try:
            if reader.rows(_INNODB_CLUSTER_SQL, timeout=QUERY_TIMEOUT_S):
                cluster_kind = "innodb_cluster"
        except ConnectorError:
            pass
        try:
            mrows = reader.rows(_GR_MEMBERS_SQL, timeout=QUERY_TIMEOUT_S)
            for r in mrows:
                if len(r) >= 5:
                    cluster_members.append({
                        "uuid": _text(r[0]), "host": _text(r[1]), "port": _int(r[2]),
                        "state": _text(r[3]), "role": _text(r[4]),
                    })
        except ConnectorError:
            pass

    channels = []
    replica_sqls = (
        ("SHOW ALL SLAVES STATUS",) if ident["engine"] == "mariadb"
        else ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS")
    )
    for sql in replica_sqls:
        try:
            rrows = _named_rows(conn, sql)
        except ConnectorError:
            continue
        for row in rrows:
            ch_name = row.get("Channel_Name") or row.get("Connection_name") or ""
            if ch_name in ("group_replication_applier", "group_replication_recovery"):
                continue
            src_host = _text(row.get("Source_Host") or row.get("Master_Host"))
            if not src_host:
                continue
            channels.append({
                "name": ch_name,
                "source_host": src_host,
                "source_port": _int(row.get("Source_Port") or row.get("Master_Port")) or 3306,
                "source_uuid": _text(row.get("Source_UUID") or row.get("Master_Server_Id")),
                "io_running": _text(row.get("Replica_IO_Running") or row.get("Slave_IO_Running")),
                "sql_running": _text(row.get("Replica_SQL_Running") or row.get("Slave_SQL_Running")),
                "lag_s": _int(row.get("Seconds_Behind_Source") or row.get("Seconds_Behind_Master")),
            })
        break

    replicas = []
    for sql in ("SHOW REPLICAS", "SHOW SLAVE HOSTS"):
        try:
            hrows = _named_rows(conn, sql)
        except ConnectorError:
            continue
        for row in hrows:
            rhost = _text(row.get("Host"))
            rport = _int(row.get("Port")) or 3306
            ruuid = _text(row.get("Replica_UUID") or row.get("Slave_UUID"))
            rsid = _int(row.get("Server_id"))
            if rhost or ruuid:
                replicas.append({"host": rhost, "port": rport, "uuid": ruuid, "server_id": rsid})
        break

    read_only = (_text(variables.get("super_read_only")) or "").upper() == "ON" or (
        (_text(variables.get("read_only")) or "").upper() == "ON"
    )
    base.update({
        "kind": "database",
        "engine": ident["engine"],
        "flavor": ident["flavor"],
        "product": ident["product"],
        "version": version,
        "hostname": _text(variables.get("hostname")),
        "port": _int(variables.get("port")) or 3306,
        "report_host": _text(variables.get("report_host")),
        "report_port": _int(variables.get("report_port")),
        "server_id": _int(variables.get("server_id")),
        "server_uuid": _text(variables.get("server_uuid")),
        "read_only": read_only,
        "cluster_kind": cluster_kind,
        "cluster_name": cluster_name,
        "cluster_members": cluster_members,
        "channels": channels,
        "replicas": replicas,
    })
    return base


def _fill_probe(entry, key, connect, profile, remaining):
    name = _profile_name(profile)
    keep, password, sent = True, None, False
    try:
        try:
            conn = connect()
        except ConnectorError:
            raise
        except ValueError as exc:
            keep, entry["problem"] = False, str(exc)
            return
        except (sqlite3.Error, OSError) as exc:
            keep, entry["problem"] = False, "Saved password for %s could not be read: %s" % (name, type(exc).__name__)
            return
        password = getattr(conn, "password", None)
        with conn:
            if remaining() < QUERY_TIMEOUT_S:
                keep, entry["problem"] = False, "Topology budget expired before reading %s." % name
                return
            sent = True
            entry["data"] = _probe_connection(conn, profile)
    except (ConnectorError, ValueError) as exc:
        entry["problem"] = "Saved connection %s could not be read: %s" % (name, dbops.redact(reason(exc), password))
    except BaseException:
        keep, entry["problem"] = sent, "Saved connection %s was not read." % name
        raise
    finally:
        with _cache_lock:
            if not keep:
                entry["kept"] = False
                _probe_attempts.discard(key)
                if _probe_cache.get(key) is entry:
                    del _probe_cache[key]
            entry.pop("connect", None)
            entry["done"].set()
            if _probe_pending.get(key[0]) is entry:
                del _probe_pending[key[0]]
            _evict_locked()


def probe_saved_profiles(profiles=(), open_profile=None, skip_profile_id=None):
    """Probe usable saved connections within ``PROFILE_BUDGET_S`` and ``PROFILE_READ_LIMIT``.

    Returns ``(probed_list, notes_list)``; never raises when a saved connection
    fails or times out.
    """
    if open_profile is None:
        return [], []
    usable = _usable_profiles(profiles, skip_profile_id=skip_profile_id)
    if not usable:
        return [], []
    reads = [0]
    deadline = [time.monotonic() + PROFILE_BUDGET_S]
    probed = []
    notes = []

    def remaining():
        return deadline[0] - time.monotonic()

    for profile in usable:
        name = _profile_name(profile)
        while True:
            try:
                entry, owner, pending = _claim_probe(profile, open_profile, reads, remaining)
            except ValueError as exc:
                notes.append(str(exc))
                entry = None
                break
            except (sqlite3.Error, OSError) as exc:
                notes.append("Saved password for %s could not be read: %s" % (name, type(exc).__name__))
                entry = None
                break
            if owner:
                reads[0] += 1
                _fill_probe(entry, entry["key"], entry["connect"], profile, remaining)
                break
            if not pending:
                break
            if not entry["done"].wait(max(0.0, remaining())):
                notes.append("Another read of saved connection %s did not finish in time." % name)
                entry = None
                break
            if not entry["kept"]:
                if entry.get("problem"):
                    notes.append(entry["problem"])
                entry = None
                break
        if entry and entry.get("data"):
            probed.append(entry["data"])
        elif entry and entry.get("problem"):
            notes.append(entry["problem"])
    return probed, notes


def _endpoint_matches_profile(host, port, profile_data, observer_host=None):
    """True when ``host:port`` directly names *profile_data*'s connection endpoint
    under the same loopback / SSH safety rules as replication.py."""
    thost = _host_key(host)
    tport = _int(port)
    if not thost or not tport:
        return False
    phost = _host_key(profile_data.get("profile_host") or "127.0.0.1")
    pport = _int(profile_data.get("profile_port"))
    if pport != tport:
        return False
    if is_loopback(thost):
        if not observer_host or not is_loopback(observer_host):
            return False
        if profile_data.get("ssh_host"):
            return False
        if thost == phost:
            return True
        return thost in _LOCAL_NAMES and phost in _LOCAL_NAMES
    return thost == phost


def match_database_endpoint(host, port, probed, uuid=None, observer_host=None):
    """Match a database endpoint to one probed database profile with explicit evidence.

    Never guesses: returns ``(profile_data, evidence)`` or ``(None, None)``.
    """
    dbs = [p for p in probed if p.get("kind") == "database"]
    if uuid:
        u = uuid.strip().lower()
        by_uuid = [p for p in dbs if (p.get("server_uuid") or "").lower() == u]
        if len(by_uuid) == 1:
            return by_uuid[0], "server_uuid"
    direct = [p for p in dbs if _endpoint_matches_profile(host, port, p, observer_host=observer_host)]
    if len(direct) == 1:
        return direct[0], "endpoint"
    thost = _host_key(host)
    tport = _int(port) or 3306
    if thost and not is_loopback(thost):
        internal = []
        for p in dbs:
            names = {_host_key(p.get("hostname")), _host_key(p.get("report_host"))} - {""}
            ports = {p.get("port"), p.get("report_port")} - {None}
            if thost in names and tport in ports:
                internal.append(p)
        if len(internal) == 1:
            return internal[0], "hostname_port"
    return None, None


def _database_matches_target(db_info, target_host, target_port, observer_host=None):
    """True when ``target_host:target_port`` (seen by Readyset or ProxySQL) points at *db_info*."""
    if _endpoint_matches_profile(target_host, target_port, db_info, observer_host=observer_host):
        return True
    thost = _host_key(target_host)
    tport = _int(target_port) or 3306
    if thost and not is_loopback(thost):
        names = {
            _host_key(db_info.get("hostname")),
            _host_key(db_info.get("report_host")),
            _host_key(db_info.get("host")),
        } - {""}
        ports = {db_info.get("port"), db_info.get("report_port")} - {None}
        if thost in names and tport in ports:
            return True
    # Loopback target when both Readyset/ProxySQL and the DB run on this local host.
    if is_loopback(thost) and observer_host and is_loopback(observer_host) and not db_info.get("ssh_host"):
        local_hostname = _host_key(socket.gethostname()).split(".")[0]
        db_short = _host_key(db_info.get("hostname")).split(".")[0]
        if local_hostname and db_short == local_hostname and tport in ({db_info.get("port"), db_info.get("profile_port")} - {None}):
            return True
    return False


def parse_readyset_rule_meta(comment):
    """Extract QueryPilot ``readyset-meta: {...}`` JSON from a ProxySQL rule comment, if valid."""
    if not isinstance(comment, str) or "readyset-meta:" not in comment.lower():
        return None
    m = _READYSET_META_RE.search(comment)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def classify_proxysql_topology(report, profiles=(), open_profile=None, observer_host=None, current_profile_id=None):
    """Build ``report["topology"]`` for a ProxySQL report, recognizing Readyset
    cache tiers, QueryPilot rules, and downstream MySQL/MariaDB clusters or replicas."""
    probed, probe_notes = probe_saved_profiles(profiles, open_profile, skip_profile_id=current_profile_id)
    rs_probed = [p for p in probed if p.get("kind") == "readyset"]
    db_probed = [p for p in probed if p.get("kind") == "database"]

    # Identify hostgroups targeted by QueryPilot / Readyset rules and general mirror rules.
    rs_dest_hgs = set()
    rs_mirror_hgs = set()
    all_mirror_hgs = set()
    rs_rule_ids = []
    for rule in report.get("query_rules") or ():
        if not rule.get("active", True) and rule.get("runtime_active") is not True:
            continue
        comment = rule.get("comment") or ""
        meta = parse_readyset_rule_meta(comment)
        mirror_hg = rule.get("mirror_hostgroup")
        if mirror_hg is None:
            mirror_hg = (rule.get("fields") or {}).get("mirror_hostgroup")
        if mirror_hg is not None:
            mhg = _int(mirror_hg)
            if mhg is not None:
                all_mirror_hgs.add(mhg)
        if meta is not None or "readyset" in comment.lower():
            rs_rule_ids.append(rule.get("rule_id"))
            dest_hg = rule.get("destination_hostgroup")
            if dest_hg is None:
                dest_hg = (rule.get("fields") or {}).get("destination_hostgroup")
            if mirror_hg is not None:
                mhg = _int(mirror_hg)
                if mhg is not None:
                    rs_mirror_hgs.add(mhg)
            elif dest_hg is not None:
                dhg = _int(dest_hg)
                if dhg is not None:
                    rs_dest_hgs.add(dhg)

    # First pass: resolve MySQL/MariaDB backends so Readyset upstream matching can cross-check them.
    raw_servers = report.get("servers") or ()
    matched_dbs = {}
    for srv in raw_servers:
        key = srv.get("key") or ("%s|%s|%s" % (srv.get("hostgroup_id"), srv.get("hostname"), srv.get("port")))
        db_match, ev = match_database_endpoint(srv.get("hostname"), srv.get("port"), probed, observer_host=observer_host)
        if db_match is not None:
            matched_dbs[key] = (db_match, ev)

    known_db_Set = [pair[0] for pair in matched_dbs.values()] or db_probed

    def _match_readyset_server(host, port, is_readyset_hint=False):
        direct = [p for p in rs_probed if _endpoint_matches_profile(host, port, p, observer_host=observer_host)]
        if len(direct) == 1:
            return direct[0], "endpoint"
        if len(direct) > 1:
            adapters = [p for p in direct if p.get("port_role") != "admin"]
            if len(adapters) == 1:
                return adapters[0], "endpoint"
        thost = _host_key(host)
        if is_readyset_hint and thost and not is_loopback(thost) and rs_probed and known_db_Set:
            # Verify by Readyset's own upstream_servers / CDC Server matching the same DB tier.
            verified = []
            for rsp in rs_probed:
                up_endpoints = [(u.get("host"), _int(u.get("port")) or 3306) for u in (rsp.get("upstreams") or ())]
                cdc_h, cdc_p = split_host_port(rsp.get("cdc_server"), 3306)
                if cdc_h:
                    up_endpoints.append((cdc_h, cdc_p))
                if any(_database_matches_target(dbi, uh, up, observer_host=rsp.get("profile_host"))
                       for uh, up in up_endpoints if uh for dbi in known_db_Set):
                    verified.append(rsp)
            if len(verified) == 1:
                return verified[0], "upstream_match"
            if len(verified) > 1:
                adapters = [p for p in verified if p.get("port_role") != "admin"]
                if len(adapters) == 1:
                    return adapters[0], "upstream_match"
        return None, None

    readyset_nodes = []
    backend_nodes = []
    unmatched_notes = []

    for srv in raw_servers:
        hg = _int(srv.get("hostgroup_id"))
        host = srv.get("hostname")
        port = _int(srv.get("port")) or 3306
        key = srv.get("key") or ("%s|%s|%s" % (hg, host, port))
        settings = srv.get("settings") or {}
        cfg = srv.get("config") or {}
        pool = srv.get("pool") or {}
        comment = srv.get("comment") or settings.get("comment") or cfg.get("comment") or ""
        status = srv.get("runtime_status") or cfg.get("status") or srv.get("configured_status") or "UNKNOWN"
        raw_weight = srv.get("weight")
        if raw_weight is None:
            raw_weight = settings.get("weight") if settings.get("weight") is not None else cfg.get("weight")
        weight = _int(raw_weight)
        queries = _int(pool.get("queries") if "queries" in pool else pool.get("Queries"))
        conn_used = _int(pool.get("conn_used") if "conn_used" in pool else pool.get("ConnUsed"))
        conn_free = _int(pool.get("conn_free") if "conn_free" in pool else pool.get("ConnFree"))
        latency_us = _int(pool.get("latency_us") if "latency_us" in pool else pool.get("Latency_us"))
        roles = list(srv.get("hostgroup_roles") or ([srv["role"]] if srv.get("role") else ()))

        rs_hint = (
            "readyset" in comment.lower()
            or "readyset" in _host_key(host)
            or port == 3307
            or hg in rs_dest_hgs
            or hg in rs_mirror_hgs
        )
        rs_match, rs_ev = _match_readyset_server(host, port, is_readyset_hint=rs_hint)
        explicit_rs = (
            rs_match is not None
            or "readyset" in comment.lower()
            or hg in rs_dest_hgs
            or hg in rs_mirror_hgs
        )
        if explicit_rs and key not in matched_dbs:
            is_mirrored = hg in rs_mirror_hgs or hg in all_mirror_hgs
            if hg in rs_dest_hgs and is_mirrored:
                mode = "cached_and_mirrored"
            elif is_mirrored:
                mode = "shadow_mirror"
            elif hg in rs_dest_hgs:
                mode = "cached_reads"
            else:
                mode = "hostgroup_member"
            node = {
                "key": "readyset:%s" % key,
                "server_key": key,
                "kind": "readyset",
                "host": host,
                "port": port,
                "address": format_endpoint(host, port),
                "hostgroup_id": hg,
                "status": status,
                "weight": weight,
                "routing_mode": mode,
                "queries": queries,
                "conn_used": conn_used,
                "conn_free": conn_free,
                "latency_us": latency_us,
                "version": rs_match.get("version") if rs_match else None,
                "readyset_status": rs_match.get("status_text") if rs_match else None,
                "replication_status": rs_match.get("replication_status") if rs_match else None,
                "cache_count": rs_match.get("cache_count") if rs_match else None,
                "profile_id": rs_match.get("profile_id") if rs_match else None,
                "profile_name": rs_match.get("profile_name") if rs_match else None,
                "verified": rs_match is not None,
                "evidence": rs_ev,
                "upstreams": [],
            }
            if rs_match:
                for u in rs_match.get("upstreams") or ():
                    uh, up = u.get("host"), _int(u.get("port")) or 3306
                    dbm, _ = match_database_endpoint(uh, up, probed, observer_host=rs_match.get("profile_host"))
                    node["upstreams"].append({
                        "host": uh,
                        "port": up,
                        "address": format_endpoint(uh, up),
                        "purpose": u.get("purpose") or "",
                        "active_cdc": str(u.get("active_cdc") or "").lower() in ("true", "1", "t"),
                        "health": u.get("health"),
                        "profile_id": dbm.get("profile_id") if dbm else None,
                        "profile_name": dbm.get("profile_name") if dbm else None,
                    })
            else:
                unmatched_notes.append(
                    "Readyset backend %s in hostgroup %s did not match a saved connection."
                    % (format_endpoint(host, port), hg)
                )
            readyset_nodes.append(node)
            continue

        db_match, db_ev = matched_dbs.get(key, (None, None))
        backend_nodes.append({
            "key": "db:%s" % key,
            "server_key": key,
            "kind": "database",
            "host": host,
            "port": port,
            "address": format_endpoint(host, port),
            "hostgroup_id": hg,
            "hostgroup_roles": roles,
            "status": status,
            "weight": weight,
            "comment": comment or None,
            "queries": queries,
            "conn_used": conn_used,
            "conn_free": conn_free,
            "latency_us": latency_us,
            "product": db_match.get("product") if db_match else None,
            "version": db_match.get("version") if db_match else None,
            "read_only": db_match.get("read_only") if db_match else None,
            "cluster_kind": db_match.get("cluster_kind") if db_match else None,
            "cluster_name": db_match.get("cluster_name") if db_match else None,
            "profile_id": db_match.get("profile_id") if db_match else None,
            "profile_name": db_match.get("profile_name") if db_match else None,
            "verified": db_match is not None,
            "evidence": db_ev,
        })

    return {
        "has_readyset": bool(readyset_nodes),
        "querypilot_rule_count": len(rs_rule_ids),
        "querypilot_rule_ids": [r for r in rs_rule_ids if r is not None][:50],
        "readyset_nodes": readyset_nodes,
        "database_nodes": backend_nodes,
        "notes": unmatched_notes[:6] + probe_notes[:4],
    }


def read_live_client_footprint(conn):
    """Check whether Readyset or ProxySQL is actively connected to this MySQL/MariaDB server."""
    programs = []
    proxysql_seen = False
    reader = Reader(conn, TOPOLOGY_TAG)
    try:
        rows = reader.rows(_LIVE_ATTRS_SQL, timeout=QUERY_TIMEOUT_S)
        for row in rows:
            if len(row) < 3:
                continue
            val = _text(row[2]) or ""
            if val.upper().startswith("READYSET"):
                if val not in programs:
                    programs.append(val)
            elif "proxysql" in val.lower():
                proxysql_seen = True
    except ConnectorError:
        pass
    heartbeat = False
    try:
        if reader.rows(_HEARTBEAT_TABLE_SQL, timeout=QUERY_TIMEOUT_S):
            heartbeat = True
    except ConnectorError:
        pass
    return {
        "readyset_connected": bool(programs),
        "readyset_programs": programs,
        "readyset_heartbeat_table": heartbeat,
        "proxysql_connected": proxysql_seen,
    }


def _cached_probe_kind(profile_id):
    if not profile_id:
        return None
    with _cache_lock:
        for (pid, _), entry in _probe_cache.items():
            if pid == profile_id and entry["done"].is_set() and entry.get("data"):
                return entry["data"].get("kind")
    return None


def _filter_tier_candidates(profiles, local_identity, peer_endpoints=(), skip_profile_id=None):
    """Return only saved profiles that can plausibly represent an upstream Readyset or ProxySQL tier.

    Excludes this server's own endpoint, known replication channel sources / cluster
    members, profiles already known to be database servers, and plain database-named
    or port-3306 profiles unless explicitly named for Readyset or ProxySQL, so viewing
    Replication or Cluster never opens unrelated MySQL/MariaDB connections.
    """
    known_eps = []
    for item in [local_identity] + list(peer_endpoints or ()):
        if not isinstance(item, dict):
            continue
        eh = _host_key(item.get("profile_host") or item.get("host"))
        ep = _int(item.get("profile_port") or item.get("port")) or 3306
        if eh:
            known_eps.append((eh, ep))
    candidates = []
    for p in profiles or ():
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if skip_profile_id and pid and pid == skip_profile_id:
            continue
        ph = _host_key(p.get("host") or "127.0.0.1")
        pp = _profile_port(p, 3306)
        ssh = _text(p.get("ssh_host"))
        is_known_db = False
        for eh, ep in known_eps:
            if pp != ep:
                continue
            if ph == eh or (not ssh and is_loopback(ph) and is_loopback(eh)):
                is_known_db = True
                break
        if is_known_db:
            continue
        cached_kind = _cached_probe_kind(pid)
        if cached_kind in ("readyset", "proxysql"):
            candidates.append(p)
            continue
        if cached_kind == "database":
            continue
        label = ("%s %s" % (p.get("name") or "", p.get("host") or "")).lower()
        is_tier_port = pp in (3307, 6036, 6032, 6033) or (pp % 10000) in (3307, 6036, 6032, 6033)
        is_db_named = any(
            tok in label
            for tok in ("mysql", "mariadb", "percona", "primary", "replica", "source", "galera", "pxc", "cluster", "aurora", "rds")
        )
        is_tier_named = (
            any(tok in label for tok in ("readyset", "proxysql", "cache", "proxy", "querypilot", "adapter"))
            or (not is_db_named and bool(re.search(r"(?:^|[^a-z0-9])rs(?:$|[^a-z0-9])", label)))
        )
        if is_tier_port or is_tier_named or (pp != 3306 and not is_db_named):
            candidates.append(p)
    return candidates


def discover_database_tiers(conn, local_identity, peer_endpoints=(), profiles=(), open_profile=None,
                            observer_host=None, current_profile_id=None, check_footprint=False):
    """Discover upstream Readyset and ProxySQL tiers sitting in front of a database server,
    cluster, or primary/replica pair."""
    footprint = read_live_client_footprint(conn) if check_footprint else {
        "readyset_connected": False,
        "readyset_programs": [],
        "readyset_heartbeat_table": False,
        "proxysql_connected": False,
    }
    candidates = _filter_tier_candidates(
        profiles, local_identity, peer_endpoints=peer_endpoints, skip_profile_id=current_profile_id
    )
    probed, probe_notes = probe_saved_profiles(candidates, open_profile, skip_profile_id=current_profile_id)
    if not probed and not footprint["readyset_connected"] and not footprint["readyset_heartbeat_table"] \
            and not footprint["proxysql_connected"]:
        return {
            "readyset": [],
            "proxysql": [],
            "live_footprint": footprint,
            "notes": probe_notes[:4],
        }

    # Build the set of known database identities for this topology (this node + cluster/replication peers).
    local_copy = dict(local_identity)
    if any(p.get("kind") in ("readyset", "proxysql") for p in probed) and not local_copy.get("hostname"):
        try:
            vmap = Reader(conn, TOPOLOGY_TAG).pairs(_DB_VARS_SQL)
            local_copy.update({
                "hostname": _text(vmap.get("hostname")),
                "port": _int(vmap.get("port")) or local_copy.get("port") or 3306,
                "report_host": _text(vmap.get("report_host")),
                "report_port": _int(vmap.get("report_port")),
                "server_uuid": _text(vmap.get("server_uuid")) or local_copy.get("server_uuid"),
            })
        except ConnectorError:
            pass
    known_dbs = [local_copy]
    for peer in peer_endpoints or ():
        if isinstance(peer, dict):
            known_dbs.append(peer)

    # Also include any probed database profile that matches one of the peer endpoints or UUIDs.
    known_uuids = {(d.get("server_uuid") or "").lower() for d in known_dbs if d.get("server_uuid")}
    for p in probed:
        if p.get("kind") != "database":
            continue
        puuid = (p.get("server_uuid") or "").lower()
        if puuid and puuid in known_uuids:
            known_dbs.append(p)
            continue
        for ch in p.get("channels") or ():
            if (ch.get("source_uuid") or "").lower() in known_uuids:
                known_dbs.append(p)
                break

    def _targets_this_db_topology(uh, up, tier_host):
        for dbi in known_dbs:
            if _database_matches_target(dbi, uh, up, observer_host=tier_host):
                return True, dbi
        return False, None

    def _rs_deployment_sig(rsp):
        ups = tuple(sorted(
            (_host_key(u.get("host")), _int(u.get("port")) or 3306)
            for u in (rsp.get("upstreams") or ())
            if u.get("host")
        ))
        return (
            _host_key(rsp.get("profile_host")),
            _host_key(rsp.get("ssh_host")),
            ups,
            (_text(rsp.get("cdc_server")) or "").lower(),
        )

    readyset_tiers = []
    matched_rs_profiles = []
    matched_rs_entries = []
    for rsp in probed:
        if rsp.get("kind") != "readyset":
            continue
        tier_host = rsp.get("profile_host")
        purposes = []
        matched = False
        for u in rsp.get("upstreams") or ():
            uh, up = u.get("host"), _int(u.get("port")) or 3306
            ok, _ = _targets_this_db_topology(uh, up, tier_host)
            if ok:
                matched = True
                p_str = _text(u.get("purpose"))
                if p_str and p_str not in purposes:
                    purposes.append(p_str)
        if not matched and rsp.get("cdc_server"):
            ch, cp = split_host_port(rsp.get("cdc_server"), 3306)
            if ch:
                ok, _ = _targets_this_db_topology(ch, cp, tier_host)
                if ok:
                    matched = True
                    purposes.append("cdc")
        if matched:
            matched_rs_profiles.append(rsp)
            matched_rs_entries.append((rsp, purposes))

    adapter_sigs = {
        _rs_deployment_sig(rsp)
        for rsp, _ in matched_rs_entries
        if rsp.get("port_role") != "admin"
    }
    for rsp, purposes in matched_rs_entries:
        if rsp.get("port_role") == "admin" and _rs_deployment_sig(rsp) in adapter_sigs:
            continue
        readyset_tiers.append({
            "key": "readyset:%s" % (rsp.get("profile_id") or format_endpoint(rsp.get("profile_host"), rsp.get("profile_port"))),
            "label": rsp.get("profile_name") or "Readyset",
            "host": rsp.get("profile_host"),
            "port": rsp.get("profile_port"),
            "address": format_endpoint(rsp.get("profile_host"), rsp.get("profile_port")),
            "version": rsp.get("version"),
            "status": rsp.get("status_text") or "Online",
            "replication_status": rsp.get("replication_status"),
            "cache_count": rsp.get("cache_count"),
            "purpose": ", ".join(purposes) if purposes else "upstream",
            "profile_id": rsp.get("profile_id"),
            "profile_name": rsp.get("profile_name"),
            "verified": True,
            "evidence": "upstream_servers",
        })

    notes = list(probe_notes[:4])
    if not readyset_tiers and (footprint["readyset_connected"] or footprint["readyset_heartbeat_table"]):
        notes.append(
            "Readyset footprint detected on this server (%s); save a Readyset connection to link its cache tier."
            % (", ".join(footprint["readyset_programs"]) if footprint["readyset_programs"] else "_readyset._readyset_heartbeat")
        )

    proxysql_tiers = []
    for pxp in probed:
        if pxp.get("kind") != "proxysql":
            continue
        tier_host = pxp.get("profile_host")
        links_db = False
        direct_rs = False
        comment_rs = False
        for srv in pxp.get("servers") or ():
            sh, sp = srv.get("hostname"), _int(srv.get("port")) or 3306
            ok, _ = _targets_this_db_topology(sh, sp, tier_host)
            if ok:
                links_db = True
            if any(_endpoint_matches_profile(sh, sp, rsp, observer_host=tier_host) for rsp in matched_rs_profiles):
                direct_rs = True
            elif matched_rs_profiles and not is_loopback(sh) and "readyset" in (srv.get("comment") or "").lower():
                comment_rs = True
        if links_db or direct_rs:
            rs_rules = sum(
                1 for r in (pxp.get("query_rules") or ())
                if parse_readyset_rule_meta(r.get("comment") or "") is not None
                or "readyset" in (r.get("comment") or "").lower()
            )
            proxysql_tiers.append({
                "key": "proxysql:%s" % (pxp.get("profile_id") or format_endpoint(pxp.get("profile_host"), pxp.get("profile_port"))),
                "label": pxp.get("profile_name") or "ProxySQL",
                "host": pxp.get("profile_host"),
                "port": pxp.get("profile_port"),
                "address": format_endpoint(pxp.get("profile_host"), pxp.get("profile_port")),
                "version": pxp.get("version"),
                "routes_to_readyset": direct_rs or (links_db and comment_rs) or rs_rules > 0,
                "querypilot_rule_count": rs_rules,
                "profile_id": pxp.get("profile_id"),
                "profile_name": pxp.get("profile_name"),
                "verified": True,
                "evidence": "mysql_servers",
            })

    if not proxysql_tiers and footprint["proxysql_connected"]:
        notes.append("ProxySQL client sessions are connected to this server; save a ProxySQL Admin connection to link routing.")

    return {
        "readyset": readyset_tiers,
        "proxysql": proxysql_tiers,
        "live_footprint": footprint,
        "notes": notes,
    }


def build_readyset_topology(status_map, upstream_rows, connections_rows, port_role, version,
                            profiles=(), open_profile=None, observer_host=None, current_profile_id=None,
                            current_endpoint=None):
    """Build the multi-tier topology structure centered on this Readyset instance:
    ``[ProxySQL tier] -> Readyset (this server) -> [MySQL/MariaDB primary / replicas / cluster]``."""
    probed, probe_notes = probe_saved_profiles(profiles, open_profile, skip_profile_id=current_profile_id)
    db_probed = [p for p in probed if p.get("kind") == "database"]
    px_probed = [p for p in probed if p.get("kind") == "proxysql"]

    upstreams = []
    seen_db_keys = set()
    matched_db_profiles = []
    notes = []

    raw_upstreams = list(upstream_rows or ())
    if not raw_upstreams and status_map.get("CDC Server"):
        ch, cp = split_host_port(status_map.get("CDC Server"), 3306)
        if ch:
            raw_upstreams.append({
                "host": ch, "port": cp, "database": None, "user": None,
                "weight": 1, "purpose": "cdc", "active_cdc": "true",
                "health": "up" if status_map.get("Database Connection") == "Connected" else "unknown",
                "connections": None,
            })

    for row in raw_upstreams:
        uh = _text(row.get("host"))
        up = _int(row.get("port")) or 3306
        if not uh:
            continue
        purpose = _text(row.get("purpose")) or "proxy"
        active_cdc = str(row.get("active_cdc") or "").lower() in ("true", "1", "t")
        health = _text(row.get("health"))
        db_match, ev = match_database_endpoint(uh, up, probed, observer_host=observer_host)
        if db_match is not None and db_match not in matched_db_profiles:
            matched_db_profiles.append(db_match)
        elif db_match is None:
            notes.append("Upstream %s did not match a saved database connection." % format_endpoint(uh, up))
        node_key = "upstream:%s:%s" % (uh, up)
        seen_db_keys.add((_host_key(uh), up))
        if db_match and db_match.get("server_uuid"):
            seen_db_keys.add(("uuid", db_match["server_uuid"].lower()))
        upstreams.append({
            "key": node_key,
            "host": uh,
            "port": up,
            "address": format_endpoint(uh, up),
            "database": _text(row.get("database")),
            "user": _text(row.get("user")),
            "weight": _int(row.get("weight")),
            "purpose": purpose,
            "is_cdc": "cdc" in purpose.lower() or active_cdc,
            "is_proxy": "proxy" in purpose.lower(),
            "active_cdc": active_cdc,
            "health": health,
            "connections": _int(row.get("connections")),
            "product": db_match.get("product") if db_match else None,
            "version": db_match.get("version") if db_match else None,
            "engine": db_match.get("engine") if db_match else None,
            "read_only": db_match.get("read_only") if db_match else None,
            "cluster_kind": db_match.get("cluster_kind") if db_match else None,
            "cluster_name": db_match.get("cluster_name") if db_match else None,
            "profile_id": db_match.get("profile_id") if db_match else None,
            "profile_name": db_match.get("profile_name") if db_match else None,
            "verified": db_match is not None,
            "evidence": ev,
        })

    # Expand cluster peers and async replicas discovered through matched DB profiles.
    peers = []
    cluster_summary = None
    for dbm in matched_db_profiles:
        if dbm.get("cluster_kind") and dbm.get("cluster_kind") != "none":
            if cluster_summary is None:
                cluster_summary = {
                    "kind": dbm["cluster_kind"],
                    "name": dbm.get("cluster_name"),
                    "member_count": len(dbm.get("cluster_members") or ()) or 1,
                }
            for cm in dbm.get("cluster_members") or ():
                cuuid = (cm.get("uuid") or "").lower()
                chost, cport = cm.get("host"), _int(cm.get("port")) or 3306
                if (cuuid and ("uuid", cuuid) in seen_db_keys) or (_host_key(chost), cport) in seen_db_keys:
                    continue
                peer_prof, pev = match_database_endpoint(chost, cport, probed, uuid=cuuid or None,
                                                         observer_host=dbm.get("profile_host"))
                if cuuid:
                    seen_db_keys.add(("uuid", cuuid))
                if chost:
                    seen_db_keys.add((_host_key(chost), cport))
                peers.append({
                    "key": "cluster-peer:%s" % (cuuid or format_endpoint(chost, cport)),
                    "relation": "cluster_member",
                    "cluster_kind": dbm["cluster_kind"],
                    "name": cm.get("name") or format_endpoint(chost, cport),
                    "host": chost,
                    "port": cport,
                    "address": format_endpoint(chost, cport),
                    "state": cm.get("state"),
                    "role": cm.get("role"),
                    "product": peer_prof.get("product") if peer_prof else dbm.get("product"),
                    "version": peer_prof.get("version") if peer_prof else None,
                    "profile_id": peer_prof.get("profile_id") if peer_prof else None,
                    "profile_name": peer_prof.get("profile_name") if peer_prof else None,
                    "verified": peer_prof is not None,
                    "evidence": pev,
                })
        # Async replicas reported by SHOW REPLICAS on primary.
        for rep in dbm.get("replicas") or ():
            ruuid = (rep.get("uuid") or "").lower()
            rhost, rport = rep.get("host"), _int(rep.get("port")) or 3306
            if (ruuid and ("uuid", ruuid) in seen_db_keys) or (rhost and (_host_key(rhost), rport) in seen_db_keys):
                continue
            rep_prof, rev = match_database_endpoint(rhost, rport, probed, uuid=ruuid or None,
                                                    observer_host=dbm.get("profile_host"))
            # Readyset registers on MySQL as a binlog follower (often reporting port 3307 or a readyset hostname);
            # never list Readyset itself as an unverified downstream MySQL replica peer of itself.
            if rep_prof is None and (rport == 3307 or "readyset" in _host_key(rhost)):
                continue
            if ruuid:
                seen_db_keys.add(("uuid", ruuid))
            if rhost:
                seen_db_keys.add((_host_key(rhost), rport))
            peers.append({
                "key": "replica:%s" % (ruuid or format_endpoint(rhost, rport)),
                "relation": "replica",
                "source_profile_id": dbm.get("profile_id"),
                "name": (rep_prof.get("profile_name") if rep_prof else None) or format_endpoint(rhost or "replica", rport),
                "host": rhost,
                "port": rport,
                "address": format_endpoint(rhost, rport) if rhost else None,
                "product": rep_prof.get("product") if rep_prof else None,
                "version": rep_prof.get("version") if rep_prof else None,
                "profile_id": rep_prof.get("profile_id") if rep_prof else None,
                "profile_name": rep_prof.get("profile_name") if rep_prof else None,
                "verified": rep_prof is not None,
                "evidence": rev,
            })

    # Also check any other probed DB profile whose replication channel replicates from one of our matched DBs!
    matched_uuids = {(d.get("server_uuid") or "").lower() for d in matched_db_profiles if d.get("server_uuid")}
    for p in db_probed:
        if p in matched_db_profiles:
            continue
        puuid = (p.get("server_uuid") or "").lower()
        if puuid and ("uuid", puuid) in seen_db_keys:
            continue
        for ch in p.get("channels") or ():
            suuid = (ch.get("source_uuid") or "").lower()
            by_uuid = bool(suuid and suuid in matched_uuids)
            by_host = any(
                _database_matches_target(d, ch.get("source_host"), ch.get("source_port"), observer_host=p.get("profile_host"))
                for d in matched_db_profiles
            )
            if by_uuid or by_host:
                if puuid:
                    seen_db_keys.add(("uuid", puuid))
                peers.append({
                    "key": "replica:%s" % (p.get("profile_id") or format_endpoint(p.get("profile_host"), p.get("profile_port"))),
                    "relation": "replica",
                    "name": p.get("profile_name"),
                    "host": p.get("profile_host"),
                    "port": p.get("profile_port"),
                    "address": format_endpoint(p.get("profile_host"), p.get("profile_port")),
                    "lag_s": ch.get("lag_s"),
                    "io_running": ch.get("io_running"),
                    "sql_running": ch.get("sql_running"),
                    "product": p.get("product"),
                    "version": p.get("version"),
                    "profile_id": p.get("profile_id"),
                    "profile_name": p.get("profile_name"),
                    "verified": True,
                    "evidence": "server_uuid" if by_uuid else "channel_source",
                })
                break

    # Discover ProxySQL tier in front of this Readyset instance.
    proxysql_nodes = []
    cur_host, cur_port = current_endpoint if isinstance(current_endpoint, tuple) else (None, None)
    for pxp in px_probed:
        px_host = pxp.get("profile_host")
        rs_hgs = set()
        mirror_hgs = set()
        all_mirror_hgs = set()
        dest_hgs = set()
        qp_rules = 0
        for rule in pxp.get("query_rules") or ():
            c = rule.get("comment") or ""
            mhg = _int(rule.get("mirror_hostgroup"))
            dhg = _int(rule.get("destination_hostgroup"))
            if mhg is not None:
                all_mirror_hgs.add(mhg)
            if parse_readyset_rule_meta(c) is not None or "readyset" in c.lower():
                qp_rules += 1
                if mhg is not None:
                    mirror_hgs.add(mhg)
                elif dhg is not None:
                    dest_hgs.add(dhg)
        matched_srv = None
        for srv in pxp.get("servers") or ():
            sh, sp = srv.get("hostname"), _int(srv.get("port")) or 3307
            hg = _int(srv.get("hostgroup_id"))
            if any(_database_matches_target(dbm, sh, sp, observer_host=px_host) for dbm in matched_db_profiles):
                continue
            if cur_host and cur_port and _endpoint_matches_profile(
                sh, sp, {"profile_host": cur_host, "profile_port": cur_port, "ssh_host": None},
                observer_host=px_host,
            ):
                matched_srv = srv
                rs_hgs.add(hg)
                break
            # Or ProxySQL points at the same DB tier AND has a Readyset backend in a Readyset HG / comment.
            if ("readyset" in (srv.get("comment") or "").lower() or hg in dest_hgs or hg in mirror_hgs) and matched_db_profiles:
                same_db = any(
                    _database_matches_target(dbm, b.get("hostname"), _int(b.get("port")) or 3306, observer_host=px_host)
                    for b in (pxp.get("servers") or ())
                    for dbm in matched_db_profiles
                )
                if same_db:
                    matched_srv = srv
                    rs_hgs.add(hg)
                    break
        if matched_srv is not None:
            is_mirrored = bool(rs_hgs & (mirror_hgs | all_mirror_hgs))
            if rs_hgs & dest_hgs and is_mirrored:
                rmode = "cached_and_mirrored"
            elif is_mirrored:
                rmode = "shadow_mirror"
            elif rs_hgs & dest_hgs:
                rmode = "cached_reads"
            else:
                rmode = "backend"
            proxysql_nodes.append({
                "key": "proxysql:%s" % (pxp.get("profile_id") or format_endpoint(px_host, pxp.get("profile_port"))),
                "label": pxp.get("profile_name") or "ProxySQL",
                "host": px_host,
                "port": pxp.get("profile_port"),
                "address": format_endpoint(px_host, pxp.get("profile_port")),
                "version": pxp.get("version"),
                "routing_mode": rmode,
                "backend_status": matched_srv.get("status"),
                "hostgroup_id": _int(matched_srv.get("hostgroup_id")),
                "querypilot_rule_count": qp_rules,
                "profile_id": pxp.get("profile_id"),
                "profile_name": pxp.get("profile_name"),
                "verified": True,
                "evidence": "mysql_servers",
            })

    return {
        "this_server": {
            "kind": "readyset",
            "port_role": port_role,
            "version": version,
            "address": format_endpoint(cur_host, cur_port) if cur_host else None,
            "status": _text(status_map.get("Status")) or "Online",
            "replication_status": _text(status_map.get("Replication Status")),
            "connection_count": len(connections_rows or ()),
        },
        "proxysql_nodes": proxysql_nodes,
        "upstreams": upstreams,
        "database_peers": peers,
        "cluster": cluster_summary,
        "notes": notes[:6] + probe_notes[:4],
    }
