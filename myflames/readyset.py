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
import copy
import hashlib
import heapq
import json
import re
import unicodedata

from . import dbops
from .connection_groups import grouped_read
from .connector import ConnectorError, terminal_text
from .perf_common import named_rows, now
from .reviewed_writes import ActionUnavailable
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


def _display(value):
    """A server value safe for a finding title or action reason: no control or
    format characters (a bidi override could make the text read as another),
    at most 80 characters."""
    return "".join(c for c in str(value) if not _has_ctl_or_fmt(c))[:80]


def _has_ctl_or_fmt(value):
    """True for controls, format characters and Unicode line/paragraph
    separators, which could reorder or split the reviewed target visually."""
    return any(ch in "\u2028\u2029" or unicodedata.category(ch) in ("Cc", "Cf") for ch in value)


def _review_text(value):
    """Make control/format characters visible without concealing name bytes.

    Review prose uses this helper; SQL, exact typed guards and state used for
    comparison retain their original values.
    """
    return "".join(
        ("\\u%04x" % ord(ch) if ord(ch) <= 0xffff else "\\U%08x" % ord(ch))
        if _has_ctl_or_fmt(ch) else ch
        for ch in value
    )


def _finish_plan(plan):
    """Format only review display fields, never statements or expected state."""
    def shown(value):
        if isinstance(value, str):
            return _review_text(value)
        if isinstance(value, list):
            return [shown(item) for item in value]
        return value

    guard = plan.get("typed_guard")
    if isinstance(guard, str) and guard != guard.strip(" "):
        plan["warnings"].append(
            "The name starts or ends with a space: type it exactly, spaces included."
        )
    for key in ("eyebrow", "title", "confirm_label", "description", "target_text",
                "facts", "warnings", "changes", "undo", "next", "verified", "checks"):
        if key in plan:
            plan[key] = shown(plan[key])
    return plan


def _us_to_ms(value):
    num = _float(value)
    return round(num / 1000.0, 3) if num is not None else None


def _rs_ident_parse(value, label="Identifier"):
    """The exact identifier a user typed: validates like :func:`_rs_ident` and
    resolves one pair of quoting backticks (collapsing doubled backticks inside
    it), so callers compare the identifier the generated SQL names, never the
    raw typed text."""
    if not isinstance(value, str):
        raise ValueError("%s is required." % label)
    s = value.strip()
    if not s or len(s) > 128 or "\x00" in s or "\r" in s or "\n" in s:
        raise ValueError("%s must be 1 to 128 characters without line breaks." % label)
    if s.startswith("`") and s.endswith("`") and len(s) >= 2:
        s = s[1:-1].replace("``", "`")
    if not s:
        raise ValueError("%s cannot be empty." % label)
    return s


def _rs_ident(value, label="Identifier"):
    """Quote one user-typed SQL identifier with backticks for Readyset SQL.

    Typed text may already carry one pair of quoting backticks; strip that
    pair (collapsing doubled backticks inside it), then quote exactly once."""
    return "`%s`" % _rs_ident_parse(value, label).replace("`", "``")


def _rs_ident_server(value, label="Identifier"):
    """Quote an identifier exactly as the server reported it.

    SHOW CACHES prints cache names unquoted (Readyset
    readyset-adapter/src/backend/extensions.rs:1113), so every character,
    backticks and whitespace included, is part of the name: wrap in backticks
    and double inner backticks, never strip a pair or trim whitespace."""
    if not isinstance(value, str):
        raise ValueError("%s is required." % label)
    if not value or len(value) > 128 or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("%s must be 1 to 128 characters without line breaks." % label)
    return "`%s`" % value.replace("`", "``")


