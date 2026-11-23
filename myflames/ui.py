"""Local browser workspace with explicit saved investigations. Runtime dependencies: Python stdlib only."""
import argparse
import html
import io
import json
import os
import re
import secrets
import selectors
import socket
import socketserver
import sqlite3
import sys
import threading
import time
from http.cookies import CookieError, SimpleCookie
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .connector import ConnectorError, client_missing_error
from .connection_groups import ProfileReadError
from . import read_cancel
from .parser import analyze_plan
# Compatibility exports for one release; validation is independent of HTTP.
from .plan_input import MAX_PLAN, _plan
from .output_sidecar import build_sidecar
from .output_compare_sidecar import build_compare_sidecar
from .output_compare import render_compare
from .output_html_report import render_html_report, _render_svg

ASSET_DIR = Path(__file__).resolve().parent / "ui_assets"
MAX_BODY = 12 * 1024 * 1024
# A session snapshot may reach SESSION_MAX_BYTES (64 MiB) plus request framing.
MAX_SESSION_BODY = 65 * 1024 * 1024
VIEWS = ("flamegraph", "diagram", "tree", "bargraph", "workbench")


def _operators(root):
    """UI projection of canonical parsed nodes; sidecar schema stays unchanged."""
    result = []
    stack = [(root, 0, None)]
    while stack:
        node, depth, parent_id = stack.pop()
        result.append({
            "node_id": node["node_id"], "label": node["short_label"],
            "depth": depth, "self_time_ms": node["self_time"],
            "total_time_ms": node["total_time"], "rows": node["rows"],
            "loops": node["loops"], "details": node.get("details", {}),
            "parent_id": parent_id, "children": [c["node_id"] for c in node.get("children", [])],
        })
        stack.extend((child, depth + 1, node["node_id"]) for child in reversed(node.get("children", [])))
    return result


def _discover_response(path, payload, workspace=None):
    """Read-only server views over the saved connection (see discover.py,
    sessions.py, innodb_status.py, error_log.py, cluster.py, proxysql.py, readyset.py, locks.py, health.py, waits.py, indexes.py, schema_health.py, io_stats.py,
    config_review.py; the schema picker's list is perf_common.schema_catalog)."""
    from .live import connection_options
    from . import discover
    if path == "/api/live/ddl-progress":
        from .ddl_progress import check_ids
        check_ids(payload.get("connection_ids"))
    if path == "/api/live/digest-tables":
        discover.check_digest_tables(payload.get("schema"), payload.get("text"))
    if path == "/api/live/slow-log":
        from .slow_queries import check_request
        check_request(payload.get("from"), payload.get("to"), payload.get("order") or "total",
                      payload.get("limit", 50), payload.get("user"), payload.get("database"))
    if path == "/api/live/sessions/history":
        from .sessions import check_history_id
        check_history_id(payload.get("id"))
    options = connection_options(payload.get("connection"))
    password = options.get("password")
    if path in ("/api/live/ddl-progress", "/api/live/sessions/history", "/api/live/memory"):
        options["query_timeout"] = 5
    elif path == "/api/live/explain-connection":
        # MariaDB waits up to 30 s for the target thread to answer SHOW EXPLAIN.
        options["query_timeout"] = 40
    elif path in ("/api/live/indexes", "/api/live/tables", "/api/live/report"):
        # A server-wide information_schema.STATISTICS read is slow on MariaDB.
        options["query_timeout"] = 60
    elif path in ("/api/live/io", "/api/live/config", "/api/live/innodb", "/api/live/error-log", "/api/live/cluster",
                  "/api/live/proxysql", "/api/live/proxysql/digests", "/api/live/readyset",
                  "/api/live/readyset/explain-cache", "/api/live/readyset/export-caches",
                  "/api/live/watch/capture", "/api/live/digest-tables", "/api/live/slow-log"):
        options["query_timeout"] = 30
    if path in ("/api/live/indexes", "/api/live/tables", "/api/live/report"):
        # schemas: a list (the picker), or the older single schema; none means
        # every user schema (Indexes and the report: only up to
        # perf_common.ALL_SCHEMAS_TABLE_LIMIT tables unless all_schemas is true).
        from .perf_common import check_schemas
        schemas = check_schemas(payload.get("schemas"))
        if payload.get("schema"):
            schemas = check_schemas([payload.get("schema")] + list(schemas or ()))
    if path == "/api/live/report":
        return _report_response(payload, options, schemas, workspace)
    try:
        with _group_guarded_connection(options, payload.get("connection"),
                                       getattr(workspace, "store", None)) as conn:
            if path == "/api/live/index-suggestion":
                from .index_hint import read_suggestion
                return read_suggestion(conn, payload.get("digest"), payload.get("schema"))
            if path == "/api/live/top":
                schema = payload.get("schema") or None
                # table: "schema.table" or {"schema", "table"} (workload attribution).
                return discover.top_report(conn, payload.get("limit", 20), schema,
                                           payload.get("order", "total"), payload.get("table") or None,
                                           extra_metrics=bool(payload.get("extra_metrics")),
                                           with_identity=payload.get("with_identity") is True)
            if path == "/api/live/slow-log":
                from .slow_queries import slow_log_table_report
                return slow_log_table_report(conn, payload.get("from"), payload.get("to"), payload.get("user"),
                                             payload.get("database"), payload.get("order") or "total",
                                             payload.get("limit", 50))
            if path == "/api/live/digest-tables":
                return discover.digest_tables(conn, payload.get("schema"), payload.get("text"))
            if path == "/api/live/digest-histogram":
                return discover.digest_histogram(conn, payload.get("schema") or None,
                                                 payload.get("digest"))
            if path == "/api/live/running":
                return discover.running_report(conn, payload.get("min_seconds", 0))
            if path == "/api/live/ddl-progress":
                from .ddl_progress import ddl_progress_report
                return ddl_progress_report(conn, payload.get("connection_ids"))
            if path == "/api/live/sessions/history":
                from .sessions import sessions_history
                return sessions_history(conn, payload.get("id"))
            if path == "/api/live/sessions":
                from .sessions import DEFAULT_LIMIT, sessions_report
                return sessions_report(conn, active_only=payload.get("active_only", True),
                                       user=payload.get("user") or None, host=payload.get("host") or None,
                                       db=payload.get("db") or None, min_seconds=payload.get("min_seconds", 0),
                                       limit=payload.get("limit", DEFAULT_LIMIT), identity=True,
                                       source_mode=payload.get("source_mode", "auto"),
                                       locked_only=payload.get("locked_only", False))
            if path == "/api/live/innodb":
                from .innodb_status import innodb_report
                return innodb_report(conn, raw=payload.get("raw", False))
            if path == "/api/live/error-log":
                from .error_log import DEFAULT_LIMIT, error_log_report
                return error_log_report(conn, limit=payload.get("limit", DEFAULT_LIMIT), levels=payload.get("levels"))
            def _topo_args():
                from . import replication as rp
                from .connection_groups import members, request_profiles
                if workspace is None or getattr(workspace, "store", None) is None:
                    return {}, [], None
                conn_in = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
                profiles, current = request_profiles(workspace.store, conn_in)
                pid = current['id'] if current else None
                opener = None
                if current and current.get('group'):
                    if conn_in.get('group_name') is not None:
                        opener = rp.profile_opener(workspace.store, pid, current,
                                                   group_name=conn_in.get('group_name'), connection=conn_in)
                    else:
                        opener = rp.profile_opener(workspace.store, pid, current)
                return {"profiles": members(profiles, current),
                        "open_profile": opener,
                        "reached_host": rp.reached_host(options), "current_profile_id": pid,
                        "current_profile": current}, profiles, current

            if path in ("/api/live/cluster", "/api/live/proxysql", "/api/live/readyset"):
                from .connection_groups import begin_read, finish_read
                t_args, profiles, current = _topo_args()
                identity = begin_read(getattr(workspace, "store", None), current)
                if path == "/api/live/cluster":
                    from .cluster import attach_topology_tiers, read_status, with_actions
                    report = read_status(conn, _identity=identity)
                    attach_topology_tiers(conn, report, **t_args)
                elif path == "/api/live/proxysql":
                    from .proxysql import read_status
                    report = read_status(conn, **t_args)
                else:
                    from .readyset import read_status
                    report = read_status(conn, schema=payload.get("schema") or "readyset", **t_args)
                if workspace is not None and getattr(workspace, "store", None) is not None:
                    finish_read(workspace.store, report, payload.get("connection") or {}, profiles, current, identity=identity)
                if path == "/api/live/cluster":
                    # The probes just confirmed peer identities. Build the
                    # catalog from the updated store in this same response.
                    if workspace is not None and getattr(workspace, "store", None) is not None:
                        from .connection_groups import request_profiles
                        profiles, current = request_profiles(workspace.store, payload.get("connection") or {})
                    report = with_actions(report, ssh=bool(getattr(conn, "ssh_host", None)),
                                          group_profiles=profiles, current_profile=current,
                                          group_name=(payload.get("connection") or {}).get("group_name") if current else None)
                return report
            if path == "/api/live/readyset/explain-cache":
                from .readyset import explain_cache
                q = payload.get("query_or_id")
                if q is None:
                    q = payload.get("query")
                return explain_cache(conn, q, cache_type=payload.get("cache_type"),
                                     schema=payload.get("schema") or "readyset")
            if path == "/api/live/readyset/export-caches":
                from .readyset import export_caches
                return export_caches(conn)
            if path == "/api/live/proxysql/digests":
                from .proxysql_digests import read_digests
                return read_digests(conn, order=payload.get("order"), limit=payload.get("limit"),
                                    hostgroup=payload.get("hostgroup"), schema=payload.get("schema"),
                                    user=payload.get("user"), text=payload.get("text"),
                                    digest=payload.get("digest"), rule_id=payload.get("rule_id"))
            if path == "/api/live/locks":
                from .locks import lock_report
                return lock_report(conn, payload.get("limit", 500),
                                   waits_only=payload.get("waits_only") is True, identity=True)
            if path == "/api/live/health":
                from .health import DEFAULT_INTERVAL, health_report
                return health_report(conn, payload.get("interval", DEFAULT_INTERVAL),
                                     with_identity=payload.get("with_identity") is True)
            if path == "/api/live/watch":
                from .watch import DEFAULT_LIMIT, watch_report
                return watch_report(conn, payload.get("limit", DEFAULT_LIMIT))
            if path == "/api/live/watch/capture":
                from .watch import capture_evidence
                return capture_evidence(conn, watch_snapshot=payload.get("watch"))
            if path == "/api/live/memory":
                from .memory import DEFAULT_WINDOW, memory_report
                return memory_report(conn, payload.get("interval", DEFAULT_WINDOW))
            if path == "/api/live/waits":
                from .perf_common import DEFAULT_WINDOW
                from .waits import DEFAULT_LIMIT, waits_report
                return waits_report(conn, payload.get("interval", DEFAULT_WINDOW), payload.get("limit", DEFAULT_LIMIT))
            if path == "/api/live/indexes":
                from .indexes import index_report
                from .perf_common import ALL_SCHEMAS_TABLE_LIMIT
                return index_report(conn, schemas=schemas, limit=payload.get("limit"),
                                    require_choice_above=None if payload.get("all_schemas") is True
                                    else ALL_SCHEMAS_TABLE_LIMIT)
            if path == "/api/live/tables":
                from .schema_health import table_report
                from .perf_common import ALL_SCHEMAS_TABLE_LIMIT
                return table_report(conn, schemas, payload.get("fresh", False), payload.get("limit"),
                                    require_choice_above=None if payload.get("all_schemas") is True
                                    else ALL_SCHEMAS_TABLE_LIMIT)
            if path == "/api/live/schemas":
                from .perf_common import schema_catalog
                return schema_catalog(conn)
            if path == "/api/live/io":
                from .io_stats import DEFAULT_LIMIT, io_report
                from .perf_common import DEFAULT_WINDOW
                return io_report(conn, payload.get("interval", DEFAULT_WINDOW), payload.get("schema") or None,
                                 payload.get("limit", DEFAULT_LIMIT),
                                 all_instances=payload.get("all_instances", False))
            if path == "/api/live/config":
                from .config_review import config_report
                return config_report(conn, parameters=True)
            if path == "/api/live/instruments":
                from .ps_instruments import instruments_report
                return instruments_report(conn)
            result = discover.explain_connection(conn, payload.get("connection_id"))
            result["capture"] = discover.explain_capture(result)
            return result
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, password)


def _report_response(payload, options, schemas, workspace=None):
    """The Full analysis (report.analysis_report): one run per connection at
    a time; an identical request while one runs waits for it and gets the
    same report (report.run_single_flight)."""
    from .perf_common import ALL_SCHEMAS_TABLE_LIMIT
    from . import report as report_module
    password = options.get("password")
    interval = payload.get("interval", report_module.DEFAULT_INTERVAL)
    limit = None if payload.get("all_schemas") is True else ALL_SCHEMAS_TABLE_LIMIT
    connection = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}

    def work():
        with _group_guarded_connection(options, connection, getattr(workspace, "store", None)) as conn:
            return report_module.analysis_report(conn, interval, schemas=schemas, require_choice_above=limit)
    try:
        return _single_flight_read("/api/live/report", payload, workspace, work,
                                   options=options, exclusive=True)
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, password)


def _single_flight_read(path, payload, workspace, work, options=None, exclusive=False):
    """Share only identical reads within one workspace and consent snapshot.

    Resolve ordinary view credentials before this call. Topology instead
    binds the stored credential references (rotated on every password save),
    leaving secret lookup behind its per-member consent checks.
    Every request field participates,
    even fields a view currently ignores, so adding a parameter cannot silently
    broaden sharing. Hash request data with the process secret, never retain a
    plaintext password in a registry key. Saved profile snapshots also bind
    peer endpoints, credential references and group incarnations.
    """
    from . import report as report_module
    from .accounts_api import _group_login_guard
    from .connection_groups import members, request_profiles
    from .live import connection_options
    connection = payload.get("connection") or {}
    options = connection_options(connection) if options is None else options
    store = getattr(workspace, "store", None)
    profiles, current = request_profiles(store, connection)
    live_guard = _group_login_guard(store, connection)
    guard = None
    if live_guard is not None:
        if current is None:
            raise ProfileReadError("This saved connection is outside the current connection group.")
        consent = (current.get("group"), current.get("group_generation"))

        def guard():
            # Bind the read to the key's original consent snapshot, even if
            # the live guard was constructed after a group recreation. Read
            # consent deliberately does not bind type or read-only policy.
            live_guard()
            _profiles, fresh = request_profiles(store, connection)
            if fresh is None or (fresh.get("group"), fresh.get("group_generation")) != consent:
                raise ProfileReadError("The connection group changed. Refresh before reading it again.")

        guard()
    peers = members(profiles, current)
    credentials = []
    if path == "/api/live/topology":
        from .workspace_store import _same_endpoint, _stored_ref
        # Public profile projections deliberately omit secret_ref. Read only
        # private metadata here, never the vault/keychain password. A save
        # rotates this reference even when its public timestamp is unchanged.
        for profile in [current] + peers:
            stored = store._profile(profile["id"])
            if (not isinstance(stored, dict) or not _same_endpoint(profile, stored)
                    or stored.get("group") != profile.get("group")
                    or stored.get("group_generation") != profile.get("group_generation")):
                raise ProfileReadError("The connection group changed. Refresh before reading it again.")
            credentials.append((profile["id"], stored.get("remember_password"),
                                stored.get("secret_backend"), _stored_ref(profile["id"], stored)))
    scope = report_module.request_fingerprint([current, peers, credentials])
    # Direct in-process callers retain their injected timing hooks. A socket
    # request must own a cancellable flight, not join that synchronous work.
    key = ("live-read", path, store, scope, read_cancel.current() is not None,
           report_module.flight_key(options, connection.get("profile_id"),
                                    {"type": connection.get("type"), "read_only": connection.get("read_only")}))
    settings = report_module.request_fingerprint(payload)
    # Full analysis retains its existing one-analysis-per-connection refusal.
    # Ordinary views allow different settings to run independently.
    if not exclusive:
        key += (settings,)

    def checked_work():
        with read_cancel.scope(read_cancel.current(), guard):
            if guard is not None:
                guard()
            return work()

    result = report_module.run_single_flight(key, settings, checked_work, cancel=read_cancel.current())
    if guard is not None:
        guard()
    return result


def _export_response(payload):
    """A report-1.0 report (as returned by /api/live/report) as Markdown,
    HTML, text, digest or JSON, generated from the JSON, never from the page."""
    from . import report as report_module
    fmt = payload.get("format")
    # source: the connection the report was read from (label, host, port,
    # user, type), shown in every format; any other field is dropped.
    content = report_module.export(payload.get("report"), fmt, payload.get("source"))
    mime = {"markdown": "text/markdown", "html": "text/html", "json": "application/json"}.get(fmt, "text/plain")
    return {"format": fmt, "content": content, "mime": mime,
            "filename": report_module.taken_at_name(payload["report"], fmt)}


def _reraise_redacted(exc, password):
    from .dbops import redact
    text = str(exc)
    scrubbed = redact(text, password)
    if scrubbed != text:
        raise type(exc)(scrubbed)
    raise exc


def _request_profile(connection, store):
    """The saved profile a request names with connection.profile_id, or None."""
    if not isinstance(connection, dict) or not connection.get("profile_id"):
        return None
    profile_id = connection["profile_id"]
    if not isinstance(profile_id, str) or len(profile_id) > 200:
        raise ValueError("Invalid connection field: profile_id")
    return store.get("profile", profile_id)


_REVIEW_TOKENS_LOCK = threading.Lock()


def _review_tokens(workspace):
    from .review_tokens import ReviewTokens
    with _REVIEW_TOKENS_LOCK:
        registry = getattr(workspace, "review_tokens", None)
        if registry is None:
            registry = workspace.review_tokens = ReviewTokens()
        return registry


def _review_identity(connection, workspace):
    from . import dbops
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection,
                                     store=workspace.store)
    return dbops.connection_identity(connection if isinstance(connection, dict) else {}, safety)


def _group_guarded_connection(options, connection, store, identity=None):
    """Keep consent current at transport setup and every client spawn."""
    from .accounts_api import _group_login_guard
    from .connector import MySQLConnection
    conn = MySQLConnection(**options)
    guard = _group_login_guard(store, connection, identity)
    if guard is not None:
        conn._login_guard = guard
        conn._group_login_started = False
    return conn


def _group_login_refusal(audit, statements, exc, target=None):
    """A local consent rejection proves that this client never spawned."""
    if audit.pending:
        audit.complete("refused", outcome="changed", message=str(exc))
        return audit.id
    return audit.outcome(statements, "refused", outcome="changed", message=str(exc), target=target)


def _group_guarded_options(options, connection, store, identity=None, audit=None, statements=(), target=None):
    """Carry trusted consent to clients owned by jobs and persistent sessions.

    The private hook is added after validating request fields, never accepted
    from JSON or written to a client option file. Audit a rejected login at
    the boundary so callers cannot mislabel it as a server or transport error.
    """
    from .accounts_api import _group_login_guard
    from .connection_groups import ProfileReadError
    guard = _group_login_guard(store, connection, identity)
    if guard is None:
        return options

    def checked():
        try:
            guard()
        except ProfileReadError as exc:
            if audit is not None:
                refusal = audit
                if audit.finished:
                    # A persistent session may open a cancellation helper
                    # after its original write completed. Keep that completed
                    # record and audit this newly refused login separately.
                    from . import dbops
                    refusal = dbops.AuditTrail(audit.store, audit.operation, audit.surface,
                                               connection=audit.connection, safety=audit.safety,
                                               password=audit.password, policy=audit.policy)
                    refusal.server(audit.engine, audit.server_version)
                _group_login_refusal(refusal, statements, exc, target)
            raise

    return dict(options, _login_guard=checked)


