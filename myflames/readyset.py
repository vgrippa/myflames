# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""Readyset cache engine inspection, topology, cache DDL, dynamic parameters and recovery.

Readyset speaks the MySQL wire protocol on two listeners:

* the **Adapter SQL port** (default ``3307``), which serves cached queries,
  proxies cache misses and writes to upstream MySQL or MariaDB, and accepts
  ``EXPLAIN CREATE CACHE``, ``CREATE CACHE``, ``DROP CACHE``, ``FLUSH CACHE``
  and ``ALTER READYSET`` (except ``SHUTDOWN``) when ``--cache-ddl-address``
  allows DDL;
* the **Admin SQL port** (default ``6036``), which routes every query to the
  ``readyset`` virtual schema (``readyset_schema_route_all = true``), accepts
  ``DROP CACHE``, ``FLUSH CACHE``, ``ALTER READYSET`` and ``ALTER READYSET
  SHUTDOWN [RESET]``, and refuses ``EXPLAIN CREATE CACHE`` and ``CREATE CACHE``
  because no upstream database schema is active there.

Every SQL statement and result schema in this module is grounded in Readyset's
source (``readyset-sql-parsing/src/lib.rs``, ``readyset-adapter/src/backend.rs``,
``readyset-adapter/src/backend/extensions.rs``, ``readyset-schema/src/*_vrel.rs``).
Every mutation goes through :mod:`myflames.reviewed_writes` and
:mod:`myflames.dbops` with fresh re-planning, collision checks and post-error
state verification.
"""
import hashlib
import json
import re

from . import dbops
from .connector import ConnectorError, terminal_text
from .perf_common import named_rows, now
from .server_info import is_readyset_comment
from .topology_links import build_readyset_topology, format_endpoint, parse_readyset_version, split_host_port

SCHEMA_VERSION = "readyset-1.0"
ACTION_SCHEMA_VERSION = "readyset-action-1.0"
EXPLAIN_SCHEMA_VERSION = "readyset-explain-1.0"
EXPORT_SCHEMA_VERSION = "readyset-export-1.0"
ACTION_TIMEOUT = 30
PROXIED_LIMIT = 200
SHALLOW_ENTRIES_LIMIT = 200

STATUSES = ("ok", "watch", "problem", "unknown")
_STATUS_RANK = {"ok": 0, "watch": 1, "problem": 2, "unknown": 1}
_QUERY_ID_RE = re.compile(r"^q_[0-9a-fA-F]{1,64}$")
_SAFE_SCHEMA_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_MAX_BYTES = 1099511627776  # 1 TiB
_MAX_PERIOD_MS = 86400000   # 24 h
_MAX_TTL_S = 31536000       # 365 days
_MAX_COALESCE_MS = 60000    # 60 s

SHALLOW_ALLOWLIST_KINDS = {
    "function": ("FUNCTION", "FUNCTIONS", "functions"),
    "variable": ("VARIABLE", "VARIABLES", "variables"),
    "schema": ("SCHEMA", "SCHEMAS", "schemas"),
}


def select(conn, sql, timeout=None):
    """Run *sql* with ``--comments --xml`` so NULLs and multi-line SQL queries round-trip."""
    text = conn.run(sql, extra_flags=["--comments", "--xml"], timeout=timeout)
    try:
        return named_rows(text)
    except ValueError:
        raise ConnectorError("a value the client could not print as XML")


def _short(exc):
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else "no answer"
    if ": " in text:
        text = text.split(": ", 2)[-1]
    return text[:200]


def _int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "t", "yes"):
        return True
    if s in ("false", "0", "f", "no"):
        return False
    return None


def _text(value):
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _us_to_ms(value):
    num = _float(value)
    return round(num / 1000.0, 3) if num is not None else None


def _rs_ident(value, label="Identifier"):
    """Quote one SQL identifier with backticks for Readyset SQL."""
    if not isinstance(value, str):
        raise ValueError("%s is required." % label)
    s = value.strip()
    if not s or len(s) > 128 or "\x00" in s or "\r" in s or "\n" in s:
        raise ValueError("%s must be 1 to 128 characters without line breaks." % label)
    if s.startswith("`") and s.endswith("`") and len(s) >= 2:
        s = s[1:-1].replace("``", "`")
    if not s:
        raise ValueError("%s cannot be empty." % label)
    return "`%s`" % s.replace("`", "``")


def _rs_table_ident(value):
    """Quote ``table`` or ``schema.table`` for ``RESNAPSHOT TABLE`` / ``ADD TABLES``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Pass a table name (table or schema.table).")
    raw = value.strip()
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise ValueError("Table name cannot contain line breaks or NUL characters.")
    # Split unquoted schema.table or `schema`.`table`.
    parts = []
    cur = []
    in_bt = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "`":
            if in_bt and i + 1 < len(raw) and raw[i + 1] == "`":
                cur.append("`")
                i += 2
                continue
            in_bt = not in_bt
            i += 1
            continue
        if ch == "." and not in_bt:
            parts.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if in_bt:
        raise ValueError("Unclosed backtick in table name.")
    parts.append("".join(cur).strip())
    if not 1 <= len(parts) <= 2 or any(not p for p in parts):
        raise ValueError("Pass a valid table or schema.table name.")
    return ".".join(_rs_ident(p, "Table name") for p in parts)


def _rs_string(value, label="Value", max_len=512):
    """Single-quote a plain string literal for Readyset without backslashes or newlines."""
    if not isinstance(value, str):
        raise ValueError("%s must be text." % label)
    s = value.strip()
    if not s or len(s) > max_len:
        raise ValueError("%s must be 1 to %d characters." % (label, max_len))
    if "\x00" in s or "\r" in s or "\n" in s or "\\" in s:
        raise ValueError("%s cannot contain line breaks, backslashes or NUL characters." % label)
    return "'%s'" % s.replace("'", "''")


def _normalize_query_or_id(raw):
    """Validate and normalize a Readyset query ID (``q_...``) or single-line ``SELECT``."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Enter a SELECT query or a Readyset query ID (q_...).")
    if "\x00" in raw:
        raise ValueError("Query text cannot contain NUL characters.")
    trimmed = raw.strip()
    while trimmed.endswith(";"):
        trimmed = trimmed[:-1].rstrip()
    if not trimmed:
        raise ValueError("Enter a SELECT query or a Readyset query ID (q_...).")
    if _QUERY_ID_RE.match(trimmed):
        return "query_id", trimmed

    from .live import TOKEN, _select_problem
    problem = _select_problem(trimmed)
    if problem:
        raise ValueError(problem)

    # Collapse whitespace outside quoted tokens to single spaces so the reviewed
    # statement has no \r or \n while preserving exact characters inside string
    # and identifier literals; reject '--' / '#' line comments and multi-line literals.
    parts = []
    unquoted = []
    pos = 0
    for m in TOKEN.finditer(trimmed):
        unquoted.append(trimmed[pos:m.start()])
        tok = m.group()
        if tok[0] in ("-", "#"):
            raise ValueError(
                "Remove '--' and '#' line comments (or use /* ... */) so the cache query fits on one line."
            )
        if tok.startswith("/*"):
            unquoted.append(" ")
        else:
            if "\n" in tok or "\r" in tok:
                raise ValueError("String or identifier literals in a cache query cannot contain raw line breaks.")
            chunk = "".join(unquoted)
            if chunk:
                if chunk.strip():
                    lead = " " if chunk[:1].isspace() else ""
                    trail = " " if chunk[-1:].isspace() else ""
                    parts.append(lead + " ".join(chunk.split()) + trail)
                else:
                    parts.append(" ")
                unquoted = []
            parts.append(tok)
        pos = m.end()
    unquoted.append(trimmed[pos:])
    chunk = "".join(unquoted)
    if chunk and chunk.strip():
        lead = " " if chunk[:1].isspace() and parts else ""
        parts.append(lead + " ".join(chunk.split()))
    normalized = "".join(parts).strip()
    recheck = _select_problem(normalized)
    if recheck:
        raise ValueError(recheck)
    return "sql", normalized


def _strict_int(value, minimum, maximum, label):
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("%s must be an integer between %d and %d." % (label, minimum, maximum))
    try:
        n = int(value) if isinstance(value, int) else int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer between %d and %d." % (label, minimum, maximum))
    if not minimum <= n <= maximum:
        raise ValueError("%s must be between %d and %d." % (label, minimum, maximum))
    return n


def identify(conn):
    """``(True, version)`` for Readyset (Adapter or Admin port), ``(False, reason)`` otherwise.

    Uses single-column ``SELECT @@version_comment`` (intercepted locally by
    Readyset's query handler on Adapter 3307 and routed to DataFusion on Admin
    6036, returning ``'Readyset'`` on both), then confirms ``SHOW READYSET VERSION``.
    Connection failures raise :class:`ConnectorError`.
    """
    try:
        rows = select(conn, "SELECT @@version_comment")
    except ConnectorError as exc:
        from .reviewed_writes import client_error, error_number
        code = error_number(str(exc))
        if code is None or client_error(code) or "Access denied" in str(exc):
            raise
        return False, "SELECT @@version_comment failed (%s)" % _short(exc)
    comment = _text(next(iter(rows[0].values()))) if rows and rows[0] else ""
    if not is_readyset_comment(comment):
        return False, "@@version_comment is %s, not Readyset" % (comment[:60] if comment else "empty")
    try:
        vrows = select(conn, "SHOW READYSET VERSION")
    except ConnectorError as exc:
        return False, "SHOW READYSET VERSION failed (%s)" % _short(exc)
    version = parse_readyset_version(vrows) or "unknown"
    return True, version


def _detect_port_mode(conn, schema_name):
    """Return ``(port_role, in_readyset_schema)`` without relying on DataFusion version strings.

    On the Admin SQL listener (default 6036), Readyset initializes the session's
    search path to ``[readyset_schema]`` and sets ``readyset_schema_route_all = true``
    even when no default database was passed, and refuses any other ``USE``/``-D``.
    On the Adapter SQL listener (default 3307), ``readyset_schema_route_all`` is
    true only when the session's default database is ``readyset_schema``.
    """
    db = (getattr(conn, "database", None) or "").strip()
    port = _int(getattr(conn, "port", None))
    if not db:
        try:
            select(conn, "SELECT 1 FROM replication_status LIMIT 0")
            return "admin", True
        except ConnectorError:
            return "adapter", False
    if db.lower() == schema_name.lower():
        is_admin = port == 6036
        if not is_admin:
            try:
                drows = select(conn, "SHOW DATABASES")
                names = {
                    (next(iter(r.values())) or "").strip().lower()
                    for r in drows if r
                } - {""}
                # DataFusion's catalog exposes only information_schema / public / readyset.
                if names and names <= {"information_schema", "public", schema_name.lower()}:
                    is_admin = True
            except ConnectorError:
                pass
        return ("admin" if is_admin else "adapter"), True
    return "adapter", False