def _rs_table_ident(value):
    """Quote ``table`` or ``schema.table`` for ``RESNAPSHOT TABLE`` / ``ADD TABLES``.

    Parses the value once, accepting a plain ``schema.table`` or the quoted
    form SHOW READYSET ALL TABLES prints (Readyset quotes every part and
    doubles inner backticks: readyset-sql/src/ast/table.rs:122-131 with
    dialect.rs:65-72), then quotes each parsed part exactly once. Parts are
    raw names after parsing, so the server-safe quoter is used: a literal
    backtick at either end of a part stays part of the name."""
    if not isinstance(value, str):
        raise ValueError("Pass a table name (table or schema.table).")
    # Bound work before stripping or parsing untrusted server names. This
    # still fits two 128-character parts with every backtick doubled (517).
    if len(value) > 600:
        raise ValueError("Table name must be at most 600 characters.")
    raw = value.strip()
    if not raw:
        raise ValueError("Pass a table name (table or schema.table).")
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise ValueError("Table name cannot contain line breaks or NUL characters.")
    # Split unquoted schema.table or `schema`.`table`. Each character is
    # tracked as quoted or not: whitespace inside a quoted component is part
    # of the name and stays; only whitespace around unquoted input is dropped.
    parts = []
    cur = []
    in_bt = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "`":
            if in_bt and i + 1 < len(raw) and raw[i + 1] == "`":
                cur.append(("`", True))
                i += 2
                continue
            in_bt = not in_bt
            i += 1
            continue
        if ch == "." and not in_bt:
            parts.append(cur)
            cur = []
            i += 1
            continue
        cur.append((ch, in_bt))
        i += 1
    if in_bt:
        raise ValueError("Unclosed backtick in table name.")
    parts.append(cur)

    def _part_text(chars):
        start, end = 0, len(chars)
        while start < end and not chars[start][1] and chars[start][0].isspace():
            start += 1
        while end > start and not chars[end - 1][1] and chars[end - 1][0].isspace():
            end -= 1
        return "".join(c for c, _ in chars[start:end])

    parts = [_part_text(p) for p in parts]
    if not 1 <= len(parts) <= 2 or any(not p for p in parts):
        raise ValueError("Pass a valid table or schema.table name.")
    return ".".join(_rs_ident_server(p, "Table name") for p in parts)


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
    Connection failures raise :class:`ConnectorError`. Server error text is
    redacted with the connection password: it is shown to the user.
    """
    pw = getattr(conn, "password", None)
    try:
        rows = select(conn, "SELECT @@version_comment")
    except ConnectorError as exc:
        from .reviewed_writes import client_error, error_number
        code = error_number(str(exc))
        if code is None or client_error(code) or "Access denied" in str(exc):
            raise
        return False, "SELECT @@version_comment failed (%s)" % _short(dbops.redact(str(exc), pw, quoted=False))
    comment = _text(next(iter(rows[0].values()))) if rows and rows[0] else ""
    if not is_readyset_comment(comment):
        shown_comment = _display(dbops.redact(comment, pw, quoted=False))[:60] if comment else "empty"
        return False, "@@version_comment is %s, not Readyset" % shown_comment
    try:
        vrows = select(conn, "SHOW READYSET VERSION")
    except ConnectorError as exc:
        return False, "SHOW READYSET VERSION failed (%s)" % _short(dbops.redact(str(exc), pw, quoted=False))
    version = parse_readyset_version(vrows) or "unknown"
    return True, _display(dbops.redact(version, pw, quoted=False))


def _detect_port_mode(conn, schema_name, timeout=None):
    """Return ``(port_role, in_readyset_schema)`` from listener evidence, never port numbers.

    On the Admin SQL listener (default 6036), Readyset initializes the session's
    search path to ``[readyset_schema]`` and sets ``readyset_schema_route_all = true``
    even when no default database was passed, and refuses any other ``USE``/``-D``.
    On the Adapter SQL listener (default 3307), ``readyset_schema_route_all`` is
    true only when the session's default database is ``readyset_schema``.

    *port_role* is ``None`` ("not read") when the probe failed without proving
    the role: with no default database, only MySQL error 1046 (No database
    selected) or 1146 (table doesn't exist) proves the adapter answered, because
    the admin listener resolves the unqualified virtual table; any other failure
    (a timeout, a lost connection) says nothing. When the readyset schema is
    selected, probe a copy with no default database: both listeners otherwise
    route SHOW DATABASES to DataFusion (backend.rs:1494-1548). The caller's
    connection, including its selected database, must remain unchanged.
    """
    from .reviewed_writes import error_number
    db = (getattr(conn, "database", None) or "").strip()
    in_rs_schema = db.lower() == schema_name.lower()
    probe_conn = conn
    if in_rs_schema:
        try:
            probe_conn = copy.copy(conn)
            if probe_conn is conn:
                return None, True
            # A shallow copy must not own the original connection's cleanup
            # resources: MySQLConnection.__del__ closes leases and its legacy
            # defaults file. The probe acquires its own transport if needed.
            for name in ("_held_lease", "_legacy_lease", "_defaults_file_path"):
                setattr(probe_conn, name, None)
            probe_conn.database = None
            if getattr(probe_conn, "database", None) is not None:
                return None, True
        except Exception:
            # Some connection wrappers cannot be copied or have a read-only
            # database property. No role can be established without this probe.
            return None, True
    if not db or in_rs_schema:
        try:
            select(probe_conn, "SELECT 1 FROM replication_status LIMIT 0", timeout=timeout)
            return "admin", True
        except ConnectorError as exc:
            if error_number(str(exc)) in (1046, 1146):
                return "adapter", in_rs_schema
            return None, in_rs_schema
        finally:
            # Retain attempted group logins even when the probe fails, so
            # consent accounting cannot refund an attempt that reached spawn.
            if probe_conn is not conn and getattr(probe_conn, "_group_login_started", False):
                conn._group_login_started = True
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
            self.missing.append((label or sql, _short(dbops.redact(
                str(exc), getattr(self.conn, "password", None), quoted=False))))
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
        "session_database": None,
        "message": message,
        "status": "unknown",
        "status_why": [message] if message else [],
        "findings": [],
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
        "allowlist_actions": {"functions": [], "variables": [], "schemas": []},
        "allowlist_item_actions": {"functions": [], "variables": [], "schemas": []},
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


def _classify_table_status(status_text, description=None):
    """Bucket a ``SHOW READYSET ALL TABLES`` status.

    Readyset reports exactly Initializing, Snapshotting, Compacting, Creating
    index, Online, Not replicated or Dropped (readyset-client/src/table.rs
    242-264); a table that stopped replicating because of an error stays
    "Not replicated" and carries a description starting "An unexpected
    replication error occurred:" (readyset-sql/src/ast/table.rs 411-413).
    """
    low = (_text(status_text) or "").lower()
    if low == "online":
        return "snapshotted"
    if low in ("initializing", "snapshotting", "compacting", "creating index"):
        return "snapshotting"
    if low == "not replicated":
        if (_text(description) or "").startswith("An unexpected replication error occurred:"):
            return "error"
        return "ignored"
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


@grouped_read
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
    # The connection's default database exactly as given (None when empty): the
    # workspace compares it with the connection in use to catch a stale report.
    session_db = getattr(conn, "database", None)
    report.update({
        "available": True,
        "is_readyset": True,
        "port_role": port_role,
        "in_readyset_schema": in_rs_schema,
        "readyset_schema": schema_name,
        "session_database": session_db if isinstance(session_db, str) and session_db else None,
        "message": None,
        "status_why": [],
        "statements": ["SELECT @@version_comment", "SHOW READYSET VERSION"],
    })

    # Core SHOW READYSET commands
    pw = getattr(conn, "password", None)
    status_rows = reads.query("SHOW READYSET STATUS", "SHOW READYSET STATUS") or []
    raw_status_map = _parse_kv_rows(status_rows)
    status_map = dict(
        (k, dbops.redact(v, pw, quoted=False) if isinstance(v, str) else v)
        for k, v in raw_status_map.items()
    )
    report["status_fields"] = {
        "database_connection": _text(status_map.get("Database Connection")),
        "connection_count": _int(status_map.get("Connection Count")),
        "status": _text(status_map.get("Status")),
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
        "can_explain_cache": port_role == "adapter" and not in_rs_schema,
        "can_create_cache": port_role == "adapter" and not in_rs_schema and not cache_ddl_blocked,
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
                raw_val = next(iter(r.values())) if r else None
                # Preserve boundary controls and spaces for the row gate;
                # stripping could turn an unsafe reported name into another.
                val = str(raw_val) if raw_val is not None else None
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
            "upstream": dbops.redact(_text(r.get("upstream")), pw, quoted=False) if _text(r.get("upstream")) else None,
        }
        for r in conn_rows
    ]

    # Tables
    table_rows = reads.query("SHOW READYSET ALL TABLES", "SHOW READYSET ALL TABLES") or []
    tables = []
    counts = {"total": 0, "snapshotted": 0, "snapshotting": 0, "ignored": 0, "error": 0, "other": 0}
    for r in table_rows:
        raw_name = r.get("table")
        tname = str(raw_name) if raw_name is not None else None
        if not tname:
            continue
        tstatus = _text(r.get("status")) or "Unknown"
        tdesc = _text(r.get("description"))
        bucket = _classify_table_status(tstatus, tdesc)
        counts["total"] += 1
        counts[bucket] += 1
        tables.append({
            "table": tname,
            "status": tstatus,
            "bucket": bucket,
            "description": dbops.redact(tdesc, pw, quoted=False),
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
        # Name and database stay byte-exact as reported: they feed SQL quoting
        # and row matching, where trimming could select a different cache.
        raw_name = r.get("name")
        name = str(raw_name) if raw_name not in (None, "") else qid
        raw_db = r.get("database")
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
            "query": dbops.redact(
                r.get("query") or r.get("query string") or sh.get("query") or dp.get("query") or "", pw),
            "properties": props,
            "database": str(raw_db) if raw_db not in (None, "") else None,
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
                "query": dbops.redact(r.get("query") or r.get("proxied query") or "", pw),
                "count": _int(r.get("count")),
                "deep_support": sup,
                "deep_reason": dbops.redact(reason_txt, pw, quoted=False),
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
                "query": dbops.redact(r.get("query") or r.get("proxied query") or "", pw),
                "count": _int(r.get("count")),
                "deep_support": sup,
                "deep_reason": dbops.redact(reason_txt, pw, quoted=False),
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
                    "query": dbops.redact(r.get("query") or r.get("proxied query") or "", pw),
                    "count": _int(r.get("count")),
                    "deep_support": "unknown",
                    "deep_reason": None,
                    "shallow_support": sup,
                    "shallow_reason": dbops.redact(reason_txt, pw, quoted=False),
                }
            else:
                item["shallow_support"] = sup
                item["shallow_reason"] = dbops.redact(reason_txt, pw, quoted=False)
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

    # Telemetry aggregation. A statistics source that was not read leaves its
    # counts null, and every figure derived from it: an unread source is not a
    # zero, and a share computed from one side alone would be false.
    cached_execs = None if cached_stat_rows is None else sum(
        (_int(r.get("total_count")) or 0) for r in cached_stat_rows)
    upstream_execs = None if upstream_stat_rows is None else sum(
        (_int(r.get("total_count")) or 0) for r in upstream_stat_rows)
    total_obs = (
        None if cached_execs is None or upstream_execs is None else cached_execs + upstream_execs
    )
    sh_hits = None if shallow_rows is None else sum((_int(r.get("hits")) or 0) for r in shallow_rows)
    sh_misses = None if shallow_rows is None else sum((_int(r.get("misses")) or 0) for r in shallow_rows)
    sh_refreshes = None if shallow_rows is None else sum((_int(r.get("refreshes")) or 0) for r in shallow_rows)
    sh_coalesces = None if shallow_rows is None else sum((_int(r.get("coalesces")) or 0) for r in shallow_rows)
    wasted_ref = None if refresh_rows is None else sum((_int(r.get("wasted_refreshes")) or 0) for r in refresh_rows)
    dropped_ref = None if refresh_rows is None else sum((_int(r.get("dropped_refreshes")) or 0) for r in refresh_rows)
    saved_us = None if coalesce_rows is None else sum(
        (_float(r.get("upstream_saved_total_us")) or 0.0) for r in coalesce_rows)

    report["telemetry"] = {
        "cached_executions": cached_execs,
        "upstream_executions": upstream_execs,
        "total_observed_executions": total_obs,
        "cached_share_pct": round(100.0 * cached_execs / total_obs, 1) if total_obs else None,
        "shallow_hits": sh_hits,
        "shallow_misses": sh_misses,
        "shallow_hit_rate_pct": (
            round(100.0 * sh_hits / (sh_hits + sh_misses), 1)
            if sh_hits is not None and sh_misses is not None and (sh_hits + sh_misses) > 0 else None
        ),
        "shallow_refreshes": sh_refreshes,
        "wasted_refreshes": wasted_ref,
        "dropped_refreshes": dropped_ref,
        "coalesces": sh_coalesces,
        "upstream_saved_ms": round(saved_us / 1000.0, 2) if saved_us is not None else None,
    }

    # Replication summary (mode-aware lag units!)
    sf = report["status_fields"]
    repl = {
        "status": sf.get("replication_status") or "Unknown",
        "cdc_server": sf.get("cdc_server"),
        "max_offset": sf.get("max_replication_offset"),
        "min_offset": sf.get("min_replication_offset"),
        "last_error": dbops.redact(sf.get("last_replicator_error"), quoted=False) if sf.get("last_replicator_error") else None,
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
            "active_cdc": _bool(u.get("active_cdc")),
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

    # Findings: every problem or watch state the reads proved, worst first
    # (stable inside one severity). A state the server did not report stays
    # unreported: no default fills it in.
    findings = []

    def _finding(fstatus, fid, title, text, tab):
        # The fixed words are never capped: only interpolated server values go
        # through _display (80 chars each), so a long value cannot cut the
        # fixed tail of a title off.
        findings.append({"id": fid, "status": fstatus, "title": title, "text": text, "tab": tab})

    def _shown(value):
        return _display(dbops.redact(value, pw, quoted=False))

    if sf.get("database_connection") == "Unreachable":
        _finding("problem", "upstream_unreachable",
                 "The upstream database is unreachable.",
                 "Readyset's Database Connection is Unreachable: Readyset could not connect to the upstream "
                 "database, a reconnect attempt failed, or no upstream URL is configured. Shallow cache misses, "
                 "uncached queries and every write go to the upstream (deep-cache misses are filled from "
                 "Readyset's local base tables), so the application feels this directly; check that the "
                 "upstream server is running and reachable from Readyset.",
                 "topology")
    if sf.get("controller_status") == "Unavailable":
        _finding("problem", "controller_unavailable",
                 "The Readyset Controller is unavailable.",
                 "Readyset Controller Status is Unavailable: the controller RPC did not answer, so Readyset cannot "
                 "report snapshot or replication progress. Check the Readyset server process and its controller.",
                 "topology")
    repl_error = repl.get("last_error")
    repl_st = sf.get("replication_status")
    if repl_error or repl_st == "Error":
        # One finding for either signal: the pinned engine emits only Running,
        # Stopped and Disabled (readyset-client/src/status.rs 107-123), but a
        # status of Error means the same thing as a last replicator error.
        text = ("Replication from the upstream database hit an error; deep caches are not receiving upstream "
                "writes while it persists.")
        if repl_error:
            text += " Readyset's last replicator error: %s" % repl_error[:2000]
        _finding("problem", "replication_error", "Replication reported an error.", text, "topology")
    for i, u in enumerate(upstreams):
        if (u.get("health") or "").lower() == "down":
            lvl = "problem" if u.get("active_cdc") else "watch"
            _finding(lvl, "upstream_down:%d" % i,
                     "Upstream %s (%s) health is down." % (_shown(u["address"]), _shown(u["purpose"] or "proxy")),
                     "Readyset's adapter marked this upstream server's proxy-pool health down after a "
                     "connection failure and skips it when picking connections; a stale mark is retried once "
                     "the healthy servers are exhausted. When this is the active CDC server, the replicator's "
                     "connection to it is likely failing too; Readyset fails over to another CDC server if one "
                     "is listed. Check that the server is running and reachable from Readyset.",
                     "topology")
    if counts["error"] > 0:
        _finding("problem", "table_errors",
                 "%d replicated base %s reported an error." % (
                     counts["error"], "table" if counts["error"] == 1 else "tables"),
                 "SHOW READYSET ALL TABLES reports an error status for these tables: Readyset stopped replicating "
                 "them, so deep caches over them serve stale data or were dropped. The Tables tab shows each "
                 "table's status and reason; fix the cause, then re-snapshot the table.",
                 "tables")
    if sf.get("status") == "Maintenance Mode":
        _finding("watch", "maintenance_mode",
                 "Readyset is in maintenance mode.",
                 "Status is Maintenance Mode, set with ALTER READYSET ENTER MAINTENANCE MODE: external tools such "
                 "as ProxySQL are meant to stop sending queries to Readyset until it exits. Exit maintenance mode "
                 "when the maintenance work is done.",
                 "topology")
    elif sf.get("status") == "Snapshot In Progress":
        _finding("watch", "snapshot_in_progress",
                 "Base table snapshot is in progress.",
                 "Status is Snapshot In Progress: the controller's leader is not ready while base tables are "
                 "snapshotted from the upstream. Deep cache creation may have to wait; the Tables tab shows "
                 "per-table progress.",
                 "topology")
    has_deep = any(c.get("cache_type") == "deep" for c in caches)
    if repl_st == "Stopped":
        _finding("watch", "replication_stopped",
                 "Replication is Stopped; deep caches are not receiving upstream writes.",
                 "Replication Status is Stopped via ALTER READYSET STOP REPLICATION. Shallow caches keep serving "
                 "on their TTLs, but deep caches go stale until replication is started again.",
                 "topology")
    elif repl_st == "Disabled":
        if has_deep:
            _finding("watch", "replication_disabled",
                     "Replication is Disabled while deep caches exist.",
                     "The replicator was not started (for example in shallow caching mode), yet deep caches exist "
                     "and need replication to stay current. Restart Readyset with replication configured, or drop "
                     "the deep caches.",
                     "topology")
    elif repl_st and repl_st not in ("Running", "Online", "Unknown", "Error"):
        _finding("watch", "replication_status",
                 "Replication status is %s." % _shown(repl_st),
                 "Readyset reports a replication status myflames does not classify; deep caches depend on "
                 "replication Running. Check SHOW READYSET STATUS on the server for the full picture.",
                 "topology")
    hb_stale = repl.get("heartbeat_staleness_s")
    if hb_stale is not None and hb_stale > 60.0 and repl_st == "Running":
        _finding("watch", "replication_heartbeat_stale",
                 "Replication heartbeat is %d s old." % int(round(hb_stale)),
                 "Readyset measures heartbeat staleness at %d s; this alone does not establish how far "
                 "replicated data or deep caches lag. Check consume lag, the replicator, heartbeat writes "
                 "to the upstream, and clock skew between the upstream and Readyset hosts."
                 % int(round(hb_stale)),
                 "topology")
    if (dropped_ref or 0) > 0 or any(c.get("over_cap") for c in caches):
        _finding("watch", "shallow_refresh_capped",
                 "Shallow cache background refresh hit its cap or dropped refreshes.",
                 "An adaptive shallow cache's actual refresh load reached its cap over the baseline load "
                 "(over_cap), or refreshes never produced a result: the refresh pool was saturated or the "
                 "upstream connection or query failed (dropped_refreshes). Entries can go stale between "
                 "refreshes; check the refresh stats on the Caches tab.",
                 "caches")
    read_failed = set()
    for label, _exc in reads.missing:
        source = {"SHOW READYSET STATUS": "status", "SHOW CACHES": "caches",
                  "SHOW READYSET ALL TABLES": "tables"}.get(label)
        if source:
            read_failed.add(source)
    if "status" in read_failed:
        _finding("problem", "read_failed:status",
                 "Readyset did not answer SHOW READYSET STATUS.",
                 "Without it, nothing about this instance's status, replication or controller is known; every "
                 "state reads as not reported, never as healthy. Check the connection and run SHOW READYSET "
                 "STATUS by hand.",
                 "topology")
    if "caches" in read_failed:
        _finding("watch", "read_failed:caches",
                 "Readyset did not answer SHOW CACHES.",
                 "The cache list is unknown: an empty list here is not proof there are no caches. Check the "
                 "connection and run SHOW CACHES by hand.",
                 "caches")
    if "tables" in read_failed:
        _finding("watch", "read_failed:tables",
                 "Readyset did not answer SHOW READYSET ALL TABLES.",
                 "The replicated base table statuses are unknown: a table error would not surface here. Check "
                 "the connection and run SHOW READYSET ALL TABLES by hand.",
                 "tables")
    # Readyset emits these as distinct fields (readyset-client/src/status.rs:
    # 49-60). A failed status read already covers both; a replication error
    # covers missing replication status, but unrelated findings do not.
    if "status" not in read_failed:
        if sf.get("status") is None:
            _finding("watch", "status_unreported",
                     "Readyset did not report its status.",
                     "SHOW READYSET STATUS answered without a Status field, so this instance's state is not "
                     "known. Check SHOW READYSET STATUS on the server.",
                     "topology")
        if repl_st is None and not repl_error:
            _finding("watch", "replication_status_unreported",
                     "Readyset did not report its replication status.",
                     "SHOW READYSET STATUS answered without a Replication Status field, so whether replication "
                     "is running is not known. Check SHOW READYSET STATUS on the server.",
                     "topology")

    findings.sort(key=lambda f: -_STATUS_RANK[f["status"]])
    report["findings"] = findings
    report["status"] = findings[0]["status"] if findings else "ok"
    report["status_why"] = [f["title"] for f in findings]
    report["statements"] = reads.statements
    report["fingerprints"] = {
        "caches": _fingerprint([(c["query_id"], c["name"], c["cache_type"], c["properties"]) for c in caches]),
        "tables": _fingerprint([(t["table"], t["status"]) for t in tables]),
        "allowlists": _fingerprint(allowlists),
        "replication": _fingerprint([sf.get("status"), sf.get("replication_status"), sf.get("cdc_server")]),
    }
    attach_actions(report)
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
    pw = getattr(conn, "password", None)
    try:
        rows = select(conn, sql, timeout=ACTION_TIMEOUT)
    except ConnectorError as exc:
        return {
            "cache_type": kind,
            "supported": "error",
            "raw_support": None,
            "reason": _short(dbops.redact(str(exc), pw, quoted=False)),
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
        "raw_support": dbops.redact(sup_raw, pw, quoted=False),
        "reason": dbops.redact(detail, pw, quoted=False),
        "query_id": _text(row.get("query_id") if "query_id" in row else row.get("query id")),
        "query": dbops.redact(row.get("query") if "query" in row else row.get("proxied query"), pw),
        "statement": sql,
    }


def explain_cache(conn, query_or_id, cache_type=None, schema="readyset"):
    """Run ``EXPLAIN CREATE [SHALLOW|DEEP] CACHE`` (dry-run, read-only) and check cache collisions."""
    ok, version = identify(conn)
    if not ok:
        raise ValueError("This connection is not a Readyset instance (%s)." % version)
    schema_name = schema if isinstance(schema, str) and _SAFE_SCHEMA_RE.match(schema) else "readyset"
    port_role, in_rs_schema = _detect_port_mode(conn, schema_name)
    if port_role is None:
        raise ActionUnavailable("%s %s" % _PORT_NOT_READ)
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
                raw_name = r.get("name")
                collisions.append({
                    "query_id": qid,
                    "name": str(raw_name) if raw_name not in (None, "") else qid,
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


_EXPORT_REDACTED_COMMENT = (
    "-- Credentials in this definition are shown as [redacted]; it will not run as is."
)
_EXPORT_AMBIGUOUS_COMMENT = (
    "-- This definition was hidden because its credentials depend on the server's quote mode."
)
_EXPORT_UNSAFE_COMMENT = (
    "-- This definition was not exported as runnable SQL: it is not a single CREATE CACHE statement."
)
_EXPORT_CHARSET_COMMENT = (
    "-- This definition was not exported as runnable SQL: a non-ASCII character comes right "
    "before a backtick or backslash, which some client character sets read differently. "
    "Load it with --default-character-set=utf8mb4 after checking it."
)
_EXPORT_CREATE_CACHE = re.compile(r"\ACREATE\s+(?:(?:SHALLOW|DEEP)\s+)?CACHE\s+\S", re.I)
_EXPORT_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
# A legacy multibyte client charset can consume either ASCII byte as the
# tail of the preceding non-ASCII character, changing quote tracking.
_EXPORT_CHARSET_AMBIGUITY = re.compile(r"[^\x00-\x7f][\\`]")
# Recognize the authority prefix separately from its password: malformed
# user-info may contain SQL quotes, spaces, slashes or backticks. Scanning
# each prefix's segment once avoids regex retries on long server text.
_EXPORT_URL_CREDENTIALS = re.compile(
    r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*://[^:/@\s'\"`]*:)"
)
_EXPORT_UTF8_COMMAND = "charset utf8mb4"
_EXPORT_REDACTED_TOKEN = re.compile(
    r"'(?:" + re.escape(dbops.REDACTED) + r")'|\"(?:" +
    re.escape(dbops.REDACTED) + r")\"|" + re.escape(dbops.REDACTED)
)
_EXPORT_URL_UNSAFE_CHARS = frozenset("\\'\"`/*-#\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_EXPORT_URL_CODE_UNSAFE = re.compile(
    r"[\\'\"`#\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029;=,]|/\*|\*/|--"
)


def _export_url_spans(sql, backslash_escapes=True, prefixes=None, scan=None, skipped=None):
    """URL password spans through the last @, under one backslash reading.

    A URL inside a complete backticked identifier is an identifier, not a
    credential. A second authority before the first @ is part of a malformed
    password. Ambiguous quoted text is hidden rather than partly exposed.
    When *skipped* is a list, append ``(start, end)`` of each prefix skipped
    because it started before *scanned*, so callers can fail closed if a
    skipped prefix lies outside every yielded span.
    """
    if prefixes is None:
        if "://" not in sql:
            return
        prefixes = list(_EXPORT_URL_CREDENTIALS.finditer(sql))
    if not prefixes:
        return
    if scan is None:
        scan = dbops.lex_sql(sql, backslash_escapes=backslash_escapes)
    unclosed = set(scan['unclosed'])
    # Find each identifier's final @ once. Searching its remaining suffix
    # for every URL prefix is quadratic when many prefixes share that @.
    identifiers = [(start, end, sql.rfind('@', start, end)) for kind, start, end in scan["tokens"]
                   if kind == "identifier" and (start, end) not in unclosed]
    scanned, ident, token, pending = 0, 0, 0, None
    for index, prefix in enumerate(prefixes):
        if prefix.start() < scanned:
            if skipped is not None:
                skipped.append((prefix.start(), prefix.end()))
            continue
        while ident < len(identifiers) and identifiers[ident][1] <= prefix.start():
            ident += 1
        if (pending is None and ident < len(identifiers) and
                identifiers[ident][0] <= prefix.start() < identifiers[ident][1] and
                prefix.end() <= identifiers[ident][2]):
            continue
        end = prefixes[index + 1].start() if index + 1 < len(prefixes) else len(sql)
        while token < len(scan['tokens']) and scan['tokens'][token][2] <= prefix.start():
            token += 1
        if token < len(scan['tokens']):
            # Nested URL-like text, including an earlier @, can belong to
            # one password. Never split a literal/comment/code token there.
            end = max(end, scan['tokens'][token][2])
        if unclosed:
            end = len(sql)  # Broken quotes cannot establish a safe boundary.
        # No overlap between segments, including unsuccessful matches.
        last_at = sql.rfind("@", prefix.end(), end)
        scanned = end
        if last_at < 0:
            if pending is None:
                pending = prefix
            continue
        start = pending.end() if pending is not None else prefix.end()
        yield start, last_at
        pending = None


def _merge_export_url_spans(spans1, spans2=None):
    """Merge ordered URL password spans into disjoint ``(start, end)`` tuples."""
    stream = spans1 if spans2 is None else heapq.merge(spans1, spans2)
    merged, pending = [], None
    for start, end in stream:
        if pending is not None:
            old_start, old_end = pending
            if start <= old_end:
                pending = old_start, max(old_end, end)
                continue
            merged.append(pending)
        pending = start, end
    if pending is not None:
        merged.append(pending)
    return merged


def _redact_export_urls(sql):
    """Hide URL passwords under both backslash readings of the original SQL.

    The definition's NO_BACKSLASH_ESCAPES setting is unknown. Scan both
    readings before replacing anything, then hide the union of their spans:
    a string ending in a backslash closes in one reading and not the other,
    so one reading can keep a URL credential that the other hides.
    """
    if "://" not in sql:
        return sql, False
    prefixes = list(_EXPORT_URL_CREDENTIALS.finditer(sql))
    if not prefixes:
        return sql, False
    spans1 = _export_url_spans(sql, prefixes=prefixes)
    spans2 = _export_url_spans(sql, backslash_escapes=False, prefixes=prefixes) if "\\" in sql else None
    merged = _merge_export_url_spans(spans1, spans2)
    if not merged:
        return sql, False
    parts, kept = [], 0
    for start, end in merged:
        parts.extend((sql[kept:start], dbops.REDACTED))
        kept = end
    return "".join(parts) + sql[kept:], True


_EXPORT_SQL_ESCAPES = {
    "0": "\x00",
    "'": "'",
    '"': '"',
    "b": "\b",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "Z": "\x1a",
    "\\": "\\",
}


def _decode_export_sql_escapes(text, quote=None):
    """Decode MySQL backslash escapes (and doubled *quote* if given) in one pass."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            out.append(_EXPORT_SQL_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if quote and ch == quote and i + 1 < n and text[i + 1] == quote:
            out.append(quote)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _export_token_has_password(body, password, quote=None):
    """True when *password* appears in raw, quote-collapsed or unescaped *body*."""
    if not password:
        return False
    if password in body:
        return True
    if quote:
        doubled = quote * 2
        if doubled in body and password in body.replace(doubled, quote):
            return True
    elif ("''" in body and password in body.replace("''", "'")) or (
            '""' in body and password in body.replace('""', '"')):
        return True
    if "\\" in body:
        if password in _decode_export_sql_escapes(body):
            return True
        if quote and (quote * 2) in body and password in _decode_export_sql_escapes(body, quote):
            return True
    return False


def _export_literal_password_spans(sql, password, backslash_escapes=True, scan=None):
    """Password-bearing strings/comments under one reading of the original SQL."""
    if scan is None:
        scan = dbops.lex_sql(sql, backslash_escapes=backslash_escapes)
    unclosed = set(scan["unclosed"])
    for kind, start, end in scan["tokens"]:
        tok = sql[start:end]
        replacement = None
        if kind == "string":
            closed = (start, end) not in unclosed and len(tok) >= 2 and tok[-1] == tok[0]
            quote = tok[0] if tok[:1] in ("'", '"') else "'"
            body = tok[1:-1] if closed else tok[1:]
            if _export_token_has_password(body, password, quote):
                replacement = quote + dbops.REDACTED + quote
        elif kind == "comment":
            closed = (start, end) not in unclosed
            if tok.startswith("/*"):
                body = tok[2:-2] if closed and tok.endswith("*/") and len(tok) >= 4 else tok[2:]
                if _export_token_has_password(body, password):
                    replacement = "/* %s */" % dbops.REDACTED
            elif tok.startswith("--"):
                if _export_token_has_password(tok[2:], password):
                    replacement = "-- %s" % dbops.REDACTED
            elif tok.startswith("#"):
                if _export_token_has_password(tok[1:], password):
                    replacement = "# %s" % dbops.REDACTED
        if replacement is not None:
            yield start, end, replacement


def _merge_export_literal_spans(sql, password, scan1=None, scan2=None):
    """Merge password-bearing string/comment spans across both backslash readings."""
    if not isinstance(password, str) or not password:
        return []
    spans = _export_literal_password_spans(sql, password, True, scan=scan1)
    if "\\" in sql:
        spans = heapq.merge(spans, _export_literal_password_spans(sql, password, False, scan=scan2))
    merged, pending = [], None
    for start, end, replacement in spans:
        if pending is not None:
            old_start, old_end, old_replacement = pending
            if start < old_end:
                pending = old_start, max(old_end, end), old_replacement
                continue
            merged.append(pending)
        pending = start, end, replacement
    if pending is not None:
        merged.append(pending)
    return merged


def _redact_export_literal_passwords(sql, password):
    """Hide connection *password* inside strings/comments under either SQL mode.

    The definition's NO_BACKSLASH_ESCAPES setting is unknown. Scan both
    readings before replacing anything, then hide the union of their spans:
    replacing one reading first can change the other's quote boundaries.
    Identifiers, keywords and bare SQL words stay visible only when neither
    reading places them inside a password-bearing string or comment.
    """
    merged = _merge_export_literal_spans(sql, password)
    return _apply_export_spans(sql, merged)


def _export_identifier_spans(sql, backslash_escapes=True, scan=None):
    """Spans of closed backticked identifiers under one backslash reading."""
    if scan is None:
        scan = dbops.lex_sql(sql, backslash_escapes=backslash_escapes)
    unclosed = set(scan["unclosed"])
    return [(start, end) for kind, start, end in scan["tokens"]
            if kind == "identifier" and (start, end) not in unclosed]


def _next_export_mask(used, codepoint):
    """Next unused Unicode Private Use Area character, or None if exhausted."""
    while True:
        if codepoint == 0xf900:
            codepoint = 0xf0000
        elif codepoint == 0xffffe:
            codepoint = 0x100000
        elif codepoint >= 0x10fffe:
            return None, codepoint
        ch = chr(codepoint)
        codepoint += 1
        if ch not in used:
            return ch, codepoint


def _mask_export_identifiers(sql, ident_spans):
    """Replace *ident_spans* with one private-use mask char per original char."""
    if not ident_spans:
        return sql, {}
    parts, identifiers, kept = [], {}, 0
    used, mask_codepoint = set(sql), 0xe000
    for start, end in ident_spans:
        if start > kept:
            parts.append(sql[kept:start])
        mask, mask_codepoint = _next_export_mask(used, mask_codepoint)
        if mask is None:
            parts.append(sql[start:end])
        else:
            identifiers[mask] = sql[start:end]
            parts.append(mask * (end - start))
        kept = end
    if kept < len(sql):
        parts.append(sql[kept:])
    return "".join(parts), identifiers


def _restore_export_identifiers(text, identifiers):
    """Restore private-use identifier masks left intact after redaction."""
    if not identifiers:
        return text
    restored, previous = [], None
    for ch in text:
        if ch not in identifiers:
            restored.append(ch)
        elif ch != previous:
            restored.append(identifiers[ch])
        previous = ch
    return "".join(restored)


def _apply_export_spans(sql, spans):
    """Apply sorted disjoint ``(start, end, replacement)`` spans to *sql*."""
    if not spans:
        return sql, False
    parts, kept = [], 0
    for start, end, replacement in spans:
        parts.extend((sql[kept:start], replacement))
        kept = end
    return "".join(parts) + sql[kept:], True


def _extract_redact_spans(masked, updated, identifiers=None):
    """Recover ``(start, end, replacement)`` spans on *masked* from ``dbops.redact``.

    Returns ``(spans, exact)``. When every unchanged separator between
    ``[redacted]`` markers aligns uniquely, *exact* is True and *spans*
    reproduce *updated* verbatim. When a separator also appears inside a
    redacted value, ambiguous runs collapse to their enclosing envelope and
    *exact* is False so no secret byte can escape. Returns ``(None, False)``
    if alignment fails or splits a masked identifier, so callers fail closed.
    """
    if updated == masked:
        return [], True
    tokens = [(m.start(), m.end(), m.group(0))
              for m in _EXPORT_REDACTED_TOKEN.finditer(updated)]
    if not tokens:
        return None, False
    k = len(tokens)
    seps = [updated[:tokens[0][0]]]
    for idx in range(1, k):
        seps.append(updated[tokens[idx - 1][1]:tokens[idx][0]])
    seps.append(updated[tokens[-1][1]:])
    reps = [tok[2] for tok in tokens]
    n = len(masked)
    if not masked.startswith(seps[0]) or not masked.endswith(seps[k]):
        return None, False
    end_limit = n - len(seps[k])
    cursor = len(seps[0])
    if cursor > end_limit:
        return None, False
    lo = [0] * (k + 1)
    lo[k] = end_limit
    for idx in range(1, k):
        pos = masked.find(seps[idx], cursor, end_limit)
        if pos < 0:
            return None, False
        lo[idx] = pos
        cursor = pos + len(seps[idx])
    hi = [0] * (k + 1)
    hi[k] = end_limit
    limit = end_limit
    for idx in range(k - 1, 0, -1):
        pos = masked.rfind(seps[idx], lo[idx], limit)
        if pos < 0:
            return None, False
        hi[idx] = pos
        limit = pos
    spans = []
    exact = True
    idx = 0
    while idx < k:
        start = lo[idx] + len(seps[idx])
        if lo[idx + 1] == hi[idx + 1]:
            end = lo[idx + 1]
            rep = reps[idx]
            if masked[start:end] != rep:
                spans.append((start, end, rep))
            idx += 1
        else:
            j = idx + 1
            while j < k and lo[j] != hi[j]:
                j += 1
            end = hi[j]
            run_start, run_end = idx, j
            while run_start < run_end:
                rep = reps[run_start]
                if run_start + 1 == run_end:
                    if end - start == len(rep) and masked.startswith(rep, start):
                        start = end
                        run_start += 1
                    break
                sep_next = seps[run_start + 1]
                step = len(rep) + len(sep_next)
                if (end - start >= step and masked.startswith(rep, start) and
                        (not sep_next or masked.startswith(sep_next, start + len(rep)))):
                    start += step
                    run_start += 1
                else:
                    break
            while run_start < run_end:
                rep = reps[run_end - 1]
                if run_end - 1 == run_start:
                    if end - start == len(rep) and masked.endswith(rep, start, end):
                        end = start
                        run_end -= 1
                    break
                sep_prev = seps[run_end - 1]
                step = len(rep) + len(sep_prev)
                if (end - start >= step and masked.endswith(rep, start, end) and
                        (not sep_prev or masked.endswith(sep_prev, start, end - len(rep)))):
                    end -= step
                    run_end -= 1
                else:
                    break
            if run_start == run_end:
                if start < end:
                    return None, False
            else:
                rep = reps[run_start]
                if run_end - run_start > 1:
                    exact = False
                if masked[start:end] != rep:
                    spans.append((start, end, rep))
            idx = j
    if identifiers:
        for start, end, _rep in spans:
            if 0 < start < n and masked[start] in identifiers and masked[start - 1] == masked[start]:
                return None, False
            if 0 < end < n and masked[end] in identifiers and masked[end - 1] == masked[end]:
                return None, False
    return spans, exact


def _merge_export_all_spans(lit_spans, url_spans, cred_spans):
    """Merge literal-password, URL and SQL-credential spans on the original SQL.

    Overlapping or abutting spans across sources collapse to their union so a
    literal-password span can never erase a credential keyword while leaving
    its value literal visible. Adjacent non-empty literal spans stay distinct.
    """
    stream = heapq.merge(
        ((start, 0, end, dbops.REDACTED) for start, end in url_spans),
        ((start, 1, end, rep) for start, end, rep in cred_spans),
        ((start, 2, end, rep) for start, end, rep in lit_spans),
    )
    merged, pending = [], None
    for start, kind, end, rep in stream:
        if pending is not None:
            old_start, old_end, old_kind, old_rep = pending
            if start < old_end or (start == old_end and (old_kind != 2 or kind != 2)):
                pending = old_start, max(old_end, end), min(old_kind, kind), old_rep
                continue
            merged.append((old_start, old_end, old_rep))
        pending = start, end, kind, rep
    if pending is not None:
        merged.append((pending[0], pending[1], pending[3]))
    return merged


def _has_export_redacted_credential(masked):
    """True when an existing ``[redacted]`` marker sits in a credential clause."""
    if dbops.REDACTED not in masked:
        return False
    probe = masked.replace(dbops.REDACTED, "myflames_export_secret_marker")
    return dbops.redact(probe) != probe


def _check_export_second_reading(sql, ident_spans1, result1, check_marker=False,
                                 ident_spans2=None, raw_has_marker=False):
    """Compare backslash_escapes=False against reading 1's redacted output.

    Returns ``(ambiguous, marker_changed)``. *ambiguous* is True when the two
    readings disagree symmetrically on any hidden credential (either reading
    hides something the other kept), so the caller fails closed.
    *marker_changed* is True when reading 2 finds an already-redacted
    ``[redacted]`` marker in a credential clause.
    """
    if ident_spans2 is None:
        ident_spans2 = _export_identifier_spans(sql, backslash_escapes=False)
    if ident_spans2 == ident_spans1:
        return False, False
    masked2, identifiers2 = _mask_export_identifiers(sql, ident_spans2)
    updated2 = dbops.redact(masked2)
    restored2 = _restore_export_identifiers(updated2, identifiers2)
    if restored2 != result1:
        return True, False
    if updated2 == masked2 and check_marker and _has_export_redacted_credential(masked2):
        return False, True
    if dbops.REDACTED in sql:
        spans1 = set(ident_spans1)
        spans2 = set(ident_spans2)
        if raw_has_marker or (
                any(dbops.REDACTED in sql[a:b] for a, b in spans1 - spans2) and
                any(dbops.REDACTED in sql[a:b] for a, b in spans2 - spans1)):
            # Existing [redacted] text could align distinct replacements across
            # the two readings; re-check with existing markers substituted.
            marker, _ = _next_export_mask(set(sql), 0xe000)
            if marker is None:
                return True, False
            sql_marked = sql.replace(dbops.REDACTED, marker * len(dbops.REDACTED))
            m1, ids1 = _mask_export_identifiers(sql_marked, ident_spans1)
            m2, ids2 = _mask_export_identifiers(sql_marked, ident_spans2)
            r1 = _restore_export_identifiers(dbops.redact(m1), ids1)
            r2 = _restore_export_identifiers(dbops.redact(m2), ids2)
            if r1 != r2:
                return True, False
    return False, False


def _export_url_spans_safe(sql, url_spans, scan, ident_spans):
    """True when every URL span stays strictly inside one safe token of *scan*.

    A span is safe only when it never overlaps a closed backticked identifier in
    *ident_spans* and lies strictly inside one closed string or comment token,
    or strictly inside one code token free of quote, comment, newline, backslash
    and statement-separator syntax.
    """
    if not url_spans:
        return True
    if ident_spans:
        ident_idx = 0
        ident_count = len(ident_spans)
        for start, end in url_spans:
            while ident_idx < ident_count and ident_spans[ident_idx][1] <= start:
                ident_idx += 1
            if ident_idx < ident_count:
                ident_start = ident_spans[ident_idx][0]
                if ident_start < end or (start == end and ident_start <= start):
                    return False
    tokens = scan["tokens"]
    token_count = len(tokens)
    unclosed = set(scan["unclosed"])
    token_idx = 0
    for start, end in url_spans:
        while token_idx < token_count and tokens[token_idx][2] <= start:
            token_idx += 1
        if token_idx >= token_count:
            return False
        kind, tok_start, tok_end = tokens[token_idx]
        if start < tok_start or end > tok_end:
            return False
        if kind == "string":
            if (tok_start, tok_end) in unclosed or start <= tok_start or end >= tok_end - 1:
                return False
        elif kind == "comment":
            if (tok_start, tok_end) in unclosed:
                return False
            if sql.startswith("/*", tok_start):
                if start < tok_start + 2 or end >= tok_end - 2:
                    return False
            elif sql.startswith("--", tok_start):
                if start < tok_start + 2 or end >= tok_end:
                    return False
            elif sql.startswith("#", tok_start):
                if start < tok_start + 1 or end >= tok_end:
                    return False
            else:
                return False
        elif kind == "code":
            if start <= tok_start or end >= tok_end:
                return False
            if _EXPORT_URL_CODE_UNSAFE.search(sql, start, end) is not None:
                return False
        else:
            return False
    return True


def _has_uncovered_url_prefix(skipped, spans):
    """True when any skipped URL prefix lies outside every yielded span."""
    if not skipped:
        return False
    span_idx = 0
    span_count = len(spans)
    for p_start, p_end in skipped:
        while span_idx < span_count and spans[span_idx][1] < p_end:
            span_idx += 1
        if span_idx >= span_count or spans[span_idx][0] > p_start:
            return True
    return False


def _redact_export_sql(sql, password=None):
    """Redact credential syntax and password-bearing literals or comments.

    A connection password can also be an ordinary table, column, keyword or
    identifier. Do not pass it as the shared redactor's substring scrubber, and
    never run a global replace over the definition. Literal-password spans,
    URL credential spans and masked ``dbops.redact`` credential spans are all
    evaluated on the original definition under both backslash readings before
    any substitution can alter quote parity or erase credential keywords. When
    the two readings disagree on credentials, or any URL span crosses a token
    boundary, overlaps an identifier, or holds unsafe syntax in code, the
    definition fails closed to a single comment line.
    """
    raw_sql = sql
    has_backslash = "\\" in raw_sql
    scan1 = dbops.lex_sql(raw_sql, backslash_escapes=True)
    scan2 = dbops.lex_sql(raw_sql, backslash_escapes=False) if has_backslash else scan1

    lit_spans = _merge_export_literal_spans(raw_sql, password, scan1=scan1, scan2=scan2)

    url_spans1, url_spans2 = [], []
    url_skipped1, url_skipped2 = [], []
    if "://" in raw_sql:
        prefixes = list(_EXPORT_URL_CREDENTIALS.finditer(raw_sql))
        if prefixes:
            url_spans1 = list(_export_url_spans(raw_sql, True, prefixes, scan=scan1, skipped=url_skipped1))
            url_spans2 = (list(_export_url_spans(raw_sql, False, prefixes, scan=scan2, skipped=url_skipped2))
                          if has_backslash else url_spans1)
            if not has_backslash:
                url_skipped2 = url_skipped1
    if (_has_uncovered_url_prefix(url_skipped1, url_spans1) or
            (has_backslash and _has_uncovered_url_prefix(url_skipped2, url_spans2))):
        return _EXPORT_AMBIGUOUS_COMMENT, True
    url_disagree = url_spans1 != url_spans2
    url_spans = _merge_export_url_spans(url_spans1, url_spans2 if has_backslash else None)

    ident_spans1 = _export_identifier_spans(raw_sql, True, scan=scan1)
    ident_spans2 = _export_identifier_spans(raw_sql, False, scan=scan2) if has_backslash else ident_spans1

    if url_spans:
        if (not _export_url_spans_safe(raw_sql, url_spans, scan1, ident_spans1) or
                (has_backslash and not _export_url_spans_safe(raw_sql, url_spans, scan2, ident_spans2))):
            return _EXPORT_AMBIGUOUS_COMMENT, True

    if url_disagree:
        if lit_spans or dbops.REDACTED in raw_sql:
            return _EXPORT_AMBIGUOUS_COMMENT, True
        for start, end in url_spans:
            if any(ch in _EXPORT_URL_UNSAFE_CHARS for ch in raw_sql[start:end]):
                return _EXPORT_AMBIGUOUS_COMMENT, True

    masked1, identifiers1 = _mask_export_identifiers(raw_sql, ident_spans1)
    updated1 = dbops.redact(masked1)
    restored1 = _restore_export_identifiers(updated1, identifiers1)

    if ident_spans2 == ident_spans1:
        masked2, identifiers2, updated2, restored2 = masked1, identifiers1, updated1, restored1
    else:
        masked2, identifiers2 = _mask_export_identifiers(raw_sql, ident_spans2)
        updated2 = dbops.redact(masked2)
        restored2 = _restore_export_identifiers(updated2, identifiers2)

    raw_has_marker = dbops.REDACTED in raw_sql

    if url_disagree:
        cred_spans1, exact1 = _extract_redact_spans(masked1, updated1, identifiers1)
        cred_spans2, exact2 = _extract_redact_spans(masked2, updated2, identifiers2)
        if cred_spans1 is None or cred_spans2 is None or not exact1 or not exact2:
            return _EXPORT_AMBIGUOUS_COMMENT, True
        res1, _ = _apply_export_spans(raw_sql, _merge_export_all_spans((), url_spans, cred_spans1))
        res2, _ = _apply_export_spans(raw_sql, _merge_export_all_spans((), url_spans, cred_spans2))
        if res1 != res2:
            return _EXPORT_AMBIGUOUS_COMMENT, True
        return res1, True

    if restored1 != restored2:
        return _EXPORT_AMBIGUOUS_COMMENT, True

    if ident_spans2 != ident_spans1 and raw_has_marker:
        marker, _ = _next_export_mask(set(raw_sql), 0xe000)
        if marker is None:
            return _EXPORT_AMBIGUOUS_COMMENT, True
        sql_marked = raw_sql.replace(dbops.REDACTED, marker * len(dbops.REDACTED))
        m1, ids1_m = _mask_export_identifiers(sql_marked, ident_spans1)
        m2, ids2_m = _mask_export_identifiers(sql_marked, ident_spans2)
        r1_m = _restore_export_identifiers(dbops.redact(m1), ids1_m)
        r2_m = _restore_export_identifiers(dbops.redact(m2), ids2_m)
        if r1_m != r2_m:
            return _EXPORT_AMBIGUOUS_COMMENT, True

    if not lit_spans and not url_spans:
        changed = updated1 != masked1
        if not changed and raw_has_marker:
            changed = (_has_export_redacted_credential(masked1) or
                       (ident_spans2 != ident_spans1 and _has_export_redacted_credential(masked2)))
        return restored1, changed

    cred_spans, _exact = _extract_redact_spans(masked1, updated1, identifiers1)
    if cred_spans is None:
        return _EXPORT_AMBIGUOUS_COMMENT, True
    if ident_spans2 != ident_spans1 and updated2 != masked2:
        cred_spans2, _exact2 = _extract_redact_spans(masked2, updated2, identifiers2)
        if cred_spans2 is None or cred_spans2 != cred_spans:
            return _EXPORT_AMBIGUOUS_COMMENT, True

    merged = _merge_export_all_spans(lit_spans, url_spans, cred_spans)
    result, changed = _apply_export_spans(raw_sql, merged)
    if not changed and raw_has_marker:
        changed = (_has_export_redacted_credential(masked1) or
                   (ident_spans2 != ident_spans1 and _has_export_redacted_credential(masked2)))
    return result, changed


def _export_has_controls(text):
    return _has_ctl_or_fmt(text)


def _export_has_unsafe_dollars(text):
    """Allow Readyset's $N placeholders, never a possible dollar-quote tag."""
    for match in re.finditer(r"\$[0-9]*", text):
        end = match.end()
        if end == match.start() + 1:
            return True  # No ASCII digits after the dollar.
        if end < len(text):
            nxt = text[end]
            # Include swe7 identifier letters ([ \ ] ^ { } ~) as well.
            if nxt.isalnum() or nxt in "_$[]\\^{}~" or ord(nxt) >= 128:
                return True
    return False


def _export_db_safe(db):
    return not db or not (_export_has_controls(db) or _export_has_unsafe_dollars(db) or
                          "\\" in db or any(ch in _EXPORT_LINE_BREAKS for ch in db))


def _runnable_export_sql(sql, check_charset=True):
    """Accept one CREATE CACHE under both server and mysql-client readings.

    The importing client's quote mode is unknown. Refuse lexical uncertainty,
    executable comments and line/client framing outside strings, rather than
    letting server-supplied text control how the rest of the file is read.
    """
    if _export_has_controls(sql) or (check_charset and _EXPORT_CHARSET_AMBIGUITY.search(sql)):
        return False
    token_spans = {}
    for escapes in (True, False):
        for client in (False, True):
            scan = dbops.lex_sql(sql, backslash_escapes=escapes, client=client,
                                client_delimiter=";" if client else None)
            if scan["issues"] or scan["unclosed"] or scan["comments"]:
                return False
            # Compare escape modes within each lexer: only the client
            # reading represents the final semicolon as a delimiter token.
            if client in token_spans and scan["tokens"] != token_spans[client]:
                return False
            token_spans[client] = scan["tokens"]
            code = []
            for kind, start, end in scan["tokens"]:
                tok = sql[start:end]
                if kind == "string":
                    code.append("?")
                    continue
                # Clients can enable dollar quoting independently of the
                # server. Only a complete $N placeholder is safe outside
                # ordinary strings; $1$ and other tags can change framing.
                if (_export_has_unsafe_dollars(tok) or "\\" in tok or
                        any(ch in _EXPORT_LINE_BREAKS for ch in tok)):
                    return False
                if kind == "identifier":
                    code.append("identifier")
                elif kind == "comment":
                    if not tok.startswith("/*"):
                        return False  # A line comment can swallow the terminator.
                    code.append(" ")
                else:
                    code.append(tok)
            code = "".join(code).strip()
            # _EXPORT_CREATE_CACHE anchors on CREATE at the line start and no
            # line breaks are allowed, so a client DELIMITER command is already
            # unreachable while SQL identifiers such as t.delimiter stay safe.
            if not _EXPORT_CREATE_CACHE.match(code) or not code.endswith(";") or ";" in code[:-1]:
                return False
    return True


def _split_export_use(sql, check_charset=True):
    """Extract Readyset's quoted database prefix only before one safe CREATE.

    Return malformed prefixes and extra statements intact so the exporter
    comments out the whole definition. Never copy server-supplied USE text
    into the runnable output; its parsed name is quoted by the exporter.
    """
    # Check before trimming: tabs and other controls must not disappear into
    # the prefix parser and become runnable in an exporter-generated USE.
    if _export_has_controls(sql) or (check_charset and _EXPORT_CHARSET_AMBIGUITY.search(sql)):
        return None, sql
    tokens = dbops.lex_sql(sql)["tokens"]
    if (len(tokens) < 3 or tokens[0][0] != "code" or
            sql[tokens[0][1]:tokens[0][2]].strip(" \t").upper() != "USE" or
            tokens[1][0] != "identifier"):
        return None, sql
    _, start, end = tokens[1]
    prefix = sql[:end]
    if (_export_has_unsafe_dollars(prefix) or "\\" in prefix or
            any(ch in _EXPORT_LINE_BREAKS for ch in prefix)):
        return None, sql
    tail = sql[end:].lstrip(" \t")
    if not tail.startswith(";"):
        return None, sql
    try:
        db = _rs_ident_parse(sql[start:end], "Database")
    except ValueError:
        return None, sql
    definition = tail[1:].lstrip(" \t")
    stmt = definition if definition.endswith(";") else definition + ";"
    if not _runnable_export_sql(stmt, check_charset=check_charset):
        return None, sql
    return db, definition


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
    pw = getattr(conn, "password", None)
    # Redact before bounding, and keep server text on its comment line.
    version = _display(dbops.redact(version, pw, quoted=False))
    lines = ["-- Readyset cache definitions exported by myflames (Readyset %s)" % version,
             "-- Recognized credentials in query text are shown as [redacted]."]
    last_db = None
    needs_utf8 = False
    for r in rows:
        source_q = r.get("query") or r.get("create statement") or ""
        if not source_q.strip():
            continue
        prefix_db, raw_q = _split_export_use(source_q, check_charset=False)
        q, redacted = _redact_export_sql(raw_q, pw)
        # Readyset's prefix is the schema used by CREATE, even when the
        # database column disagrees with the shallow cache's search path.
        raw_db = prefix_db if prefix_db is not None else r.get("database")
        db = str(raw_db) if raw_db not in (None, "") else None
        # Database names are identifiers, not credential values, even when
        # one happens to equal the connection password.
        db_safe = _export_db_safe(db)
        db_charset_unsafe = False
        if db and db != last_db:
            try:
                use = "USE %s;" % _rs_ident_server(db, "Database")
                # Include the generated closing backtick in the charset
                # check, especially for names ending in non-ASCII text.
                db_charset_unsafe = bool(_EXPORT_CHARSET_AMBIGUITY.search(use))
            except ValueError:
                use = "Database name could not be exported as runnable SQL."
                db_safe = False
            if not db_safe:
                use = "-- " + _review_text(use)
            else:
                last_db = db
                needs_utf8 = needs_utf8 or db_charset_unsafe
            lines.append(use)
        hidden = redacted and q == _EXPORT_AMBIGUOUS_COMMENT
        stmt = q if hidden or q.endswith(";") else (q + ";")
        runnable = not hidden and db_safe and _runnable_export_sql(stmt, check_charset=False)
        if runnable and not redacted and _EXPORT_CHARSET_AMBIGUITY.search(stmt):
            needs_utf8 = True
        if not runnable:
            lines.append(_EXPORT_UNSAFE_COMMENT)
        if redacted:
            lines.append(_EXPORT_REDACTED_COMMENT)
        if (redacted or not runnable) and not hidden:
            # A substituted literal can still be valid SQL. Keep disabled
            # definitions on one comment line with every control visible.
            stmt = "-- " + _review_text(stmt)
        lines.append(stmt)
        items.append({"database": db, "statement": stmt})
    if needs_utf8:
        # Unlike SET NAMES alone, mysql's charset command synchronizes its
        # own lexer with the server. Only this fixed exporter-owned command
        # may precede definitions containing ambiguous legacy-charset bytes.
        lines[2:2] = ["-- Import with a mysql-compatible client; use UTF-8 for these definitions.",
                      _EXPORT_UTF8_COMMAND]
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "version": version,
        "count": len(items),
        "caches": items,
        "sql": "\n".join(lines) + "\n",
        "notes": notes,
    }