def _kill_review_details(payload):
    from .locks import _check_kill_args
    connection_id, mode, expect = payload.get("connection_id"), payload.get("mode", "connection"), payload.get("expect")
    _check_kill_args(connection_id, mode, expect)
    statement = ("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % connection_id
    return {"connection_id": connection_id, "mode": mode, "expect": expect, "statements": [statement]}


def _kill_prepare(payload, workspace):
    details = _kill_review_details(payload)
    identity = _review_identity(payload.get("connection"), workspace)
    if details["expect"].get("connection_identity") != identity:
        raise ValueError("The connection or its write policy changed. Refresh and review again.")
    details["review_token"] = _review_tokens(workspace).issue("kill", identity, details)
    return details


def _lifecycle_review_details(plan):
    return {key: value for key, value in plan.items() if key != "review_token"}


def _kill_response(payload, workspace):
    """End one session the user confirmed in the Locks tab (see locks.kill_session).

    Never called by automatic refresh; one connection per request. The
    connection's write policy (dbops.decide) runs first: on a read-only
    connection nothing is opened and the answer is a refused kill-1.0 result.
    KILL is risky, so Production and Development both need the confirmation,
    with ``confirmed: true`` and the ``expect`` identity the sheet captured.
    Refusals (gone, changed, own or server session, idle with KILL QUERY) and
    the server's own denial come back as a kill-1.0 result, not as an error.
    Every attempt gets an audit record (dbops.AuditTrail).
    """
    from . import dbops
    from .live import connection_options
    from .connector import MySQLConnection
    from .locks import KILL_TAG, _check_kill_args, _confirmed, kill_session, refused_result
    connection_id, mode, expect = payload.get("connection_id"), payload.get("mode", "connection"), payload.get("expect")
    _check_kill_args(connection_id, mode, expect)
    connection = payload.get("connection")
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection,
                                     store=workspace.store)
    statement = ("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % connection_id
    decision = dbops.decide(dbops.classify(statement), safety["type"], safety["read_only"], safety["profile_name"])
    plain = connection if isinstance(connection, dict) else {}
    audit = dbops.AuditTrail(workspace.store, "kill_query" if mode == "query" else "kill", "ui",
                             connection={k: plain.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}, safety=safety,
                             password=plain.get("password") if isinstance(plain.get("password"), str) else None,
                             policy=dbops.policy_summary(decision))
    blocked = dbops.gate(decision, confirmed=payload.get("confirmed") is True)
    if blocked:
        result = refused_result(connection_id, mode, blocked[1], status=blocked[0])
        result["audit_id"] = audit.outcome([KILL_TAG + statement], "refused",
                                           outcome="read_only" if blocked[0] == "refused" else blocked[0], message=blocked[1],
                                           target=_confirmed(expect, connection_id, mode))
        return result
    if expect.get("connection_identity") != dbops.connection_identity(plain, safety):
        result = refused_result(connection_id, mode,
                                "The connection or its write policy changed after confirmation; nothing was sent.")
        result["audit_id"] = audit.outcome([KILL_TAG + statement], "refused", outcome="identity",
                                           message=result["message"], target=expect)
        return result
    password = audit.password
    try:
        consume = _review_tokens(workspace).consumer(payload.get("review_token"), "kill",
                    dbops.connection_identity(plain, safety), _kill_review_details(payload))
        payload = _with_saved_password(payload, workspace)
        options = connection_options(payload.get("connection"))
        password = audit.password = options.get("password")
        audit.connection = {k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}
        with _group_guarded_connection(options, plain, workspace.store,
                                       dbops.connection_identity(plain, safety)) as conn:
            return kill_session(conn, connection_id, mode, expect, audit=audit, decision=decision,
                                confirmed=payload.get("confirmed") is True, connection=plain, before_send=consume)
    except ProfileReadError as exc:
        result = refused_result(connection_id, mode, str(exc))
        result["audit_id"] = _group_login_refusal(audit, [KILL_TAG + statement], exc, expect)
        return result
    except KeyboardInterrupt as exc:
        audit.fail(exc, [KILL_TAG + statement])
        raise
    except (ValueError, ConnectorError) as exc:
        audit.fail(exc, [KILL_TAG + statement])
        _reraise_redacted(exc, password)


def _bulk_kill_review_details(payload):
    from .locks import _check_kill_args
    targets = payload.get("targets")
    if not isinstance(targets, list) or not (1 <= len(targets) <= 200):
        raise ValueError("Choose between 1 and 200 sessions to kill.")
    mode = payload.get("mode", "connection")
    if mode not in ("connection", "query"):
        raise ValueError("mode must be 'connection' or 'query'.")
    prepared = []
    statements = []
    for item in targets:
        if not isinstance(item, dict):
            raise ValueError("Each session target must be an object.")
        cid = item.get("connection_id")
        exp = item.get("expect")
        _check_kill_args(cid, mode, exp)
        prepared.append({"connection_id": cid, "mode": mode, "expect": exp})
        statements.append(("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % cid)
    return {"targets": prepared, "mode": mode, "statements": statements}


def _bulk_kill_prepare(payload, workspace):
    details = _bulk_kill_review_details(payload)
    identity = _review_identity(payload.get("connection"), workspace)
    for t in details["targets"]:
        if t["expect"].get("connection_identity") != identity:
            raise ValueError("The connection or its write policy changed. Refresh and review again.")
    details["review_token"] = _review_tokens(workspace).issue("bulk_kill", identity, details)
    return details


def _bulk_kill_response(payload, workspace):
    from . import dbops
    from .live import connection_options
    from .connector import MySQLConnection
    from .locks import (KILL_TAG, kill_session, _confirmed, _kill_context,
                        _kill_batch_after, _after_view, _kill_message)
    details = _bulk_kill_review_details(payload)
    targets, mode = details["targets"], details["mode"]
    connection = payload.get("connection")
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection,
                                     store=workspace.store)
    plain = connection if isinstance(connection, dict) else {}
    decision = dbops.decide(dbops.classify(details["statements"][0]), safety["type"],
                           safety["read_only"], safety["profile_name"])
    password = plain.get("password") if isinstance(plain.get("password"), str) else None
    audits = [dbops.AuditTrail(workspace.store, "kill_query" if mode == "query" else "kill", "ui",
                              connection={k: plain.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}, safety=safety,
                              password=password, policy=dbops.policy_summary(decision)) for _ in targets]
    blocked = dbops.gate(decision, confirmed=payload.get("confirmed") is True)
    if blocked:
        refused = []
        for target, statement, audit in zip(targets, details["statements"], audits):
            cid = target["connection_id"]
            audit_id = audit.outcome([KILL_TAG + statement], "refused",
                                    outcome="read_only" if blocked[0] == "refused" else blocked[0],
                                    message=blocked[1], target=_confirmed(target["expect"], cid, mode))
            refused.append({"connection_id": cid, "status": blocked[0], "message": blocked[1],
                            "audit_id": audit_id, "after": None})
        return {"schema_version": "kill-bulk-1.0", "total": len(targets), "killed": 0, "gone": 0,
                "refused": len(targets), "denied": 0, "errors": 0,
                "message": blocked[1], "results": refused}
    results, accepted = [], []

    def current_policy():
        # A batch may outlive a profile edit. This is a local store read, so
        # retaining the send-boundary policy gate costs no client invocation.
        current = dbops.connection_safety(_request_profile(connection, workspace.store), connection,
                                          store=workspace.store)
        if dbops.connection_identity(plain, current) != dbops.connection_identity(plain, safety):
            return "The connection or its write policy changed after confirmation; nothing was sent."
        return None

    try:
        consume = _review_tokens(workspace).consumer(payload.get("review_token"), "bulk_kill",
                    dbops.connection_identity(plain, safety), details)
        consume()
        payload = _with_saved_password(payload, workspace)
        options = connection_options(payload.get("connection"))
        password = options.get("password")
        for audit in audits:
            audit.password = password
            audit.connection = {k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}
        with _group_guarded_connection(options, plain, workspace.store,
                                       dbops.connection_identity(plain, safety)) as conn:
            context = _kill_context(conn)
            try:
                for target, statement, audit in zip(targets, details["statements"], audits):
                    cid, expect = target["connection_id"], target["expect"]
                    try:
                        res = kill_session(conn, cid, mode, expect, audit=audit, decision=decision,
                                           confirmed=True, connection=plain, settle_s=0,
                                           _batch_context=context, _defer_after=True, before_send=current_policy)
                        row = {"connection_id": cid, "status": res.get("status"), "message": res.get("message"),
                               "audit_id": res.get("audit_id"), "after": res.get("after")}
                        results.append(row)
                        if res.get("status") == "killed":
                            accepted.append((row, res, audit))
                    except ProfileReadError as exc:
                        audit_id = _group_login_refusal(audit, [KILL_TAG + statement], exc, expect)
                        results.append({"connection_id": cid, "status": "refused", "message": str(exc),
                                        "audit_id": audit_id, "after": None})
                    except (ValueError, ConnectorError) as exc:
                        audit.fail(exc, [KILL_TAG + statement])
                        results.append({"connection_id": cid, "status": "error",
                                        "message": dbops.redact(str(exc), password),
                                        "audit_id": audit.id, "after": None})
                if accepted:
                    snapshot = _kill_batch_after(conn, context, [row["connection_id"] for row, _, _ in accepted])
                    for row, res, audit in accepted:
                        row["after"] = _after_view(snapshot.get(row["connection_id"])) if snapshot is not None else None
                        row["message"] = _kill_message(mode, row["connection_id"], res.get("target"),
                                                       row["after"])
            finally:
                # An interrupted after-read cannot turn an accepted KILL into a failed send.
                for row, _res, audit in accepted:
                    audit.complete("ok", outcome="killed", message=row["message"], after=row["after"])
    except ProfileReadError as exc:
        completed = {row["connection_id"] for row in results}
        for target, statement, audit in zip(targets, details["statements"], audits):
            if target["connection_id"] not in completed:
                audit_id = _group_login_refusal(audit, [KILL_TAG + statement], exc, target["expect"])
                results.append({"connection_id": target["connection_id"], "status": "refused", "message": str(exc),
                                "audit_id": audit_id, "after": None})
    except (KeyboardInterrupt, ValueError, ConnectorError) as exc:
        for audit, statement in zip(audits, details["statements"]):
            audit.fail(exc, [KILL_TAG + statement])
        if isinstance(exc, KeyboardInterrupt):
            raise
        _reraise_redacted(exc, password)
    killed_cnt = sum(1 for r in results if r["status"] == "killed")
    gone_cnt = sum(1 for r in results if r["status"] == "gone")
    refused_cnt = sum(1 for r in results if r["status"] == "refused")
    denied_cnt = sum(1 for r in results if r["status"] == "denied")
    error_cnt = sum(1 for r in results if r["status"] == "error")
    msg = "Kill sessions finished: %d killed" % killed_cnt
    if gone_cnt:
        msg += ", %d already gone" % gone_cnt
    if refused_cnt:
        msg += ", %d refused" % refused_cnt
    if denied_cnt:
        msg += ", %d denied" % denied_cnt
    if error_cnt:
        msg += ", %d errors" % error_cnt
    return {
        "schema_version": "kill-bulk-1.0",
        "total": len(targets),
        "killed": killed_cnt, "gone": gone_cnt, "refused": refused_cnt,
        "denied": denied_cnt, "errors": error_cnt,
        "message": msg,
        "results": results,
    }


def _table_columns_response(payload, workspace=None):
    from .live import connection_options
    options = connection_options(payload.get("connection"))
    schema = payload.get("schema")
    table = payload.get("table")
    if not schema or not table:
        raise ValueError("schema and table are required.")
    from . import perf_common
    schema, table = perf_common.check_schema(schema), perf_common.check_schema(table)
    sql = ("/*myflames:tables*/ SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
           "COLUMN_DEFAULT, COLUMN_KEY, EXTRA "
           "FROM information_schema.COLUMNS "
           "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
           "ORDER BY ORDINAL_POSITION" % (perf_common.literal(schema), perf_common.literal(table)))
    with _group_guarded_connection(options, payload.get("connection"), getattr(workspace, "store", None)) as conn:
        rows = conn.query_rows(sql)
        cols = []
        for r in rows:
            cols.append({
                "name": r[0],
                "type": r[1],
                "nullable": (r[2] or "").upper() == "YES",
                "default": r[3],
                "key": r[4],
                "extra": r[5] or "",
            })
        return {"schema": schema, "table": table, "columns": cols}


def _declined_response(payload, store):
    """Record that the person cancelled a write's confirmation sheet: nothing
    was sent. The statements are the ones the sheet showed (redacted)."""
    from . import dbops
    operation, statements = payload.get("operation"), payload.get("statements")
    if not isinstance(operation, str) or not 0 < len(operation) <= 100:
        raise ValueError("Name the operation that was declined.")
    if not isinstance(statements, list) or not 0 < len(statements) <= 1000 or \
            any(not isinstance(text, str) or len(text) > 200000 for text in statements):
        raise ValueError("Send the statements the confirmation showed, as a list of text.")
    target = payload.get("target")
    if target is not None and not isinstance(target, dict):
        raise ValueError("target must be an object.")
    connection = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
    safety = dbops.connection_safety(_request_profile(connection, store), connection)
    password = connection.get("password") if isinstance(connection.get("password"), str) else None
    # A confirmed sheet the workspace itself stopped before sending (its check
    # of the saved connections failed or found another policy) is a refusal.
    reason = payload.get("reason")
    refusals = {"policy_changed": "The connection or its write policy changed after the review; nothing was sent.",
                "policy_unknown": "The saved connections could not be read before sending; nothing was sent."}
    if reason is not None and reason not in refusals:
        raise ValueError("reason must be policy_changed or policy_unknown.")
    status = "refused" if reason else "declined"
    audit_id = dbops.record_outcome(store, operation, statements, "ui", status, outcome=reason or "declined",
                                    message=refusals[reason] if reason else
                                    "Cancelled in the confirmation; nothing was sent.",
                                    connection={k: connection.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")},
                                    safety=safety, target=target, password=password)
    return {"audit_id": audit_id, "status": status}


def _audit_response(path, payload, store):
    """The audit trail of server writes (dbops.py): list, export, settings, declined."""
    from . import dbops
    if path == "/api/audit/declined":
        return _declined_response(payload, store)
    since, server = payload.get("since_days"), payload.get("server") or None
    status = payload.get("status") or None
    if path == "/api/audit/object":
        return dbops.audit_object_report(store, server, payload.get("kind"), payload.get("key"), payload.get("ssh"))
    if path == "/api/audit/list":
        return dbops.audit_report(store, since, server, payload.get("limit", 500), status, payload.get("before"),
                                  record_id=payload.get("id"))
    if path == "/api/audit/export":
        return dbops.audit_export(store, payload.get("format", "json"), since, server, status,
                                  record_id=payload.get("id"))
    if path == "/api/audit/settings":
        if payload.get("retention_days") is not None:
            store.set_audit_retention_days(payload["retention_days"])
        return {"retention_days": store.audit_retention_days()}
    raise ValueError("Unknown workspace operation.")


def _lifecycle_connection(value, safety=None):
    """Connection fields for lifecycle.py (validated like every endpoint), no timeouts."""
    from .live import connection_options
    options = connection_options(value)
    options.pop("connect_timeout", None)
    options.pop("query_timeout", None)
    if safety is not None:
        options.update(profile_id=safety.get("profile_id"), type=safety["type"], read_only=safety["read_only"])
    return options


def _lifecycle_donor(payload, workspace, passwords, plan=None, audit=None):
    """Read donor policy and bind a confirmed pair before retrieving its secret.

    A read-only donor may supply data. Its type and read-only flag are still
    recorded in the plan so a different donor policy needs another review.
    """
    from . import dbops, lifecycle as lc
    donor = payload.get("donor")
    if not isinstance(donor, dict):
        raise ValueError("Choose the donor connection.")
    donor = dict(donor)
    active = payload.get("connection") or {}
    if active.get("group_name") is not None:
        donor["group_name"] = active["group_name"]
    profile = _request_profile(donor, workspace.store)
    if donor.get("profile_id") and profile is None:
        raise ValueError("The donor profile no longer exists. Choose it again.")
    safety = dbops.connection_safety(profile, donor, store=workspace.store)
    options = _lifecycle_connection(donor, safety)
    # CLONE is server to server: never through SSH, refused before any read.
    lc.refuse_clone_through_ssh(options, _lifecycle_connection(payload.get("connection")))
    request = plan.get("request", {}) if plan is not None else payload
    require_ssl = request.get("require_ssl") is not False
    if (options.get("ssl_mode") in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY") or
            (profile or {}).get("ssl_mode") in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY")) and not require_ssl:
        raise ValueError("The donor requires TLS; clone must use REQUIRE SSL.")
    if plan is not None:
        connection = payload.get("connection")
        recipient_safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection, store=workspace.store)
        recipient = _lifecycle_connection(connection, recipient_safety)
        expect = lc._clone_expect(options, recipient, require_ssl, request.get("donor_address"))
        try:
            binding = lc._plan_binding(plan, {"donor_identity": plan["donor"]["identity"], "expect": expect})
        except (KeyError, TypeError, ValueError):
            binding = None
        if (binding is None or plan.get("binding") != binding or plan.get("expect") != expect or
                plan.get("connection_key") != expect["recipient"]["connection_key"] or
                request.get("donor_key") != expect["donor"]["connection_key"] or
                ("require_ssl" in payload and (payload["require_ssl"] is not False) != require_ssl) or
                ("donor_address" in payload and payload["donor_address"] != expect["donor_address"])):
            raise ValueError("The donor or recipient changed. Confirm both endpoints again; nothing was sent.")
        if payload.get("typed_target") != plan.get("target"):
            raise ValueError("Type the exact recipient target from the preflight; nothing was sent.")
    saved = bool(profile and profile.get("remember_password") and not donor.get("password"))
    # Check before retrieving the donor credential, at prepare and start.
    recipient_value = payload.get("connection")
    recipient = _lifecycle_connection(recipient_value)
    recipient_profile = _request_profile(recipient_value, workspace.store)
    # An omitted request mode must not hide a saved explicit downgrade.
    if not recipient.get("ssl_mode") and recipient_profile:
        recipient["ssl_mode"] = recipient_profile.get("ssl_mode")
    if saved:
        lc.require_clone_recipient_tls(options, recipient, saved=saved)
    if donor.get("profile_id"):
        donor = workspace.store.connection_with_saved_password(donor)
    passwords.append(donor.get("password"))
    options = _lifecycle_connection(donor, safety)
    if saved:
        options["_saved_password"] = True
    identity = dbops.connection_identity(donor, safety)
    options = _group_guarded_options(options, donor, workspace.store, identity, audit,
                                      (plan or {}).get("commands", []))
    # A donor login is also part of the active member's request consent.
    active_options = _group_guarded_options({}, active, workspace.store,
                                            _review_identity(active, workspace), audit,
                                            (plan or {}).get("commands", []))
    if active_options.get("_login_guard") is not None:
        donor_guard = options.get("_login_guard")
        def consent():
            active_options["_login_guard"]()
            if donor_guard is not None:
                donor_guard()
        options["_login_guard"] = consent
    return options


def _lifecycle_reraise(exc, passwords):
    from .lifecycle import _safe
    raise type(exc)(_safe(str(exc), passwords)) from None


def _lifecycle_list(value, label):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("%s must be a list of names." % label)
    return value


def _lifecycle_prepare(payload, options, workspace, passwords, audit=None):
    """A lifecycle-plan-1.0 for dump, restore or clone (reads only; MySQL Shell runs its dryRun)."""
    from . import lifecycle as lc
    operation = payload.get("operation")
    threads = payload.get("threads", 4)
    # MySQL Shell dryRun tools belong to a runner that closing the workspace stops.
    with lc._connect(lc.LifecycleConnection, options) as conn, \
            workspace.lifecycle.runner("preflight", options) as runner:
        if operation == "dump":
            return lc.prepare_dump(conn, options, payload.get("directory") or "",
                                   schemas=_lifecycle_list(payload.get("schemas"), "schemas"),
                                   tables=_lifecycle_list(payload.get("tables"), "tables"), threads=threads,
                                   prefer_shell=payload.get("prefer_shell") is not False,
                                   options=payload.get("options"), job=runner)
        if operation == "restore":
            return lc.prepare_restore(conn, options, payload.get("directory") or "",
                                      schema=payload.get("schema") or None,
                                      allow_source_restore=payload.get("allow_source") is True,
                                      existing_objects=payload.get("existing", "refuse"), threads=threads,
                                      resume=payload.get("resume") is True,
                                      staging_directory=payload.get("staging_directory") or None, job=runner)
        if operation == "clone":
            donor = _lifecycle_donor(payload, workspace, passwords, audit=audit)
            with lc._connect(lc.LifecycleConnection, donor) as donor_conn:
                return lc.prepare_clone(donor_conn, conn, donor, options,
                                        data_directory=payload.get("data_directory") or None,
                                        require_ssl=payload.get("require_ssl") is not False,
                                        donor_address=payload.get("donor_address") or None)
    raise ValueError("Choose dump, restore or clone.")


def _lifecycle_start(payload, workspace):
    """Start a confirmed dump, restore or clone job (lifecycle.*_job).

    The connection's write policy runs before anything else (dbops.gate with
    the request's confirmed: true); only then are saved passwords filled. The
    job rebuilds the plan and refuses unless the server, tool, request and the
    typed target still match what the person confirmed. Every attempt is
    audited, the refused ones included. Returns a lifecycle-job-1.0 snapshot.
    """
    from . import dbops, lifecycle as lc
    operation, plan = payload.get("operation"), payload.get("plan")
    if operation not in ("dump", "restore", "clone"):
        raise ValueError("Choose dump, restore or clone.")
    if not isinstance(plan, dict) or plan.get("operation") != operation:
        raise ValueError("Send the plan the preflight returned for this operation.")
    statements = plan.get("commands")
    if not isinstance(statements, list) or not 0 < len(statements) <= 20 or \
            any(not isinstance(text, str) or len(text) > 200000 for text in statements):
        raise ValueError("The plan has no valid commands.")
    connection = payload.get("connection")
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection, store=workspace.store)
    decision = dbops.decide("risky", safety["type"], safety["read_only"], safety["profile_name"])
    plain = connection if isinstance(connection, dict) else {}
    audit = dbops.AuditTrail(workspace.store, operation, "ui",
                             connection={k: plain.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}, safety=safety,
                             password=plain.get("password") if isinstance(plain.get("password"), str) else None,
                             policy=dbops.policy_summary(decision))
    confirmed = payload.get("confirmed") is True
    passwords = [audit.password]
    if isinstance(payload.get("donor"), dict):
        passwords.append(payload["donor"].get("password"))
    blocked = dbops.gate(decision, confirmed=confirmed)
    if blocked:
        audit_id = audit.outcome(lc._safe(statements, passwords), "refused", outcome=blocked[0],
                                 message=lc._safe(blocked[1], passwords))
        return {"schema_version": "lifecycle-job-1.0", "id": None, "operation": operation, "state": "refused",
                "progress": None, "detail": None, "events": [], "error": lc._safe(blocked[1], passwords),
                "result": {"status": blocked[0], "verification": [], "audit_id": audit_id}}
    try:
        consume = _review_tokens(workspace).consumer(plan.get("review_token"), "lifecycle",
                    dbops.connection_identity(plain, safety), _lifecycle_review_details(plan))
        if operation == "restore":
            lc.validate_restore_target(plan, payload.get("typed_target"))
        donor = _lifecycle_donor(payload, workspace, passwords, plan, audit) if operation == "clone" else None
        filled = _with_saved_password(payload, workspace)
        # Track the secret before validation, which can itself fail.
        passwords.append((filled.get("connection") or {}).get("password"))
        options = _lifecycle_connection(filled.get("connection"), safety)
        options = _group_guarded_options(options, connection, workspace.store,
                                          dbops.connection_identity(plain, safety), audit, statements)
        audit.password = options.get("password")
        audit.connection = {k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}
        kwargs = dict(confirmed=confirmed, typed_target=payload.get("typed_target"), audit=audit, safety=safety,
                      start=False)
        if operation == "dump":
            job = lc.dump_job(options, plan, **kwargs)
        elif operation == "restore":
            job = lc.restore_job(options, plan, typed_acknowledgement=payload.get("typed_acknowledgement"), **kwargs)
        else:
            job = lc.clone_job(donor, options, plan, **kwargs)
        lookup = lambda key: workspace.store.get('profile', key)
        if options.get('_login_guard') is None:
            lc.bind_profile_policy(job, options, safety, lookup)
        else:
            job.policy_check = options['_login_guard']
        if donor is not None and donor.get('_login_guard') is None:
            donor_safety = dbops.connection_safety(_request_profile(payload['donor'], workspace.store), donor)
            donor_safety.update(type=donor['type'], read_only=donor['read_only'])
            lc.bind_profile_policy(job, donor, donor_safety, lookup, write=False)
        if donor is not None and donor.get('_login_guard') is not None:
            previous = getattr(job, 'policy_check', None)
            def consent():
                if previous is not None:
                    previous()
                donor['_login_guard']()
            job.policy_check = consent
        job.review_consumer = consume
        return _lifecycle_add(workspace, job, str(plan.get("target")), connection)
    except Exception as exc:
        mismatch = isinstance(exc, lc.RestoreTargetMismatch)
        audit.outcome(lc._safe(statements, passwords), "refused",
                      outcome="target_mismatch" if mismatch else "not_started",
                      target=lc._safe(plan.get("server", {}).get("identity"), passwords) if mismatch else None,
                      message=lc._safe(str(exc), passwords))
        _lifecycle_reraise(exc, passwords)


_LIFECYCLE_OWNERS_LOCK = threading.Lock()


def _lifecycle_owner(connection):
    """The endpoint a lifecycle job belongs to: dbops.identity_endpoint of the
    request's connection identity (saved connection id, host, port, user and
    TLS settings, without the type and read-only flag), so a policy change
    never cuts a job off from the page that started it."""
    from . import dbops
    return dbops.identity_endpoint(dbops.connection_identity(connection if isinstance(connection, dict) else {}))


def _lifecycle_owners(workspace):
    """job id -> owner endpoint, for the jobs this workspace started (memory only)."""
    with _LIFECYCLE_OWNERS_LOCK:
        owners = getattr(workspace, "lifecycle_owners", None)
        if owners is None:
            owners = workspace.lifecycle_owners = {}
        return owners


def _lifecycle_add(workspace, job, target, connection):
    """Register a confirmed job with its owner before it starts, so a page whose
    Start reply was lost finds it through /api/lifecycle/jobs."""
    owners = _lifecycle_owners(workspace)
    with _LIFECYCLE_OWNERS_LOCK:
        owners[job.id] = _lifecycle_owner(connection)
        try:
            result = workspace.lifecycle.add(job, target)
        except Exception:
            owners.pop(job.id, None)
            raise
        # add() expires finished jobs. Prune every owner's entries here, even
        # if no page ever polls /jobs, without racing another registration.
        with workspace.lifecycle.lock:
            live = set(workspace.lifecycle.jobs)
        for job_id in list(owners):
            if job_id not in live:
                del owners[job_id]
        return result


def _lifecycle_jobs_response(payload, workspace):
    """lifecycle-jobs-1.0: the dump, restore and clone jobs this workspace holds
    for the connection's endpoint, oldest first (running and finished; finished
    ones expire with LifecycleJobs). It opens no database connection and starts
    nothing: a page that reloaded, or whose Start reply was lost, finds and
    manages its jobs here instead of starting them again."""
    connection = payload.get("connection")
    if not isinstance(connection, dict):
        raise ValueError("Send the connection whose jobs to list.")
    owner = _lifecycle_owner(connection)
    owners = _lifecycle_owners(workspace)
    with _LIFECYCLE_OWNERS_LOCK:
        mine = [job_id for job_id, key in owners.items() if key == owner]
    jobs = []
    for job_id in mine:
        try:
            jobs.append(workspace.lifecycle.get(job_id).snapshot())
        except ValueError:
            with _LIFECYCLE_OWNERS_LOCK:
                owners.pop(job_id, None)
    return {"schema_version": "lifecycle-jobs-1.0", "jobs": jobs}


def _dump_tool_capabilities(report):
    """Publish options for detected tools without executing a dump."""
    from .dump_options import capability_catalog
    for name, tool in report.get('tools', {}).items():
        if name in ('mysqlsh', 'mysqldump', 'mariadb-dump'):
            tool['dump_capabilities'] = capability_catalog(name, tool_version=tool.get('version') or '')
            tool['dump_table_capabilities'] = capability_catalog(name, table_scope=True, tool_version=tool.get('version') or '')
    return report


def _lifecycle_response(path, payload, workspace):
    """Server lifecycle endpoints (lifecycle.py; contract in docs/DEVELOPER_REFERENCE.md)."""
    from . import dbops, lifecycle as lc
    if path in _LIFECYCLE_JOB_LIST_PATHS:
        return _lifecycle_jobs_response(payload, workspace)
    if path in ("/api/lifecycle/status", "/api/lifecycle/cancel"):
        connection, job_id = payload.get("connection"), payload.get("id")
        owners = _lifecycle_owners(workspace)
        with _LIFECYCLE_OWNERS_LOCK:
            matches = (isinstance(connection, dict) and isinstance(job_id, str) and
                       owners.get(job_id) == _lifecycle_owner(connection))
        if not matches:
            raise ValueError("No such job.")
        try:
            job = workspace.lifecycle.get(job_id)
        except ValueError:
            raise ValueError("No such job.") from None
        if path == "/api/lifecycle/cancel":
            job.cancel()
        return job.snapshot()
    if path == "/api/lifecycle/start":
        return _lifecycle_start(payload, workspace)
    if path == "/api/lifecycle/choose-directory":
        if payload:
            raise ValueError("Folder selection does not accept arguments.")
        from .folder_picker import choose_directory
        return choose_directory()
    if path == "/api/lifecycle/dump-info":
        if set(payload) - {"directory"}:
            raise ValueError("Dump folder facts accept only a directory.")
        return lc.dump_info(payload.get("directory"))
    if path == "/api/lifecycle/tools" and not isinstance(payload.get("connection"), dict):
        return _dump_tool_capabilities(lc.detect_tools(check_updates=payload.get("check_updates") is True))
    if path in ("/api/lifecycle/upgrade-check", "/api/lifecycle/prepare") and payload.get("config_path") is not None:
        raise ValueError("config_path is not accepted over HTTP; use the CLI --config-path option.")
    connection = payload.get("connection")
    passwords = [connection.get("password")] if isinstance(connection, dict) else []
    if isinstance(payload.get("donor"), dict):
        passwords.append(payload["donor"].get("password"))
    try:
        safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection, store=workspace.store)
        payload = _with_saved_password(payload, workspace)
        passwords.append((payload.get("connection") or {}).get("password"))
        options = _lifecycle_connection(payload.get("connection"), safety)
        consent_audit = None
        if isinstance(connection, dict) and connection.get('group_name') is not None:
            consent_audit = dbops.AuditTrail(workspace.store, 'lifecycle_preflight', 'ui',
                                           connection=connection, safety=safety, password=options.get('password'))
        options = _group_guarded_options(options, connection, workspace.store,
                                          dbops.connection_identity(connection, safety), consent_audit, [path])
        if path == "/api/lifecycle/tools":
            try:
                with lc._connect(lc.LifecycleConnection, options) as conn:
                    server = lc.read_server(conn, options)
            except Exception as exc:
                if client_missing_error(exc) is None:
                    raise
                tools = _dump_tool_capabilities(lc.detect_tools(check_updates=payload.get("check_updates") is True))
                return dict(tools, server={}, offered={}, server_error={
                    "code": "client-missing", "error": lc._safe(str(exc), passwords)})
            tools = _dump_tool_capabilities(lc.detect_tools(server=server,
                check_updates=payload.get("check_updates") is True))
            return dict(tools, server={k: server[k] for k in ("engine", "version")},
                        offered=lc.offered_tools(server, tools))
        if path == "/api/lifecycle/upgrade-targets":
            from . import upgrade_targets
            with lc._connect(lc.LifecycleConnection, options) as conn:
                return upgrade_targets.upgrade_targets(
                    conn, tools=lc.detect_tools(), fetch=getattr(workspace, "release_reader", None) or upgrade_targets.offline)
        if path == "/api/lifecycle/upgrade-check":
            with lc._connect(lc.LifecycleConnection, options) as conn, \
                    workspace.lifecycle.runner("upgrade_check", options) as runner:
                report = lc.upgrade_check(conn, payload.get("target_version"), options,
                                          prefer_shell=payload.get("prefer_shell") is not False, job=runner)
            report["server"] = {k: report["server"][k] for k in ("engine", "version", "identity")}
            return report
        if path == "/api/lifecycle/prepare":
            plan = lc._report_safe(_lifecycle_prepare(payload, options, workspace, passwords, consent_audit), passwords)
            plan["review_token"] = _review_tokens(workspace).issue("lifecycle",
                dbops.connection_identity(connection, safety), _lifecycle_review_details(plan))
            return plan
    except Exception as exc:
        _lifecycle_reraise(exc, passwords)
    raise ValueError("Unknown workspace operation.")


_REPLICATION_LOCK = threading.Lock()


def _replication_loops(workspace):
    """The workspace's skip loops (replication.SkipLoopRegistry), in memory
    only: a restart starts empty and nothing is ever resumed. Loops are told
    to stop when the process exits."""
    with _REPLICATION_LOCK:
        registry = getattr(workspace, "replication_loops", None)
        if registry is None:
            import atexit
            from .replication import SkipLoopRegistry
            registry = SkipLoopRegistry()
            workspace.replication_loops = registry
            atexit.register(registry.stop_all)
        # Profile handlers receive the store, not the HTTP server. Keep their
        # cancellation hook on this workspace's store rather than a global.
        workspace.store.replication_loops = registry
        return registry


def _replication_audit(workspace, operation, connection, safety, decision):
    from . import dbops
    plain = dict(connection) if isinstance(connection, dict) else {}
    password = plain.get("password") if isinstance(plain.get("password"), str) else None
    state = {"connection": {k: plain.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")},
             "password": password, "identity": dbops.connection_identity(plain, safety)}

    def decision_reader():
        # A confirmation authorizes the saved policy at that moment. Loops
        # must see later profile edits even when no browser request arrives.
        profile = _request_profile(plain, workspace.store)
        current = dbops.connection_safety(profile, plain, store=workspace.store)
        if plain.get("profile_id") and (profile is None or not current["endpoint_matches"]):
            # Deleting or moving a profile must not turn an old confirmation
            # into permission on an unsaved Production connection.
            current["read_only"] = True
        verdict = dbops.classify(";\n".join(decision.get("statements") or ["START REPLICA SQL_THREAD"]))
        return dbops.decide(verdict, current["type"], current["read_only"], current["profile_name"])

    def factory():
        return dbops.AuditTrail(workspace.store, operation, "ui", connection=state["connection"], safety=safety,
                                password=state["password"], policy=dbops.policy_summary(decision))
    state["decision_reader"] = decision_reader
    return factory, state


def _replication_preflight_error(audit, state, exc, target, statements=()):
    """Finish an unsent request without treating a preflight disconnect as
    an uncertain write. A pending write remains the core's responsibility."""
    audit.password, audit.connection = state["password"], state["connection"]
    if audit.finished:
        return
    if audit.pending:
        audit.fail(exc, statements)
    else:
        text = str(exc) or "Cancelled before sending."
        audit.outcome(list(statements), "refused", outcome="unsent", error=text,
                      message="Nothing was sent: %s" % text, target=target)


def _replication_open(payload, workspace, state):
    """A connection for a replication write, with the saved password filled
    only now (after the policy). The caller closes it."""
    from .live import connection_options
    options = connection_options(_with_saved_password(payload, workspace).get("connection"))
    state["password"] = options.get("password") or state["password"]
    state["connection"] = {k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}
    options["query_timeout"] = 30
    return _group_guarded_connection(options, payload.get("connection"), workspace.store,
                                     state.get("identity"))


def _replication_skip_response(payload, workspace, safety=None):
    """Skip the one failed transaction the person confirmed (replication.skip_one)."""
    from . import dbops, replication as rp
    connection = payload.get("connection")
    safety = safety or dbops.connection_safety(None, connection)
    decision = dbops.decide("risky", safety["type"], safety["read_only"], safety["profile_name"])
    factory, state = _replication_audit(workspace, rp.OPERATION_SKIP, connection, safety, decision)
    audit = factory()
    target, tagged = {"channel": payload.get("channel")}, []
    try:
        profile = _request_profile(connection, workspace.store)
        if isinstance(connection, dict) and connection.get("profile_id") and profile is None:
            raise ValueError("This saved connection no longer exists. Choose a connection again.")
        safety = dbops.connection_safety(profile, connection, store=workspace.store)
        audit.safety = safety
        audit.policy = dbops.policy_summary(dbops.decide("risky", safety["type"], safety["read_only"],
                                                         safety["profile_name"]))
        channel, statements = payload.get("channel"), payload.get("statements")
        if not isinstance(channel, str) or len(channel) > 256:
            raise ValueError("Name the channel to skip on.")
        expect = rp.check_expect(payload.get("expect"))
        target = expect
        if statements is not None and (not isinstance(statements, list) or len(statements) > 20 or
                                       any(not isinstance(s, str) or len(s) > 2000 for s in statements)):
            raise ValueError("statements must be the list of statements the confirmation showed.")
        tagged = [rp.SKIP_TAG + statement for statement in statements or ()]
        decision = dbops.decide(dbops.classify(";\n".join(statements) if statements else "START REPLICA SQL_THREAD"),
                                safety["type"], safety["read_only"], safety["profile_name"])
        # Replace the validation context with the exact, validated policy;
        # neither context has written an audit row or contacted the server.
        factory, state = _replication_audit(workspace, rp.OPERATION_SKIP, connection, safety, decision)
        audit = factory()
        confirmed = payload.get("confirmed") is True
        if dbops.gate(decision, confirmed):
            return rp.skip_one(None, channel, expect, audit=audit, decision=decision, confirmed=confirmed,
                               statements=statements, decision_reader=state["decision_reader"])
        consume = _review_tokens(workspace).consumer(payload.get("review_token"), "replication_skip",
                    dbops.connection_identity(connection, safety),
                    {"channel": channel, "expect": expect, "statements": statements})
        with _replication_open(payload, workspace, state) as conn:
            audit.password, audit.connection = state["password"], state["connection"]
            return rp.skip_one(conn, channel, expect, audit=audit, decision=decision, confirmed=True,
                               statements=statements, decision_reader=state["decision_reader"], before_send=consume)
    except ProfileReadError as exc:
        result = {"schema_version": rp.SKIP_SCHEMA_VERSION, "engine": None, "server_version": None,
                  "channel": payload.get("channel"), "method": target.get("method"),
                  "statements": list(payload.get("statements") or []), "verified": [], "target": target,
                  "plan": None, "after": None}
        return rp._unsent(result, audit, "refused", "changed", str(exc), target)
    except (Exception, KeyboardInterrupt) as exc:
        _replication_preflight_error(audit, state, exc, target, tagged)
        _reraise_redacted(exc, state["password"])


def _replication_loop_start(payload, workspace, safety=None, identity=None):
    """Start one skip loop (replication.SkipLoop) after its own confirmation."""
    from . import dbops, replication as rp
    connection = payload.get("connection")
    safety = safety or dbops.connection_safety(None, connection)
    decision = dbops.decide(dbops.classify("START REPLICA SQL_THREAD"), safety["type"], safety["read_only"],
                            safety["profile_name"])
    factory, state = _replication_audit(workspace, rp.OPERATION_LOOP, connection, safety, decision)
    audit = factory()
    target = {key: payload.get(key) for key in ("channel", "codes", "max_skips", "max_minutes")}
    conn = None
    try:
        profile = _request_profile(connection, workspace.store)
        if isinstance(connection, dict) and connection.get("profile_id") and profile is None:
            raise ValueError("This saved connection no longer exists. Choose a connection again.")
        safety = dbops.connection_safety(profile, connection, store=workspace.store)
        audit.safety = safety
        audit.policy = dbops.policy_summary(dbops.decide("risky", safety["type"], safety["read_only"],
                                                         safety["profile_name"]))
        identity = dbops.connection_identity(connection if isinstance(connection, dict) else {}, safety)
        channel = payload.get("channel")
        if not isinstance(channel, str) or len(channel) > 256:
            raise ValueError("Name the channel to watch.")
        codes = rp.check_codes(payload.get("codes"))
        max_skips = rp.check_max_skips(payload.get("max_skips"))
        minutes = payload.get("max_minutes")
        if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)
                                   or not 1 <= minutes <= 1440):
            raise ValueError("Choose a maximum duration from 1 to 1440 minutes.")
        poll_s = rp.check_poll(payload.get("poll_s", rp.LOOP_DEFAULT_POLL_S))
        receiver_guard = payload.get("receiver_guard")
        if receiver_guard not in (None, "on", "off"):
            raise ValueError("receiver_guard must be on or off from the loop confirmation.")
        decision = dbops.decide(dbops.classify("START REPLICA SQL_THREAD"), safety["type"], safety["read_only"],
                                safety["profile_name"])
        factory, state = _replication_audit(workspace, rp.OPERATION_LOOP, connection, safety, decision)
        audit = factory()
        target = {"channel": channel, "codes": codes, "max_skips": max_skips, "max_minutes": minutes}
        blocked = dbops.gate(decision, payload.get("confirmed") is True)
        if blocked:
            status = "refused" if blocked[0] == "refused" else "needs_confirmation"
            audit_id = audit.outcome([], "refused", outcome="read_only" if status == "refused" else status,
                                     message=blocked[1], target=target)
            return {"schema_version": rp.LOOP_SCHEMA_VERSION, "job_id": None, "state": status, "channel": channel,
                    "message": blocked[1], "audit_id": audit_id}
        conn = dbops.TargetEvidenceConnection(_replication_open(payload, workspace, state))
        report = rp.read_status(conn)
        audit.server(report.get("engine"), report.get("server_version"))
        if not report.get("available") or not report.get("replica"):
            raise ValueError(report.get("message") or "This server is not a replica.")
        found = rp.find_channel(report, channel)
        if found is None or found.get("kind") == "group_replication":
            raise ValueError("This server has no asynchronous replication channel named %r." % (channel,))
        loop = rp.SkipLoop(conn, channel, codes, max_skips, max_duration_s=minutes * 60 if minutes else None,
                           poll_s=poll_s, decision=decision, audit_factory=factory, close_conn=True,
                           decision_reader=state["decision_reader"], receiver_required=receiver_guard == "on")
        return _replication_loops(workspace).start(identity, loop)
    except (Exception, KeyboardInterrupt) as exc:
        _replication_preflight_error(audit, state, exc, target)
        if conn is not None:
            conn.close()
        _reraise_redacted(exc, state["password"])