class _Reads(object):
    def __init__(self, conn):
        self.conn = conn
        self.statements = []
        self.missing = []

    def query(self, sql, label=None):
        try:
            rows = select(self.conn, sql)
        except ConnectorError as exc:
            self.missing.append((label or sql, _short(exc)))
            return None
        self.statements.append(label or sql)
        return rows


def _empty_telemetry():
    return {
        "cached_executions": 0,
        "upstream_executions": 0,
        "total_observed_executions": 0,
        "cached_share_pct": None,
        "shallow_hits": 0,
        "shallow_misses": 0,
        "shallow_hit_rate_pct": None,
        "shallow_refreshes": 0,
        "wasted_refreshes": 0,
        "dropped_refreshes": 0,
        "coalesces": 0,
        "upstream_saved_ms": 0.0,
    }


def _not_readyset(message, version=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": now(),
        "available": False,
        "is_readyset": False,
        "version": version,
        "port_role": None,
        "in_readyset_schema": False,
        "readyset_schema": "readyset",
        "message": message,
        "status": "unknown",
        "status_why": [message] if message else [],
        "status_fields": {},
        "capabilities": {
            "can_explain_cache": False,
            "can_create_cache": False,
            "can_manage_caches": False,
            "can_alter_readyset": False,
            "can_shutdown": False,
            "virtual_tables": False,
        },
        "replication": None,
        "telemetry": _empty_telemetry(),
        "caches": [],
        "proxied_queries": [],
        "tables": [],
        "table_counts": {"total": 0, "snapshotted": 0, "snapshotting": 0, "ignored": 0, "error": 0, "other": 0},
        "shallow_allowlists": {"functions": [], "variables": [], "schemas": []},
        "connections": [],
        "upstreams": [],
        "topology": None,
        "fingerprints": {},
        "actions": [],
        "notes": [],
        "statements": [],
    }


def _parse_kv_rows(rows):
    out = {}
    for r in rows or ():
        if not r:
            continue
        if "name" in r and "value" in r:
            k, v = r.get("name"), r.get("value")
        elif "Variable_name" in r and "Value" in r:
            k, v = r.get("Variable_name"), r.get("Value")
        else:
            vals = list(r.values())
            k = vals[0] if vals else None
            v = vals[1] if len(vals) > 1 else None
        if k:
            out[str(k)] = v
    return out


def _classify_support(text_val):
    raw = _text(text_val) or "unknown"
    low = raw.lower()
    if low == "yes":
        return "yes", None
    if low == "cached":
        return "cached", None
    if low == "pending":
        return "pending", None
    if ":" in raw:
        head, detail = raw.split(":", 1)
        h = head.strip().lower()
        if h in ("no", "unsupported", "skipped"):
            return ("skipped" if h == "skipped" else "unsupported"), detail.strip() or None
    if low in ("no", "unsupported"):
        return "unsupported", None
    return "unknown", raw


def _classify_table_status(status_text):
    low = (_text(status_text) or "").lower()
    if not low:
        return "other"
    if "error" in low or "fail" in low:
        return "error"
    if "progress" in low or "snapshotting" in low or "creating" in low or "pending" in low:
        return "snapshotting"
    if "ignore" in low or "skip" in low or "unsupported" in low:
        return "ignored"
    if "complete" in low or "snapshot" in low or "online" in low or "ready" in low or low == "yes":
        return "snapshotted"
    return "other"


def _stat_metrics(row):
    if not row:
        return None
    return {
        "total_count": _int(row.get("total_count")) or 0,
        "avg_ms": _us_to_ms(row.get("avg_us")),
        "min_ms": _us_to_ms(row.get("min_us")),
        "max_ms": _us_to_ms(row.get("max_us")),
        "p50_ms": _us_to_ms(row.get("p50_us")),
        "p90_ms": _us_to_ms(row.get("p90_us")),
        "p95_ms": _us_to_ms(row.get("p95_us")),
        "p99_ms": _us_to_ms(row.get("p99_us")),
        "p999_ms": _us_to_ms(row.get("p999_us")),
        "last_executed_s": _int(row.get("last_executed_s")),
    }