# ---- reviewed actions (dbops + reviewed_writes) ------------------------------------------

# The one danger table: exactly the tone "danger" set of the pre-catalog
# available_actions. `tone` stays beside `danger` for one release.
_DANGER_ACTIONS = frozenset((
    "drop_cache",
    "drop_all_caches",
    "drop_all_shallow_caches",
    "drop_all_deep_caches",
    "drop_all_proxied_queries",
    "drop_shallow_allowed",
    "resnapshot_table",
    "enter_maintenance_mode",
    "stop_replication",
    "set_replication_position",
    "shutdown",
    "shutdown_reset",
))

# Fixed disabled reasons (why_not, fix). Only integers and _display values may
# be interpolated into a why_not; a fix never holds a server value.
_DDL_BLOCKED = (
    "This Readyset listener refuses cache DDL.",
    "Run cache changes on the CACHE_DDL_ADDRESS listener or the admin SQL port (default 6036); "
    "CREATE CACHE still needs an upstream database selected.",
)
_CACHE_SQL_PORT = (
    "CREATE CACHE needs the Readyset SQL port (default 3307) with an upstream database schema.",
    "Connect to the Readyset SQL port (default 3307) with an upstream database.",
)
_SHUTDOWN_ADMIN_PORT = (
    "ALTER READYSET SHUTDOWN is accepted only on the Readyset admin SQL port (default 6036).",
    "Connect to the Readyset admin SQL port (default 6036) to shut down Readyset.",
)
_PORT_NOT_READ = (
    "Readyset did not show which listener this is.",
    "Refresh; if it persists, check the connection's port.",
)
_STATUS_NOT_READ = (
    "Readyset did not report its Status.",
    "Check the connection and refresh: SHOW READYSET STATUS reports it.",
)
_REPL_NOT_READ = (
    "Readyset did not report its Replication Status.",
    "Check the connection and refresh: SHOW READYSET STATUS reports it.",
)
_CACHES_NOT_READ = (
    "Readyset did not report its caches.",
    "Check the connection and refresh: SHOW CACHES lists them.",
)
_BAD_ROW_NAME = (
    "Readyset reported a name that cannot be used in a statement.",
    "Run this change on Readyset by hand.",
)
_CONTROL_ROW_NAME = (
    "Readyset reported a name with control or format characters.",
    "Run this change on Readyset by hand.",
)
_FLUSH_SHALLOW_ONLY = (
    "FLUSH CACHE applies only to shallow caches.",
    "Only shallow cache entries can be flushed.",
)
# SHOW CACHES renders the qualified name `sales`.`report` and the one
# identifier `sales.report` both as sales.report (extensions.rs:1113 with
# readyset-sql/src/ast/table.rs:135), and DROP CACHE / FLUSH CACHE resolve a
# Relation name only, never a query id, for a named cache
# (readyset-sql-parsing/src/lib.rs:1687,1716; readyset-adapter
# extensions.rs:1897-1931; readyset-shallow/src/manager.rs:191-206). A dotted
# reported name is therefore not safe to address in a statement.
_DOTTED_ROW_NAME = (
    "Readyset reported a cache name with a dot, which a statement cannot address unambiguously.",
    "Run this change on Readyset by hand: SHOW CACHES does not show whether the dot is a qualifier.",
)