def _replication_response(path, payload, workspace):
    """Replication status, skip one transaction, and the skip loops
    (replication.py; contract in docs/DEVELOPER_REFERENCE.md)."""
    from . import dbops, replication as rp
    if path == "/api/live/replication":
        from .live import connection_options
        options = connection_options(payload.get("connection"))
        options["query_timeout"] = 30
        try:
            with _group_guarded_connection(options, payload.get("connection"),
                                           getattr(workspace, "store", None)) as conn:
                conn = dbops.TargetEvidenceConnection(conn)
                # Source versions: the relay log, else a matching saved connection
                # (opened at most once per process for the same settings and password).
                conn_in = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
                from .connection_groups import begin_read, finish_read, members, request_profiles
                store = getattr(workspace, 'store', None)
                profiles, current = request_profiles(store, conn_in)
                pid = current['id'] if current else None
                opener = None
                if current and current.get('group'):
                    if conn_in.get('group_name') is not None:
                        opener = rp.profile_opener(store, pid, current,
                                                   group_name=conn_in.get('group_name'), connection=conn_in)
                    else:
                        opener = rp.profile_opener(store, pid, current)
                identity = begin_read(store, current)
                report = rp.read_status(conn, source_versions=True, profiles=members(profiles, current),
                                        open_profile=opener,
                                        replica_host=rp.reached_host(options),
                                        current_profile_id=pid, current_profile=current,
                                        topology_tiers=True, _identity=identity)
                finish_read(store, report, conn_in, profiles, current, identity=identity)
                if report.get("available") and report.get("replica"):
                    guard = rp.receiver_guard_plan(conn, report)
                    report.update(guard)
                    for channel in report.get("channels", ()):
                        if channel.get("skip_plan"):
                            plan = channel["skip_plan"]
                            plan.update(guard)
                            if plan.get("available"):
                                try:
                                    if store is None:
                                        raise ValueError('Workspace store unavailable.')
                                    review_identity = _review_identity(payload.get("connection"), workspace)
                                except (AttributeError, ValueError, OSError, sqlite3.Error):
                                    # The status remains readable when optional local
                                    # storage fails. No write token bypass is allowed.
                                    plan.update(available=False, message="The connection's write policy could not be read. "
                                                "Refresh before reviewing a change.")
                                else:
                                    plan["review_token"] = _review_tokens(workspace).issue("replication_skip",
                                        review_identity,
                                        {"channel": plan["channel"], "expect": plan["identity"], "statements": plan["statements"]})
                return report
        except (ValueError, ConnectorError) as exc:
            _reraise_redacted(exc, options.get("password"))
    if path == "/api/live/replication/skip":
        return _replication_skip_response(payload, workspace)
    if path == "/api/live/replication/skip-loop/start":
        return _replication_loop_start(payload, workspace)
    connection = payload.get("connection")
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
    identity = dbops.connection_identity(connection if isinstance(connection, dict) else {}, safety)
    job_id = payload.get("job_id")
    if job_id is not None and (not isinstance(job_id, str) or len(job_id) > 64):
        raise ValueError("Invalid job_id.")
    registry = _replication_loops(workspace)
    if path == "/api/live/replication/skip-loop/status":
        return registry.status(identity, job_id)
    if job_id is None:
        raise ValueError("Name the skip loop to stop (job_id).")
    return registry.stop(identity, job_id)