def _fingerprint(value):
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def read_status(conn, profiles=(), open_profile=None, reached_host=None, current_profile_id=None, schema="readyset"):
    """Read the complete ``readyset-1.0`` report (read-only)."""
    schema_name = schema if isinstance(schema, str) and _SAFE_SCHEMA_RE.match(schema) else "readyset"
    ok, detail = identify(conn)
    if not ok:
        return _not_readyset(
            "This connection is not a Readyset SQL or admin port: %s. Readyset listens on port 3307 (SQL) and "
            "port 6036 (admin) by default." % detail
        )

    port_role, in_rs_schema = _detect_port_mode(conn, schema_name)
    reads = _Reads(conn)
    report = _not_readyset(None, detail)
    report.update({
        "available": True,
        "is_readyset": True,
        "port_role": port_role,
        "in_readyset_schema": in_rs_schema,
        "readyset_schema": schema_name,
        "message": None,
        "status_why": [],
        "statements": ["SELECT @@version_comment", "SHOW READYSET VERSION"],
    })

    # Core SHOW READYSET commands
    pw = getattr(conn, "password", None)
    status_rows = reads.query("SHOW READYSET STATUS", "SHOW READYSET STATUS") or []
    raw_status_map = _parse_kv_rows(status_rows)
    status_map = dict(
        (k, dbops.redact(v, pw) if isinstance(v, str) else v)
        for k, v in raw_status_map.items()
    )
    report["status_fields"] = {
        "database_connection": _text(status_map.get("Database Connection")),
        "connection_count": _int(status_map.get("Connection Count")),
        "status": _text(status_map.get("Status")) or "Online",
        "replication_status": _text(status_map.get("Replication Status")),
        "cdc_server": _text(status_map.get("CDC Server")),
        "max_replication_offset": _text(status_map.get("Maximum Replication Offset")),
        "min_replication_offset": _text(status_map.get("Minimum Replication Offset")),
        "controller_status": _text(status_map.get("Readyset Controller Status")),
        "last_started_controller": _text(status_map.get("Last started Controller")),
        "last_completed_snapshot": _text(status_map.get("Last completed snapshot")),
        "last_started_replication": _text(status_map.get("Last started replication")),
        "last_replicator_error": _text(status_map.get("Last replicator error")),
        "enabled_features": _text(status_map.get("Enabled Features")),
        "host_cpus": _int(status_map.get("Host CPUs")),
        "host_memory": _text(status_map.get("Host Total Memory")),
        "host_disk": _text(status_map.get("Host Total Disk")),
        "host_arch": _text(status_map.get("Host Arch")),
        "host_os": _text(status_map.get("Host OS")),
        "host_kernel": _text(status_map.get("Host Kernel")),
        "host_container": _text(status_map.get("Host Container")),
        "raw": status_map,
    }

    cache_rows = reads.query("SHOW CACHES", "SHOW CACHES")
    cache_ddl_blocked = any("Readyset Cloud UI" in msg or "cache-ddl" in msg.lower() for _, msg in reads.missing)

    # Virtual tables in readyset.* (or unqualified on Admin)
    vt_prefix = "" if in_rs_schema else ("%s." % _rs_ident(schema_name))
    upstream_rows = reads.query(
        "SELECT host, port, database, user, weight, purpose, active_cdc, health, down_since, last_ok, connections "
        "FROM %supstream_servers" % vt_prefix,
        "readyset.upstream_servers",
    )
    repl_rows = reads.query(
        "SELECT replication_mode, upstream_offset, replicator_offset, persisted_offset, consume_lag, persist_lag, "
        "heartbeat_staleness_seconds FROM %sreplication_status" % vt_prefix,
        "readyset.replication_status",
    )
    deep_rows = reads.query(
        "SELECT query_id, name, query, always, until_write, total_count FROM %sdeep_caches" % vt_prefix,
        "readyset.deep_caches",
    )
    shallow_rows = reads.query(
        "SELECT query_id, name, query, ttl_ms, refresh_ms, coalesce_ms, always, until_write, schedule, adaptive, "
        "hits, misses, refreshes, coalesces FROM %sshallow_caches" % vt_prefix,
        "readyset.shallow_caches",
    )
    refresh_rows = reads.query(
        "SELECT query_id, wasted_refreshes, dropped_refreshes, load_actual_ppm, load_baseline_ppm, over_cap, "
        "scheduler_queue_len FROM %sshallow_cache_refresh_stats" % vt_prefix,
        "readyset.shallow_cache_refresh_stats",
    )
    coalesce_rows = reads.query(
        "SELECT query_id, coalesce_timeouts, coalesce_aborts, total_us, avg_us, upstream_saved_total_us, "
        "upstream_saved_avg_us FROM %sshallow_cache_coalesce_stats" % vt_prefix,
        "readyset.shallow_cache_coalesce_stats",
    )
    cached_stat_rows = reads.query(
        "SELECT query_id, query, total_count, total_us, avg_us, min_us, max_us, p50_us, p90_us, p95_us, p99_us, "
        "p999_us, last_executed_s FROM %scached_query_stats" % vt_prefix,
        "readyset.cached_query_stats",
    )
    upstream_stat_rows = reads.query(
        "SELECT query_id, query, total_count, total_us, avg_us, min_us, max_us, p50_us, p90_us, p95_us, p99_us, "
        "p999_us, last_executed_s FROM %supstream_query_stats" % vt_prefix,
        "readyset.upstream_query_stats",
    )

    vt_ok = any(r is not None for r in (upstream_rows, repl_rows, deep_rows, shallow_rows, cached_stat_rows))
    report["capabilities"] = {
        "can_explain_cache": not in_rs_schema and not cache_ddl_blocked,
        "can_create_cache": not in_rs_schema and not cache_ddl_blocked,
        "can_manage_caches": not cache_ddl_blocked,
        "can_alter_readyset": True,
        "can_shutdown": port_role == "admin",
        "virtual_tables": vt_ok,
    }

    # Shallow cache allowlists
    allowlists = {"functions": [], "variables": [], "schemas": []}
    for kind, (_, plural, key) in SHALLOW_ALLOWLIST_KINDS.items():
        arows = reads.query(
            "SHOW SHALLOW CACHE ALLOWED %s" % plural,
            "SHOW SHALLOW CACHE ALLOWED %s" % plural,
        )
        if arows is not None:
            items = []
            for r in arows:
                val = _text(next(iter(r.values()))) if r else None
                if val:
                    items.append(val)
            allowlists[key] = sorted(items)
    report["shallow_allowlists"] = allowlists

    # Connections
    conn_rows = reads.query("SHOW CONNECTIONS", "SHOW CONNECTIONS") or []
    report["connections"] = [
        {
            "client_addr": _text(r.get("client_addr")),
            "username": _text(r.get("username")),
            "upstream": dbops.redact(_text(r.get("upstream")), pw) if _text(r.get("upstream")) else None,
        }
        for r in conn_rows
    ]

    # Tables
    table_rows = reads.query("SHOW READYSET ALL TABLES", "SHOW READYSET ALL TABLES") or []
    tables = []
    counts = {"total": 0, "snapshotted": 0, "snapshotting": 0, "ignored": 0, "error": 0, "other": 0}
    for r in table_rows:
        tname = _text(r.get("table"))
        if not tname:
            continue
        tstatus = _text(r.get("status")) or "Unknown"
        bucket = _classify_table_status(tstatus)
        counts["total"] += 1
        counts[bucket] += 1
        tables.append({
            "table": tname,
            "status": tstatus,
            "bucket": bucket,
            "description": _text(r.get("description")),
        })
    report["tables"] = tables
    report["table_counts"] = counts

    # Index virtual table stats by query_id
    deep_by_id = dict((r.get("query_id"), r) for r in (deep_rows or ()) if r.get("query_id"))
    shallow_by_id = dict((r.get("query_id"), r) for r in (shallow_rows or ()) if r.get("query_id"))
    refresh_by_id = dict((r.get("query_id"), r) for r in (refresh_rows or ()) if r.get("query_id"))
    coalesce_by_id = dict((r.get("query_id"), r) for r in (coalesce_rows or ()) if r.get("query_id"))
    cached_by_id = dict((r.get("query_id"), r) for r in (cached_stat_rows or ()) if r.get("query_id"))
    upstream_by_id = dict((r.get("query_id"), r) for r in (upstream_stat_rows or ()) if r.get("query_id"))

    # Caches
    caches = []
    for r in cache_rows or ():
        qid = _text(r.get("query_id")) or ""
        name = _text(r.get("name")) or qid
        props = _text(r.get("properties")) or ""
        if qid in shallow_by_id or "shallow" in props.lower() or "ttl" in props.lower():
            ctype = "shallow"
        else:
            ctype = "deep"
        sh = shallow_by_id.get(qid) or {}
        dp = deep_by_id.get(qid) or {}
        rf = refresh_by_id.get(qid) or {}
        co = coalesce_by_id.get(qid) or {}
        cs = _stat_metrics(cached_by_id.get(qid))
        us = _stat_metrics(upstream_by_id.get(qid))
        hits = _int(sh.get("hits"))
        misses = _int(sh.get("misses"))
        hit_rate = round(100.0 * hits / (hits + misses), 1) if (hits is not None and misses is not None and (hits + misses) > 0) else None
        count_val = _int(r.get("count"))
        if count_val is None and cs is not None:
            count_val = cs["total_count"]
        if count_val is None and dp:
            count_val = _int(dp.get("total_count"))
        caches.append({
            "query_id": qid,
            "name": name,
            "cache_type": ctype,
            "query": r.get("query") or r.get("query string") or sh.get("query") or dp.get("query") or "",
            "properties": props,
            "database": _text(r.get("database")),
            "count": count_val,
            "always": _bool(sh.get("always") if ctype == "shallow" else dp.get("always")),
            "until_write": _bool(sh.get("until_write") if ctype == "shallow" else dp.get("until_write")),
            "ttl_ms": _int(sh.get("ttl_ms")),
            "refresh_ms": _int(sh.get("refresh_ms")),
            "coalesce_ms": _int(sh.get("coalesce_ms")),
            "adaptive": _bool(sh.get("adaptive")),
            "schedule": _text(sh.get("schedule")),
            "hits": hits,
            "misses": misses,
            "hit_rate_pct": hit_rate,
            "refreshes": _int(sh.get("refreshes")),
            "coalesces": _int(sh.get("coalesces")),
            "wasted_refreshes": _int(rf.get("wasted_refreshes")),
            "dropped_refreshes": _int(rf.get("dropped_refreshes")),
            "over_cap": _bool(rf.get("over_cap")),
            "upstream_saved_ms": _us_to_ms(co.get("upstream_saved_total_us")),
            "cached_stats": cs,
            "upstream_stats": us,
        })
    report["caches"] = caches

    # Proxied queries (merge deep + shallow support verdicts)
    proxied_deep = reads.query(
        "SHOW PROXIED DEEP QUERIES LIMIT %d" % PROXIED_LIMIT,
        "SHOW PROXIED DEEP QUERIES",
    )
    proxied_shallow = reads.query(
        "SHOW PROXIED SHALLOW QUERIES LIMIT %d" % PROXIED_LIMIT,
        "SHOW PROXIED SHALLOW QUERIES",
    )
    proxied_map = {}
    if proxied_deep is None and proxied_shallow is None:
        fallback_pq = reads.query("SHOW PROXIED QUERIES LIMIT %d" % PROXIED_LIMIT, "SHOW PROXIED QUERIES") or []
        for r in fallback_pq:
            qid = _text(r.get("query_id") or r.get("query id"))
            if not qid:
                continue
            sup, reason_txt = _classify_support(r.get("readyset_supported") or r.get("readyset supported"))
            proxied_map[qid] = {
                "query_id": qid,
                "query": r.get("query") or r.get("proxied query") or "",
                "count": _int(r.get("count")),
                "deep_support": sup,
                "deep_reason": reason_txt,
                "shallow_support": "unknown",
                "shallow_reason": None,
            }
    else:
        for r in proxied_deep or ():
            qid = _text(r.get("query_id") or r.get("query id"))
            if not qid:
                continue
            sup, reason_txt = _classify_support(r.get("readyset_supported") or r.get("readyset supported"))
            proxied_map[qid] = {
                "query_id": qid,
                "query": r.get("query") or r.get("proxied query") or "",
                "count": _int(r.get("count")),
                "deep_support": sup,
                "deep_reason": reason_txt,
                "shallow_support": "unknown",
                "shallow_reason": None,
            }
        for r in proxied_shallow or ():
            qid = _text(r.get("query_id") or r.get("query id"))
            if not qid:
                continue
            sup, reason_txt = _classify_support(r.get("readyset_supported") or r.get("readyset supported"))
            item = proxied_map.get(qid)
            if item is None:
                proxied_map[qid] = {
                    "query_id": qid,
                    "query": r.get("query") or r.get("proxied query") or "",
                    "count": _int(r.get("count")),
                    "deep_support": "unknown",
                    "deep_reason": None,
                    "shallow_support": sup,
                    "shallow_reason": reason_txt,
                }
            else:
                item["shallow_support"] = sup
                item["shallow_reason"] = reason_txt
                if item["count"] is None:
                    item["count"] = _int(r.get("count"))

    cached_ids = {c["query_id"] for c in caches if c.get("query_id")}
    proxied_list = []
    for qid, item in proxied_map.items():
        us = _stat_metrics(upstream_by_id.get(qid))
        cs = _stat_metrics(cached_by_id.get(qid))
        if item["count"] is None and us is not None:
            item["count"] = us["total_count"]
        item["already_cached"] = qid in cached_ids
        item["upstream_stats"] = us
        item["cached_stats"] = cs
        proxied_list.append(item)
    proxied_list.sort(key=lambda x: -(x.get("count") or 0))
    report["proxied_queries"] = proxied_list

    # Telemetry aggregation
    cached_execs = sum((_int(r.get("total_count")) or 0) for r in (cached_stat_rows or ()))
    upstream_execs = sum((_int(r.get("total_count")) or 0) for r in (upstream_stat_rows or ()))
    total_obs = cached_execs + upstream_execs
    sh_hits = sum((_int(r.get("hits")) or 0) for r in (shallow_rows or ()))
    sh_misses = sum((_int(r.get("misses")) or 0) for r in (shallow_rows or ()))
    sh_refreshes = sum((_int(r.get("refreshes")) or 0) for r in (shallow_rows or ()))
    sh_coalesces = sum((_int(r.get("coalesces")) or 0) for r in (shallow_rows or ()))
    wasted_ref = sum((_int(r.get("wasted_refreshes")) or 0) for r in (refresh_rows or ()))
    dropped_ref = sum((_int(r.get("dropped_refreshes")) or 0) for r in (refresh_rows or ()))
    saved_us = sum((_float(r.get("upstream_saved_total_us")) or 0.0) for r in (coalesce_rows or ()))

    report["telemetry"] = {
        "cached_executions": cached_execs,
        "upstream_executions": upstream_execs,
        "total_observed_executions": total_obs,
        "cached_share_pct": round(100.0 * cached_execs / total_obs, 1) if total_obs > 0 else None,
        "shallow_hits": sh_hits,
        "shallow_misses": sh_misses,
        "shallow_hit_rate_pct": round(100.0 * sh_hits / (sh_hits + sh_misses), 1) if (sh_hits + sh_misses) > 0 else None,
        "shallow_refreshes": sh_refreshes,
        "wasted_refreshes": wasted_ref,
        "dropped_refreshes": dropped_ref,
        "coalesces": sh_coalesces,
        "upstream_saved_ms": round(saved_us / 1000.0, 2),
    }

    # Replication summary (mode-aware lag units!)
    sf = report["status_fields"]
    repl = {
        "status": sf.get("replication_status") or "Unknown",
        "cdc_server": sf.get("cdc_server"),
        "max_offset": sf.get("max_replication_offset"),
        "min_offset": sf.get("min_replication_offset"),
        "last_error": dbops.redact(sf.get("last_replicator_error")) if sf.get("last_replicator_error") else None,
        "last_started": sf.get("last_started_replication"),
        "last_snapshot": sf.get("last_completed_snapshot"),
        "mode": None,
        "lag_unit": None,
        "consume_lag": None,
        "persist_lag": None,
        "upstream_offset": None,
        "replicator_offset": None,
        "persisted_offset": None,
        "heartbeat_staleness_s": None,
    }
    if repl_rows:
        rr = repl_rows[0]
        mode = _text(rr.get("replication_mode"))
        lag_unit = (
            "transactions" if mode == "mysql_gtid"
            else ("bytes" if mode in ("mysql_file", "postgres") else "events")
        )
        repl.update({
            "mode": mode,
            "lag_unit": lag_unit,
            "consume_lag": _int(rr.get("consume_lag")),
            "persist_lag": _int(rr.get("persist_lag")),
            "upstream_offset": _text(rr.get("upstream_offset")),
            "replicator_offset": _text(rr.get("replicator_offset")),
            "persisted_offset": _text(rr.get("persisted_offset")),
            "heartbeat_staleness_s": _float(rr.get("heartbeat_staleness_seconds")),
        })
    report["replication"] = repl

    # Upstreams
    upstreams = []
    for u in upstream_rows or ():
        uh, up = _text(u.get("host")), _int(u.get("port")) or 3306
        if not uh:
            continue
        upstreams.append({
            "host": uh,
            "port": up,
            "address": format_endpoint(uh, up),
            "database": _text(u.get("database")),
            "user": _text(u.get("user")),
            "weight": _int(u.get("weight")),
            "purpose": _text(u.get("purpose")) or "",
            "active_cdc": _bool(u.get("active_cdc")) is True,
            "health": _text(u.get("health")),
            "down_since": _text(u.get("down_since")),
            "last_ok": _text(u.get("last_ok")),
            "connections": _int(u.get("connections")),
        })
    report["upstreams"] = upstreams

    # Multi-tier topology (ProxySQL -> Readyset -> MySQL/MariaDB replicas & clusters)
    cur_host = getattr(conn, "host", None)
    cur_port = _int(getattr(conn, "port", None))
    obs_host = reached_host if reached_host is not None else cur_host
    topo = build_readyset_topology(
        status_map, upstream_rows, conn_rows, port_role, detail,
        profiles=profiles, open_profile=open_profile, observer_host=obs_host,
        current_profile_id=current_profile_id,
        current_endpoint=(obs_host, cur_port) if obs_host else None,
    )
    report["topology"] = topo
    for n in topo.get("notes") or ():
        if n not in report["notes"]:
            report["notes"].append(n)

    if in_rs_schema:
        report["notes"].append(
            "Connected on the %s: EXPLAIN CREATE CACHE and CREATE CACHE require the Readyset SQL port (default 3307) "
            "with an upstream database schema."
            % ("admin SQL port" if port_role == "admin" else "readyset virtual schema")
        )
    if port_role != "admin":
        report["notes"].append(
            "ALTER READYSET SHUTDOWN is accepted only on the Readyset admin SQL port (default 6036)."
        )

    # Overall verdict
    status = "ok"
    why = []

    def _worse(cur, candidate, reason_line):
        if reason_line and reason_line not in why:
            why.append(reason_line)
        return candidate if _STATUS_RANK[candidate] > _STATUS_RANK[cur] else cur

    if sf.get("database_connection") == "Unreachable":
        status = _worse(status, "problem", "Upstream database connection is Unreachable.")
    if sf.get("controller_status") == "Unavailable":
        status = _worse(status, "problem", "Readyset Controller is Unavailable.")
    if repl.get("last_error"):
        status = _worse(status, "problem", "Replicator error: %s" % repl["last_error"])
    for u in upstreams:
        if (u.get("health") or "").lower() == "down":
            lvl = "problem" if u.get("active_cdc") else "watch"
            status = _worse(status, lvl, "Upstream %s (%s) health is down." % (u["address"], u["purpose"] or "proxy"))
    if counts["error"] > 0:
        status = _worse(status, "problem", "%d replicated base %s reported an error." % (
            counts["error"], "table" if counts["error"] == 1 else "tables"))
    if sf.get("status") == "Maintenance Mode":
        status = _worse(status, "watch", "Readyset is in Maintenance Mode (queries bypass caches to upstream).")
    elif sf.get("status") == "Snapshot In Progress":
        status = _worse(status, "watch", "Base table snapshot is in progress.")
    has_deep = any(c.get("cache_type") == "deep" for c in caches)
    repl_st = sf.get("replication_status")
    if repl_st == "Error":
        status = _worse(status, "problem", "Replication status is Error.")
    elif repl_st == "Stopped":
        status = _worse(status, "watch", "Replication is Stopped; deep caches are not receiving upstream writes.")
    elif repl_st == "Disabled":
        if has_deep:
            status = _worse(status, "watch", "Replication is Disabled while deep caches exist.")
    elif repl_st and repl_st not in ("Running", "Online", "Unknown"):
        status = _worse(status, "watch", "Replication status is %s." % repl_st)
    hb_stale = repl.get("heartbeat_staleness_s")
    if hb_stale is not None and hb_stale > 60.0 and sf.get("replication_status") == "Running":
        status = _worse(status, "watch", "Replication heartbeat is %.0f s old." % hb_stale)
    if dropped_ref > 0 or any(c.get("over_cap") for c in caches):
        status = _worse(status, "watch", "Shallow cache background refresh hit its cap or dropped refreshes.")

    report["status"] = status
    report["status_why"] = why
    report["statements"] = reads.statements
    report["fingerprints"] = {
        "caches": _fingerprint([(c["query_id"], c["name"], c["cache_type"], c["properties"]) for c in caches]),
        "tables": _fingerprint([(t["table"], t["status"]) for t in tables]),
        "allowlists": _fingerprint(allowlists),
        "replication": _fingerprint([sf.get("status"), sf.get("replication_status"), sf.get("cdc_server")]),
    }
    report["actions"] = available_actions(report)
    return report