def _action(aid, label, scope, description, why=None):
    """One action catalog entry: always listed, never hidden; when disabled,
    *why* is the (why_not, fix) pair of fixed strings."""
    entry = {
        "id": aid,
        "label": label,
        "scope": scope,
        "tone": "danger" if aid in _DANGER_ACTIONS else "default",
        "danger": aid in _DANGER_ACTIONS,
        "description": description,
        "enabled": why is None,
        "why_not": None,
        "fix": None,
    }
    if why is not None:
        entry["why_not"], entry["fix"] = why
    return entry


def _read_failures(report):
    """The core reads that failed, from the read_failed findings ('status', 'caches', 'tables')."""
    out = set()
    for f in report.get("findings") or ():
        fid = f.get("id") or ""
        if fid.startswith("read_failed:"):
            out.add(fid.split(":", 1)[1])
    return out


def _create_cache_why(report):
    """Port role first, then the one-way cache-DDL gate (S5); False proves
    nothing. The cache list must have been read: collision detection needs it."""
    if report.get("port_role") is None:
        return _PORT_NOT_READ
    if report.get("in_readyset_schema"):
        return _CACHE_SQL_PORT
    if not (report.get("capabilities") or {}).get("can_manage_caches", True):
        return _DDL_BLOCKED
    if "caches" in _read_failures(report):
        return _CACHES_NOT_READ
    return None