# Endpoints that open a database connection. Each accepts connection.profile_id
# so a remembered password is filled in on the server, for that request only.
#: Read-only live server views answered by _discover_response.
_SERVER_VIEW_PATHS = ("/api/live/sessions/history", "/api/live/index-suggestion", "/api/live/top", "/api/live/slow-log", "/api/live/running", "/api/live/sessions", "/api/live/ddl-progress", "/api/live/locks",
                      "/api/live/innodb", "/api/live/error-log", "/api/live/cluster", "/api/live/health",
                      "/api/live/watch", "/api/live/watch/capture",
                      "/api/live/memory", "/api/live/waits", "/api/live/indexes", "/api/live/tables", "/api/live/schemas",
                      "/api/live/io", "/api/live/config", "/api/live/instruments",
                      "/api/live/report", "/api/live/explain-connection", "/api/live/proxysql",
                      "/api/live/proxysql/digests", "/api/live/readyset", "/api/live/readyset/explain-cache",
                      "/api/live/readyset/export-caches", "/api/live/digest-histogram", "/api/live/digest-tables")
_SINGLE_FLIGHT_READ_PATHS = ("/api/live/health", "/api/live/waits", "/api/live/io", "/api/live/memory",
                            "/api/live/locks", "/api/live/indexes", "/api/live/tables")
#: /api/live/kill is the one write: a confirmed KILL of one session (locks.kill_session).
#: It fills the saved password itself, after the connection's write policy.
#: The Query editor's run and commit (editor_session.py) are writes too: the
#: policy runs first and the saved password is filled only after it.
_EDITOR_WRITE_PATHS = ("/api/editor/run", "/api/editor/commit")
_WRITE_PATHS = ("/api/live/kill", "/api/live/kill/bulk") + _EDITOR_WRITE_PATHS
#: Editor open and stop connect with the request's connection (stop sends
#: KILL QUERY for the tab's own session from a second connection).
_EDITOR_CONNECTION_PATHS = ("/api/editor/open", "/api/editor/stop")
_CONNECTION_PATHS = (("/api/live/readiness", "/api/connection/test", "/api/live/start") + _WRITE_PATHS + _SERVER_VIEW_PATHS
                     + _EDITOR_CONNECTION_PATHS)
#: Server lifecycle (lifecycle.py, answered by _lifecycle_response). Tools, the
#: upgrade check and the dump, restore and clone plans only read: they get the
#: saved password like the views. Start is a write: the connection's policy
#: runs first (dbops.gate with confirmed: true), then the saved password is
#: filled, and every attempt is audited. Status and cancel act on a job the
#: server holds (cancel's KILL QUERY reuses the job's own connection).
_LIFECYCLE_READ_PATHS = ("/api/lifecycle/tools", "/api/lifecycle/upgrade-targets", "/api/lifecycle/upgrade-check",
                         "/api/lifecycle/prepare")
_LIFECYCLE_WRITE_PATHS = ("/api/lifecycle/start",)
_LIFECYCLE_PATHS = _LIFECYCLE_READ_PATHS + _LIFECYCLE_WRITE_PATHS + ("/api/lifecycle/status",
                                                                     "/api/lifecycle/cancel")
#: The jobs of one connection (answered by _lifecycle_jobs_response): opens no
#: database connection, so it is neither a read nor a write path.
_LIFECYCLE_JOB_LIST_PATHS = ("/api/lifecycle/jobs",)
_LIFECYCLE_PATHS = _LIFECYCLE_PATHS + _LIFECYCLE_JOB_LIST_PATHS + ("/api/lifecycle/choose-directory",
                                                              "/api/lifecycle/dump-info")
_WRITE_PATHS = _WRITE_PATHS + _LIFECYCLE_WRITE_PATHS
_CONNECTION_PATHS = _CONNECTION_PATHS + _LIFECYCLE_READ_PATHS + _LIFECYCLE_WRITE_PATHS
#: Replication (replication.py, answered by _replication_response). Status only
#: reads and gets the saved password like the views. Skip and skip-loop start
#: are writes: the connection's policy runs first (dbops.gate with confirmed:
#: true), then the saved password is filled, and every attempt is audited. Loop
#: status and stop open no connection: they act on loops this process holds,
#: keyed by connection identity (dbops.connection_identity).
_REPLICATION_READ_PATHS = ("/api/live/replication",)
_REPLICATION_WRITE_PATHS = ("/api/live/replication/skip", "/api/live/replication/skip-loop/start")
_REPLICATION_PATHS = _REPLICATION_READ_PATHS + _REPLICATION_WRITE_PATHS + (
    "/api/live/replication/skip-loop/status", "/api/live/replication/skip-loop/stop")
_WRITE_PATHS = _WRITE_PATHS + _REPLICATION_WRITE_PATHS
_CONNECTION_PATHS = _CONNECTION_PATHS + _REPLICATION_READ_PATHS + _REPLICATION_WRITE_PATHS
#: The Query editor's schema browser (schema_browser.py): the tables of one
#: schema, the columns and indexes of one table; and Server > Tables' Table
#: structure (table-structure-1.0). Read-only, with the saved password like
#: the views; answered by _catalog_response.
_CATALOG_PATHS = ("/api/live/catalog", "/api/live/table-structure")
_TABLE_COLUMNS_PATHS = ("/api/live/table-columns",)
_CONNECTION_PATHS = _CONNECTION_PATHS + _CATALOG_PATHS + _TABLE_COLUMNS_PATHS
#: The command palette's object search (object-search-1.0): one bounded,
#: timed-out read of the connection, answered by _object_search_response.
_OBJECT_SEARCH_PATHS = ("/api/live/object-search",)
_CONNECTION_PATHS = _CONNECTION_PATHS + _OBJECT_SEARCH_PATHS

#: The route tables live in ui_routes.py so this module knows every path
#: without importing the feature handler modules at startup; those load on
#: the first request that needs them (see api_response).
from .ui_routes import DIAGRAM_PATHS as _DIAGRAM_PATHS, DIAGRAM_READ_PATH as _DIAGRAM_READ_PATH
_CONNECTION_PATHS = _CONNECTION_PATHS + (_DIAGRAM_READ_PATH,)
#: Schema versions (schema_versions_api.py). Save and compare read live
#: databases; each side fills its own connection's saved password, so they
#: are not in _CONNECTION_PATHS (which fills only the top-level connection).
from .ui_routes import (SCHEMA_VERSION_PATHS as _SCHEMA_VERSION_PATHS,
                        SCHEMA_VERSION_LARGE_PATHS as _SCHEMA_VERSION_LARGE)
from .ui_routes import COMPARE_PATHS as _COMPARE_PATHS
#: Baselines (baselines.py): a Health reading or Query digest totals kept on
#: this computer. List and save read the server's identity through the
#: connection (its saved password filled for that read); get and delete open
#: no connection.
from .ui_routes import BASELINE_PATHS as _BASELINE_PATHS


#: Parameter version catalogs (param_versions.py): answers from the generated
#: sysvar_history modules only. No connection, no server read, no audit.
_PARAM_VERSION_PATHS = ("/api/config/versions", "/api/config/versions/compare",
                        "/api/config/versions/history", "/api/config/versions/names")
#: Server variables (variables.py, answered by _variable_response). Read and
#: plan only read: they get the saved password like the views. Apply is a
#: write: the connection's policy runs first (dbops.gate with confirmed: true),
#: then the saved password is filled, and every attempt is audited.
_VARIABLE_READ_PATHS = ("/api/live/variable", "/api/live/variable/plan")
_VARIABLE_WRITE_PATHS = ("/api/live/variable/apply",)
_VARIABLE_PATHS = _VARIABLE_READ_PATHS + _VARIABLE_WRITE_PATHS
_WRITE_PATHS = _WRITE_PATHS + _VARIABLE_WRITE_PATHS
_CONNECTION_PATHS = _CONNECTION_PATHS + _VARIABLE_PATHS
# Rolling group variables route each member through its own stored login.
# Dispatch these before the generic saved-password path. Apply is a write;
# plan reads on request and Stop only prevents the next member starting.
_TOPOLOGY_CHANGE_PATHS = ('/api/live/topology/variable/plan', '/api/live/topology/variable/apply',
                         '/api/live/topology/variable/stop')
_WRITE_PATHS = _WRITE_PATHS + ('/api/live/topology/variable/apply',)
#: Privileges and Users and roles (accounts_api.py, answered by
#: _accounts_response). /api/live/privileges and /api/accounts/list, read and
#: plan only read: they get the saved password like the views. Apply is a
#: write: the connection's policy runs first, then the saved password is
#: filled, and every attempt is audited.
from .ui_routes import ACCOUNT_READ_PATHS as _ACCOUNT_READ_PATHS, ACCOUNT_WRITE_PATHS as _ACCOUNT_WRITE_PATHS
_ACCOUNT_PATHS = _ACCOUNT_READ_PATHS + _ACCOUNT_WRITE_PATHS
_WRITE_PATHS = _WRITE_PATHS + _ACCOUNT_WRITE_PATHS
_CONNECTION_PATHS = _CONNECTION_PATHS + _ACCOUNT_PATHS
#: Reviewed Cluster and ProxySQL changes (cluster.py, proxysql.py and
#: reviewed_writes.py, answered by _action_response). Plan reads the target
#: and returns the exact statements with a review token: it gets the saved
#: password like the views. Action is a write: the policy runs first, the
#: token must match the plan as reviewed, then the saved password is filled,
#: the target is read and planned again, and every attempt is audited.
_ACTION_KINDS = {"/api/live/cluster/plan": "cluster", "/api/live/cluster/action": "cluster",
                 "/api/live/proxysql/plan": "proxysql", "/api/live/proxysql/action": "proxysql",
                 "/api/live/readyset/plan": "readyset", "/api/live/readyset/action": "readyset"}
_ACTION_WRITE_PATHS = ("/api/live/cluster/action",
                       "/api/live/proxysql/action", "/api/live/readyset/action")
_ACTION_PATHS = tuple(_ACTION_KINDS)
_WRITE_PATHS = _WRITE_PATHS + _ACTION_WRITE_PATHS
_CONNECTION_PATHS = _CONNECTION_PATHS + _ACTION_PATHS
#: Import CSV (csv_import_api.py): choose or open a file, inspect it, plan
#: and apply an import, its progress and Stop. Plan reads the server with the
#: saved password; apply is a write: the policy runs first (Read-only refuses,
#: audited, before any connection opens), the review token must match the
#: whole plan, then the saved password is filled. The handler fills passwords
#: itself, so none of these is in _CONNECTION_PATHS.
from .ui_routes import CSV_IMPORT_PATHS as _CSV_IMPORT_PATHS, CSV_IMPORT_WRITE_PATHS as _CSV_IMPORT_WRITE_PATHS
from .ui_routes import SLOW_LOG_PATHS as _SLOW_LOG_PATHS
_WRITE_PATHS = _WRITE_PATHS + _CSV_IMPORT_WRITE_PATHS
#: Files uses SSH credentials in its own endpoint-bound namespace. These
#: host-level file operations are independent of database write policy.
_SSH_CONNECTION_PATHS = ("/api/ssh/connections/list", "/api/ssh/connections/save",
                         "/api/ssh/connections/delete", "/api/ssh/connections/test",
                         "/api/ssh/hostkey/scan", "/api/ssh/hostkey/trust", "/api/ssh/keys")
_FILES_PATHS = ("/api/files/remote/list", "/api/files/remote/mkdir", "/api/files/remote/rename",
                "/api/files/remote/remove", "/api/files/local/list", "/api/files/local/mkdir",
                "/api/files/local/rename", "/api/files/local/reveal", "/api/files/upload", "/api/files/download",
                "/api/files/jobs", "/api/files/cancel", "/api/files/retry", "/api/files/clear")
#: The progress of a send the review sheet is waiting for
#: (reviewed_writes.Progress by the action request's progress_id). No
#: connection, no SQL, no credentials: phases and statement states only.
_ACTION_PROGRESS_PATH = "/api/live/action/progress"

# A positive allowlist: live/start, job controls and all reviewed writes keep
# their existing lifetime. The handler also checks _WRITE_PATHS defensively.
_CANCELLABLE_READ_PATHS = (_SERVER_VIEW_PATHS + _REPLICATION_READ_PATHS + _CATALOG_PATHS
                         + _TABLE_COLUMNS_PATHS + _OBJECT_SEARCH_PATHS + _VARIABLE_READ_PATHS
                         + ("/api/live/topology", "/api/live/readiness"))
_ACTION_PROGRESS_LOCK = threading.Lock()
#: Maximum database names returned by /api/connection/test.
_TEST_DATABASES_LIMIT = 1000


def _connection_test_report(conn, options, password):
    """Server identity plus a bounded database list for one open connection."""
    from .dbops import redact
    from .server_info import product_identity
    from .server_info import is_readyset_comment
    try:
        comment = conn.server_version_comment()
    except ConnectorError as exc:
        from .reviewed_writes import client_error, error_number
        code = error_number(str(exc))
        if code is None or client_error(code) or "Access denied" in str(exc):
            raise
        comment = ""
    if is_readyset_comment(comment):
        from . import readyset
        from .topology_links import parse_readyset_version
        try:
            rs_rows = readyset.select(conn, "SHOW READYSET VERSION")
            version = parse_readyset_version(rs_rows) or "Readyset"
        except ConnectorError:
            version = "Readyset"
    else:
        version = conn.server_version()
    identity = product_identity(version, comment)
    databases = []
    databases_truncated = False
    databases_error = ""
    try:
        rows = conn.query_rows("SHOW DATABASES")
        names = [row[0] for row in rows if row and row[0]]
        databases_truncated = len(names) > _TEST_DATABASES_LIMIT
        databases = names[:_TEST_DATABASES_LIMIT]
    except (ValueError, ConnectorError) as exc:
        databases_error = redact(str(exc), password) or "Could not read the database list."
    result = {
        "version": version,
        "engine": identity["engine"],
        "flavor": identity["flavor"],
        "product": identity["product"],
        "schemas": databases,
        "databases": databases,
        "databases_truncated": databases_truncated,
    }
    if databases_error:
        result["databases_error"] = databases_error
    from .ssh_tunnel import through_text
    through = through_text(options)
    if through:
        # "Through SSH host bastion": the test went through the tunnel.
        result["through_ssh"] = through
    return result


def _test_connection_response(payload, workspace=None):
    """Test a connection and return server identity plus a bounded database list."""
    from .live import connection_options
    options = connection_options(payload.get("connection"))
    password = options.get("password")
    try:
        with _group_guarded_connection(options, payload.get("connection"), getattr(workspace, "store", None)) as conn:
            return _connection_test_report(conn, options, password)
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, password)


def _readiness_response(payload, workspace):
    """Read readiness with the same per-login consent as other server views."""
    from . import readiness
    from .live import connection_options
    options = connection_options(payload.get("connection"))
    options.update(query_timeout=readiness.TIMEOUT, connect_timeout=readiness.TIMEOUT)
    try:
        with _group_guarded_connection(options, payload.get("connection"), getattr(workspace, "store", None)) as conn:
            return readiness.read_readiness(conn)
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, options.get("password"))


#: Group connection probes (POST /api/connect with probe_members: true): at
#: most this many member logins at a time, and never more logins than the
#: group has members (capped at connection_groups.GROUP_MAX_MEMBERS). Probing
#: happens only on an explicit open or Refresh, never on automatic refresh.
_CONNECT_PROBE_CONCURRENCY = 4
_CONNECT_PROBE_BUDGET = 16
_MEMBER_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                          r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _member_live_facts(conn):
    """Live identity facts of one probed member; None where the server has none.

    server_uuid, role and state are reported as their own fields: they never
    rename a member (the store's profile name stands) and never decide group
    membership (the store does).
    """
    facts = {"server_uuid": None, "role": None, "state": None}
    try:
        rows = conn.query_rows("SELECT @@global.server_uuid")
    except ConnectorError:
        rows = None
    uuid_text = rows[0][0] if rows and rows[0] else None
    if isinstance(uuid_text, str) and _MEMBER_UUID.fullmatch(uuid_text):
        facts["server_uuid"] = uuid_text
        try:
            rows = conn.query_rows("SELECT MEMBER_ROLE, MEMBER_STATE FROM "
                                   "performance_schema.replication_group_members "
                                   "WHERE MEMBER_ID = '%s'" % uuid_text)
            if rows and rows[0]:
                facts["role"] = rows[0][0] if isinstance(rows[0][0], str) else None
                facts["state"] = rows[0][1] if isinstance(rows[0][1], str) else None
        except ConnectorError:
            pass
    return facts


def _connect_response(payload, workspace):
    """Open a connection group as one Group connection (POST /api/connect).

    The group's current members are read from the store (consent is the
    store, re-read on every request). The active member is the request's
    ``active_member_profile_id`` when it is a current member, else the member
    the stored topology identity marks primary or source, else the first
    member. The reply is the usual connection discovery for the active member
    plus ``group_connection``: the effective safety (the strictest member
    type; read-only stays each member's own), the primary member, and one
    entry per member with its store name and endpoint. Live facts
    (server_uuid, role, state, reachable) are separate fields, always read
    for the active member and for the other members only when the request
    asks (``probe_members: true``): at most _CONNECT_PROBE_CONCURRENCY logins
    at a time, within _CONNECT_PROBE_BUDGET in total. Every login uses the
    member's own saved password; the reply never contains a secret. Every
    member connection also carries a per-login guard (``conn._login_guard``)
    that re-reads membership from the store, so a member removed from the
    group while discovery runs gets no later client invocation.
    """
    from . import connection_groups, dbops
    from .group_members import open_group_member
    store = workspace.store
    group_name = connection_groups.validate_group(payload.get("group_name"))
    if not group_name:
        raise ValueError("Name the connection group to open.")
    probe = payload.get("probe_members") is True
    requested = payload.get("active_member_profile_id")
    if requested is not None and (not isinstance(requested, str) or not 0 < len(requested) <= 200):
        raise ValueError("Invalid active_member_profile_id.")
    profiles = [p for p in store.list("profile")
                if isinstance(p, dict) and isinstance(p.get("id"), str)]
    members = [p for p in profiles if (p.get("group") or "") == group_name]
    if not members:
        raise ValueError("No saved connection belongs to this connection group.")
    active = None
    if requested is not None:
        active = next((p for p in members if p["id"] == requested), None)
        if active is None:
            raise ValueError("That saved connection is not a member of this connection group.")
    primary = connection_groups.group_primary(members)
    if active is None:
        active = primary or members[0]

    from .accounts_api import _group_login_guard
    from .workspace_store import ENDPOINT_FIELDS
    active_route = dict({k: active.get(k) for k in ENDPOINT_FIELDS}, profile_id=active["id"], group_name=group_name)
    active_guard = _group_login_guard(store, active_route)

    def open_member(profile, with_facts):
        with open_group_member(store, group_name, profile, active_guard) as opened:
            conn, options = opened
            return _connection_test_report(conn, options, options.get("password")), \
                _member_live_facts(conn) if with_facts else None

    discovery, active_facts = open_member(active, True)
    facts_by_id = {active["id"]: active_facts}
    reachable = {active["id"]: True}
    others = [p for p in members if p["id"] != active["id"]]
    if probe and others:
        from concurrent.futures import ThreadPoolExecutor

        def probe_one(profile):
            try:
                _report, facts = open_member(profile, True)
                return profile["id"], facts, True
            except Exception as exc:
                if not isinstance(exc, (ConnectorError, ValueError, OSError)):
                    _log_unexpected_error('/api/connect', sys.exc_info(), active_route, store)
                # A member that cannot be read is reported unreachable; its
                # response contains neither the error nor any secret in it.
                return profile["id"], None, False

        with ThreadPoolExecutor(max_workers=_CONNECT_PROBE_CONCURRENCY) as pool:
            for pid, facts, ok in pool.map(probe_one, others[:max(0, _CONNECT_PROBE_BUDGET - 1)]):
                facts_by_id[pid] = facts
                reachable[pid] = ok
    entries = []
    for profile in members:
        identity = profile.get("topology_identity") or {}
        facts = facts_by_id.get(profile["id"]) or {}
        ptype = profile.get("type")
        entries.append({
            "profile_id": profile["id"],
            "profile_name": profile.get("name") or profile["id"],
            "host": profile.get("host") or "",
            "port": profile.get("port") or 3306,
            "safety": {"type": ptype if ptype in dbops.CONNECTION_TYPES else dbops.DEFAULT_CONNECTION_TYPE,
                       "read_only": bool(profile.get("read_only"))},
            "server_uuid": facts.get("server_uuid") or identity.get("server_uuid"),
            "role": facts.get("role"),
            "state": facts.get("state"),
            "reachable": reachable.get(profile["id"]),
        })
    effective = connection_groups.group_effective_safety(profiles, group_name) or dbops.DEFAULT_CONNECTION_TYPE
    result = dict(discovery)
    result["group_connection"] = {
        "group_name": group_name,
        "effective_safety": {"type": effective, "read_only": bool(active.get("read_only"))},
        "active_member_profile_id": active["id"],
        "primary_profile_id": primary["id"] if primary is not None else None,
        "members": entries,
    }
    return result