def at_or_worse(report, threshold):
    """True when *report*'s status is at or worse than *threshold* (``watch`` or ``problem``)."""
    rank = {"ok": 0, "watch": 1, "unknown": 1, "problem": 2}
    if not report or not report.get("available"):
        return True
    return rank.get(report.get("status"), 1) >= rank.get(threshold, 2)


# ---- explain & export caches (read-only) -------------------------------------------------

def _explain_one(conn, target, kind):
    keyword = "SHALLOW " if kind == "shallow" else ("DEEP " if kind == "deep" else "")
    sql = "EXPLAIN CREATE %sCACHE FROM %s" % (keyword, target)
    try:
        rows = select(conn, sql, timeout=ACTION_TIMEOUT)
    except ConnectorError as exc:
        return {
            "cache_type": kind,
            "supported": "error",
            "raw_support": None,
            "reason": dbops.redact(_short(exc), getattr(conn, "password", None)),
            "query_id": None,
            "query": None,
            "statement": sql,
        }
    row = rows[0] if rows else {}
    sup_raw = _text(row.get("readyset_supported") if "readyset_supported" in row else row.get("readyset supported"))
    verdict, detail = _classify_support(sup_raw)
    return {
        "cache_type": kind,
        "supported": verdict,
        "raw_support": sup_raw,
        "reason": detail,
        "query_id": _text(row.get("query_id") if "query_id" in row else row.get("query id")),
        "query": row.get("query") if "query" in row else row.get("proxied query"),
        "statement": sql,
    }


def explain_cache(conn, query_or_id, cache_type=None, schema="readyset"):
    """Run ``EXPLAIN CREATE [SHALLOW|DEEP] CACHE`` (dry-run, read-only) and check cache collisions."""
    ok, version = identify(conn)
    if not ok:
        raise ValueError("This connection is not a Readyset instance (%s)." % version)
    schema_name = schema if isinstance(schema, str) and _SAFE_SCHEMA_RE.match(schema) else "readyset"
    port_role, in_rs_schema = _detect_port_mode(conn, schema_name)
    if in_rs_schema:
        raise ValueError(
            "EXPLAIN CREATE CACHE requires the Readyset SQL port (default 3307) with an upstream database schema, "
            "not the %s." % ("admin SQL port (6036)" if port_role == "admin" else "readyset virtual schema")
        )
    input_kind, target = _normalize_query_or_id(query_or_id)
    if cache_type not in (None, "both", "shallow", "deep"):
        raise ValueError("Choose cache_type 'shallow', 'deep' or 'both'.")

    shallow = _explain_one(conn, target, "shallow") if cache_type in (None, "both", "shallow") else None
    deep = _explain_one(conn, target, "deep") if cache_type in (None, "both", "deep") else None

    candidate_ids = {
        r["query_id"] for r in (shallow, deep)
        if r and r.get("query_id")
    }
    collisions = []
    try:
        crows = select(conn, "SHOW CACHES", timeout=ACTION_TIMEOUT)
        for r in crows:
            qid = _text(r.get("query_id"))
            if qid and qid in candidate_ids:
                collisions.append({
                    "query_id": qid,
                    "name": _text(r.get("name")) or qid,
                    "properties": _text(r.get("properties")) or "",
                })
    except ConnectorError:
        pass

    return {
        "schema_version": EXPLAIN_SCHEMA_VERSION,
        "version": version,
        "input_kind": input_kind,
        "target": target,
        "shallow": shallow,
        "deep": deep,
        "collisions": collisions,
        "notes": [],
    }


def export_caches(conn):
    """Run ``EXPLAIN ALL CACHES`` (falling back to ``EXPLAIN CACHES`` on older Readyset releases)."""
    ok, version = identify(conn)
    if not ok:
        raise ValueError("This connection is not a Readyset instance (%s)." % version)
    notes = []
    try:
        rows = select(conn, "EXPLAIN ALL CACHES", timeout=ACTION_TIMEOUT)
    except ConnectorError as exc:
        from .reviewed_writes import error_number
        if error_number(str(exc)) != 1064:
            raise
        rows = select(conn, "EXPLAIN CACHES", timeout=ACTION_TIMEOUT)
        notes.append("Exported with EXPLAIN CACHES (EXPLAIN ALL CACHES requires a newer Readyset release).")
    items = []
    lines = ["-- Readyset cache definitions exported by myflames (Readyset %s)" % version]
    last_db = None
    for r in rows:
        q = (r.get("query") or r.get("create statement") or "").strip()
        if not q:
            continue
        db = _text(r.get("database"))
        if db and db != last_db and not q.upper().startswith("USE "):
            lines.append("USE %s;" % _rs_ident(db, "Database"))
            last_db = db
        stmt = q if q.endswith(";") else (q + ";")
        lines.append(stmt)
        items.append({"database": db, "statement": stmt})
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "version": version,
        "count": len(items),
        "caches": items,
        "sql": "\n".join(lines) + "\n",
        "notes": notes,
    }


# ---- reviewed actions (dbops + reviewed_writes) ------------------------------------------