def _server_actions(report):
    caps = report.get("capabilities") or {}
    sf = report.get("status_fields") or {}
    caches = report.get("caches") or []
    failed = _read_failures(report)
    ddl_blocked = not caps.get("can_manage_caches", True)
    shallow_count = sum(1 for c in caches if c.get("cache_type") == "shallow")
    deep_count = len(caches) - shallow_count
    status = sf.get("status")
    repl = sf.get("replication_status")

    def counted_why(count, none_why, none_fix):
        if ddl_blocked:
            return _DDL_BLOCKED
        if "caches" in failed:
            return _CACHES_NOT_READ
        if count == 0:
            return (none_why, none_fix)
        return None

    out = [
        _action("create_cache", "Create cache…", "server",
                "Creates a shallow or deep cache for a query or query id.", _create_cache_why(report)),
        _action("flush_all_shallow_caches", "Flush shallow caches…", "server",
                "Evicts every cached entry across all shallow caches; cache definitions remain active.",
                counted_why(shallow_count,
                            "There are no shallow caches to flush.", "Create a shallow cache first.")),
        _action("drop_all_caches", "Drop all caches…", "server",
                "Removes every cache definition; queries immediately fall back to the upstream database.",
                counted_why(len(caches), "There are no caches to drop.", "Create a cache first.")),
        _action("drop_all_shallow_caches", "Drop shallow caches…", "server",
                "Removes every shallow cache definition.",
                counted_why(shallow_count,
                            "There are no shallow caches to drop.", "Create a shallow cache first.")),
        _action("drop_all_deep_caches", "Drop deep caches…", "server",
                "Removes every deep cache definition.",
                counted_why(deep_count,
                            "There are no deep caches to drop.", "Create a deep cache first.")),
        _action("drop_all_proxied_queries", "Clear proxied queries…", "server",
                "Clears Readyset's catalog of observed uncached proxied queries; active caches are untouched.",
                _DDL_BLOCKED if ddl_blocked else None),
    ]

    if status is None:
        enter_why = exit_why = _STATUS_NOT_READ
    elif status == "Maintenance Mode":
        enter_why = ("Readyset is already in maintenance mode.", "Exit maintenance mode first.")
        exit_why = None
    elif status == "Online":
        enter_why = None
        exit_why = ("Readyset is not in maintenance mode.", "Only a server in maintenance mode can exit it.")
    else:
        # Readyset reports Maintenance Mode only once its leader is ready, so
        # during a snapshot the flag is not readable (controller/inner.rs 749-757).
        enter_why = (
            "Readyset reports Maintenance Mode only when its Status is Online (now: %s)." % _display(status),
            "Wait until the snapshot finishes and Status is Online.",
        )
        exit_why = (
            "Readyset does not show maintenance mode until its Status is Online (now: %s)." % _display(status),
            "Wait until the snapshot finishes and Status is Online.",
        )
    out.append(_action("enter_maintenance_mode", "Enter maintenance mode…", "server",
                       "Sets Readyset's status to Maintenance Mode, a signal for tools such as ProxySQL to stop "
                       "sending queries.", enter_why))
    out.append(_action("exit_maintenance_mode", "Exit maintenance mode…", "server",
                       "Clears the Maintenance Mode status signal.", exit_why))

    if repl is None:
        start_why = stop_why = _REPL_NOT_READ
    elif repl == "Stopped":
        start_why = None
        stop_why = ("Replication is already Stopped.", "Only running replication can be stopped.")
    elif repl == "Running":
        start_why = ("Replication is already Running.", "Only stopped replication can be started.")
        stop_why = None
    else:
        start_why = ("Replication status is %s." % _display(repl),
                     "Readyset starts replication only from Stopped.")
        stop_why = ("Replication status is %s." % _display(repl),
                    "Readyset stops replication only from Running.")
    out.append(_action("start_replication", "Start replication…", "server",
                       "Resumes ingesting binlog changes from the upstream CDC server.", start_why))
    out.append(_action("stop_replication", "Stop replication…", "server",
                       "Pauses ingesting binlog changes from the upstream CDC server; deep caches stop receiving "
                       "updates.", stop_why))

    out.extend([
        _action("set_replication_position", "Set replication position…", "server",
                "Sets the upstream binlog or GTID position from which Readyset's replicator resumes."),
        _action("add_tables", "Add tables to replicate…", "server",
                "Starts snapshotting and replicating the specified upstream base tables into Readyset."),
        _action("set_log_level", "Set log level…", "server",
                "Updates Readyset's runtime tracing filter immediately."),
        _action("set_eviction_memory_limit", "Set eviction memory limit…", "server",
                "Updates the runtime memory threshold at which Readyset evicts cached entries."),
        _action("set_eviction_period", "Set eviction period…", "server",
                "Sets how often Readyset runs its cache memory eviction sweep."),
        _action("add_shallow_allowed", "Add shallow allowlist item…", "server",
                "Lets automatic shallow cache creation accept queries that reference one function, variable "
                "or schema; explicit CREATE SHALLOW CACHE and query hints bypass the allowlists.",
                _DDL_BLOCKED if ddl_blocked else None),
        _action("flush_privileges", "Flush Readyset privileges…", "server",
                "Refreshes Readyset's cached upstream user credentials and table ACLs immediately."),
    ])
    port_role = report.get("port_role")
    shutdown_why = _PORT_NOT_READ if port_role is None else (None if port_role == "admin" else _SHUTDOWN_ADMIN_PORT)
    out.append(_action("shutdown", "Shut down Readyset…", "server",
                       "Stops the Readyset process cleanly.", shutdown_why))
    out.append(_action("shutdown_reset", "Shut down & reset snapshot…", "server",
                       "Stops the Readyset process and discards persistent snapshot state so the next start "
                       "performs a full re-snapshot.", shutdown_why))
    return out