def _action_progress(workspace):
    from .reviewed_writes import ProgressRegistry
    from . import topology_change
    with _ACTION_PROGRESS_LOCK:
        registry = getattr(workspace, "action_progress", None)
        if registry is None:
            registry = topology_change.share_progress(workspace.store, ProgressRegistry())
            workspace.action_progress = registry
        return registry

def _active_lifecycle_operations(workspace):
    """The dump, restore and clone jobs queued or running in this workspace."""
    jobs = getattr(workspace, "lifecycle", None) if workspace is not None else None
    return jobs.active_operations() if jobs is not None else []

#: Application updates (app_update.py). Check reads GitHub only when the
#: workspace asks (a click, or the automatic check when that setting is on).
#: Checks keep their outcome in the workspace store; status never reads the network.
_APP_UPDATE_PATHS = ('/api/app/update/check', '/api/app/update/download', '/api/app/update/status',
                     '/api/app/update/stage', '/api/app/update/settings')
#: Local application settings kept in the workspace store (workspace_store.py):
#: the Query editor's settings. No connection, no server read, no audit.
_SETTINGS_PATHS = ('/api/settings/editor', '/api/settings/ui')
# Application client selection is separate from saved connection credentials.
_CLIENT_PATHS = ('/api/client/tools', '/api/client/tools/choose')
#: Managed MySQL Shell downloads (managed_shell.py, answered by
#: _shell_response): status is read-only and never touches the network,
#: install starts the one process-wide flight (idempotent while it runs),
#: cancel must name the running job's id. No connection, no saved password,
#: no SQL; every reply is managed_shell's own status payload.
_MANAGED_SHELL_PATHS = ('/api/client/shell/status', '/api/client/shell/install',
                        '/api/client/shell/cancel')
_CLIENT_SETTINGS_LOCK = threading.Lock()


class _ClientChoiceError(ValueError):
    """A plain, user-facing refusal from the client tools chooser."""


class _ManagedShellError(ValueError):
    """A plain, user-facing refusal from the managed MySQL Shell download."""


def _shell_response(path, payload, workspace):
    from . import managed_shell
    if workspace is None:
        raise ValueError("This operation requires a running workspace.")
    # The workspace store's folder is the fallback root of the tools folder.
    directory = os.fspath(workspace.store.path.parent)
    try:
        if path == '/api/client/shell/cancel':
            if set(payload) - {'id'}:
                raise ValueError("Unexpected MySQL Shell field.")
            job_id = payload.get('id')
            if not isinstance(job_id, str) or not 0 < len(job_id) <= 64:
                raise ValueError("Name the running MySQL Shell download to stop.")
            managed_shell.cancel(job_id)
        elif payload:
            raise ValueError("Unexpected MySQL Shell field.")
        elif path == '/api/client/shell/install':
            managed_shell.start_install(directory)
    except managed_shell.Refused as exc:
        raise _ManagedShellError(str(exc)) from None
    return managed_shell.status(directory)


def _client_settings_response(path, payload, workspace):
    from . import connector, client_tools
    if workspace is None:
        raise ValueError("This operation requires a running workspace.")
    choosing = path == '/api/client/tools/choose'
    allowed = {'engine', 'path', 'connection'} if choosing else {'refresh', 'connection'}
    if set(payload) - allowed:
        raise _ClientChoiceError('Unexpected client tools setting.')
    if choosing:
        if 'path' not in payload or 'engine' not in payload:
            raise _ClientChoiceError('Choose an engine and client tools path, or null for Automatic.')
        # Validation can run a version probe. Only choice mutation is serialized;
        # ordinary readers and unrelated connection resolution remain independent.
        with _CLIENT_SETTINGS_LOCK:
            try:
                value = client_tools.validate_choice(payload['engine'], payload['path'])
                workspace.store.set_client_choice(payload['engine'], value)
            except ValueError as exc:
                raise _ClientChoiceError(str(exc)) from None
            choices = {e: workspace.store.client_choice(e) for e in ('mysql', 'mariadb')}
            connector.set_client_choices(choices)
    else:
        with _CLIENT_SETTINGS_LOCK:
            choices = {e: workspace.store.client_choice(e) for e in ('mysql', 'mariadb')}
    discovered = client_tools.discover_choices(choices, refresh=payload.get('refresh') is True)
    keys, errors = client_tools.choice_keys(discovered, choices)
    installations = []
    for group in sorted(discovered['installations'], key=lambda g: (
            g['engine'], tuple(-n for n in g['version_tuple']), g['order'], g['key'])):
        item = {key: group[key] for key in ('key', 'engine', 'version', 'series', 'folder', 'found_in',
                'origin', 'origin_label', 'bundled', 'has_auth_plugins')}
        item['programs'] = {name: {k: program[k] for k in ('path', 'realpath', 'version')}
                            for name, program in group['programs'].items() if name != 'mysqlpump'}
        item['chosen_for'] = [e for e in keys if keys[e] == group['key']]
        installations.append(item)
    result = dict(schema_version='client-tools-1.0', installations=installations,
                  shells=[{k: shell[k] for k in ('path', 'realpath', 'version', 'origin', 'origin_label')}
                          for shell in discovered['shells']],
                  skipped=[dict(path=entry['path'], reason=entry['reason']) for entry in discovered['skipped']],
                  choice={e: dict(path=choices[e], installation=keys[e], error=errors[e]) for e in keys})
    if 'connection' in payload:
        from . import lifecycle as lc
        filled = _with_saved_password(payload, workspace)
        options = _lifecycle_connection(filled.get('connection'))
        passwords = [(filled.get('connection') or {}).get('password')]
        result.update(server=None, server_error=None)
        try:
            with lc._connect(lc.LifecycleConnection, options) as conn:
                server = lc.read_server(conn, options)
                client_tools.remember_server(client_tools.endpoint_key(options), server['engine'], server['version'])
            result['server'] = {k: server[k] for k in ('engine', 'version', 'product')}
        except (ConnectorError, ValueError, OSError) as exc:
            missing = connector.client_missing_error(exc)
            result['server_error'] = dict(code=missing.code if missing else 'server-read',
                                         error=lc._safe(str(missing or exc), passwords))
        used = client_tools.select(discovered, result['server'], keys)
        used.pop('server', None)
        for role in ('client', 'dump', 'load', 'backup', 'shell'):
            if used[role] is not None:
                used[role] = {k: v for k, v in used[role].items() if k not in ('realpath', 'version_tuple', 'order')}
                if role == 'shell':
                    used[role]['installation'] = None
        if result['server_error'] and result['server_error']['code'] in ('client-missing', 'client-auth-plugin'):
            used['client'] = None
        result['used'] = used
    return result


# Home news (news.py): fixed official feeds, read only while Home is shown
# and the Settings switch is on. No connection, no password.
_NEWS_PATHS = ('/api/news', '/api/news/settings')


def _param_version_response(path, payload):
    """The offline parameter version catalogs; never opens a connection."""
    from . import param_versions
    if path == "/api/config/versions":
        return param_versions.catalog_index()
    engine = payload.get("engine")
    if engine not in param_versions.ENGINES:
        raise ValueError("Choose engine mysql or mariadb.")
    if path == "/api/config/versions/names":
        return param_versions.names(engine, payload.get("version"))
    if path == "/api/config/versions/compare":
        for field in ("from", "to"):
            value = payload.get(field)
            if not isinstance(value, str) or not value or len(value) > param_versions.MAX_VERSION_LENGTH:
                raise ValueError("Choose a cataloged version for %s, at most %d characters."
                                 % (field, param_versions.MAX_VERSION_LENGTH))
        return param_versions.compare(engine, payload["from"], payload["to"])
    name = payload.get("name")
    if not isinstance(name, str) or not name or len(name) > param_versions.MAX_NAME_LENGTH:
        raise ValueError("Choose a parameter name of at most %d characters."
                         % param_versions.MAX_NAME_LENGTH)
    return param_versions.history(engine, name)


def _settings_response(path, payload, workspace):
    """Local application settings in the workspace store; never opens a connection."""
    if workspace is None:
        raise ValueError("This operation requires a running workspace.")
    if path == "/api/settings/ui":
        from .workspace_store import UI_PREFERENCE_DEFAULTS
        if set(payload) - {"set"}:
            raise ValueError("Unexpected UI preferences field.")
        if "set" in payload:
            workspace.store.set_ui_preferences(payload["set"])
        return {"schema_version": "ui-preferences-1.0",
                "preferences": workspace.store.ui_preferences(),
                "defaults": dict(UI_PREFERENCE_DEFAULTS)}
    if path == "/api/settings/editor":
        from .workspace_store import EDITOR_SETTING_DEFAULTS
        if set(payload) - {"set"}:
            raise ValueError("Unexpected editor settings field.")
        changes = payload.get("set")
        if changes is not None:
            workspace.store.set_editor_settings(changes)
        return {"schema_version": "editor-settings-1.0",
                "settings": workspace.store.editor_settings(),
                "defaults": dict(EDITOR_SETTING_DEFAULTS)}
    raise ValueError("Not found.")


def _accounts_response(path, payload, workspace):
    """Privileges and Users and roles: accounts_api.response with this
    module's saved-password and profile helpers."""
    from . import accounts_api, editor_session
    ticket = editor_session.begin_privileges_read(workspace, payload) if path == '/api/live/privileges' else None
    if path == '/api/accounts/apply':
        editor_session.invalidate_privileges(workspace)
    try:
        report = accounts_api.response(path, payload, workspace,
                                       lambda value: _with_saved_password(value, workspace), _request_profile)
        if ticket is not None:
            editor_session.finish_privileges_read(workspace, ticket, report)
        return report
    finally:
        if path == '/api/accounts/apply':
            editor_session.invalidate_privileges(workspace)


def _catalog_response(payload, path="/api/live/catalog", workspace=None):
    """catalog-1.0 for the schema browser, or table-structure-1.0 for Table
    structure; the password never reaches an error."""
    from .live import connection_options
    from .schema_browser import catalog_report, check_table, table_structure
    prefix, after = check_table(payload.get("prefix")), check_table(payload.get("after"))
    options = connection_options(payload.get("connection"))
    options["query_timeout"] = 30
    try:
        with _group_guarded_connection(options, payload.get("connection"), getattr(workspace, "store", None)) as conn:
            if path == "/api/live/table-structure":
                return table_structure(conn, payload.get("schema"), payload.get("table"))
            return catalog_report(conn, payload.get("schema"), payload.get("table"),
                                  prefix=prefix, after=after, stats=payload.get("stats") is True,
                                  group=payload.get("group"))
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, options.get("password"))


def _object_search_response(payload, workspace=None):
    """object-search-1.0 for the command palette: one bounded information_schema
    read with a 2-second budget; the password never reaches an error."""
    from .live import connection_options
    from .schema_browser import check_search, object_search
    text = check_search(payload.get("text"))
    options = connection_options(payload.get("connection"))
    options["query_timeout"] = 2
    try:
        with _group_guarded_connection(options, payload.get("connection"), getattr(workspace, "store", None)) as conn:
            return object_search(conn, text)
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, options.get("password"))


def _variable_policy(name, mode, connection, store):
    """The connection's safety and write policy for one variable change,
    judged on a statement of the same kind (no connection is opened)."""
    from . import dbops, variables
    safety = dbops.connection_safety(_request_profile(connection, store), connection, store=store)
    decision = dbops.decide(dbops.classify(variables.representative_statement(name, mode)), safety["type"],
                            safety["read_only"], safety["profile_name"])
    return safety, decision


def _variable_response(path, payload, workspace):
    """Server variables (variables.py): read one, plan a change (reads only),
    or apply one confirmed change. Every request opens one connection."""
    from . import dbops, variables
    from .live import connection_options
    from .connector import MySQLConnection
    if path == "/api/live/variable/apply":
        return _variable_apply(payload, workspace)
    name = variables.check_name(payload.get("name"))
    mode = variables.check_mode(payload.get("mode", "global")) if path == "/api/live/variable/plan" else None
    if mode and mode != "reset_persist" and "value" not in payload:
        raise ValueError("Send the new value for %s (JSON null means SQL NULL)." % name)
    options = connection_options(payload.get("connection"))
    options["query_timeout"] = 30
    try:
        with _group_guarded_connection(options, payload.get("connection"), workspace.store) as conn:
            if path == "/api/live/variable":
                return variables.read_variable(conn, name)
            plan = variables.plan_change(conn, name, payload.get("value"), mode)
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, options.get("password"))
    safety, decision = _variable_policy(name, mode, payload.get("connection"), workspace.store)
    if plan.get("expect") is not None:
        plan["expect"]["connection_identity"] = dbops.connection_identity(payload.get("connection"), safety)
    plan["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text", "connection_type",
                                                    "read_only")}
    return plan


def _variable_apply(payload, workspace):
    """Apply one variable change the person confirmed (variables.apply_change).

    The connection's write policy runs first (dbops.gate with the request's
    confirmed: true); on a read-only connection nothing is opened. Only then
    is the saved password filled. The change is re-planned from a fresh read
    and refused unless it matches ``expect``. Every attempt is audited."""
    from . import dbops, param_compare, variables
    from .live import connection_options
    from .connector import MySQLConnection
    name = variables.check_name(payload.get("name"))
    mode = variables.check_mode(payload.get("mode"))
    value = payload.get("value")
    if mode != "reset_persist" and ("value" not in payload or isinstance(value, (dict, list))):
        raise ValueError("Send the new value for %s." % name)
    connection = payload.get("connection")
    safety, decision = _variable_policy(name, mode, connection, workspace.store)
    plain = connection if isinstance(connection, dict) else {}
    audit = dbops.AuditTrail(workspace.store, variables.OPERATIONS[mode], "ui",
                             connection={k: plain.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}, safety=safety,
                             password=plain.get("password") if isinstance(plain.get("password"), str) else None,
                             policy=dbops.policy_summary(decision))
    if param_compare.sensitive(name):
        audit = variables._SensitiveAudit(audit)
    request = {"name": name, "mode": mode, "requested": None if mode == "reset_persist" else value}
    if payload.get("declined") is True:
        # Planning can be pending, invalid or failed when a sheet is closed.
        # Preserve that intent without inventing a statement the user reviewed.
        reviewed = payload.get("reviewed_statement")
        if reviewed is not None and (not isinstance(reviewed, str) or len(reviewed) > variables.MAX_VALUE_LENGTH * 3):
            raise ValueError("The reviewed statement must be text or null.")
        target = dict(payload.get("expect") or {}, **request)
        result = variables.unsent_result(name, mode, request["requested"], "declined", "Nothing was sent.")
        result["audit_id"] = audit.outcome([variables.TAG + reviewed] if reviewed else [], "declined",
                                           outcome="declined", message=result["message"], target=target)
        return result
    expect = variables.check_expect(payload.get("expect"), name, mode)
    blocked = dbops.gate(decision, confirmed=payload.get("confirmed") is True)
    if blocked:
        result = variables.unsent_result(name, mode, request["requested"], blocked[0], blocked[1])
        result["audit_id"] = audit.outcome([variables.TAG + expect["statement"]], "refused",
                                           outcome="read_only" if blocked[0] == "refused" else blocked[0],
                                           message=blocked[1], target=expect)
        return result
    if expect.get("connection_identity") != dbops.connection_identity(connection, safety):
        message = "The connection changed since you reviewed the plan, so nothing was sent. Review it again."
        result = variables.unsent_result(name, mode, request["requested"], "refused", message)
        result["audit_id"] = audit.outcome([variables.TAG + expect["statement"]], "refused",
                                           outcome="changed", message=message, target=expect)
        return result
    password = audit.password
    try:
        filled = _with_saved_password(payload, workspace)
        options = connection_options(filled.get("connection"))
        options["query_timeout"] = 30
        password = audit.password = options.get("password")
        audit.connection = {k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}
        with _group_guarded_connection(options, connection, workspace.store,
                                       dbops.connection_identity(connection, safety)) as conn:
            return variables.apply_change(conn, request, expect, decision=decision, confirmed=True, audit=audit)
    except ProfileReadError as exc:
        result = variables.unsent_result(name, mode, request["requested"], "refused", str(exc))
        result["audit_id"] = _group_login_refusal(audit, [variables.TAG + expect["statement"]], exc, expect)
        return result
    except (ValueError, ConnectorError) as exc:
        audit.fail(exc, [variables.TAG + expect["statement"]])
        _reraise_redacted(exc, password)


def _topology_variable_response(path, payload, workspace):
    """Strict Group-only requests; no request endpoint supplies member credentials."""
    from . import connection_groups, dbops, reviewed_writes, topology_change
    from .editor_session import _connection_facts
    from .workspace_store import ENDPOINT_FIELDS
    operation = path.rsplit('/', 1)[-1]
    required = {'plan': {'connection', 'name', 'value', 'mode', 'member_ids'},
                'apply': {'connection', 'plan_id', 'expect', 'confirmed', 'progress_id'},
                'stop': {'connection', 'progress_id'}}[operation]
    allowed = required | ({'typed_name'} if operation == 'apply' else set())
    if set(payload) - allowed or required - set(payload):
        raise ValueError('Send only the required rolling change fields.')
    connection = payload.get('connection')
    fields = set(ENDPOINT_FIELDS) | {'profile_id', 'group_name', 'database', 'type', 'read_only', 'name'}
    if (not isinstance(connection, dict) or set(connection) - fields or
            not isinstance(connection.get('group_name'), str) or not connection['group_name'] or
            not isinstance(connection.get('profile_id'), str) or not 0 < len(connection['profile_id']) <= 200):
        raise ValueError('A rolling change requires a current Group connection with saved credentials.')
    for key, value in connection.items():
        if key in ('port', 'ssh_port'):
            if value is not None and (type(value) is not int or not 0 <= value <= 65535):
                raise ValueError('Connection ports must be integers.')
        elif key == 'read_only':
            if type(value) is not bool:
                raise ValueError('Connection read_only must be a boolean.')
        elif value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise ValueError('Connection fields must be bounded text.')
    if operation != 'plan':
        progress_id = payload['progress_id']
        if not isinstance(progress_id, str) or not reviewed_writes._PROGRESS_ID.fullmatch(progress_id):
            raise ValueError('Invalid progress_id.')
    if operation == 'apply':
        if (not isinstance(payload['plan_id'], str) or not 1 <= len(payload['plan_id']) <= 128 or
                not isinstance(payload['expect'], dict) or type(payload['confirmed']) is not bool or
                'typed_name' in payload and (not isinstance(payload['typed_name'], str) or len(payload['typed_name']) > 80)):
            raise ValueError('Send the reviewed plan_id and expect, boolean confirmed and optional typed_name text.')
    # The same consent and endpoint checks as /api/live/topology, before any
    # saved-password lookup. The executor repeats them at each member login.
    _connection_facts(payload, workspace.store)
    _profiles, current = connection_groups.request_profiles(workspace.store, connection)
    if current is None:
        raise ValueError('This saved connection is outside the current connection group.')
    topology_change.share_progress(workspace.store, _action_progress(workspace))
    if operation == 'plan':
        if connection.get('read_only') is True:
            raise ValueError('This Group connection is Read-only. No connections were opened.')
        return topology_change.plan(workspace.store, connection['group_name'], current['id'],
            payload['name'], payload['value'], payload['mode'], payload['member_ids'],
            active_endpoint=topology_change._endpoint(current), group_generation=current.get('group_generation'))
    if operation == 'stop':
        return topology_change.stop(workspace.store, payload['progress_id'], connection['group_name'], current['id'])
    audit = dbops.AuditTrail(workspace.store, topology_change.OPERATION, 'ui')
    return topology_change.apply(workspace.store, audit, payload['plan_id'], payload['expect'], payload['confirmed'],
        payload.get('typed_name'), payload['progress_id'], connection=connection)


_ACTION_OPERATION = {"cluster": "cluster_action", "proxysql": "proxysql_action", "readyset": "readyset_action"}


def _action_module(kind):
    if kind == "cluster":
        from . import cluster as module
    elif kind == "readyset":
        from . import readyset as module
    else:
        from . import proxysql as module
    return module


def _action_plan(kind, module, conn, payload, target=None, group_checks=None):
    """A plan from a fresh read of *conn* for the request's inputs."""
    if kind == "cluster":
        report = module.read_status(conn)
        action = payload.get("action")
        checks = module.action_checks(conn, report, action)
        if group_checks is not None:
            checks.update(group_checks())
        return module.plan_action(report, action, payload.get("member_key"), checks, target=target)
    if kind == "readyset":
        report = module.read_status(conn, schema=payload.get("schema") or "readyset")
        return module.plan_action(report, payload.get("action"), payload.get("target"), payload.get("value"), conn=conn)
    report = module.read_status(conn)
    return module.plan_action(report, payload.get("action"), payload.get("target"), payload.get("value"))