def available_actions(report, target=None):
    """Actions offered for *report* (server-level when *target* is None, or row-scoped)."""
    if not report or not report.get("is_readyset"):
        return []
    caps = report.get("capabilities") or {}
    sf = report.get("status_fields") or {}
    caches = report.get("caches") or []
    shallow_count = sum(1 for c in caches if c.get("cache_type") == "shallow")
    deep_count = sum(1 for c in caches if c.get("cache_type") == "deep")

    if isinstance(target, dict):
        kind = target.get("kind")
        if kind == "cache":
            qid = target.get("query_id")
            name = target.get("name")
            found = next((c for c in caches if c.get("name") == name or (qid and c.get("query_id") == qid)), None)
            if not found or not caps.get("can_manage_caches", True):
                return []
            out = [{"id": "drop_cache", "label": "Drop cache…", "tone": "danger"}]
            if found.get("cache_type") == "shallow":
                out.insert(0, {"id": "flush_cache", "label": "Flush entries…", "tone": "default"})
            return out
        if kind == "proxied_query":
            if not caps.get("can_create_cache"):
                return []
            return [{"id": "create_cache", "label": "Create cache…", "tone": "default"}]
        if kind == "table":
            tname = target.get("table")
            if not tname:
                return []
            try:
                canon = _rs_table_ident(tname)
            except ValueError:
                return []
            if not any(_rs_table_ident(t.get("table")) == canon for t in (report.get("tables") or ()) if t.get("table")):
                return []
            return [{"id": "resnapshot_table", "label": "Re-snapshot table…", "tone": "danger"}]
        if kind in SHALLOW_ALLOWLIST_KINDS:
            if target.get("name"):
                return [{"id": "drop_shallow_allowed", "label": "Remove…", "tone": "danger"}]
            return [{"id": "add_shallow_allowed", "label": "Allow…", "tone": "default"}]
        return []

    out = []
    if caps.get("can_create_cache"):
        out.append({"id": "create_cache", "label": "Create cache…", "tone": "default"})
    if caps.get("can_manage_caches", True):
        if shallow_count > 0:
            out.append({"id": "flush_all_shallow_caches", "label": "Flush shallow caches…", "tone": "default"})
        if caches:
            out.append({"id": "drop_all_caches", "label": "Drop all caches…", "tone": "danger"})
        if deep_count > 0 and shallow_count > 0:
            out.append({"id": "drop_all_shallow_caches", "label": "Drop shallow caches…", "tone": "danger"})
            out.append({"id": "drop_all_deep_caches", "label": "Drop deep caches…", "tone": "danger"})
        out.append({"id": "drop_all_proxied_queries", "label": "Clear proxied queries…", "tone": "danger"})

    if sf.get("status") == "Maintenance Mode":
        out.append({"id": "exit_maintenance_mode", "label": "Exit maintenance mode…", "tone": "default"})
    else:
        out.append({"id": "enter_maintenance_mode", "label": "Enter maintenance mode…", "tone": "danger"})

    if sf.get("replication_status") == "Stopped":
        out.append({"id": "start_replication", "label": "Start replication…", "tone": "default"})
    elif sf.get("replication_status") == "Running":
        out.append({"id": "stop_replication", "label": "Stop replication…", "tone": "danger"})

    out.extend([
        {"id": "set_replication_position", "label": "Set replication position…", "tone": "danger"},
        {"id": "add_tables", "label": "Add tables to replicate…", "tone": "default"},
        {"id": "set_log_level", "label": "Set log level…", "tone": "default"},
        {"id": "set_eviction_memory_limit", "label": "Set eviction memory limit…", "tone": "default"},
        {"id": "set_eviction_period", "label": "Set eviction period…", "tone": "default"},
        {"id": "add_shallow_allowed", "label": "Add shallow allowlist item…", "tone": "default"},
        {"id": "flush_privileges", "label": "Flush Readyset privileges…", "tone": "default"},
    ])
    if caps.get("can_shutdown"):
        out.append({"id": "shutdown", "label": "Shut down Readyset…", "tone": "danger"})
        out.append({"id": "shutdown_reset", "label": "Shut down & reset snapshot…", "tone": "danger"})
    return out


def _build_create_cache_sql(spec):
    if not isinstance(spec, dict):
        raise ValueError("Pass cache creation options.")
    ctype = (spec.get("cache_type") or "shallow").strip().lower()
    if ctype not in ("shallow", "deep"):
        raise ValueError("Choose cache_type 'shallow' or 'deep'.")
    input_kind, target = _normalize_query_or_id(spec.get("query_or_id") or "")
    raw_name = (spec.get("name") or "").strip()
    quoted_name = _rs_ident(raw_name, "Cache name") if raw_name else None

    always = spec.get("always") is True
    until_write = spec.get("until_write") is True
    if always and until_write:
        raise ValueError("Choose ALWAYS or UNTIL WRITE, not both.")

    opts = []
    if ctype == "shallow":
        if spec.get("concurrently") is True:
            raise ValueError("CONCURRENTLY applies only to deep caches.")
        ttl_s = spec.get("ttl_seconds")
        refresh_s = spec.get("refresh_seconds")
        if ttl_s not in (None, ""):
            ttl_val = _strict_int(ttl_s, 1, _MAX_TTL_S, "TTL (seconds)")
            policy = "POLICY TTL %d SECONDS" % ttl_val
            if refresh_s not in (None, ""):
                if ttl_val <= 1:
                    raise ValueError("TTL must be at least 2 seconds when setting a background refresh period.")
                ref_val = _strict_int(refresh_s, 1, ttl_val - 1, "Refresh period (seconds)")
                policy += " REFRESH EVERY %d SECONDS" % ref_val
            opts.append(policy)
        elif refresh_s not in (None, ""):
            raise ValueError("Set a TTL before setting a background refresh period.")
        coalesce_ms = spec.get("coalesce_ms")
        if coalesce_ms not in (None, ""):
            cval = _strict_int(coalesce_ms, 1, _MAX_COALESCE_MS, "Coalesce window (ms)")
            opts.append("COALESCE %d MS" % cval)
        if spec.get("adaptive") is True:
            opts.append("ADAPTIVE")
        if always:
            opts.append("ALWAYS")
        elif until_write:
            opts.append("UNTIL WRITE")
    else:
        for forbidden in ("ttl_seconds", "refresh_seconds", "coalesce_ms"):
            if spec.get(forbidden) not in (None, ""):
                raise ValueError("%s applies only to shallow caches." % forbidden)
        if spec.get("adaptive") is True:
            raise ValueError("ADAPTIVE applies only to shallow caches.")
        if always:
            opts.append("ALWAYS")
        elif until_write:
            opts.append("UNTIL WRITE")
        if spec.get("concurrently") is True:
            opts.append("CONCURRENTLY")

    parts = ["CREATE", ctype.upper(), "CACHE"]
    if quoted_name:
        parts.append(quoted_name)
    if opts:
        parts.append("WITH (%s)" % ", ".join(opts))
    parts.extend(["FROM", target])
    return {
        "cache_type": ctype,
        "input_kind": input_kind,
        "target": target,
        "name": raw_name or None,
        "options": opts,
        "statement": " ".join(parts) + ";",
    }