def _server_name_why(name, quoter, label):
    """None when the server-reported *name* can be quoted byte for byte and
    shown in a review without visual reordering; the disabled reason otherwise."""
    if not isinstance(name, str) or not name:
        return _BAD_ROW_NAME
    try:
        quoter(name, label)
    except (ValueError, TypeError):
        return _BAD_ROW_NAME
    if _has_ctl_or_fmt(name):
        return _CONTROL_ROW_NAME
    return None


def _cache_row_actions(report, row):
    ddl_blocked = not (report.get("capabilities") or {}).get("can_manage_caches", True)
    row_name = row.get("name") or row.get("query_id")
    name_why = _server_name_why(row_name, _rs_ident_server, "Cache name")
    if name_why is None and "." in row_name:
        name_why = _DOTTED_ROW_NAME
    if name_why is not None:
        flush_why = drop_why = name_why
    else:
        if ddl_blocked:
            flush_why = _DDL_BLOCKED
        elif row.get("cache_type") != "shallow":
            flush_why = _FLUSH_SHALLOW_ONLY
        else:
            flush_why = None
        drop_why = _DDL_BLOCKED if ddl_blocked else None
    return [
        _action("flush_cache", "Flush entries…", "row",
                "Discards all currently stored entries of this shallow cache; the definition stays active.",
                flush_why),
        _action("drop_cache", "Drop cache…", "row",
                "Removes this cache so matching queries run directly on the upstream database.",
                drop_why),
    ]