def _action_target(payload, connection, kind, store):
    """The group member a cluster action targets, checked against the store.

    A target other than the active member exists only inside a group
    connection: the request must name ``group_name``, and the active member
    and the target must both be current members of that group (consent is the
    store, read at every call). ProxySQL and Readyset actions take no target.
    An omitted target on a Group connection means its active saved member.
    Returns the target's stored profile, or None outside Group routing.
    """
    explicit = "target_profile_id" in payload
    if not explicit and (kind != "cluster" or connection.get("group_name") is None):
        return None
    if kind != "cluster":
        raise ValueError("A target server is only accepted for cluster actions.")
    target_profile_id = payload.get("target_profile_id") if explicit else connection.get("profile_id")
    if not isinstance(target_profile_id, str) or not 0 < len(target_profile_id) <= 200:
        raise ValueError("Invalid target_profile_id.")
    group_name = connection.get("group_name")
    group = None
    if group_name is not None:
        from .connection_groups import validate_group
        try:
            group = validate_group(group_name) or None
        except ValueError:
            group = None
    if not group:
        raise ValueError("Acting on another member requires the connection group it belongs to.")
    from .connection_groups import request_profiles
    _, active = request_profiles(store, connection)
    target = store.get("profile", target_profile_id)
    if (not isinstance(active, dict) or (active.get("group") or "") != group or
            not isinstance(target, dict) or (target.get("group") or "") != group):
        raise ValueError("The target server is not in the active connection group.")
    if target_profile_id != active.get("id") and not target.get("remember_password"):
        raise ValueError("The target server has no saved password. Save its password before reviewing a cluster action.")
    return target


def _action_target_connection(profile, connection):
    """Stored endpoints; only the opened member may use the request password."""
    from .connection_groups import validate_group
    from .workspace_store import ENDPOINT_FIELDS
    value = {k: profile.get(k) for k in ENDPOINT_FIELDS if profile.get(k) not in (None, "")}
    value.update(profile_id=profile["id"], group_name=validate_group(connection["group_name"]))
    if profile["id"] == connection.get("profile_id") and "password" in connection:
        value["password"] = connection["password"]
    if connection.get("read_only") is True:
        value["read_only"] = True
    return value


def _action_target_binding(profile, connection, cluster_kind=None):
    """Rebuilt without credentials, including before apply's password lookup."""
    from .workspace_store import ENDPOINT_FIELDS, endpoint_value
    from .cluster import target_server_identity
    value = _action_target_connection(profile, connection)
    saved_identity = profile.get("topology_identity") or {}
    return {"profile_id": profile["id"], "profile_name": profile.get("name") or profile["id"],
            "endpoint": {k: endpoint_value(profile, k) for k in ENDPOINT_FIELDS}, "group_name": value["group_name"],
            "group_generation": profile.get("group_generation") or "",
            "server_identity": {k: saved_identity[k] for k in target_server_identity({"kind": cluster_kind})
                                if saved_identity.get(k)}}


def _action_response(path, payload, workspace):
    """Plan (read, review token) and Action (the write) for the Cluster and
    ProxySQL places. Action refuses before opening any connection on a
    read-only connection or without confirmation; a missing, expired or
    changed review is refused too. Refusals and every send are audited
    (dbops.AuditTrail); the envelope is cluster-action-1.1 or
    proxysql-action-1.0 (reviewed_writes.py)."""
    from . import dbops, reviewed_writes
    from .live import connection_options
    from .connector import MySQLConnection
    kind = _ACTION_KINDS[path]
    module = _action_module(kind)
    connection = payload.get("connection")
    plain = connection if isinstance(connection, dict) else {}
    planning = path.endswith("/plan")
    reviewed = None if planning else reviewed_writes.check_reviewed(payload.get("plan"))
    expect = reviewed.get("expect") if reviewed is not None else None
    if reviewed is not None and not isinstance(expect, dict):
        raise ValueError("The reviewed plan is not valid. Review the action again; nothing was sent.")
    statements = list(reviewed["statements"]) if reviewed is not None else []
    progress = (_action_progress(workspace).start(payload.get("progress_id"), len(statements), module.ACTION_TIMEOUT)
                if reviewed is not None else None)

    try:
        def make_audit(route, safety, decision):
            return dbops.AuditTrail(workspace.store, _ACTION_OPERATION[kind], "ui",
                                   connection={k: route.get(k) for k in
                                               ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")},
                                   safety=safety, password=plain.get("password") if isinstance(plain.get("password"), str)
                                   else None, policy=dbops.policy_summary(decision))

        try:
            target_profile = _action_target(payload, plain, kind, workspace.store)
        except ValueError as exc:
            if planning:
                raise
            # Consent can disappear between review and apply. Audit the refusal
            # before any credentials are read, using the requested saved target.
            target_id = payload.get("target_profile_id", plain.get("profile_id"))
            target = workspace.store.get("profile", target_id) if isinstance(target_id, str) and len(target_id) <= 200 else None
            route = dict(target, group_name=plain.get("group_name")) if isinstance(target, dict) else plain
            safety = dbops.connection_safety(target, route, store=workspace.store)
            decision = dbops.decide(reviewed_writes.worst_kind(statements), safety["type"], safety["read_only"],
                                    safety.get("profile_name"))
            audit = make_audit(route, safety, decision)
            out = reviewed_writes.result(module.ACTION_SCHEMA_VERSION, reviewed)
            return reviewed_writes.refused(out, str(exc), "changed", audit, expect, progress)
        routed = target_profile is not None
        route = _action_target_connection(target_profile, plain) if routed else plain
        identity_kind = expect.get("kind") if expect is not None else None
        binding = _action_target_binding(target_profile, plain, identity_kind) if routed else None
        if routed and planning:
            # The fresh report selects the identity key. Until then neither UUID
            # is evidence of the kind of cluster this endpoint currently serves.
            binding = dict(binding, server_identity={})
        safety = dbops.connection_safety(target_profile if routed else _request_profile(connection, workspace.store), route,
                                         store=workspace.store)
        identity = dbops.connection_identity(plain, safety)

        def group_checks():
            return {"group_name": route["group_name"], "group_profiles": workspace.store.list("profile"),
                    "current_profile": workspace.store.get("profile", target_profile["id"])}

        def current_target():
            if not routed:
                return None
            try:
                current = _action_target(payload, plain, kind, workspace.store)
            except ValueError as exc:
                raise reviewed_writes.Refusal(str(exc), "changed") from None
            fresh_binding = _action_target_binding(current, plain, identity_kind)
            if planning and identity_kind is None:
                fresh_binding["server_identity"] = {}
            if fresh_binding != binding:
                raise reviewed_writes.Refusal("The target saved connection changed after the review, so nothing was sent. "
                                              "Review the action again.", "changed")
            # Conflicting claims must also refuse a route added after planning or
            # during a read. No store ordering or credential availability wins.
            node_identity = binding["server_identity"]
            member = {"uuid": next(iter(node_identity.values()), None)}
            report = {"kind": identity_kind}
            checks = group_checks()
            if len(module._member_profiles(report, member, checks)) > 1:
                raise reviewed_writes.Refusal(" ".join(module._member_route_issue(report, member, checks)), "changed")
            now = dbops.connection_safety(current, route, store=workspace.store)
            if dbops.connection_identity(plain, now) != identity:
                raise reviewed_writes.Refusal("The connection's write policy changed after the review, so nothing was "
                                              "sent. Review the change again.", "policy_changed")
            return fresh_binding

        def open_options():
            # Validation precedes the lookup, and runs again after the secret store
            # returns. Per-login consent also covers each subprocess in read_status.
            current_target()
            filled = _with_saved_password(dict(payload, connection=route), workspace)
            options = connection_options(filled.get("connection"))
            current_target()
            if routed and target_profile["id"] != plain.get("profile_id") and not options.get("password"):
                raise reviewed_writes.Refusal("The target server's saved password is unavailable.", "changed")
            return options

        def guarded_connection(options):
            if routed:
                conn = MySQLConnection(**options)
                conn._login_guard = current_target
                conn._group_login_started = False
                return conn
            return _group_guarded_connection(options, connection, workspace.store,
                                             identity=None if planning else identity)

        if planning:
            options = open_options()
            from .connection_groups import begin_read
            observation = begin_read(workspace.store, target_profile).get("_topology_observation")
            try:
                with guarded_connection(options) as conn:
                    plan = _action_plan(kind, module, conn, payload, target=binding,
                                        group_checks=group_checks if routed else None)
                if routed:
                    current_target()
                    live_identity = plan["expect"]["target"]["server_identity"]
                    try:
                        workspace.store.record_topology_identity(target_profile["id"], live_identity,
                                                                  expected=target_profile, observation=observation)
                    except Exception:
                        raise ValueError("Could not save the target server's identity. Nothing was sent. "
                                         "Try reviewing the action again.") from None
                    # Persist the actual node identity before issuing the token, so
                    # apply can rebuild the whole binding before looking up a secret.
                    binding = dict(binding, server_identity=live_identity)
                    identity_kind = plan["expect"].get("kind")
                    current_target()
            except (ValueError, ConnectorError) as exc:
                _reraise_redacted(exc, options.get("password"))
            decision = dbops.decide(reviewed_writes.worst_kind(plan["statements"]), safety["type"], safety["read_only"],
                                    safety.get("profile_name"))
            plan["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text", "connection_type",
                                                           "read_only")}
            plan["review_token"] = _review_tokens(workspace).issue(_ACTION_OPERATION[kind], identity,
                                                                   reviewed_writes.review_details(plan))
            return plan
        decision = dbops.decide(reviewed_writes.worst_kind(statements), safety["type"], safety["read_only"],
                                safety.get("profile_name"))
        audit = make_audit(route, safety, decision)
        out = reviewed_writes.result(module.ACTION_SCHEMA_VERSION, reviewed)
        out["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text")}
        if kind == "cluster" and expect.get("target") != binding:
            return reviewed_writes.refused(out, "The target saved connection changed after the review, so nothing was sent. "
                                          "Review the action again.", "changed", audit, reviewed.get("expect"), progress)
        blocked = dbops.gate(decision, confirmed=payload.get("confirmed") is True)
        if blocked:
            return reviewed_writes.refused(out, blocked[1], "read_only" if blocked[0] == "refused" else blocked[0],
                                           audit, reviewed.get("expect"), progress)
        try:
            consume = _review_tokens(workspace).consumer(payload.get("review_token"), _ACTION_OPERATION[kind], identity,
                                                         reviewed_writes.review_details(reviewed))
        except ValueError as exc:
            return reviewed_writes.refused(out, str(exc), "review", audit, reviewed.get("expect"), progress)
        typed = payload.get("typed")
        typed = typed if isinstance(typed, str) else None

        def before_send():
            # The saved policy is read again right before sending: a profile made
            # Read-only (or changed) while the target was read sends nothing.
            current_target()
            now = dbops.connection_safety(_action_target(payload, plain, kind, workspace.store) if routed else
                                          _request_profile(connection, workspace.store), route, store=workspace.store)
            if dbops.connection_identity(plain, now) != identity or dbops.gate(dbops.decide(
                    reviewed_writes.worst_kind(statements), now["type"], now["read_only"], now.get("profile_name")),
                    confirmed=True):
                raise reviewed_writes.Refusal("The connection's write policy changed after the review, so nothing was "
                                              "sent. Review the change again.", "policy_changed")
            consume()
        password = audit.password
        try:
            options = open_options()
            password = audit.password = options.get("password")
            audit.connection = {k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user", "ssh_port")}
            with guarded_connection(options) as conn:
                extra = {"target": current_target, "group_checks": group_checks} if routed else {}
                result = module.execute_action(conn, reviewed, audit=audit, typed=typed, before_send=before_send,
                                               progress=progress, **extra)
            result["policy"] = out["policy"]
            return result
        except KeyboardInterrupt as exc:
            if progress is not None:
                progress.fail()
            audit.fail(exc, statements)
            raise
        except reviewed_writes.Refusal as exc:
            return reviewed_writes.refused(out, str(exc), exc.outcome, audit, expect, progress)
        except ProfileReadError as exc:
            audit_id = _group_login_refusal(audit, statements, exc, expect)
            result = reviewed_writes.refused(out, str(exc), "changed", target=expect, progress=progress)
            result["audit_id"] = audit_id
            return result
        except (ValueError, ConnectorError) as exc:
            if progress is not None:
                progress.fail()
            audit.fail(exc, statements)
            _reraise_redacted(exc, password)
    finally:
        # Finish progress even when validation, routing or audit creation
        # raises before the executor owns it. fail() preserves final results.
        if progress is not None:
            progress.fail()


def _with_saved_password(payload, workspace):
    connection = payload.get("connection")
    if not isinstance(connection, dict) or not connection.get("profile_id"):
        return payload
    filled = workspace.store.connection_with_saved_password(connection)
    return dict(payload, connection=filled)


def _profile_group_response(path, payload, store):
    """Save or delete a connection group as one object; responses never contain a password."""
    if path == "/api/profiles/groups/save":
        if set(payload) - {"name", "profile_ids", "previous_name"}:
            raise ValueError("Unexpected connection group field.")
        if "name" not in payload or "profile_ids" not in payload:
            raise ValueError("Send name and profile_ids to save a connection group.")
        return store.save_connection_group(
            payload.get("name"), payload.get("profile_ids"), payload.get("previous_name"))
    if path == "/api/profiles/groups/delete":
        if set(payload) - {"name"}:
            raise ValueError("Unexpected connection group field.")
        if "name" not in payload:
            raise ValueError("Send the connection group name to delete.")
        return store.delete_connection_group(payload.get("name"))
    raise ValueError("Unknown workspace operation.")


def _profile_response(operation, payload, store):
    """Connection profiles. Responses never contain a password."""
    from . import secret_store
    if operation == "list":
        return {"items": store.list("profile"), "secret_store": secret_store.describe()}
    if operation == "save":
        remember = payload.get("remember_password")
        if remember is not None and not isinstance(remember, bool):
            raise ValueError("remember_password must be true or false.")
        password = payload.get("password")
        if password is not None and (not isinstance(password, str) or len(password) > 4096):
            raise ValueError("Invalid connection field: password")
        value = payload.get("value")
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in ("password", "profile_id")}
        try:
            saved = store.save("profile", value, payload.get("id"), password=password or None, remember=remember)
            registry = getattr(store, "replication_loops", None)
            if registry is not None and saved.get("read_only"):
                registry.stop_profile(saved["id"])
            return saved
        except ValueError as exc:
            _reraise_redacted(exc, password)
    if operation == "group":
        return store.assign_profile_group(payload.get("profile_ids"), payload.get("group"))
    if operation == "forget-password":
        return store.forget_password(payload.get("id"))
    if operation == "delete":
        result = store.delete("profile", payload.get("id"))
        registry = getattr(store, "replication_loops", None)
        if registry is not None:
            registry.stop_profile(payload.get("id"))
        return result
    raise ValueError("Unknown workspace operation.")


def _session_response(path, payload, workspace):
    """Session restore: the autosaved workspace snapshot (see workspace_store)."""
    store = workspace.store
    client_id = payload.get("client_id", "")
    skip = bool(getattr(workspace, "no_restore", False))
    if path == "/api/session/load":
        session = store.load_session()
        if skip or not session["restore"]:
            session["state"] = None
        session["skip_restore"] = skip
        return session
    if path == "/api/session/save":
        if skip:
            raise ValueError("Autosave is off for this launch (myflames ui --no-restore).")
        return store.save_session(payload.get("state"), payload.get("revision"),
                                  client_id, payload.get("seq"))
    if path == "/api/session/clear":
        return store.clear_session(client_id, payload.get("seq"))
    if path == "/api/session/settings":
        return store.set_session_restore(payload.get("restore"), client_id, payload.get("seq"))
    raise ValueError("Unknown workspace operation.")


def _sftp_response(path, payload, store):
    """The Files place's calls. Answer shapes match filesApi.ts."""
    from . import sftp_api
    return sftp_api.response(path, payload, store)


def _session_capture_response(payload, workspace):
    """A synchronous tab plan registered as a normal completed capture job.

    live/start's id -> live/status -> /api/analyze ingestion stays unchanged.
    While start is pending, editor/stop targets the in-session statement.
    No fresh connection is opened and no request password is resolved.
    """
    from . import editor_session as editor
    from .live import LiveJob, normalize_capture
    tab = editor._tab_id({"tab_id": payload.get("session_tab")})
    _, identity, _, _ = editor._connection_facts(payload, workspace.store)
    session = editor._session_for(editor._registry(workspace), tab, identity)
    if session is None:
        raise editor.EditorError("This tab has no live session. Run SQL in the tab first.", "session_ended")
    job = LiveJob(payload)
    if job.repeat != 1 or job.trace:
        raise ValueError("In-session plans support one run without optimizer trace.")
    raw = session.run_plan(job.query, job.mode, timeout=job.timeout)
    _, root, _, _ = _plan({"plan": raw})
    details = root.get("details") or {}
    total = root["total_time"] if job.mode == "analyze" and details.get("actual_last_row_ms") is not None else None
    job.result = normalize_capture({
        "schema_version": "capture-1.0", "raw": raw, "query": job.query, "mode": job.mode,
        "engine": session.engine, "server_version": session.server_version, "captured_at": editor._now(),
        "runs": [{"total_time_ms": total, "statistics": None, "session_status_delta": {}, "plan": raw}],
        "conditions": job.conditions, "source": job.source,
        "warnings": ["Captured in the tab's session. Statement counters, schema metadata and optimizer trace "
                     "were not collected."],
    })
    job.state, job.progress = "complete", 1
    job.options.pop("password", None)
    # Keep the same ownership lookup and bounded cache as ordinary captures.
    with workspace.jobs.lock:
        jobs = workspace.jobs.jobs
        for key, old in list(jobs.items()):
            if old.state not in ("queued", "running") and time.monotonic() - old.created >= 3600:
                del jobs[key]
        if len(jobs) >= 50:
            completed = [old for old in jobs.values() if old.state not in ("queued", "running")]
            if not completed:
                raise ValueError("Wait for a running capture to finish first.")
            del jobs[min(completed, key=lambda old: old.created).id]
        jobs[job.id] = job
    return {"id": job.id, "state": job.state}