def plan_action(report, action, target=None, value=None, conn=None):
    """Build a ``readyset-action-1.0`` reviewed plan from a fresh *report*."""
    if not report or not report.get("is_readyset"):
        raise ValueError("This connection is not a Readyset instance.")
    if not isinstance(action, str) or not action:
        raise ValueError("Choose a Readyset action.")

    caps = report.get("capabilities") or {}
    sf = report.get("status_fields") or {}
    caches = report.get("caches") or []
    prints = report.get("fingerprints") or {}

    plan = {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": action,
        "target": target,
        "value": value,
        "eyebrow": "READYSET",
        "title": "",
        "confirm_label": "",
        "description": "",
        "target_text": "Readyset (%s)" % (report.get("version") or "unknown"),
        "statements": [],
        "facts": [["Readyset", "%s (%s port)" % (report.get("version") or "unknown", report.get("port_role") or "sql")]],
        "warnings": [],
        "changes": None,
        "typed_guard": None,
        "undo": None,
        "next": None,
        "verified": ["Readyset identity (@@version_comment, SHOW READYSET VERSION)"],
        "timeout_s": ACTION_TIMEOUT,
        "checks": [
            "Refresh the Readyset place to inspect the current caches, tables and status.",
            "The Audit log keeps this attempt as Outcome unknown; nothing is sent again.",
        ],
    }

    if action == "create_cache":
        if not caps.get("can_create_cache"):
            raise ValueError(
                "CREATE CACHE requires the Readyset SQL port (default 3307) with an upstream database schema "
                "and cache DDL enabled."
            )
        spec_in = {}
        if isinstance(target, dict):
            spec_in.update(target)
        if isinstance(value, dict):
            spec_in.update(value)
        if not spec_in.get("query_or_id"):
            spec_in["query_or_id"] = spec_in.get("query_id") or spec_in.get("query") or ""
        built = _build_create_cache_sql(spec_in)
        ctype = built["cache_type"]

        # Collision & support pre-check via EXPLAIN CREATE CACHE when conn is provided.
        candidate_ids = set()
        if built["input_kind"] == "query_id":
            candidate_ids.add(built["target"])
        if conn is not None:
            exp_sh = _explain_one(conn, built["target"], "shallow")
            exp_dp = _explain_one(conn, built["target"], "deep")
            chosen = exp_sh if ctype == "shallow" else exp_dp
            if chosen["supported"] in ("unsupported", "error"):
                raise ValueError(
                    "Readyset cannot create a %s cache for this query: %s"
                    % (ctype, chosen.get("reason") or chosen.get("raw_support") or "unsupported")
                )
            for ex in (exp_sh, exp_dp):
                if ex and ex.get("query_id"):
                    candidate_ids.add(ex["query_id"])
            plan["verified"].append("EXPLAIN CREATE %s CACHE (%s)" % (ctype.upper(), chosen["supported"]))

        collisions = []
        for c in caches:
            same_name = bool(built["name"] and c.get("name") == built["name"])
            same_qid = bool(c.get("query_id") and c["query_id"] in candidate_ids)
            if same_name or same_qid:
                collisions.append({
                    "name": c.get("name"),
                    "query_id": c.get("query_id"),
                    "cache_type": c.get("cache_type"),
                    "properties": c.get("properties"),
                })

        if ctype == "deep" and sf.get("status") == "Snapshot In Progress":
            plan["warnings"].append(
                "A base table snapshot is currently in progress: the Readyset controller may refuse deep cache "
                "creation until Status returns to Online."
            )
        if collisions:
            replaced_names = ", ".join("%s (%s)" % (c["name"], c["cache_type"]) for c in collisions)
            plan["warnings"].append(
                "Readyset drops colliding caches before creating the new cache: %s will be replaced." % replaced_names
            )
            plan["changes"] = ["Replace existing %s cache %s (%s)" % (c["cache_type"], c["name"], c["query_id"])
                               for c in collisions]
            plan["typed_guard"] = collisions[0]["name"] or built["name"]

        label_name = built["name"] or (next(iter(candidate_ids)) if len(candidate_ids) == 1 else built["target"][:48])
        plan.update(
            eyebrow="CREATE CACHE",
            title="Create %s cache%s?" % (ctype, (" %s" % built["name"]) if built["name"] else ""),
            confirm_label="Create %s cache" % ctype,
            target_text="%s cache (%s)" % (ctype.capitalize(), label_name),
            description=(
                "Caches full query results with TTL/refresh expiry on Readyset."
                if ctype == "shallow"
                else "Materializes an incrementally maintained streaming dataflow cache on Readyset."
            ),
            statements=[built["statement"]],
            undo=("DROP CACHE %s;" % _rs_ident(built["name"], "Cache name")) if built["name"] else "DROP CACHE `<query_id>`;",
            expect={
                "readyset_version": report.get("version"),
                "cache_type": ctype,
                "name": built["name"],
                "target": built["target"],
                "collisions": collisions,
            },
        )
        plan["facts"].extend([
            ["Cache type", ctype.capitalize()],
            ["Options", ", ".join(built["options"]) if built["options"] else "Default"],
            ["From", built["target"][:140]],
        ])
        return plan

    if action in ("drop_cache", "flush_cache"):
        if not caps.get("can_manage_caches", True):
            raise ValueError("Cache management is disabled on this Readyset listener.")
        t = target if isinstance(target, dict) else {}
        tname = _text(t.get("name"))
        tqid = _text(t.get("query_id"))
        found = next(
            (c for c in caches if (tname and c.get("name") == tname) or (tqid and c.get("query_id") == tqid)),
            None,
        )
        if found is None:
            raise ValueError("That cache no longer exists on Readyset.")
        ident_name = found["name"] or found["query_id"]
        quoted = _rs_ident(ident_name, "Cache name")
        if action == "flush_cache":
            if found.get("cache_type") != "shallow":
                raise ValueError("FLUSH CACHE applies only to shallow caches.")
            plan.update(
                eyebrow="FLUSH CACHE",
                title="Flush shallow cache %s?" % ident_name,
                confirm_label="Flush cache",
                target_text="Shallow cache %s" % ident_name,
                description="Discards all currently stored entries for %s; the cache definition stays active and "
                            "subsequent queries refresh from upstream." % ident_name,
                statements=["FLUSH CACHE %s;" % quoted],
                undo="Entries repopulate automatically on the next matching read.",
                expect={
                    "readyset_version": report.get("version"),
                    "cache": {
                        "name": found["name"],
                        "query_id": found["query_id"],
                        "cache_type": found["cache_type"],
                    },
                },
            )
        else:
            plan.update(
                eyebrow="DROP CACHE",
                title="Drop %s cache %s?" % (found["cache_type"], ident_name),
                confirm_label="Drop cache",
                target_text="%s cache %s" % (found["cache_type"].capitalize(), ident_name),
                description="Removes cache %s so matching queries run directly on the upstream database." % ident_name,
                statements=["DROP CACHE %s;" % quoted],
                typed_guard=ident_name if found.get("cache_type") == "deep" else None,
                undo="Re-create from query %s." % (found.get("query_id") or ident_name),
                expect={
                    "readyset_version": report.get("version"),
                    "cache": {
                        "name": found["name"],
                        "query_id": found["query_id"],
                        "cache_type": found["cache_type"],
                        "properties": found.get("properties"),
                    },
                },
            )
        plan["facts"].extend([
            ["Cache", "%s (%s)" % (ident_name, found["cache_type"])],
            ["Query ID", found["query_id"] or "Unknown"],
            ["Query", (found.get("query") or "")[:140]],
        ])
        plan["verified"].append("cache %s unchanged since review" % ident_name)
        return plan

    if action == "flush_all_shallow_caches":
        shallow = [c for c in caches if c.get("cache_type") == "shallow"]
        if not shallow:
            raise ValueError("There are no shallow caches to flush.")
        plan.update(
            eyebrow="FLUSH CACHES",
            title="Flush all %d shallow %s?" % (len(shallow), "cache" if len(shallow) == 1 else "caches"),
            confirm_label="Flush shallow caches",
            target_text="%d shallow %s" % (len(shallow), "cache" if len(shallow) == 1 else "caches"),
            description="Evicts every cached entry across all shallow caches; cache definitions remain active.",
            statements=["FLUSH ALL SHALLOW CACHES;"],
            typed_guard="FLUSH ALL SHALLOW CACHES" if len(shallow) > 1 else None,
            undo="Entries repopulate on subsequent reads.",
            expect={"readyset_version": report.get("version"), "shallow_count": len(shallow)},
        )
        plan["warnings"].append("Upstream query load may spike while evicted shallow cache entries repopulate.")
        return plan

    if action in ("drop_all_caches", "drop_all_shallow_caches", "drop_all_deep_caches"):
        scope = "shallow" if action == "drop_all_shallow_caches" else (
            "deep" if action == "drop_all_deep_caches" else (
                value if value in ("shallow", "deep") else "all"
            )
        )
        matching = [c for c in caches if scope == "all" or c.get("cache_type") == scope]
        if not matching:
            raise ValueError("There are no %scaches to drop." % ("" if scope == "all" else scope + " "))
        stmt = "DROP ALL CACHES;" if scope == "all" else ("DROP ALL %s CACHES;" % scope.upper())
        guard = stmt[:-1]
        plan.update(
            eyebrow="DROP CACHES",
            title="Drop %d %s?" % (
                len(matching),
                ("cache" if len(matching) == 1 else "caches") if scope == "all"
                else ("%s %s" % (scope, "cache" if len(matching) == 1 else "caches")),
            ),
            confirm_label="Drop caches",
            target_text="%d %s" % (len(matching), "cache" if len(matching) == 1 else "caches"),
            description="Removes every matching cache definition; queries immediately fall back to the upstream database.",
            statements=[stmt],
            changes=["%s (%s, %s)" % (c["name"], c["cache_type"], c["query_id"]) for c in matching[:25]],
            typed_guard=guard,
            undo="Export cache DDL (EXPLAIN ALL CACHES) before dropping if you may need to recreate them.",
            expect={"readyset_version": report.get("version"), "scope": scope, "caches_fp": prints.get("caches")},
        )
        plan["warnings"].append("Every traffic surge absorbed by these caches will hit the upstream database immediately.")
        return plan

    if action == "drop_all_proxied_queries":
        count = len(report.get("proxied_queries") or ())
        plan.update(
            eyebrow="PROXIED QUERIES",
            title="Clear recorded proxied queries?",
            confirm_label="Clear proxied queries",
            target_text="Proxied query catalog (%d shown)" % count,
            description="Clears Readyset's catalog of observed uncached proxied queries. Active caches are untouched.",
            statements=["DROP ALL PROXIED QUERIES;"],
            typed_guard="DROP ALL PROXIED QUERIES",
            undo="Cannot be undone; Readyset records proxied queries again as new traffic arrives.",
            expect={"readyset_version": report.get("version")},
        )
        return plan

    if action in ("enter_maintenance_mode", "exit_maintenance_mode"):
        entering = action == "enter_maintenance_mode"
        cur_status = sf.get("status") or "Online"
        if entering and cur_status == "Maintenance Mode":
            raise ValueError("Readyset is already in Maintenance Mode.")
        if not entering and cur_status != "Maintenance Mode":
            raise ValueError("Readyset is not in Maintenance Mode (current status: %s)." % cur_status)
        stmt = "ALTER READYSET %s MAINTENANCE MODE;" % ("ENTER" if entering else "EXIT")
        plan.update(
            eyebrow="MAINTENANCE MODE",
            title="Enter Readyset maintenance mode?" if entering else "Exit Readyset maintenance mode?",
            confirm_label="Enter maintenance mode" if entering else "Exit maintenance mode",
            description=(
                "Stops serving queries from Readyset caches and proxies traffic directly to upstream."
                if entering
                else "Resumes serving eligible queries from Readyset caches."
            ),
            statements=[stmt],
            typed_guard="MAINTENANCE MODE" if entering else None,
            undo="ALTER READYSET %s MAINTENANCE MODE;" % ("EXIT" if entering else "ENTER"),
            expect={"readyset_version": report.get("version"), "status": cur_status},
        )
        plan["facts"].append(["Status", "%s → %s" % (cur_status, "Maintenance Mode" if entering else "Online")])
        return plan

    params = {}
    if isinstance(target, dict):
        params.update(target)
    if isinstance(value, dict):
        params.update(value)

    if action == "set_log_level":
        raw_lvl = params.get("directives") if "directives" in params else value
        lit = _rs_string(raw_lvl, "Log level directives", max_len=256)
        stmt = "ALTER READYSET SET LOG LEVEL %s;" % lit
        plan.update(
            eyebrow="PARAMETER",
            title="Set Readyset log level to %s?" % lit,
            confirm_label="Set log level",
            description="Updates Readyset's runtime tracing filter immediately (for example 'info' or "
                        "'info,readyset_adapter=debug').",
            statements=[stmt],
            undo="ALTER READYSET SET LOG LEVEL 'info';",
            expect={"readyset_version": report.get("version")},
        )
        plan["facts"].append(["Log directives", str(raw_lvl).strip()])
        plan["warnings"].append("Runtime change only: resets to the startup --log-level value if Readyset restarts.")
        return plan

    if action == "set_eviction_memory_limit":
        raw_bytes = params.get("bytes") if "bytes" in params else value
        nbytes = _strict_int(raw_bytes, 0, _MAX_BYTES, "Eviction memory limit (bytes)")
        stmt = "ALTER READYSET SET EVICTION MEMORY LIMIT %d;" % nbytes
        mb = round(nbytes / (1024.0 * 1024.0), 2)
        plan.update(
            eyebrow="PARAMETER",
            title="Set eviction memory limit to %s bytes (%.2f MiB)?" % ("{:,}".format(nbytes), mb),
            confirm_label="Set memory limit",
            description="Updates the runtime memory threshold at which Readyset evicts cached entries.",
            statements=[stmt],
            typed_guard="0" if nbytes == 0 else None,
            undo="Run ALTER READYSET SET EVICTION MEMORY LIMIT <previous_bytes>; to restore.",
            expect={"readyset_version": report.get("version")},
        )
        if nbytes == 0:
            plan["warnings"].append("A limit of 0 bytes aggressively evicts cached values.")
        plan["warnings"].append("Runtime change only: update startup flags to persist across restarts.")
        plan["facts"].append(["Memory limit", "%s bytes (%.2f MiB)" % ("{:,}".format(nbytes), mb)])
        return plan

    if action == "set_eviction_period":
        raw_ms = params.get("period_ms") if "period_ms" in params else (
            params.get("milliseconds") if "milliseconds" in params else value
        )
        ms = _strict_int(raw_ms, 1, _MAX_PERIOD_MS, "Eviction period (ms)")
        stmt = "ALTER READYSET SET EVICTION PERIOD %d;" % ms
        plan.update(
            eyebrow="PARAMETER",
            title="Set eviction check period to %s ms?" % "{:,}".format(ms),
            confirm_label="Set eviction period",
            description="Sets how often Readyset runs its cache memory eviction sweep.",
            statements=[stmt],
            undo="Run ALTER READYSET SET EVICTION PERIOD <previous_ms>; to restore.",
            expect={"readyset_version": report.get("version")},
        )
        plan["warnings"].append("Runtime change only: update startup flags to persist across restarts.")
        plan["facts"].append(["Eviction period", "%s ms" % "{:,}".format(ms)])
        return plan

    if action in ("add_shallow_allowed", "drop_shallow_allowed"):
        adding = action == "add_shallow_allowed"
        src = target if isinstance(target, dict) else {}
        val_dict = value if isinstance(value, dict) else {}
        kind_key = (params.get("allow_kind") or params.get("kind") or "function").strip().lower()
        if kind_key not in SHALLOW_ALLOWLIST_KINDS:
            raise ValueError("Choose allowlist kind 'function', 'variable' or 'schema'.")
        singular, _, list_key = SHALLOW_ALLOWLIST_KINDS[kind_key]
        raw_item = (
            src.get("name") or src.get("identifier")
            if not adding and (src.get("name") or src.get("identifier"))
            else (params.get("name") or params.get("identifier") if params else value)
        )
        if not isinstance(raw_item, str) or not raw_item.strip():
            raise ValueError("Pass the %s name." % kind_key)
        clean_item = raw_item.strip()
        quoted_item = _rs_ident(clean_item, "%s name" % kind_key.capitalize())
        current_items = list((report.get("shallow_allowlists") or {}).get(list_key) or ())
        exists = any(x.lower() == clean_item.lower() for x in current_items)
        if adding and exists:
            raise ValueError("%s %s is already in the shallow cache allowlist." % (kind_key.capitalize(), clean_item))
        if not adding and not exists:
            raise ValueError("%s %s is not in the shallow cache allowlist." % (kind_key.capitalize(), clean_item))
        verb = "ADD" if adding else "DROP"
        undo_verb = "DROP" if adding else "ADD"
        stmt = "ALTER READYSET %s SHALLOW CACHE ALLOWED %s %s;" % (verb, singular, quoted_item)
        plan.update(
            eyebrow="SHALLOW ALLOWLIST",
            title="%s shallow cache allowed %s %s?" % ("Add" if adding else "Remove", kind_key, clean_item),
            confirm_label="%s %s" % ("Allow" if adding else "Remove", clean_item),
            target_text="Shallow cache allowed %ss" % kind_key,
            description=(
                "Allows shallow caches to cache queries referencing %s %s." % (kind_key, clean_item)
                if adding
                else "Stops new shallow caches from referencing %s %s." % (kind_key, clean_item)
            ),
            statements=[stmt],
            undo="ALTER READYSET %s SHALLOW CACHE ALLOWED %s %s;" % (undo_verb, singular, quoted_item),
            expect={
                "readyset_version": report.get("version"),
                "kind": kind_key,
                "item": clean_item,
                "allowlists_fp": prints.get("allowlists"),
            },
        )
        plan["facts"].append([kind_key.capitalize(), clean_item])
        return plan

    if action == "flush_privileges":
        plan.update(
            eyebrow="PRIVILEGES",
            title="Reload Readyset cached privileges?",
            confirm_label="Flush privileges",
            description="Refreshes Readyset's cached upstream user credentials and table ACLs immediately "
                        "(ALTER READYSET FLUSH PRIVILEGES).",
            statements=["ALTER READYSET FLUSH PRIVILEGES;"],
            undo="No undo needed: refreshes Readyset's ACL cache from upstream.",
            expect={"readyset_version": report.get("version")},
        )
        return plan

    if action == "resnapshot_table":
        raw_tbl = (target.get("table") if isinstance(target, dict) else None) or (
            value.get("table") if isinstance(value, dict) else value
        )
        if not isinstance(raw_tbl, str) or not raw_tbl.strip():
            raise ValueError("Choose a replicated table to re-snapshot.")
        tname = raw_tbl.strip()
        quoted_tbl = _rs_table_ident(tname)
        found_tbl = next(
            (t for t in (report.get("tables") or ()) if t.get("table") and _rs_table_ident(t["table"]) == quoted_tbl),
            None,
        )
        if found_tbl is None:
            raise ValueError("Table %s is not in Readyset's table list." % tname)
        stmt = "ALTER READYSET RESNAPSHOT TABLE %s;" % quoted_tbl
        plan.update(
            eyebrow="RESNAPSHOT",
            title="Re-snapshot table %s?" % tname,
            confirm_label="Re-snapshot %s" % tname,
            target_text="Base table %s" % tname,
            description="Drops Readyset's local copy of %s and reads a fresh snapshot from the upstream CDC server. "
                        "Dependent deep caches on %s are dropped and must be re-created once Status returns to Online."
                        % (tname, tname),
            statements=[stmt],
            typed_guard=tname,
            undo="Cannot be cancelled once started; wait for Status to return to Online, then re-create any dropped deep caches.",
            expect={"readyset_version": report.get("version"), "table": tname, "table_status": found_tbl.get("status")},
        )
        plan["warnings"].extend([
            "Reads a full table scan of %s on the upstream CDC database and pauses deep cache DDL until snapshotting finishes."
            % tname,
            "Dependent deep caches on %s are dropped when the base table resets; use Export cache DDL beforehand so you can re-create them once %s is Online."
            % (tname, tname),
        ])
        plan["facts"].extend([
            ["Table", tname],
            ["Current status", found_tbl.get("status") or "Unknown"],
        ])
        return plan

    if action == "add_tables":
        raw_val = params.get("tables") if "tables" in params else value
        if isinstance(raw_val, list):
            raw_items = [str(x).strip() for x in raw_val if str(x).strip()]
        elif isinstance(raw_val, str):
            raw_items = [x.strip() for x in raw_val.split(",") if x.strip()]
        else:
            raw_items = []
        if not raw_items or len(raw_items) > 32:
            raise ValueError("Pass 1 to 32 table names (schema.table) to add.")
        quoted_list = [_rs_table_ident(x) for x in raw_items]
        stmt = "ALTER READYSET ADD TABLES %s;" % ", ".join(quoted_list)
        plan.update(
            eyebrow="REPLICATION TABLES",
            title="Add %d %s to Readyset replication?" % (
                len(raw_items), "table" if len(raw_items) == 1 else "tables"
            ),
            confirm_label="Add tables",
            target_text=", ".join(raw_items[:4]) + ("…" if len(raw_items) > 4 else ""),
            description="Starts snapshotting and replicating the specified upstream base tables into Readyset.",
            statements=[stmt],
            expect={"readyset_version": report.get("version"), "tables": raw_items, "tables_fp": prints.get("tables")},
        )
        plan["warnings"].append("Each added table runs an initial snapshot query against the upstream CDC server.")
        plan["facts"].append(["Tables", ", ".join(raw_items)])
        return plan

    if action in ("stop_replication", "start_replication"):
        stopping = action == "stop_replication"
        cur_repl = sf.get("replication_status") or "Unknown"
        if stopping and cur_repl != "Running":
            raise ValueError("Replication is not Running (current state: %s)." % cur_repl)
        if not stopping and cur_repl != "Stopped":
            raise ValueError("Replication is not Stopped (current state: %s)." % cur_repl)
        stmt = "ALTER READYSET %s REPLICATION;" % ("STOP" if stopping else "START")
        plan.update(
            eyebrow="REPLICATION",
            title="Stop Readyset upstream replication?" if stopping else "Start Readyset upstream replication?",
            confirm_label="Stop replication" if stopping else "Start replication",
            description=(
                "Pauses ingesting binlog changes from the upstream CDC server; deep caches stop receiving updates."
                if stopping
                else "Resumes ingesting binlog changes from the upstream CDC server."
            ),
            statements=[stmt],
            typed_guard="STOP REPLICATION" if stopping else None,
            undo="ALTER READYSET %s REPLICATION;" % ("START" if stopping else "STOP"),
            expect={"readyset_version": report.get("version"), "replication_status": cur_repl},
        )
        plan["facts"].append(["Replication Status", "%s → %s" % (cur_repl, "Stopped" if stopping else "Running")])
        return plan

    if action == "set_replication_position":
        raw_pos = params.get("position") if "position" in params else value
        pos_lit = _rs_string(raw_pos, "Replication position", max_len=2048)
        stmt = "ALTER READYSET SET REPLICATION POSITION %s;" % pos_lit
        cur_repl = sf.get("replication_status") or "Unknown"
        plan.update(
            eyebrow="REPLICATION RECOVERY",
            title="Set Readyset replication position?",
            confirm_label="Set position",
            description="Sets the upstream binlog or GTID position from which Readyset's replicator resumes.",
            statements=[stmt],
            typed_guard="SET REPLICATION POSITION",
            undo="Record Maximum Replication Offset (%s) before changing; an invalid skip requires re-snapshotting."
                 % (sf.get("max_replication_offset") or "unknown"),
            expect={
                "readyset_version": report.get("version"),
                "replication_status": cur_repl,
                "max_offset": sf.get("max_replication_offset"),
            },
        )
        plan["warnings"].append(
            "Skipping transactions leaves Readyset's base tables inconsistent with upstream unless those tables "
            "were untouched or are re-snapshotted afterwards."
        )
        plan["facts"].extend([
            ["Current max offset", sf.get("max_replication_offset") or "Unknown"],
            ["New position", str(raw_pos).strip()[:160]],
        ])
        return plan

    if action in ("shutdown", "shutdown_reset"):
        if not caps.get("can_shutdown"):
            raise ValueError(
                "ALTER READYSET SHUTDOWN is only accepted on the Readyset admin SQL port (default 6036)."
            )
        reset = action == "shutdown_reset"
        stmt = "ALTER READYSET SHUTDOWN RESET;" if reset else "ALTER READYSET SHUTDOWN;"
        guard = "SHUTDOWN RESET" if reset else "SHUTDOWN"
        plan.update(
            eyebrow="SHUTDOWN",
            title="Shut down Readyset and reset snapshot state?" if reset else "Shut down Readyset?",
            confirm_label="Shut down & reset" if reset else "Shut down Readyset",
            description=(
                "Stops the Readyset process and discards persistent snapshot state so the next start performs a full "
                "re-snapshot from upstream."
                if reset
                else "Stops the Readyset process cleanly."
            ),
            statements=[stmt],
            typed_guard=guard,
            undo="Start the Readyset service externally through your supervisor, container runtime or orchestrator.",
            expect={"readyset_version": report.get("version"), "port_role": report.get("port_role")},
        )
        plan["warnings"].append("myflames cannot start Readyset again once the process exits.")
        if reset:
            plan["warnings"].append(
                "SHUTDOWN RESET deletes persisted snapshot state: restarting Readyset will scan all replicated "
                "tables on the upstream database."
            )
        return plan

    raise ValueError("Unknown Readyset action %r." % (action,))