def _table_row_actions(row):
    why = _server_name_why(row.get("table"), lambda n, _l: _rs_table_ident(n), "Table name")
    return [
        _action("resnapshot_table", "Re-snapshot table…", "row",
                "Drops Readyset's local copy of this base table and reads a fresh snapshot from the upstream "
                "CDC server.", why),
    ]


def _proxied_row_actions(report):
    return [
        _action("create_cache", "Create cache…", "row",
                "Creates a shallow or deep cache for this proxied query.", _create_cache_why(report)),
    ]


def _allowlist_actions(report, list_key, name=None):
    ddl_blocked = not (report.get("capabilities") or {}).get("can_manage_caches", True)
    items = (report.get("shallow_allowlists") or {}).get(list_key) or ()
    kind = list_key[:-1]
    if isinstance(name, str) and _has_ctl_or_fmt(name):
        drop_why = _CONTROL_ROW_NAME
    elif name is not None and _server_name_why(name, _rs_ident_server, "Allowlist name"):
        drop_why = _BAD_ROW_NAME
    elif ddl_blocked:
        drop_why = _DDL_BLOCKED
    elif items:
        drop_why = None
    else:
        drop_why = ("There are no allowed %ss to remove." % kind, "Add an allowed %s first." % kind)
    return [
        _action("add_shallow_allowed", "Allow…", "row",
                "Lets automatic shallow cache creation accept queries that reference this %s." % kind,
                _DDL_BLOCKED if ddl_blocked else None),
        _action("drop_shallow_allowed", "Remove…", "row",
                "Stops automatic shallow cache creation from accepting queries that reference this %s; "
                "existing caches and explicit CREATE SHALLOW CACHE are not affected." % kind, drop_why),
    ]


def attach_actions(report):
    """Attach the action catalog to *report*: server scope on ``actions``, row
    scope on every cache, table and proxied query, per-kind ``allowlist_actions``
    and per-item ``allowlist_item_actions`` (parallel to ``shallow_allowlists``).
    Never raises on a server-supplied name and is linear in rows: a row whose
    name cannot be canonicalized gets its actions disabled with a fixed reason
    while other rows stay enabled."""
    report["actions"] = _server_actions(report)
    for c in report.get("caches") or ():
        c["actions"] = _cache_row_actions(report, c)
    for t in report.get("tables") or ():
        t["actions"] = _table_row_actions(t)
    for p in report.get("proxied_queries") or ():
        p["actions"] = _proxied_row_actions(report)
    report["allowlist_actions"] = dict(
        (key, _allowlist_actions(report, key)) for key in ("functions", "variables", "schemas"))
    report["allowlist_item_actions"] = dict(
        (key, [next(a for a in _allowlist_actions(report, key, name=item)
                    if a["id"] == "drop_shallow_allowed")
               for item in (report.get("shallow_allowlists") or {}).get(key) or ()])
        for key in ("functions", "variables", "schemas"))
    return report


def _find_cache_row(caches, name, qid):
    """The one cache row a target means, by exact reported name or query id.
    When the target carries both halves, both must name the same current row:
    a cache whose name or query id changed since it was read matches nothing.
    Used by both the catalog and the planner so they can never disagree."""
    qid = _text(qid)
    if not name and not qid:
        return None
    if name and qid:
        return next(
            (c for c in caches or ()
             if c.get("name") == name and c.get("query_id") == qid),
            None,
        )
    return next(
        (c for c in caches or ()
         if (name and c.get("name") == name) or (qid and c.get("query_id") == qid)),
        None,
    )


def available_actions(report, target=None):
    """The full action catalog for *report*: server scope when *target* is None,
    row scope for a target of kind "cache", "table", "proxied_query" or a shallow
    allowlist kind. Every action is listed, enabled or with why_not and fix."""
    if not report or not report.get("is_readyset"):
        return []
    if target is None:
        return _server_actions(report)
    if not isinstance(target, dict):
        return []
    kind = target.get("kind")
    if kind == "cache":
        found = _find_cache_row(report.get("caches"), target.get("name"), target.get("query_id"))
        return _cache_row_actions(report, found) if found else []
    if kind == "proxied_query":
        return _proxied_row_actions(report)
    if kind == "table":
        tname = target.get("table")
        if not tname:
            return []
        try:
            canon = _rs_table_ident(tname)
        except (ValueError, TypeError):
            canon = None
        if canon is not None:
            for t in report.get("tables") or ():
                try:
                    if t.get("table") and _rs_table_ident(t["table"]) == canon:
                        return _table_row_actions(t)
                except (ValueError, TypeError):
                    continue
            return []
        # Unparseable target: offered (disabled) only when Readyset reported
        # this exact name; a name Readyset never reported is not offered.
        for t in report.get("tables") or ():
            if t.get("table") == tname:
                return _table_row_actions(t)
        return []
    if kind in SHALLOW_ALLOWLIST_KINDS:
        return _allowlist_actions(report, SHALLOW_ALLOWLIST_KINDS[kind][2],
                                 target.get("name") or target.get("identifier"))
    return []


def _build_create_cache_sql(spec):
    if not isinstance(spec, dict):
        raise ValueError("Pass cache creation options.")
    ctype = (spec.get("cache_type") or "shallow").strip().lower()
    if ctype not in ("shallow", "deep"):
        raise ValueError("Choose cache_type 'shallow' or 'deep'.")
    input_kind, target = _normalize_query_or_id(spec.get("query_or_id") or "")
    raw_name = spec.get("name") or ""
    if isinstance(raw_name, str):
        # Check before trimming: even leading/trailing controls must not reach
        # a review or create a cache our row actions cannot safely address.
        if "." in raw_name or _has_ctl_or_fmt(raw_name):
            raise ValueError("Choose a cache name without dots or control characters.")
        raw_name = raw_name.strip()
    # The identifier the statement names, after user-input parsing: collision
    # warnings and the typed guard compare this exact identifier against the
    # cache names Readyset reported, so a prequoted `` `name` `` cannot slip
    # past the checks (Readyset replaces a colliding cache: extensions.rs:96,243).
    canon_name = _rs_ident_parse(raw_name, "Cache name") if raw_name else None
    quoted_name = "`%s`" % canon_name.replace("`", "``") if canon_name else None

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
        "name": canon_name,
        "options": opts,
        "statement": " ".join(parts) + ";",
    }


def _allowlist_kind(target, value):
    """The one allowlist kind an add/drop allowlist request means. Every kind
    the request names (``kind`` and ``allow_kind`` on target and value) must
    agree; a request naming two kinds is refused so the catalog gate and the
    planner can never evaluate different lists."""
    claims = []
    for src in (target, value):
        if not isinstance(src, dict):
            continue
        for key in ("allow_kind", "kind"):
            raw = src.get(key)
            if raw is None:
                continue
            k = str(raw).strip().lower()
            if not k:
                continue
            if k not in SHALLOW_ALLOWLIST_KINDS:
                raise ValueError("Choose allowlist kind 'function', 'variable' or 'schema'.")
            claims.append(k)
    if len(set(claims)) > 1:
        raise ValueError(
            "The target and the value name different allowlist kinds. Pass one kind and try again."
        )
    return claims[0] if claims else "function"