def api_response(path, payload, workspace=None):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON request object.")
    if path == "/api/connections/import":
        from .connection_import import response
        return response(payload)
    if path == "/api/connect":
        # Opening a connection group as one Group connection (F8).
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return _connect_response(payload, workspace)
    if path == "/api/live/topology":
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        from . import connection_groups, topology_group
        from .editor_session import _connection_facts
        connection = payload.get("connection")
        if not isinstance(connection, dict) or not connection.get("group_name"):
            raise ValueError("This saved connection is outside the current connection group.")
        section = payload.get("section")
        if not isinstance(section, str) or section not in topology_group.SECTIONS:
            raise ValueError("Choose a topology section: map, members, parameters, accounts or plugins.")
        # Reuse the Group connection membership check before any secret lookup.
        _connection_facts(payload, workspace.store)
        _profiles, current = connection_groups.request_profiles(workspace.store, connection)
        if current is None:
            raise ValueError("This saved connection is outside the current connection group.")
        return _single_flight_read(path, payload, workspace, lambda: topology_group.read_group(
            workspace.store, connection["group_name"], section, current["id"],
            expected_current=current, expected_generation=current.get("group_generation")))
    if path in _TOPOLOGY_CHANGE_PATHS:
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return _topology_variable_response(path, payload, workspace)
    if path == "/api/analysis/prepare-query":
        from .analysis_query import prepare
        return prepare(payload.get("query"))
    if path.startswith("/api/session/"):
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return _session_response(path, payload, workspace)
    if path.startswith("/api/audit/"):
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return _audit_response(path, payload, workspace.store)
    if path in _SLOW_LOG_PATHS:
        # Query digest's Slow log file source: files on this computer, no server.
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        from . import slowlog_api
        return slowlog_api.response(path, payload, workspace)
    if path in _CSV_IMPORT_PATHS:
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        from . import csv_import_api
        return csv_import_api.response(path, payload, workspace, lambda value: _with_saved_password(value, workspace))
    if path.startswith("/api/table-data/"):
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        from . import table_data
        return table_data.response(path, payload, workspace, lambda value: _with_saved_password(value, workspace))
    if path.startswith("/api/editor/"):
        # The Query editor's per-tab sessions (editor_session.py). Run and
        # commit fill the saved password only after the write policy.
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        from . import editor_session
        return editor_session.api_response(path, payload, workspace,
                                           lambda value: _with_saved_password(value, workspace))
    if path in _NEWS_PATHS:
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        from . import news
        return news.api_response(path, payload, workspace.store, getattr(workspace, "news_reader", None))
    if path == "/api/ssh/hosts":
        # Host aliases of ~/.ssh/config for the connection form (no network,
        # no ssh run; ssh-hosts-1.0).
        from .ssh_tunnel import hosts_response
        return hosts_response()
    if path in _CLIENT_PATHS:
        return _client_settings_response(path, payload, workspace)
    if path in _MANAGED_SHELL_PATHS:
        return _shell_response(path, payload, workspace)
    if path == "/api/app/update/check":
        from . import app_update
        result = app_update.check_for_updates(force=payload.get("force") is True)
        if workspace is not None:
            record = app_update.last_check_record(result)
            try:
                result["last_check"] = workspace.store.set_app_update_last_check(record)
            except (sqlite3.Error, OSError):
                # The check itself succeeded or failed on its own terms; a
                # store that cannot be written must not hide that answer.
                result["last_check"] = record
        return result
    if path == "/api/app/update/status":
        from . import app_update
        stored = workspace.store.app_update_last_check() if workspace is not None else None
        return {"schema_version": "app-update-status-1.0", "current_version": __version__,
                "last_check": app_update.stored_last_check(stored),
                "auto_check": workspace.store.app_update_auto() if workspace is not None else True,
                "staging": app_update.staging_state(), "staged": app_update.staged_update(),
                # Restart to update stops these (quitting stops the backend's tools).
                "active_operations": _active_lifecycle_operations(workspace)}
    if path == "/api/app/update/stage":
        from . import app_update
        return app_update.stage_update()
    if path == "/api/app/update/settings":
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        if "auto_check" in payload:
            workspace.store.set_app_update_auto(payload["auto_check"])
        return {"schema_version": "app-update-settings-1.0", "auto_check": workspace.store.app_update_auto()}
    if path in _SETTINGS_PATHS:
        return _settings_response(path, payload, workspace)
    if path == "/api/app/update/download":
        from . import app_update
        return app_update.download_binary_update(
            asset_url=payload.get("asset_url"),
            release_url=payload.get("release_url")
        )
    if path.startswith("/api/lifecycle/"):
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return _lifecycle_response(path, payload, workspace)
    if path in _COMPARE_PATHS:
        from . import compare_api
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return compare_api.response(path, payload, workspace.store,
                                    lambda value: _with_saved_password(value, workspace))
    if path in _BASELINE_PATHS:
        from . import baselines
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return baselines.response(path, payload, workspace.store,
                                  lambda value: _with_saved_password(value, workspace))
    if path in _PARAM_VERSION_PATHS:
        return _param_version_response(path, payload)
    if path in _SCHEMA_VERSION_PATHS:
        from . import schema_versions_api
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return schema_versions_api.response(path, payload, workspace.store,
                                            lambda value: _with_saved_password(value, workspace))
    if path in _DIAGRAM_PATHS:
        from . import schema_diagram_api
        fill = (lambda value: _with_saved_password(value, workspace)) if workspace is not None else None
        return schema_diagram_api.response(path, payload, fill)
    if path in _ACCOUNT_PATHS:
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return _accounts_response(path, payload, workspace)
    if path in _SSH_CONNECTION_PATHS + _FILES_PATHS:
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        if path in _SSH_CONNECTION_PATHS:
            from . import ssh_connections
            return ssh_connections.response(path, payload, workspace.store)
        return _sftp_response(path, payload, workspace.store)
    if path.startswith(("/api/investigations/", "/api/profiles/", "/api/live/", "/api/connection/")):
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        if path in ("/api/profiles/groups/save", "/api/profiles/groups/delete"):
            return _profile_group_response(path, payload, workspace.store)
        if path.startswith(("/api/investigations/", "/api/profiles/")):
            kind = "investigation" if path.startswith("/api/investigations/") else "profile"
            operation = path[len("/api/investigations/"):] if kind == "investigation" else path[len("/api/profiles/"):]
            if kind == "profile":
                return _profile_response(operation, payload, workspace.store)
            if operation == "list":
                # Summaries only (investigation-list-1.0); "get" returns a bundle.
                from .workspace_store import INVESTIGATION_PAGE_SIZE
                return workspace.store.list_investigations(
                    limit=payload.get("limit", INVESTIGATION_PAGE_SIZE), cursor=payload.get("cursor"),
                    search=payload.get("search", ""))
            if operation == "get":
                bundle = workspace.store.get(kind, payload.get("id"))
                if bundle is None:
                    raise ValueError("This investigation no longer exists.")
                return bundle
            if operation == "save": return workspace.store.save(kind, payload.get("value"), payload.get("id"))
            if operation == "delete": return workspace.store.delete(kind, payload.get("id"))
        if path == "/api/live/start" and "session_tab" in payload:
            return _session_capture_response(payload, workspace)
        if path in _CONNECTION_PATHS and path not in _WRITE_PATHS and path not in _ACTION_PATHS:
            payload = _with_saved_password(payload, workspace)
        if path == "/api/live/start":
            from .accounts_api import _group_login_guard
            guard = _group_login_guard(getattr(workspace, "store", None), payload.get("connection"))
            if guard is not None:
                return workspace.jobs.start(payload, login_guard=guard)
            return workspace.jobs.start(payload)
        if path in ("/api/live/status", "/api/live/cancel"):
            from . import dbops
            connection = payload.get("connection")
            identity = (dbops.identity_endpoint(dbops.connection_identity(connection))
                        if isinstance(connection, dict) else None)
            job = workspace.jobs.get(payload.get("id"), identity)
            if path == "/api/live/cancel":
                job.cancel()
            return job.snapshot()
        if path == "/api/live/readiness":
            return _readiness_response(payload, workspace)
        if path == "/api/connection/test":
            return _test_connection_response(payload, workspace)
        if path == "/api/live/kill/prepare":
            return _kill_prepare(payload, workspace)
        if path == "/api/live/kill":
            return _kill_response(payload, workspace)
        if path == "/api/live/kill/bulk/prepare":
            return _bulk_kill_prepare(payload, workspace)
        if path == "/api/live/kill/bulk":
            return _bulk_kill_response(payload, workspace)
        if path == "/api/live/table-columns":
            return _table_columns_response(payload, workspace)
        if path in _REPLICATION_PATHS:
            return _replication_response(path, payload, workspace)
        if path in _CATALOG_PATHS:
            return _catalog_response(payload, path, workspace)
        if path in _OBJECT_SEARCH_PATHS:
            return _object_search_response(payload, workspace)
        if path in _VARIABLE_PATHS:
            return _variable_response(path, payload, workspace)
        if path in _ACTION_PATHS:
            return _action_response(path, payload, workspace)
        if path == _ACTION_PROGRESS_PATH:
            from . import topology_change
            return topology_change.share_progress(workspace.store, _action_progress(workspace)).get(
                payload.get('progress_id'))
        if path in _SERVER_VIEW_PATHS:
            if path in _SINGLE_FLIGHT_READ_PATHS:
                return _single_flight_read(path, payload, workspace,
                                           lambda: _discover_response(path, payload, workspace=workspace))
            return _discover_response(path, payload, workspace=workspace)
        raise ValueError("Unknown workspace operation.")
    if path.startswith("/api/teach/"):
        from .teach import (LESSONS, CURRICULUM, LESSON_FAMILIES, _FAMILY_LABELS,
                            render_lesson)
        if path == "/api/teach/catalog":
            track = [key for key in CURRICULUM if key in LESSONS]
            keys = track + [key for key in LESSONS if key not in track]
            return {
                "curriculum": track,
                "families": [{"key": key, "label": _FAMILY_LABELS[key]}
                             for key in LESSON_FAMILIES],
                "lessons": [{"key": key, "title": LESSONS[key]["title"],
                             "summary": LESSONS[key]["summary"],
                             "family": LESSONS[key]["family"]} for key in keys],
            }
        if path == "/api/teach/lesson":
            key = payload.get("lesson")
            if not isinstance(key, str) or key not in LESSONS:
                raise ValueError("Choose a lesson from the Teach catalog.")
            view = payload.get("view", "2d")
            if view != "2d":
                raise ValueError("Only the 2D lesson view is available.")
            return {"html": render_lesson(key)}
        raise ValueError("Unknown Teach endpoint.")
    if path == "/api/report/export":
        return _export_response(payload)
    if path == "/api/compare":
        before, _, _, _ = _plan(payload, "before")
        after, _, _, _ = _plan(payload, "after")
        hidden = payload.get("hide_literals", False)
        if not isinstance(hidden, bool):
            raise ValueError("Hide literal values must be a boolean.")
        return {"analysis": build_compare_sidecar(before, after, hide_literals=hidden),
                "html": render_compare(before, after, hide_literals=hidden)}
    text, root, query, engine = _plan(payload)
    if path == "/api/analyze":
        from .teach_hooks import build_teach_hooks, build_teach_index_maps
        from .chart_data import build_chart_data
        analysis = analyze_plan(root)
        from .live import normalize_capture
        capture = normalize_capture(payload.get("capture"))
        if isinstance(capture, dict):
            from .advisor import advise
            advise(analysis, schema=capture.get("schema"), stats=capture.get("table_stats"), variables=capture.get("variables"),
                   freshness=capture.get("stats_freshness"), capture=capture)
            query = capture.get("query") or query
        # The SQL travels once, as "query" (and the sidecar's query.raw), never
        # in each teach hook: hooks x SQL length made responses of 100+ MiB.
        # Export JSON in the workspace puts query_sql back into each hook.
        teach_hooks = build_teach_hooks(root)
        sidecar_hooks = build_teach_hooks(root, variables=analysis.get("collected_variables") or {},
                                          stats=analysis.get("collected_stats") or {})
        # The full canonical tree for the in-page chart runtime. Focus,
        # collapse, selection and metric are applied client-side as a
        # copy-only projection, so this is built once per plan. chart-data-2.0
        # stores each analysis message once (a grouped warning can name every
        # table; repeating it per operator grew quadratically).
        chart_data = build_chart_data(
            root, title="Query plan", engine=engine,
            teach_index_by_folded=build_teach_index_maps(teach_hooks)["by_folded_label"],
            analysis=analysis, compact=True)
        return {"analysis": build_sidecar(root, analysis, source_type="file", engine=engine,
                                          query_raw=query, teach_hooks=sidecar_hooks),
                "operators": _operators(root), "query": query, "teach_hooks": teach_hooks,
                "capture": capture, "chart_data": chart_data}
    view = payload.get("view", "flamegraph")
    if view not in VIEWS:
        raise ValueError("Choose a supported visualization.")
    if path == "/api/report":
        hidden = payload.get("hide_literals", False)
        if not isinstance(hidden, bool):
            raise ValueError("Hide literal values must be a boolean.")
        query = payload.get("query", query)
        if not isinstance(query, str) or len(query.encode("utf-8")) > MAX_PLAN:
            raise ValueError("SQL text must be a string within the plan size limit.")
        if payload.get("format") == "json":
            original = payload.get("analysis")
            if original is not None:
                from .output_sidecar import validate_sidecar
                validate_sidecar(original)
                if hidden:
                    from .parser import flatten_nodes, hide_parsed_plan_literals
                    from .plan_privacy import hide_analysis_literals
                    redacted = hide_parsed_plan_literals(root)
                    labels = {}
                    for old, new in zip(flatten_nodes(root), flatten_nodes(redacted)):
                        for key in ("short_label", "full_label", "folded_label"):
                            labels[old[key]] = new[key]
                    original = hide_analysis_literals(original, labels)
                return {"analysis": original}
            if hidden:
                from .parser import hide_plan_json_literals, parse_explain
                from .plan_privacy import hide_literals
                text = hide_plan_json_literals(text)
                root = parse_explain(text)
                query = hide_literals(query)
            return {"analysis": build_sidecar(root, analyze_plan(root), query_raw=query)}
        return {"html": render_html_report(text, view_type=view, query_text=query, hide_literals=hidden)}
    if path == "/api/render":
        unit = "\u00b5s" if 0 < root["total_time"] < 1 else "ms"
        from .exploration import render_exploration
        return {"html": render_exploration(root, view, width=1200,
                    selected_id=payload.get("selected_id", ""), focus_id=payload.get("focus_id", ""),
                    collapsed_ids=payload.get("collapsed_ids"), metric=payload.get("metric", "self_time"))}

    raise ValueError("Unknown API endpoint.")


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    MAX_REQUEST_WORKERS = 32
    # Refused sockets have their own small pool: slow bodies must not occupy
    # request workers or block the single accept loop while they are drained.
    MAX_REFUSAL_DRAINS = 8
    REFUSAL_DRAIN_SECONDS = 2.0
    REFUSAL_DRAIN_BYTES = 8 * 1024 * 1024
    #: Listen backlog: the socketserver default of 5 drops burst connections
    #: (a browser fires many chunk and API requests at once) while the
    #: workers are busy, which the browser sees as a network error.
    request_queue_size = 128

    def server_bind(self):
        # On Windows SO_REUSEADDR lets a later socket bind this same port, so
        # the server leaves it off there and binds exclusively instead.
        if os.name == "nt":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        # HTTPServer.server_bind names the server with socket.getfqdn(host), a
        # reverse DNS lookup that can stall start-up for many seconds on some
        # Macs. This server listens only on 127.0.0.1 and never uses the name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    #: Seconds after shutdown begins by which SSH tunnels are closed, even
    #: while the editor or lifecycle cleanup that may still send KILL QUERY
    #: through them runs on (each can take several seconds). Tunnels live in
    #: their own process groups, and the desktop's backend entry kills this
    #: process five seconds after it was asked to stop.
    TUNNEL_CLOSE_SECONDS = 3.0
    #: Bounded wait for the startup client probe warm-up thread when closing.
    TOOLS_WARMUP_WAIT_SECONDS = 2.0

    def __init__(self, port=0, store_path=None, no_restore=False, tool_groups_file=None, news_reader=None,
                 release_reader=None):
        from .workspace_store import WorkspaceStore
        from .live import LiveJobs
        self.store = WorkspaceStore(store_path)
        from . import connector
        self.store.migrate_client_choices()
        connector.set_client_choices({e: self.store.client_choice(e) for e in ('mysql', 'mariadb')})
        # --no-restore: start clean for this launch and leave the stored
        # session untouched (no restore, no autosave).
        self.no_restore = bool(no_restore)
        # Home news reads the network only through this reader: cmd_ui passes
        # news.fetch_feed; servers built without one (tests) never read feeds.
        self.news_reader = news_reader
        # The Upgrade check's version list reads vendor release pages only
        # through this reader: cmd_ui passes upgrade_targets.fetch_page;
        # servers built without one (tests) answer from the bundled catalog.
        self.release_reader = release_reader
        self.jobs = LiveJobs()
        from .editor_session import EditorSessions
        # One mysql/mariadb client process per Query editor tab; all are
        # ended when the server closes.
        self.editor = EditorSessions()
        from .lifecycle import LifecycleJobs
        # Dump, restore and clone jobs; their tools are stopped first on close.
        # tool_groups_file: the desktop app's record of tool process groups.
        self.lifecycle = LifecycleJobs(tool_groups_file)
        # SSH tunnels list their process groups there too (as services), so the
        # desktop app can stop them if it must kill this backend.
        from .ssh_tunnel import TUNNELS
        TUNNELS.attach_groups(self.lifecycle.groups)
        from .table_data import TableData
        self.table_data = TableData()
        # Browser authentication is separate from the CSRF header. Only the
        # one-time launch URL can establish the HttpOnly session cookie.
        self.token = secrets.token_urlsafe(32)
        self.session_secret = secrets.token_urlsafe(32)
        self.cookie_name = "myflames_" + secrets.token_hex(12)
        self.bootstrap_secret = secrets.token_urlsafe(32)
        self.bootstrap_deadline = time.monotonic() + 30 * 60
        self.bootstrap_lock = threading.Lock()
        self._worker_slots = threading.BoundedSemaphore(self.MAX_REQUEST_WORKERS)
        self._refusal_drain_slots = threading.BoundedSemaphore(self.MAX_REFUSAL_DRAINS)
        super().__init__(("127.0.0.1", port), WorkspaceHandler)
        self.origin = "http://127.0.0.1:%s" % self.server_address[1]
        self.launch_url = self.origin + "/#launch=" + self.bootstrap_secret
        # Warm cached version probes of the query tools (the same names the
        # connection layer probes, connector.py) without delaying startup or
        # recovery; started last so a construction that fails leaves no probe
        # running. MySQL Shell is not probed here: Settings > Client tools
        # (client_tools.discover_choices), Maintenance (lifecycle.detect_tools)
        # and Cluster (cluster_shell) discover it when they are opened.
        from . import client_tools
        self._tools_warmup_stop = threading.Event()
        self._tools_warmup = threading.Thread(target=client_tools.discover,
                                              kwargs={"names": client_tools.QUERY_DISCOVERY_NAMES,
                                                      "stop": self._tools_warmup_stop},
                                              daemon=True)
        self._tools_warmup.start()


    def process_request(self, request, client_address):
        # ThreadingHTTPServer otherwise creates an unbounded number of threads
        # before the handler can reject an unauthenticated slow client.
        if not self._worker_slots.acquire(blocking=False):
            self._refuse_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def _refuse_request(self, request):
        try:
            # This runs on the accept loop. Even sending the small response
            # must not wait for a client that is not reading its socket.
            request.settimeout(0)
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            request.shutdown(socket.SHUT_WR)
        except OSError:
            self.shutdown_request(request)
            return
        if not self._refusal_drain_slots.acquire(blocking=False):
            # Extreme overload: retain the accept loop's availability even if
            # unread request bytes make this immediate close reset the socket.
            self.shutdown_request(request)
            return
        try:
            threading.Thread(target=self._drain_refused_request, args=(request,),
                             name="myflames-refusal-drain", daemon=True).start()
        except BaseException:
            self._refusal_drain_slots.release()
            self.shutdown_request(request)
            raise

    def _drain_refused_request(self, request):
        # Draining after the half-close lets an in-flight body finish without
        # replacing the 503 with a reset. Bound both time and discarded bytes.
        deadline = time.monotonic() + self.REFUSAL_DRAIN_SECONDS
        remaining_bytes = self.REFUSAL_DRAIN_BYTES
        try:
            while remaining_bytes > 0:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    break
                request.settimeout(remaining_time)
                chunk = request.recv(min(65536, remaining_bytes))
                if not chunk:
                    break
                remaining_bytes -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                self.shutdown_request(request)
            finally:
                self._refusal_drain_slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def server_close(self):
        # Stop startup probes before cleanup that can wait for remote servers.
        warmup_stop = getattr(self, "_tools_warmup_stop", None)
        if warmup_stop is not None:
            warmup_stop.set()
        # SSH tunnels close after the cleanup below (its KILL QUERY and
        # session ends may go through them), but never later than
        # TUNNEL_CLOSE_SECONDS from now, and also when that cleanup fails.
        from .ssh_tunnel import TUNNELS
        deadline = threading.Timer(self.TUNNEL_CLOSE_SECONDS, TUNNELS.close_all)
        deadline.daemon = True
        deadline.start()
        try:
            # Lifecycle tools (dump, restore, clone) run in their own process
            # groups, beyond a supervisor's signal to this process group. Stop
            # them first and boundedly, before cleanup that can block on the
            # server (KILL QUERY in editor sessions, replication loops).
            lifecycle = getattr(self, "lifecycle", None)
            if lifecycle is not None:
                lifecycle.shutdown()
            # Stop tool process groups before giving the optional downloader
            # a bounded cleanup window within the desktop supervisor's grace.
            from . import managed_shell
            managed_shell.shutdown(timeout=1.0)
            registry = getattr(self, "replication_loops", None)
            if registry is not None:
                registry.stop_all(wait=True)
            table_data = getattr(self, "table_data", None)
            if table_data is not None:
                table_data.close()
            # Running CSV imports stop between statements; their sessions end
            # with the workspace's other client processes.
            csv_imports = getattr(self, "csv_imports", None)
            if csv_imports is not None:
                csv_imports.close()
            self.jobs.close()
            self.editor.close()
            self.lifecycle.close()
        finally:
            deadline.cancel()
            # Waits for a close the deadline already started.
            TUNNELS.close_all()
            TUNNELS.detach_groups(self.lifecycle.groups)
            # Join the stopped warm-up boundedly before returning from cleanup.
            warmup = getattr(self, "_tools_warmup", None)
            if warmup is not None:
                warmup.join(self.TOOLS_WARMUP_WAIT_SECONDS)
            super().server_close()


# Public, static launch guidance: never interpolate authentication material here.
_LAUNCH_REFUSAL_STYLE = """<style id="launch-refusal-style">
/* Launch refusal: the app's font, surfaces and spacing without a script. */
#root:has(> .launch-refusal) {
  --bg: #ffffff;
  --surface-pane: #f4f5f6;
  --ink: #202429;
  --ink-muted: #59616b;
  --line: #d1d5da;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
  --text-md: 13px;
  --leading-md: 20px;
  --text-xl: 20px;
  --leading-xl: 28px;
  --space-3: 12px;
  --space-5: 24px;
  --space-6: 32px;
  --radius-lg: 10px;
  color-scheme: light;
  box-sizing: border-box;
  display: grid;
  place-items: center;
  min-height: 100dvh;
  padding: var(--space-5);
  background: var(--surface-pane);
}
.launch-refusal {
  box-sizing: border-box;
  width: 100%;
  max-width: 480px;
  margin: 0;
  padding: var(--space-6);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--bg);
  color: var(--ink);
  font: var(--text-md)/var(--leading-md) var(--font-sans);
  overflow-wrap: anywhere;
}
.launch-brand { display: flex; align-items: center; gap: var(--space-3); }
.launch-brand img { flex: none; border-radius: var(--radius-lg); }
.launch-refusal h1 { margin: var(--space-5) 0 var(--space-3); font-size: var(--text-xl); line-height: var(--leading-xl); }
.launch-refusal p { margin: 0; color: var(--ink-muted); }
@media (prefers-color-scheme: dark) {
  #root:has(> .launch-refusal) {
    color-scheme: dark;
    --bg: #151719;
    --surface-pane: #1c1e21;
    --ink: #ededee;
    --ink-muted: #a5a9af;
    --line: #34383d;
  }
}
</style>"""
_LAUNCH_REFUSAL_HTML = """<main class="launch-refusal" role="alert">
<div class="launch-brand"><img src="/assets/myflames-logo.png" alt="" width="48" height="48"><strong>myflames</strong></div>
<h1>Open your workspace</h1>
<p>Open the launch URL printed by myflames ui. If it was already used or expired, restart myflames ui.</p>
</main>"""


class _HeaderDeadlineReader(io.RawIOBase):
    """Apply the remaining header budget before each actual socket read.

    A BufferedReader.readline may perform many reads while a peer trickles
    bytes. Setting a timeout once before readline only limits inactivity.
    """
    def __init__(self, raw, connection, deadline):
        super().__init__()
        self.raw, self.connection, self.deadline = raw, connection, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        deadline = self.deadline()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('Request headers timed out.')
            self.connection.settimeout(remaining)
        return self.raw.readinto(buffer)

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