def _snapshot_state(report):
    sf = report.get("status_fields") or {}
    caches = report.get("caches") or []
    tables = report.get("tables") or []
    return {
        "status": sf.get("status"),
        "replication_status": sf.get("replication_status"),
        "max_offset": sf.get("max_replication_offset"),
        "cache_count": len(caches),
        "caches": dict((c["name"], c["query_id"]) for c in caches if c.get("name")),
        "shallow_entries_hits": sum((c.get("hits") or 0) for c in caches if c.get("cache_type") == "shallow"),
        "proxied_count": len(report.get("proxied_queries") or ()),
        "tables": dict((t["table"], t["status"]) for t in tables if t.get("table")),
        "allowlists": dict(report.get("shallow_allowlists") or {}),
    }


def _effect(plan, before, after):
    rows = []
    if not before or not after:
        return rows
    if before.get("status") != after.get("status"):
        rows.append(["Readyset status", str(before.get("status") or "Unknown"), str(after.get("status") or "Unknown")])
    if before.get("replication_status") != after.get("replication_status"):
        rows.append([
            "Replication status",
            str(before.get("replication_status") or "Unknown"),
            str(after.get("replication_status") or "Unknown"),
        ])
    if before.get("max_offset") != after.get("max_offset") and plan.get("action") == "set_replication_position":
        rows.append([
            "Replication offset",
            str(before.get("max_offset") or "Unknown"),
            str(after.get("max_offset") or "Unknown"),
        ])
    if before.get("cache_count") != after.get("cache_count") or before.get("caches") != after.get("caches"):
        rows.append(["Caches", str(before.get("cache_count", 0)), str(after.get("cache_count", 0))])
    if plan.get("action") == "drop_all_proxied_queries" and before.get("proxied_count") != after.get("proxied_count"):
        rows.append(["Proxied queries", str(before.get("proxied_count", 0)), str(after.get("proxied_count", 0))])
    if before.get("tables") != after.get("tables"):
        changed = [
            k for k in set(list((before.get("tables") or {}).keys()) + list((after.get("tables") or {}).keys()))
            if (before.get("tables") or {}).get(k) != (after.get("tables") or {}).get(k)
        ]
        for tbl in sorted(changed)[:5]:
            rows.append([
                "Table %s" % tbl,
                str((before.get("tables") or {}).get(tbl) or "Absent"),
                str((after.get("tables") or {}).get(tbl) or "Absent"),
            ])
    if before.get("allowlists") != after.get("allowlists"):
        for k in ("functions", "variables", "schemas"):
            bset = list((before.get("allowlists") or {}).get(k) or ())
            aset = list((after.get("allowlists") or {}).get(k) or ())
            if bset != aset:
                rows.append(["Allowed %s" % k, ", ".join(bset) or "None", ", ".join(aset) or "None"])
    return rows