def _catalog_target(action, target, value):
    """The catalog lookup target for *action*: the caller's row target when it
    names its kind, else the row the action implies, else None (server scope)."""
    t = target if isinstance(target, dict) else {}
    kind = t.get("kind")
    if action in ("add_shallow_allowed", "drop_shallow_allowed"):
        row = {"kind": _allowlist_kind(target, value)}
        if action == "drop_shallow_allowed":
            row["name"] = t.get("name") or t.get("identifier")
        return row
    if kind == "table" and not t.get("table"):
        tbl = value.get("table") if isinstance(value, dict) else value
        return {"kind": "table", "table": tbl}
    if kind in ("cache", "table", "proxied_query") or kind in SHALLOW_ALLOWLIST_KINDS:
        return t
    if action in ("drop_cache", "flush_cache"):
        return {"kind": "cache", "name": t.get("name"), "query_id": t.get("query_id")}
    if action == "resnapshot_table":
        tbl = t.get("table") or (value.get("table") if isinstance(value, dict) else value)
        return {"kind": "table", "table": tbl}
    return None


def plan_action(report, action, target=None, value=None, conn=None):
    """Build a ``readyset-action-1.0`` reviewed plan from a fresh *report*.

    Selects the action from :func:`available_actions` on that report first,
    before any SQL is built and before ``EXPLAIN CREATE CACHE`` is sent: an id
    the catalog does not offer is refused without echoing it, and a disabled
    entry raises :class:`ActionUnavailable` with exactly the catalog's
    ``why_not`` and ``fix``. The per-action branches keep their own refusals
    as defense in depth."""
    if not report or not report.get("is_readyset"):
        raise ValueError("This connection is not a Readyset instance.")
    if not isinstance(action, str) or not action:
        raise ValueError("Choose a Readyset action.")

    if action in ("drop_cache", "flush_cache") and isinstance(target, dict):
        t_name = target.get("name")
        t_qid = _text(target.get("query_id"))
        if t_name and t_qid:
            rows = report.get("caches") or []
            if not any(c.get("name") == t_name and c.get("query_id") == t_qid for c in rows):
                if any(c.get("name") == t_name or c.get("query_id") == t_qid for c in rows):
                    raise ValueError(
                        "That cache changed since it was read. Refresh and try again."
                    )

    selected = next(
        (a for a in available_actions(report, _catalog_target(action, target, value))
         if a.get("id") == action),
        None,
    )
    if selected is None:
        raise ValueError("That change is not offered on this Readyset now. Refresh and try again.")
    if not selected["enabled"]:
        raise ActionUnavailable("%s %s" % (selected["why_not"], selected["fix"]))

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
        "facts": [["Readyset", "%s (%s)" % (
            report.get("version") or "unknown",
            ("%s port" % report["port_role"]) if report.get("port_role") else "port not read",
        )]],
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
        if "caches" in _read_failures(report):
            # Without SHOW CACHES the collision check below cannot run, and
            # Readyset drops a colliding cache before creating the replacement
            # (extensions.rs:96,243); the catalog gate refuses this first.
            raise ActionUnavailable("%s %s" % _CACHES_NOT_READ)
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
            if any(_has_ctl_or_fmt(c["name"] or "") for c in collisions):
                raise ActionUnavailable(
                    "A cache with control or format characters in its name collides; "
                    "drop it on Readyset by hand first."
                )
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
            undo=("DROP CACHE %s;" % _rs_ident_server(built["name"], "Cache name")) if built["name"] else "DROP CACHE `<query_id>`;",
            expect={
                "readyset_version": report.get("version"),
                "session_database": report.get("session_database"),
                "cache_type": ctype,
                "name": built["name"],
                "target": built["target"],
                "collisions": collisions,
            },
        )
        plan["facts"].extend([
            ["Default database", report.get("session_database") or "none"],
            ["Cache type", ctype.capitalize()],
            ["Options", ", ".join(built["options"]) if built["options"] else "Default"],
            ["From", built["target"][:140]],
        ])
        return _finish_plan(plan)

    if action in ("drop_cache", "flush_cache"):
        if not caps.get("can_manage_caches", True):
            raise ValueError("Cache management is disabled on this Readyset listener.")
        t = target if isinstance(target, dict) else {}
        found = _find_cache_row(caches, t.get("name"), _text(t.get("query_id")))
        if found is None:
            raise ValueError("That cache no longer exists on Readyset.")
        ident_name = found["name"] or found["query_id"]
        if "." in ident_name:
            # The catalog gate refuses this first; defense in depth. A dotted
            # reported name cannot be addressed unambiguously (_DOTTED_ROW_NAME).
            raise ActionUnavailable("%s %s" % _DOTTED_ROW_NAME)
        quoted = _rs_ident_server(ident_name, "Cache name")
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
        return _finish_plan(plan)

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
        return _finish_plan(plan)

    if action in ("drop_all_caches", "drop_all_shallow_caches", "drop_all_deep_caches"):
        if action == "drop_all_caches" and value not in (None, "", {}):
            # One id builds one statement shape: the retired value scope of
            # drop_all_caches is refused, never silently broadened to all caches.
            raise ValueError("Use Drop shallow caches or Drop deep caches for a scoped drop.")
        scope = "shallow" if action == "drop_all_shallow_caches" else (
            "deep" if action == "drop_all_deep_caches" else "all"
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
        return _finish_plan(plan)

    if action == "drop_all_proxied_queries":
        if not caps.get("can_manage_caches", True):
            raise ValueError("Cache management is disabled on this Readyset listener.")
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
        return _finish_plan(plan)

    if action in ("enter_maintenance_mode", "exit_maintenance_mode"):
        entering = action == "enter_maintenance_mode"
        cur_status = sf.get("status")
        shown_status = cur_status if cur_status is not None else "not reported"
        if entering and cur_status == "Maintenance Mode":
            raise ValueError("Readyset is already in Maintenance Mode.")
        if not entering and cur_status != "Maintenance Mode":
            raise ValueError("Readyset is not in Maintenance Mode (current status: %s)." % shown_status)
        stmt = "ALTER READYSET %s MAINTENANCE MODE;" % ("ENTER" if entering else "EXIT")
        plan.update(
            eyebrow="MAINTENANCE MODE",
            title="Enter Readyset maintenance mode?" if entering else "Exit Readyset maintenance mode?",
            confirm_label="Enter maintenance mode" if entering else "Exit maintenance mode",
            description=(
                "Sets Readyset's status to Maintenance Mode: a status signal that tools such as ProxySQL are "
                "meant to act on by stopping traffic to Readyset, which keeps serving the queries it receives. "
                "The setting is kept in memory; restarting Readyset clears it."
                if entering
                else "Clears the Maintenance Mode status signal so tools such as ProxySQL send queries to "
                     "Readyset again; Readyset kept serving the queries it received. The setting is kept in "
                     "memory; restarting Readyset clears it."
            ),
            statements=[stmt],
            typed_guard="MAINTENANCE MODE" if entering else None,
            undo="ALTER READYSET %s MAINTENANCE MODE;" % ("EXIT" if entering else "ENTER"),
            expect={"readyset_version": report.get("version"), "status": cur_status},
        )
        plan["facts"].append(["Status", "%s → %s" % (shown_status, "Maintenance Mode" if entering else "Online")])
        return _finish_plan(plan)

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
        return _finish_plan(plan)

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
        return _finish_plan(plan)

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
        return _finish_plan(plan)

    if action in ("add_shallow_allowed", "drop_shallow_allowed"):
        adding = action == "add_shallow_allowed"
        src = target if isinstance(target, dict) else {}
        kind_key = _allowlist_kind(target, value)
        singular, _, list_key = SHALLOW_ALLOWLIST_KINDS[kind_key]
        raw_item = (
            src.get("name") or src.get("identifier")
            if not adding and (src.get("name") or src.get("identifier"))
            else (params.get("name") or params.get("identifier") if params else value)
        )
        if not isinstance(raw_item, str):
            raise ValueError("Pass the %s name." % kind_key)
        from_row = not adding and bool(src.get("name") or src.get("identifier"))
        if _has_ctl_or_fmt(raw_item):
            if from_row or not adding:
                raise ActionUnavailable("%s %s" % _CONTROL_ROW_NAME)
            raise ValueError("Choose an allowlist name without control or format characters.")
        clean_item = raw_item if from_row else raw_item.strip()
        if not clean_item:
            raise ValueError("Pass the %s name." % kind_key)
        if from_row:
            quoted_item = _rs_ident_server(clean_item, "%s name" % kind_key.capitalize())
        else:
            clean_item = _rs_ident_parse(clean_item, "%s name" % kind_key.capitalize())
            quoted_item = _rs_ident_server(clean_item, "%s name" % kind_key.capitalize())
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
        return _finish_plan(plan)

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
        return _finish_plan(plan)

    if action == "resnapshot_table":
        raw_tbl = (target.get("table") if isinstance(target, dict) else None) or (
            value.get("table") if isinstance(value, dict) else value
        )
        if not isinstance(raw_tbl, str) or not raw_tbl.strip():
            raise ValueError("Choose a replicated table to re-snapshot.")
        quoted_tbl = _rs_table_ident(raw_tbl)
        found_tbl = None
        for t in report.get("tables") or ():
            raw = t.get("table")
            if not raw:
                continue
            if raw == raw_tbl:
                found_tbl = t
                break
            try:
                if _rs_table_ident(raw) == quoted_tbl:
                    found_tbl = t
                    break
            except (ValueError, TypeError):
                continue
        if found_tbl is None:
            raise ValueError("Table %s is not in Readyset's table list." % _display(raw_tbl))
        tname = found_tbl["table"]
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
        return _finish_plan(plan)

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
            expect={"readyset_version": report.get("version"),
                    "session_database": report.get("session_database"),
                    "tables": raw_items, "tables_fp": prints.get("tables")},
        )
        plan["warnings"].append("Each added table runs an initial snapshot query against the upstream CDC server.")
        plan["facts"].append(["Tables", ", ".join(raw_items)])
        plan["facts"].append(["Default database", report.get("session_database") or "none"])
        return _finish_plan(plan)

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
        return _finish_plan(plan)

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
        return _finish_plan(plan)

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
        return _finish_plan(plan)

    # Unreachable while every catalog id maps to a branch above; the catalog
    # gate at the top already refuses any id the catalog does not offer.
    raise ValueError("That change is not offered on this Readyset now. Refresh and try again.")


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

    def _cnt(value):
        return str(value) if value is not None else "not read"

    role = report.get("port_role")
    port_text = terminal_text("%s port" % role) if role else "port not read"
    lines = [
        "Readyset %s (%s) — status: %s (%s)" % (
            terminal_text(report.get("version") or "unknown"),
            port_text,
            terminal_text((report.get("status") or "unknown").upper()),
            terminal_text(sf.get("status") or "Not read"),
        )
    ]
    findings = report.get("findings") or ()
    why_lines = [f["title"] for f in findings] if findings else list(report.get("status_why") or ())
    for why in why_lines:
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
    if tel.get("cached_share_pct") is not None:
        share = "%.1f%% cached share" % tel["cached_share_pct"]
    elif tel.get("total_observed_executions") is None:
        share = "cached share not read"
    else:
        share = "no traffic yet"
    lines.append(
        "Telemetry: %s cached / %s upstream observed executions (%s); shallow hits %s, misses %s%s" % (
            _cnt(tel.get("cached_executions")),
            _cnt(tel.get("upstream_executions")),
            share,
            _cnt(tel.get("shallow_hits")),
            _cnt(tel.get("shallow_misses")),
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