#: Endpoints that turn plan JSON into a view; every other endpoint reads a
#: server, a store or a job, so the plan wording would mislead there.
_PLAN_PATHS = ("/api/analyze", "/api/render", "/api/report", "/api/compare")

#: Error reporting only: persistent-session actions have their own write
#: policy and review handling rather than the generic _WRITE_PATHS dispatch.
#: They can still send SQL, including KILL QUERY while ending a session.
_SQL_OUTCOME_PATHS = _WRITE_PATHS + ("/api/table-data/apply", "/api/table-data/cancel", "/api/table-data/close",
                                     "/api/editor/rollback", "/api/editor/stop", "/api/editor/close",
                                     "/api/editor/use")


def _unexpected_error_message(path):
    """The neutral 500 text for *path*. The response never carries the cause."""
    if path in _SQL_OUTCOME_PATHS:
        return "The request could not be completed. Its outcome is unknown. Check the server state before trying again."
    if path in _PLAN_PATHS:
        return "Could not render this plan. Try another view or the CLI."
    if path.startswith("/api/live/"):
        return "Could not read this from the server. Try Refresh or the CLI."
    return "Could not complete the request. Try again or use the CLI."


#: A message that already says what failed ("Could not read the server
#: status: ...") is sent as it is, never under a second prefix.
_SAYS_WHAT_FAILED = ("Could not ", "Cannot ", "Can't ", "Unable to ", "Failed to ")


def request_error_text(exc, prefix=True):
    """The error text of a refused request (400), the one path every read
    and write of the workspace answers through. A common connection error
    (could not connect, unknown host, lost connection, access denied) leads
    with plain words and what to check, then the client's own error line
    (dbops.connection_error_text); other messages keep their words, and only
    one that does not say what failed gets "Could not complete the request: "
    (*prefix* False: never, for a client error passed through as it is)."""
    from .dbops import connection_error_text
    from .cluster import ActionUnavailable
    text = str(exc)
    # Catalog refusals already contain the exact disabled-button reason and
    # fix. Keep their text intact through the HTTP 400 boundary.
    if isinstance(exc, (_ClientChoiceError, ActionUnavailable, _ManagedShellError)):
        return text
    plain = connection_error_text(text)
    if plain:
        return plain
    if not prefix or text.startswith(_SAYS_WHAT_FAILED):
        return text
    return "Could not complete the request: %s" % text


def _secret_values(value, store=None, found=None, depth=0):
    """Every non-empty string stored under a key that names a password (any
    depth), and the saved password of each connection that names a profile
    and sends none, so a traceback never carries one."""
    found = set() if found is None else found
    if depth > 12:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and ("password" in key.lower() or key.lower() == "secret") and isinstance(item, str) and item:
                found.add(item)
            else:
                _secret_values(item, store, found, depth + 1)
        if store is not None and value.get("profile_id") and not value.get("password"):
            try:
                saved = store.connection_with_saved_password(value).get("password")
            except Exception:
                saved = None
            if isinstance(saved, str) and saved:
                found.add(saved)
        # Group probes have only group_name and active_member_profile_id in
        # their request. A failure can originate in any saved member, whose
        # password must be hidden too. Read only the explicitly named group;
        # this opens no server connection and never scans unrelated secrets.
        group = value.get("group_name")
        if store is not None and isinstance(group, str) and group:
            try:
                from .workspace_store import ENDPOINT_FIELDS
                for profile in store.list("profile"):
                    if not isinstance(profile, dict) or profile.get("group") != group:
                        continue
                    member = {key: profile.get(key) for key in ENDPOINT_FIELDS}
                    member["profile_id"] = profile.get("id")
                    _secret_values(member, store, found, depth + 1)
            except Exception:
                pass
    elif isinstance(value, (list, tuple)):
        for item in value:
            _secret_values(item, store, found, depth + 1)
    return found


def _log_unexpected_error(path, exc_info, payload=None, store=None):
    """Write the traceback of an unexpected error to stderr, never to the
    response, with request passwords and SQL/URL credentials removed."""
    import traceback
    from .dbops import redact
    try:
        text = "".join(traceback.format_exception(*exc_info))
        variants = set()
        for secret in _secret_values(payload, store):
            # Also as it reads inside a repr or JSON text of the exception.
            variants.update((secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]))
        for secret in sorted(variants, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        # Redact complete literals before truncating, including credentials
        # that cross the log limit and are absent from the request payload.
        text = redact(text)
        if len(text) > 64 * 1024:
            text = text[:64 * 1024] + "\n[traceback truncated]\n"
        sys.stderr.write("myflames: unexpected error in POST %s\n%s" % (path, text))
        sys.stderr.flush()
    except Exception:
        pass


class WorkspaceHandler(BaseHTTPRequestHandler):
    # StreamRequestHandler.setup applies this before reading request headers.
    timeout = 30
    BODY_TIMEOUT = 15

    def setup(self):
        super().setup()
        self._header_deadline = None
        # Socket streams are buffered; in-memory request adapters may already
        # be raw streams. No bytes have been read yet, so detaching is safe.
        raw = self.rfile.detach() if isinstance(self.rfile, io.BufferedReader) else self.rfile
        self.rfile = io.BufferedReader(_HeaderDeadlineReader(
            raw, self.connection, lambda: self._header_deadline))

    def handle_one_request(self):
        self._header_deadline = time.monotonic() + self.timeout
        return super().handle_one_request()

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            # Endpoint work has its own deadlines; only headers use this one.
            self._header_deadline = None
            self.connection.settimeout(self.timeout)

    def log_message(self, fmt, *args):
        # Plan text, filenames, and tokens must not end up in access logs.
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
        if isinstance(body, dict):
            body = json.dumps(body, allow_nan=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self):
        # Host checking also prevents DNS rebinding. No wildcard bind or CORS.
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self._send(403, {"error": "Open the URL printed by myflames ui."})
            return False
        origin = self.headers.get("Origin")
        if (self.command == "POST" or origin) and origin != self.server.origin:
            self._send(403, {"error": "Cross-origin requests are not allowed."})
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        path = urlsplit(self.path).path
        if path == "/":
            try:
                index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
            except FileNotFoundError:
                self._send(503, "Workspace assets are missing. Run `npm ci` and `npm run build` "
                           "in apps/workspace, then reload this page.\n", "text/plain; charset=utf-8")
                return
            # Only a validated registry id may enter HTML. This also rejects
            # malformed rows in a store written by an older or external tool.
            from .workspace_store import UI_THEME_IDS
            try:
                theme = self.server.store.ui_theme_for_index()
            except Exception:
                # Preferences must never prevent startup. Do not log storage
                # errors, which may contain private paths or stored values.
                theme = None
            if theme in UI_THEME_IDS and theme != 'system':
                index = index.replace('<html lang="en">', '<html lang="en" data-theme="%s">' % theme, 1)
            # About myflames shows the installed package version.
            index = index.replace("__MYFLAMES_VERSION__", html.escape(__version__))
            index = index.replace("</head>", _LAUNCH_REFUSAL_STYLE + "</head>")
            index = index.replace('<div id="root"></div>', '<div id="root">' + _LAUNCH_REFUSAL_HTML + '</div>')
            self._send(200, index, "text/html; charset=utf-8")
            return
        # Only flat bundled assets are exposed, never arbitrary local files.
        name = path[len("/assets/"):] if path.startswith("/assets/") else ""
        if name and "/" not in name and "\\" not in name and name not in (".", ".."):
            asset = ASSET_DIR / "assets" / name
            types = {".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml", ".woff2": "font/woff2"}
            if asset.is_file() and asset.suffix in types:
                self._send(200, asset.read_bytes(), types[asset.suffix] + "; charset=utf-8")
                return
        self._send(404, {"error": "Not found."})

    def _read_body(self, size):
        # read1 performs at most one underlying read, so a peer sending bytes
        # just before each socket timeout cannot extend the total deadline.
        deadline = time.monotonic() + self.BODY_TIMEOUT
        chunks = []
        remaining = size
        while remaining:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("Request body deadline exceeded.")
            self.connection.settimeout(budget)
            chunk = self.rfile.read1(min(remaining, 65536))
            if not chunk:
                raise ValueError("Incomplete request body.")
            chunks.append(chunk)
            remaining -= len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("Request body deadline exceeded.")
        return b"".join(chunks)

    def _authenticated(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        value = cookies.get(self.server.cookie_name)
        return bool(value and secrets.compare_digest(
            value.value.encode("utf-8"), self.server.session_secret.encode("ascii")))

    def _bootstrap(self):
        # The custom header and JSON content type prevent HTML form requests;
        # exact Origin and Host checks run before this handler.
        if self.headers.get("X-Myflames-Bootstrap") != "1":
            self._send(403, {"error": "Open the launch URL printed by myflames ui."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Send application/json."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 or self.headers.get("Transfer-Encoding"):
                self._send(413, {"error": "Invalid authentication request size."})
                return
            payload = json.loads(self._read_body(size).decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("secret", ""), str):
                raise ValueError("Invalid authentication request.")
        except (TimeoutError, OSError):
            return
        except (ValueError, UnicodeError, RecursionError):
            self._send(400, {"error": "Invalid authentication request."})
            return
        # Cookies are host-scoped, not port-scoped: another loopback service
        # can receive this cookie. Refresh also needs the browser's origin-
        # scoped proof so a copied cookie cannot recover API authority.
        proof = self.headers.get("X-Myflames-Token", "")
        if self._authenticated() and secrets.compare_digest(
                proof.encode("utf-8"), self.server.token.encode("ascii")):
            self._send(200, {"token": self.server.token})
            return
        secret = payload.get("secret", "")
        with self.server.bootstrap_lock:
            accepted = bool(self.server.bootstrap_secret and
                            time.monotonic() <= self.server.bootstrap_deadline and
                            secrets.compare_digest(secret.encode("utf-8", "replace"),
                                                   self.server.bootstrap_secret.encode("ascii")))
            if accepted:
                self.server.bootstrap_secret = None
        if not accepted:
            self._send(403, {"error": "Open the launch URL printed by myflames ui. If it was already used or expired, restart myflames ui."})
            return
        self._send(200, {"token": self.server.token}, cookie="%s=%s; Path=/api/; HttpOnly; SameSite=Strict" %
                   (self.server.cookie_name, self.server.session_secret))

    def _cancellable_read(self, payload):
        """Watch EOF only after the complete body has been read.

        MSG_PEEK never consumes bytes, including a pipelined request. The
        socket is nonblocking until the watcher is joined; response writes
        begin only after its original timeout has been restored.
        TCP write half-close also looks like EOF; the workspace client never
        half-closes its requests, so it is treated as a disconnected caller.
        """
        cancel, stopped = threading.Event(), threading.Event()
        timeout = self.connection.gettimeout()

        def watch():
            try:
                # kqueue/epoll avoid select()'s FD_SETSIZE limit when many
                # database clients or workspace requests are open.
                with selectors.DefaultSelector() as selector:
                    selector.register(self.connection, selectors.EVENT_READ)
                    while not stopped.wait(0.1):
                        try:
                            if selector.select(0) and self.connection.recv(1, socket.MSG_PEEK) == b"":
                                cancel.set()
                                return
                        except (BlockingIOError, InterruptedError):
                            continue
            except (OSError, ValueError):
                # A failed watcher is not evidence of a disconnect. Leave
                # the read running under its ordinary query deadline.
                return

        watcher = threading.Thread(target=watch, name="myflames-read-disconnect", daemon=True)
        self.connection.setblocking(False)
        try:
            watcher.start()
            with read_cancel.scope(cancel):
                result = api_response(self.path, payload, self.server)
                read_cancel.check()
                return result
        finally:
            stopped.set()
            if watcher.ident is not None:
                watcher.join()
            self.connection.settimeout(timeout)

    def do_POST(self):
        if not self._local_request():
            return
        if self.path == "/api/auth/session":
            self._bootstrap()
            return
        token = self.headers.get("X-Myflames-Token", "")
        if not self._authenticated() or not secrets.compare_digest(token.encode("utf-8"), self.server.token.encode("ascii")):
            self._send(403, {"error": "Session expired. Open the launch URL printed by myflames ui."})
            return
        if self.path not in ("/api/analyze", "/api/render", "/api/report", "/api/compare",
                             "/api/teach/catalog", "/api/teach/lesson",
                             "/api/investigations/list", "/api/investigations/get", "/api/investigations/save", "/api/investigations/delete",
                             "/api/profiles/list", "/api/profiles/save", "/api/profiles/delete", "/api/profiles/group",
                             "/api/profiles/groups/save", "/api/profiles/groups/delete",
                             "/api/profiles/forget-password", "/api/ssh/hosts",
                             "/api/live/start", "/api/live/status", "/api/live/cancel", "/api/connection/test",
                             "/api/connect",
                             "/api/live/topology", "/api/live/index-suggestion", "/api/live/top", "/api/live/slow-log", "/api/live/digest-histogram", "/api/live/digest-tables",
                             "/api/live/running", "/api/live/sessions", "/api/live/sessions/history", "/api/live/ddl-progress",
                             "/api/live/locks", "/api/live/innodb", "/api/live/error-log", "/api/live/cluster", "/api/live/proxysql",
                             "/api/live/kill", "/api/live/kill/prepare",
                             "/api/live/kill/bulk", "/api/live/kill/bulk/prepare",
                             "/api/live/table-columns",
                             "/api/live/health", "/api/live/watch", "/api/live/watch/capture",
                             "/api/live/memory", "/api/live/waits", "/api/live/indexes",
                             "/api/live/tables", "/api/live/schemas",
                             "/api/live/io", "/api/live/config", "/api/live/instruments",
                             "/api/live/report", "/api/report/export",
                             "/api/live/explain-connection",
                             "/api/session/load", "/api/session/save", "/api/session/clear",
                             "/api/session/settings",
                             "/api/audit/list", "/api/audit/export", "/api/audit/settings",
                             "/api/audit/declined", "/api/audit/object",
                             "/api/table-data/open", "/api/table-data/prepare", "/api/table-data/apply",
                             "/api/table-data/decline", "/api/table-data/status", "/api/table-data/cancel", "/api/table-data/close",
                             "/api/editor/open", "/api/editor/run", "/api/editor/status", "/api/editor/preview",
                             "/api/editor/stop", "/api/editor/commit", "/api/editor/rollback",
                             "/api/editor/close", "/api/editor/history", "/api/editor/export",
                             "/api/editor/use", "/api/editor/file/open", "/api/editor/file/save",
                             "/api/editor/saved/list", "/api/editor/saved/save", "/api/editor/saved/rename",
                             "/api/editor/saved/move", "/api/editor/saved/delete",
                             "/api/analysis/prepare-query") + _LIFECYCLE_PATHS \
                + _REPLICATION_PATHS + _CATALOG_PATHS + _OBJECT_SEARCH_PATHS + _VARIABLE_PATHS + _TOPOLOGY_CHANGE_PATHS + _ACTION_PATHS + _ACCOUNT_PATHS + _DIAGRAM_PATHS + _SCHEMA_VERSION_PATHS + _COMPARE_PATHS + _APP_UPDATE_PATHS \
                + _CLIENT_PATHS + _MANAGED_SHELL_PATHS + _NEWS_PATHS + _SETTINGS_PATHS + _PARAM_VERSION_PATHS + _CSV_IMPORT_PATHS + _SLOW_LOG_PATHS \
                + _SSH_CONNECTION_PATHS + _FILES_PATHS + ("/api/live/readiness", "/api/connections/import") \
                + _BASELINE_PATHS \
                + (_ACTION_PROGRESS_PATH, "/api/live/proxysql/digests", "/api/live/readyset",
                   "/api/live/readyset/explain-cache", "/api/live/readyset/export-caches"):
            self._send(404, {"error": "Not found."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Send application/json."})
            return
        from .workspace_store import SessionConflict, SessionTooLarge
        from .editor_session import EditorError
        session = self.path == "/api/session/save"
        large = session or self.path in _SCHEMA_VERSION_LARGE
        payload = None
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= (MAX_SESSION_BODY if large else MAX_BODY) or self.headers.get("Transfer-Encoding"):
                self._send(413, {"error": "The workspace is larger than 64 MiB. Remove some plans so it can be saved."
                                 if session and size > 0 else "Request must be between 1 byte and %s."
                                 % ("64 MiB" if large else "12 MiB")})
                return
            try:
                payload = json.loads(self._read_body(size).decode("utf-8"))
            except RecursionError:
                # Decoder limits describe malformed input. A RecursionError
                # from endpoint code still reaches the logged response below.
                raise ValueError("Request JSON is too deeply nested.") from None
            if (self.path in _CANCELLABLE_READ_PATHS and self.path not in _WRITE_PATHS
                    and hasattr(self, "connection")):
                result = self._cancellable_read(payload)
            else:
                result = api_response(self.path, payload, self.server)
            self._send(200, result)
        except read_cancel.CancelledRead:
            # The caller has gone away. No response and no error/fallback read.
            self.close_connection = True
            return
        except SessionConflict as exc:
            self._send(409, {"error": str(exc), "code": "session_conflict", "revision": exc.revision})
        except SessionTooLarge as exc:
            self._send(413, {"error": str(exc), "code": "session_too_large"})
        except EditorError as exc:
            # busy, identity_mismatch, too_many_sessions, no_session, ...
            self._send(409 if exc.code in ("busy", "identity_mismatch", "too_many_sessions") else 400,
                       {"error": str(exc), "code": exc.code})
        except Exception as exc:
            # SSH failures (sftp.py) name their reason; other exceptions with a
            # reason attribute (UnicodeError, SSLError, URLError) are not codes.
            reason = getattr(exc, "reason", None) if isinstance(exc, ConnectorError) else None
            missing = (client_missing_error(exc) if isinstance(
                exc, (ConnectorError, ValueError))
                else None)
            if missing is not None:
                self._send(400, {"error": str(missing), "code": missing.code})
            elif isinstance(reason, str) and reason:
                self._send(400, {"error": request_error_text(exc, prefix=False), "code": reason})
            elif isinstance(exc, ValueError):
                self._send(400, {"error": request_error_text(exc)})
            elif isinstance(exc, ConnectorError):
                self._send(400, {"error": request_error_text(exc, prefix=False)})
            elif self.path in _SQL_OUTCOME_PATHS:
                # An internal error can follow a successful SQL send (for
                # example during read-back). Only explicit refusals above
                # establish a 4xx outcome; an unexpected write failure is
                # uncertain, including malformed-field exception types.
                _log_unexpected_error(self.path, sys.exc_info(), payload, getattr(self.server, "store", None))
                self._send(500, {"error": _unexpected_error_message(self.path)})
            elif isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
                return
            elif isinstance(exc, (TypeError, KeyError, AttributeError, RecursionError, OverflowError)):
                # Handlers may detect malformed request fields by indexing
                # or using their expected type. Preserve the 400 contract,
                # while logging redacted diagnostics for possible code bugs.
                _log_unexpected_error(self.path, sys.exc_info(), payload, getattr(self.server, "store", None))
                self._send(400, {"error": _unexpected_error_message(self.path)})
            else:
                _log_unexpected_error(self.path, sys.exc_info(), payload, getattr(self.server, "store", None))
                self._send(500, {"error": _unexpected_error_message(self.path)})


def cmd_ui(argv):
    parser = argparse.ArgumentParser(
        prog="myflames ui",
        description="Open the local Performance workspace in your browser (deprecated; use the desktop application).",
        epilog="The workspace (open plans, query tabs, connection settings without passwords, "
               "and the current screen) is autosaved to ~/.myflames/workspace.sqlite3 and "
               "restored on the next launch. Turn this off in the UI under Settings, "
               "'Restore workspace on startup', or use --no-restore for one launch.")
    parser.add_argument("--port", type=int, default=0, metavar="PORT",
                        help="Listen on this local port (default: choose an available port).")
    parser.add_argument("--no-browser", action="store_true",
                        help="Print the local URL without opening a browser.")
    parser.add_argument("--no-restore", action="store_true",
                        help="Start with a clean workspace for this launch. The saved session "
                             "is kept for the next launch, and this launch does not autosave.")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if not (ASSET_DIR / "index.html").is_file():
        parser.exit(2, "UI assets are missing. Run `npm ci` and `npm run build` in apps/workspace, or reinstall myflames.\n")
    def _fetch_feed(*args, **kwargs):
        from .news import fetch_feed  # Home news: the only server that reads feeds
        return fetch_feed(*args, **kwargs)

    def _fetch_page(*args, **kwargs):
        from .upgrade_targets import fetch_page
        return fetch_page(*args, **kwargs)

    try:
        # Set only by the desktop app: a file in its private runtime directory.
        # The readers import news and upgrade_targets on first use, not here.
        server = WorkspaceServer(args.port, no_restore=args.no_restore,
                                 tool_groups_file=os.environ.get("MYFLAMES_TOOL_GROUPS_FILE") or None,
                                 news_reader=_fetch_feed, release_reader=_fetch_page)
    except ValueError as exc:
        parser.exit(2, "Cannot start the UI: %s\n" % exc)
    except OSError as exc:
        parser.exit(2, "Cannot start the UI: %s. Try --port 0.\n" % exc)
    with server:
        if not os.environ.get("MYFLAMES_DESKTOP_DATA"):
            sys.stderr.write("warning: 'myflames ui' (browser workspace) is deprecated; use the standalone desktop application (myflames.app).\n")
        sys.stderr.write("myflames UI: %s\nPress Ctrl+C to stop.\n" % server.launch_url)
        if not args.no_browser:
            try:
                if not webbrowser.open(server.launch_url):
                    sys.stderr.write("Open the URL above in your browser.\n")
            except webbrowser.Error:
                sys.stderr.write("Open the URL above in your browser.\n")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("\nUI stopped.\n")