def _done_text(plan, after):
    action = plan.get("action")
    if action in ("shutdown", "shutdown_reset"):
        return "Shutdown command sent to Readyset."
    if action == "create_cache":
        return "Cache created on Readyset (%s caches active)." % (after or {}).get("cache_count", "?")
    if action == "resnapshot_table":
        return "Re-snapshot started; Readyset status is %s." % ((after or {}).get("status") or "updating")
    return "Applied on Readyset."


def execute_action(conn, reviewed, audit=None, typed=None, before_send=None, progress=None):
    """Re-read Readyset, verify reviewed expectations, execute, and verify post-state
    (including after single-statement server errors that may have partially mutated state)."""
    from . import reviewed_writes
    reviewed = reviewed_writes.check_reviewed(reviewed)
    if audit is not None:
        audit.server("readyset", (reviewed.get("expect") or {}).get("readyset_version"))
    state = {}
    is_shutdown = reviewed.get("action") in ("shutdown", "shutdown_reset")

    def fresh():
        report = read_status(conn)
        if not report.get("is_readyset"):
            raise ValueError("This connection is no longer a Readyset instance.")
        state["before"] = _snapshot_state(report)
        return plan_action(report, reviewed["action"], reviewed.get("target"), reviewed.get("value"), conn=conn)

    def read_after():
        try:
            rep = read_status(conn)
            return _snapshot_state(rep)
        except ConnectorError as exc:
            if is_shutdown and any(code in str(exc) for code in ("2002", "2003", "2006", "2013")):
                return {"status": "Shut down", "replication_status": "Stopped", "cache_count": 0, "caches": {}}
            raise

    return reviewed_writes.execute(
        conn, reviewed, fresh, ACTION_SCHEMA_VERSION,
        audit=audit, typed=typed, before_send=before_send,
        read_after=read_after, timeout=ACTION_TIMEOUT,
        done_text=_done_text, progress=progress,
        effect=lambda plan, after: _effect(plan, state.get("before") or {}, after),
        verify_on_error=not is_shutdown,
    )


# ---- CLI & MCP text formatter ------------------------------------------------------------

def format_text(report):
    """Compact, plain-text summary of a ``readyset-1.0`` report for CLI and MCP."""
    if not report or not report.get("is_readyset"):
        msg = (report or {}).get("message") or "Not a Readyset instance."
        return "Readyset: unavailable — %s\n" % terminal_text(msg)

    sf = report.get("status_fields") or {}
    tel = report.get("telemetry") or {}
    repl = report.get("replication") or {}
    tcounts = report.get("table_counts") or {}
    caches = report.get("caches") or []
    upstreams = report.get("upstreams") or []

    lines = [
        "Readyset %s (%s port) — status: %s (%s)" % (
            terminal_text(report.get("version") or "unknown"),
            terminal_text(report.get("port_role") or "sql"),
            terminal_text((report.get("status") or "unknown").upper()),
            terminal_text(sf.get("status") or "Online"),
        )
    ]
    for why in report.get("status_why") or ():
        lines.append("  ! %s" % terminal_text(why))

    lag_str = ""
    if repl.get("consume_lag") is not None:
        lag_str = ", consume lag %s %s" % (repl["consume_lag"], repl.get("lag_unit") or "units")
    lines.append(
        "Replication: %s%s%s" % (
            terminal_text(repl.get("status") or "Unknown"),
            (" from %s" % terminal_text(repl["cdc_server"])) if repl.get("cdc_server") else "",
            lag_str,
        )
    )
    share = ("%.1f%% cached share" % tel["cached_share_pct"]) if tel.get("cached_share_pct") is not None else "no traffic yet"
    lines.append(
        "Telemetry: %d cached / %d upstream observed executions (%s); shallow hits %d, misses %d%s" % (
            tel.get("cached_executions", 0),
            tel.get("upstream_executions", 0),
            share,
            tel.get("shallow_hits", 0),
            tel.get("shallow_misses", 0),
            (", hit rate %.1f%%" % tel["shallow_hit_rate_pct"]) if tel.get("shallow_hit_rate_pct") is not None else "",
        )
    )
    lines.append(
        "Objects: %d caches (%d shallow, %d deep), %d proxied queries, %d tables (%d snapshotted, %d snapshotting, %d errors)" % (
            len(caches),
            sum(1 for c in caches if c.get("cache_type") == "shallow"),
            sum(1 for c in caches if c.get("cache_type") == "deep"),
            len(report.get("proxied_queries") or ()),
            tcounts.get("total", 0),
            tcounts.get("snapshotted", 0),
            tcounts.get("snapshotting", 0),
            tcounts.get("error", 0),
        )
    )
    if upstreams:
        lines.append("Upstream servers:")
        for u in upstreams[:10]:
            lines.append("  - %s [%s] health=%s%s" % (
                terminal_text(u["address"]),
                terminal_text(u.get("purpose") or "proxy"),
                terminal_text(u.get("health") or "unknown"),
                " (active CDC)" if u.get("active_cdc") else "",
            ))
    topo = report.get("topology") or {}
    for px in topo.get("proxysql_nodes") or ():
        lines.append("ProxySQL tier: %s (%s, mode=%s)" % (
            terminal_text(px.get("label") or px.get("address")),
            terminal_text(px.get("address") or ""),
            terminal_text(px.get("routing_mode") or "backend"),
        ))
    for peer in topo.get("database_peers") or ():
        lines.append("Database peer: %s [%s] %s" % (
            terminal_text(peer.get("name") or peer.get("address") or "?"),
            terminal_text(peer.get("relation") or "peer"),
            terminal_text(peer.get("product") or ""),
        ))
    if caches:
        lines.append("Caches:")
        for c in caches[:20]:
            p50 = (c.get("cached_stats") or {}).get("p50_ms")
            lat = (" p50=%.2fms" % p50) if p50 is not None else ""
            lines.append("  - %s [%s] %s count=%s%s" % (
                terminal_text(c["name"]),
                terminal_text(c["cache_type"]),
                terminal_text(c["query_id"]),
                c.get("count") if c.get("count") is not None else "n/a",
                lat,
            ))
    return "\n".join(lines) + "\n"
