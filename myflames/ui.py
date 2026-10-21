"""Local browser workspace with explicit saved investigations. Runtime dependencies: Python stdlib only."""
import argparse
import html
import io
import json
import os
import secrets
import socket
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
from .connector import ConnectorError
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


def _discover_response(path, payload):
    """Read-only server views over the saved connection (see discover.py,
    sessions.py, innodb_status.py, error_log.py, cluster.py, locks.py, health.py, waits.py, indexes.py, schema_health.py, io_stats.py,
    config_review.py; the schema picker's list is perf_common.schema_catalog)."""
    from .live import connection_options
    from .connector import MySQLConnection
    from . import discover
    options = connection_options(payload.get("connection"))
    password = options.get("password")
    if path == "/api/live/explain-connection":
        # MariaDB waits up to 30 s for the target thread to answer SHOW EXPLAIN.
        options["query_timeout"] = 40
    elif path in ("/api/live/indexes", "/api/live/tables", "/api/live/report"):
        # A server-wide information_schema.STATISTICS read is slow on MariaDB.
        options["query_timeout"] = 60
    elif path in ("/api/live/io", "/api/live/config", "/api/live/innodb", "/api/live/error-log", "/api/live/cluster",
                  "/api/live/proxysql", "/api/live/proxysql/digests"):
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
        return _report_response(payload, options, schemas)
    try:
        with MySQLConnection(**options) as conn:
            if path == "/api/live/top":
                schema = payload.get("schema") or None
                # table: "schema.table" or {"schema", "table"} (workload attribution).
                return discover.top_report(conn, payload.get("limit", 20), schema,
                                           payload.get("order", "total"), payload.get("table") or None,
                                           extra_metrics=bool(payload.get("extra_metrics")))
            if path == "/api/live/slow-queries":
                return discover.slow_queries_report(conn, payload.get("min_seconds", 1.0),
                                                    payload.get("limit", 50), payload.get("source", "auto"))
            if path == "/api/live/running":
                return discover.running_report(conn, payload.get("min_seconds", 0))
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
            if path == "/api/live/cluster":
                from .cluster import read_status, with_actions
                return with_actions(read_status(conn))
            if path == "/api/live/proxysql":
                from .proxysql import read_status as proxysql_status
                return proxysql_status(conn)
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
                return health_report(conn, payload.get("interval", DEFAULT_INTERVAL))
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


def _report_response(payload, options, schemas):
    """The Full analysis (report.analysis_report): one run per connection at
    a time; an identical request while one runs waits for it and gets the
    same report (report.run_single_flight)."""
    from .connector import MySQLConnection
    from .perf_common import ALL_SCHEMAS_TABLE_LIMIT
    from . import report as report_module
    password = options.get("password")
    interval = payload.get("interval", report_module.DEFAULT_INTERVAL)
    limit = None if payload.get("all_schemas") is True else ALL_SCHEMAS_TABLE_LIMIT
    # Only requests that would authenticate the same way share a run: the
    # profile, every connection and TLS setting, and a password fingerprint.
    connection = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
    key = report_module.flight_key(options, connection.get("profile_id"),
                                   {"type": connection.get("type"), "read_only": connection.get("read_only")})
    settings = (interval, tuple(schemas or ()), limit)

    def work():
        with MySQLConnection(**options) as conn:
            return report_module.analysis_report(conn, interval, schemas=schemas, require_choice_above=limit)
    try:
        return report_module.run_single_flight(key, settings, work)
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, password)


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
    if password and password in str(exc):
        raise type(exc)(str(exc).replace(password, "[redacted]"))
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
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
    return dbops.connection_identity(connection if isinstance(connection, dict) else {}, safety)


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
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
    statement = ("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % connection_id
    decision = dbops.decide(dbops.classify(statement), safety["type"], safety["read_only"], safety["profile_name"])
    plain = connection if isinstance(connection, dict) else {}
    audit = dbops.AuditTrail(workspace.store, "kill_query" if mode == "query" else "kill", "ui",
                             connection={k: plain.get(k) for k in ("host", "port", "user")}, safety=safety,
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
        audit.connection = {k: options.get(k) for k in ("host", "port", "user")}
        with MySQLConnection(**options) as conn:
            return kill_session(conn, connection_id, mode, expect, audit=audit, decision=decision,
                                confirmed=payload.get("confirmed") is True, connection=plain, before_send=consume)
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
    from .locks import KILL_TAG, _check_kill_args, kill_session, refused_result
    targets = payload.get("targets")
    if not isinstance(targets, list) or not (1 <= len(targets) <= 200):
        raise ValueError("Choose between 1 and 200 sessions to kill.")
    mode = payload.get("mode", "connection")
    if mode not in ("connection", "query"):
        raise ValueError("mode must be 'connection' or 'query'.")
    connection = payload.get("connection")
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
    plain = connection if isinstance(connection, dict) else {}
    sample_stmt = ("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % targets[0].get("connection_id", 1)
    decision = dbops.decide(dbops.classify(sample_stmt), safety["type"], safety["read_only"], safety["profile_name"])
    blocked = dbops.gate(decision, confirmed=payload.get("confirmed") is True)
    if blocked:
        return {
            "schema_version": "kill-bulk-1.0",
            "total": len(targets),
            "killed": 0, "gone": 0, "refused": len(targets), "denied": 0, "errors": 0,
            "message": blocked[1],
            "results": [{"connection_id": t.get("connection_id"), "status": blocked[0], "message": blocked[1]} for t in targets]
        }
    details = _bulk_kill_review_details(payload)
    consume = _review_tokens(workspace).consumer(payload.get("review_token"), "bulk_kill",
                dbops.connection_identity(plain, safety), details)
    consume()
    payload = _with_saved_password(payload, workspace)
    options = connection_options(payload.get("connection"))
    password = options.get("password")
    results = []
    with MySQLConnection(**options) as conn:
        for t in targets:
            cid = t.get("connection_id")
            exp = t.get("expect")
            stmt = ("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % cid
            audit = dbops.AuditTrail(workspace.store, "kill_query" if mode == "query" else "kill", "ui",
                                     connection={k: options.get(k) for k in ("host", "port", "user")}, safety=safety,
                                     password=password, policy=dbops.policy_summary(decision))
            try:
                res = kill_session(conn, cid, mode, exp, audit=audit, decision=decision,
                                   confirmed=True, connection=plain)
                results.append({"connection_id": cid, "status": res.get("status"), "message": res.get("message")})
            except Exception as exc:
                results.append({"connection_id": cid, "status": "error", "message": str(exc)})
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


def _table_columns_response(payload):
    from .live import connection_options
    from .connector import MySQLConnection
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
    with MySQLConnection(**options) as conn:
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
                                    connection={k: connection.get(k) for k in ("host", "port", "user")},
                                    safety=safety, target=target, password=password)
    return {"audit_id": audit_id, "status": status}


def _audit_response(path, payload, store):
    """The audit trail of server writes (dbops.py): list, export, settings, declined."""
    from . import dbops
    if path == "/api/audit/declined":
        return _declined_response(payload, store)
    since, server = payload.get("since_days"), payload.get("server") or None
    status = payload.get("status") or None
    if path == "/api/audit/list":
        return dbops.audit_report(store, since, server, payload.get("limit", 500), status, payload.get("before"))
    if path == "/api/audit/export":
        return dbops.audit_export(store, payload.get("format", "json"), since, server, status)
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


def _lifecycle_donor(payload, workspace, passwords, plan=None):
    """Read donor policy and bind a confirmed pair before retrieving its secret.

    A read-only donor may supply data. Its type and read-only flag are still
    recorded in the plan so a different donor policy needs another review.
    """
    from . import dbops, lifecycle as lc
    donor = payload.get("donor")
    if not isinstance(donor, dict):
        raise ValueError("Choose the donor connection.")
    profile = _request_profile(donor, workspace.store)
    if donor.get("profile_id") and profile is None:
        raise ValueError("The donor profile no longer exists. Choose it again.")
    safety = dbops.connection_safety(profile, donor)
    options = _lifecycle_connection(donor, safety)
    request = plan.get("request", {}) if plan is not None else payload
    require_ssl = request.get("require_ssl") is not False
    if (options.get("ssl_mode") in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY") or
            (profile or {}).get("ssl_mode") in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY")) and not require_ssl:
        raise ValueError("The donor requires TLS; clone must use REQUIRE SSL.")
    if plan is not None:
        connection = payload.get("connection")
        recipient_safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
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


def _lifecycle_prepare(payload, options, workspace, passwords):
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
            donor = _lifecycle_donor(payload, workspace, passwords)
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
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
    decision = dbops.decide("risky", safety["type"], safety["read_only"], safety["profile_name"])
    plain = connection if isinstance(connection, dict) else {}
    audit = dbops.AuditTrail(workspace.store, operation, "ui",
                             connection={k: plain.get(k) for k in ("host", "port", "user")}, safety=safety,
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
        donor = _lifecycle_donor(payload, workspace, passwords, plan) if operation == "clone" else None
        filled = _with_saved_password(payload, workspace)
        # Track the secret before validation, which can itself fail.
        passwords.append((filled.get("connection") or {}).get("password"))
        options = _lifecycle_connection(filled.get("connection"), safety)
        audit.password = options.get("password")
        audit.connection = {k: options.get(k) for k in ("host", "port", "user")}
        kwargs = dict(confirmed=confirmed, typed_target=payload.get("typed_target"), audit=audit, safety=safety,
                      start=False)
        if operation == "dump":
            job = lc.dump_job(options, plan, **kwargs)
        elif operation == "restore":
            job = lc.restore_job(options, plan, typed_acknowledgement=payload.get("typed_acknowledgement"), **kwargs)
        else:
            job = lc.clone_job(donor, options, plan, **kwargs)
        lookup = lambda key: workspace.store.get('profile', key)
        lc.bind_profile_policy(job, options, safety, lookup)
        if donor is not None:
            donor_safety = dbops.connection_safety(_request_profile(payload['donor'], workspace.store), donor)
            donor_safety.update(type=donor['type'], read_only=donor['read_only'])
            lc.bind_profile_policy(job, donor, donor_safety, lookup, write=False)
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
        return workspace.lifecycle.add(job, target)
    except Exception:
        with _LIFECYCLE_OWNERS_LOCK:
            owners.pop(job.id, None)
        raise


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
    if path == "/api/lifecycle/tools" and not isinstance(payload.get("connection"), dict):
        return _dump_tool_capabilities(lc.detect_tools(check_updates=payload.get("check_updates") is True))
    if path in ("/api/lifecycle/upgrade-check", "/api/lifecycle/prepare") and payload.get("config_path") is not None:
        raise ValueError("config_path is not accepted over HTTP; use the CLI --config-path option.")
    connection = payload.get("connection")
    passwords = [connection.get("password")] if isinstance(connection, dict) else []
    if isinstance(payload.get("donor"), dict):
        passwords.append(payload["donor"].get("password"))
    try:
        safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
        payload = _with_saved_password(payload, workspace)
        passwords.append((payload.get("connection") or {}).get("password"))
        options = _lifecycle_connection(payload.get("connection"), safety)
        if path == "/api/lifecycle/tools":
            tools = _dump_tool_capabilities(lc.detect_tools(check_updates=payload.get("check_updates") is True))
            with lc._connect(lc.LifecycleConnection, options) as conn:
                server = lc.read_server(conn, options)
            return dict(tools, server={k: server[k] for k in ("engine", "version")},
                        offered=lc.offered_tools(server, tools))
        if path == "/api/lifecycle/upgrade-check":
            with lc._connect(lc.LifecycleConnection, options) as conn, \
                    workspace.lifecycle.runner("upgrade_check", options) as runner:
                report = lc.upgrade_check(conn, payload.get("target_version"), options,
                                          prefer_shell=payload.get("prefer_shell") is not False, job=runner)
            report["server"] = {k: report["server"][k] for k in ("engine", "version", "identity")}
            return report
        if path == "/api/lifecycle/prepare":
            plan = lc._report_safe(_lifecycle_prepare(payload, options, workspace, passwords), passwords)
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
    state = {"connection": {k: plain.get(k) for k in ("host", "port", "user")}, "password": password}

    def decision_reader():
        # A confirmation authorizes the saved policy at that moment. Loops
        # must see later profile edits even when no browser request arrives.
        profile = _request_profile(plain, workspace.store)
        current = dbops.connection_safety(profile, plain)
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
    from .connector import MySQLConnection
    options = connection_options(_with_saved_password(payload, workspace).get("connection"))
    state["password"] = options.get("password") or state["password"]
    state["connection"] = {k: options.get(k) for k in ("host", "port", "user")}
    options["query_timeout"] = 30
    return MySQLConnection(**options)


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
        safety = dbops.connection_safety(profile, connection)
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
        safety = dbops.connection_safety(profile, connection)
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
        conn = _replication_open(payload, workspace, state)
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
        from .connector import MySQLConnection
        options = connection_options(payload.get("connection"))
        options["query_timeout"] = 30
        try:
            with MySQLConnection(**options) as conn:
                # Source versions: the relay log, else a matching saved connection
                # (opened at most once per process for the same settings and password).
                report = rp.read_status(conn, source_versions=True, profiles=rp.saved_profiles(workspace.store),
                                        open_profile=rp.profile_opener(workspace.store),
                                        replica_host=options.get("host"))
                if report.get("available") and report.get("replica"):
                    guard = rp.receiver_guard_plan(conn, report)
                    report.update(guard)
                    for channel in report.get("channels", ()):
                        if channel.get("skip_plan"):
                            plan = channel["skip_plan"]
                            plan.update(guard)
                            if plan.get("available"):
                                plan["review_token"] = _review_tokens(workspace).issue("replication_skip",
                                    _review_identity(payload.get("connection"), workspace),
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
_SERVER_VIEW_PATHS = ("/api/live/top", "/api/live/slow-queries", "/api/live/running", "/api/live/sessions", "/api/live/locks",
                      "/api/live/innodb", "/api/live/error-log", "/api/live/cluster", "/api/live/health",
                      "/api/live/waits", "/api/live/indexes", "/api/live/tables", "/api/live/schemas",
                      "/api/live/io", "/api/live/config", "/api/live/instruments",
                      "/api/live/report", "/api/live/explain-connection", "/api/live/proxysql",
                      "/api/live/proxysql/digests")
#: /api/live/kill is the one write: a confirmed KILL of one session (locks.kill_session).
#: It fills the saved password itself, after the connection's write policy.
#: The Query editor's run and commit (editor_session.py) are writes too: the
#: policy runs first and the saved password is filled only after it.
_EDITOR_WRITE_PATHS = ("/api/editor/run", "/api/editor/commit")
_WRITE_PATHS = ("/api/live/kill", "/api/live/kill/bulk") + _EDITOR_WRITE_PATHS
#: Editor open and stop connect with the request's connection (stop sends
#: KILL QUERY for the tab's own session from a second connection).
_EDITOR_CONNECTION_PATHS = ("/api/editor/open", "/api/editor/stop")
_CONNECTION_PATHS = (("/api/connection/test", "/api/live/start") + _WRITE_PATHS + _SERVER_VIEW_PATHS
                     + _EDITOR_CONNECTION_PATHS)
#: Server lifecycle (lifecycle.py, answered by _lifecycle_response). Tools, the
#: upgrade check and the dump, restore and clone plans only read: they get the
#: saved password like the views. Start is a write: the connection's policy
#: runs first (dbops.gate with confirmed: true), then the saved password is
#: filled, and every attempt is audited. Status and cancel act on a job the
#: server holds (cancel's KILL QUERY reuses the job's own connection).
_LIFECYCLE_READ_PATHS = ("/api/lifecycle/tools", "/api/lifecycle/upgrade-check", "/api/lifecycle/prepare")
_LIFECYCLE_WRITE_PATHS = ("/api/lifecycle/start",)
_LIFECYCLE_PATHS = _LIFECYCLE_READ_PATHS + _LIFECYCLE_WRITE_PATHS + ("/api/lifecycle/status",
                                                                     "/api/lifecycle/cancel")
#: The jobs of one connection (answered by _lifecycle_jobs_response): opens no
#: database connection, so it is neither a read nor a write path.
_LIFECYCLE_JOB_LIST_PATHS = ("/api/lifecycle/jobs",)
_LIFECYCLE_PATHS = _LIFECYCLE_PATHS + _LIFECYCLE_JOB_LIST_PATHS + ("/api/lifecycle/choose-directory",)
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
#: schema, the columns and indexes of one table. Read-only, with the saved
#: password like the views; answered by _catalog_response.
_CATALOG_PATHS = ("/api/live/catalog",)
_TABLE_COLUMNS_PATHS = ("/api/live/table-columns",)
_CONNECTION_PATHS = _CONNECTION_PATHS + _CATALOG_PATHS + _TABLE_COLUMNS_PATHS

from .schema_diagram_api import PATHS as _DIAGRAM_PATHS, READ_PATH as _DIAGRAM_READ_PATH  # noqa: E402
_CONNECTION_PATHS = _CONNECTION_PATHS + (_DIAGRAM_READ_PATH,)
#: Server variables (variables.py, answered by _variable_response). Read and
#: plan only read: they get the saved password like the views. Apply is a
#: write: the connection's policy runs first (dbops.gate with confirmed: true),
#: then the saved password is filled, and every attempt is audited.
_VARIABLE_READ_PATHS = ("/api/live/variable", "/api/live/variable/plan")
_VARIABLE_WRITE_PATHS = ("/api/live/variable/apply",)
_VARIABLE_PATHS = _VARIABLE_READ_PATHS + _VARIABLE_WRITE_PATHS
_WRITE_PATHS = _WRITE_PATHS + _VARIABLE_WRITE_PATHS
_CONNECTION_PATHS = _CONNECTION_PATHS + _VARIABLE_PATHS
#: Privileges and Users and roles (accounts_api.py, answered by
#: _accounts_response). /api/live/privileges and /api/accounts/list, read and
#: plan only read: they get the saved password like the views. Apply is a
#: write: the connection's policy runs first, then the saved password is
#: filled, and every attempt is audited.
from .accounts_api import READ_PATHS as _ACCOUNT_READ_PATHS, WRITE_PATHS as _ACCOUNT_WRITE_PATHS  # noqa: E402
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
                 "/api/live/proxysql/plan": "proxysql", "/api/live/proxysql/action": "proxysql"}
_ACTION_WRITE_PATHS = ("/api/live/cluster/action", "/api/live/proxysql/action")
_ACTION_PATHS = tuple(_ACTION_KINDS)
_WRITE_PATHS = _WRITE_PATHS + _ACTION_WRITE_PATHS
_CONNECTION_PATHS = _CONNECTION_PATHS + _ACTION_PATHS
#: The progress of a send the review sheet is waiting for
#: (reviewed_writes.Progress by the action request's progress_id). No
#: connection, no SQL, no credentials: phases and statement states only.
_ACTION_PROGRESS_PATH = "/api/live/action/progress"
_ACTION_PROGRESS_LOCK = threading.Lock()


def _action_progress(workspace):
    from .reviewed_writes import ProgressRegistry
    with _ACTION_PROGRESS_LOCK:
        registry = getattr(workspace, "action_progress", None)
        if registry is None:
            registry = workspace.action_progress = ProgressRegistry()
        return registry

#: Application updates (app_update.py). Check reads GitHub only when the
#: workspace asks (the Check for updates button) and keeps the outcome in the
#: workspace store; status reads that stored outcome and never the network.
_APP_UPDATE_PATHS = ('/api/app/update/check', '/api/app/update/download', '/api/app/update/status')
# Home news (news.py): fixed official feeds, read only while Home is shown
# and the Settings switch is on. No connection, no password.
_NEWS_PATHS = ('/api/news', '/api/news/settings')


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


def _catalog_response(payload):
    """catalog-1.0 for the schema browser; the password never reaches an error."""
    from .live import connection_options
    from .connector import MySQLConnection
    from .schema_browser import catalog_report, check_table
    prefix, after = check_table(payload.get("prefix")), check_table(payload.get("after"))
    options = connection_options(payload.get("connection"))
    options["query_timeout"] = 30
    try:
        with MySQLConnection(**options) as conn:
            return catalog_report(conn, payload.get("schema"), payload.get("table"),
                                  prefix=prefix, after=after, stats=payload.get("stats") is True)
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, options.get("password"))


def _variable_policy(name, mode, connection, store):
    """The connection's safety and write policy for one variable change,
    judged on a statement of the same kind (no connection is opened)."""
    from . import dbops, variables
    safety = dbops.connection_safety(_request_profile(connection, store), connection)
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
        with MySQLConnection(**options) as conn:
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
    from . import dbops, variables
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
                             connection={k: plain.get(k) for k in ("host", "port", "user")}, safety=safety,
                             password=plain.get("password") if isinstance(plain.get("password"), str) else None,
                             policy=dbops.policy_summary(decision))
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
        audit.connection = {k: options.get(k) for k in ("host", "port", "user")}
        with MySQLConnection(**options) as conn:
            return variables.apply_change(conn, request, expect, decision=decision, confirmed=True, audit=audit)
    except (ValueError, ConnectorError) as exc:
        audit.fail(exc, [variables.TAG + expect["statement"]])
        _reraise_redacted(exc, password)


_ACTION_OPERATION = {"cluster": "cluster_action", "proxysql": "proxysql_action"}


def _action_module(kind):
    if kind == "cluster":
        from . import cluster as module
    else:
        from . import proxysql as module
    return module


def _action_plan(kind, module, conn, payload):
    """A plan from a fresh read of *conn* for the request's inputs."""
    if kind == "cluster":
        report = module.read_status(conn)
        action = payload.get("action")
        return module.plan_action(report, action, payload.get("member_key"), module.action_checks(conn, report, action))
    report = module.read_status(conn)
    return module.plan_action(report, payload.get("action"), payload.get("target"), payload.get("value"))


def _action_response(path, payload, workspace):
    """Plan (read, review token) and Action (the write) for the Cluster and
    ProxySQL places. Action refuses before opening any connection on a
    read-only connection or without confirmation; a missing, expired or
    changed review is refused too. Refusals and every send are audited
    (dbops.AuditTrail); the envelope is cluster-action-1.0 or
    proxysql-action-1.0 (reviewed_writes.py)."""
    from . import dbops, reviewed_writes
    from .live import connection_options
    from .connector import MySQLConnection
    kind = _ACTION_KINDS[path]
    module = _action_module(kind)
    connection = payload.get("connection")
    plain = connection if isinstance(connection, dict) else {}
    safety = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
    identity = dbops.connection_identity(plain, safety)
    if path.endswith("/plan"):
        options = connection_options(connection)
        try:
            with MySQLConnection(**options) as conn:
                plan = _action_plan(kind, module, conn, payload)
        except (ValueError, ConnectorError) as exc:
            _reraise_redacted(exc, options.get("password"))
        decision = dbops.decide(reviewed_writes.worst_kind(plan["statements"]), safety["type"], safety["read_only"],
                                safety.get("profile_name"))
        plan["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text", "connection_type",
                                                       "read_only")}
        plan["review_token"] = _review_tokens(workspace).issue(_ACTION_OPERATION[kind], identity,
                                                               reviewed_writes.review_details(plan))
        return plan
    reviewed = reviewed_writes.check_reviewed(payload.get("plan"))
    statements = list(reviewed["statements"])
    progress = _action_progress(workspace).start(payload.get("progress_id"), len(statements), module.ACTION_TIMEOUT)
    decision = dbops.decide(reviewed_writes.worst_kind(statements), safety["type"], safety["read_only"],
                            safety.get("profile_name"))
    audit = dbops.AuditTrail(workspace.store, _ACTION_OPERATION[kind], "ui",
                             connection={k: plain.get(k) for k in ("host", "port", "user")}, safety=safety,
                             password=plain.get("password") if isinstance(plain.get("password"), str) else None,
                             policy=dbops.policy_summary(decision))
    out = reviewed_writes.result(module.ACTION_SCHEMA_VERSION, reviewed)
    out["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text")}
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
        now = dbops.connection_safety(_request_profile(connection, workspace.store), connection)
        if dbops.connection_identity(plain, now) != identity or dbops.gate(dbops.decide(
                reviewed_writes.worst_kind(statements), now["type"], now["read_only"], now.get("profile_name")),
                confirmed=True):
            raise reviewed_writes.Refusal("The connection's write policy changed after the review, so nothing was "
                                          "sent. Review the change again.", "policy_changed")
        consume()
    password = audit.password
    try:
        filled = _with_saved_password(payload, workspace)
        options = connection_options(filled.get("connection"))
        password = audit.password = options.get("password")
        audit.connection = {k: options.get(k) for k in ("host", "port", "user")}
        with MySQLConnection(**options) as conn:
            result = module.execute_action(conn, reviewed, audit=audit, typed=typed, before_send=before_send,
                                           progress=progress)
        result["policy"] = out["policy"]
        return result
    except KeyboardInterrupt as exc:
        if progress is not None:
            progress.fail()
        audit.fail(exc, statements)
        raise
    except (ValueError, ConnectorError) as exc:
        if progress is not None:
            progress.fail()
        audit.fail(exc, statements)
        _reraise_redacted(exc, password)


def _with_saved_password(payload, workspace):
    connection = payload.get("connection")
    if not isinstance(connection, dict) or not connection.get("profile_id"):
        return payload
    filled = workspace.store.connection_with_saved_password(connection)
    return dict(payload, connection=filled)


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


def api_response(path, payload, workspace=None):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON request object.")
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
                "last_check": app_update.stored_last_check(stored)}
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
    if path in _DIAGRAM_PATHS:
        from . import schema_diagram_api
        fill = (lambda value: _with_saved_password(value, workspace)) if workspace is not None else None
        return schema_diagram_api.response(path, payload, fill)
    if path in _ACCOUNT_PATHS:
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        return _accounts_response(path, payload, workspace)
    if path.startswith(("/api/investigations/", "/api/profiles/", "/api/live/", "/api/connection/")):
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        if path.startswith(("/api/investigations/", "/api/profiles/")):
            kind = "investigation" if path.startswith("/api/investigations/") else "profile"
            operation = path.rsplit("/", 1)[-1]
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
        if path in _CONNECTION_PATHS and path not in _WRITE_PATHS:
            payload = _with_saved_password(payload, workspace)
        if path == "/api/live/start": return workspace.jobs.start(payload)
        if path in ("/api/live/status", "/api/live/cancel"):
            from . import dbops
            connection = payload.get("connection")
            identity = (dbops.identity_endpoint(dbops.connection_identity(connection))
                        if isinstance(connection, dict) else None)
            job = workspace.jobs.get(payload.get("id"), identity)
            if path == "/api/live/cancel":
                job.cancel()
            return job.snapshot()
        if path == "/api/connection/test":
            from .live import connection_options
            from .connector import MySQLConnection
            options = connection_options(payload.get("connection"))
            try:
                with MySQLConnection(**options) as conn:
                    from .server_info import product_identity
                    version = conn.server_version()
                    comment = "" if conn.is_mariadb() else conn.server_version_comment()
                    identity = product_identity(version, comment)
                    return {"version": version, "engine": identity["engine"], "flavor": identity["flavor"],
                            "product": identity["product"],
                            "schemas": [row[0] for row in conn.query_rows("SHOW DATABASES") if row]}
            except (ValueError, ConnectorError) as exc:
                _reraise_redacted(exc, options.get("password"))
        if path == "/api/live/kill/prepare":
            return _kill_prepare(payload, workspace)
        if path == "/api/live/kill":
            return _kill_response(payload, workspace)
        if path == "/api/live/kill/bulk/prepare":
            return _bulk_kill_prepare(payload, workspace)
        if path == "/api/live/kill/bulk":
            return _bulk_kill_response(payload, workspace)
        if path == "/api/live/table-columns":
            return _table_columns_response(payload)
        if path in _REPLICATION_PATHS:
            return _replication_response(path, payload, workspace)
        if path in _CATALOG_PATHS:
            return _catalog_response(payload)
        if path in _VARIABLE_PATHS:
            return _variable_response(path, payload, workspace)
        if path in _ACTION_PATHS:
            return _action_response(path, payload, workspace)
        if path == _ACTION_PROGRESS_PATH:
            return _action_progress(workspace).get(payload.get("progress_id"))
        if path in _SERVER_VIEW_PATHS:
            return _discover_response(path, payload)
        raise ValueError("Unknown workspace operation.")
    if path.startswith("/api/teach/"):
        from .teach import (LESSONS, CURRICULUM, LESSON_FAMILIES, _FAMILY_LABELS, lesson_views,
                            render_lesson, render_lesson_3d)
        if path == "/api/teach/catalog":
            track = [key for key in CURRICULUM if key in LESSONS]
            keys = track + [key for key in LESSONS if key not in track]
            return {
                "curriculum": track,
                "families": [{"key": key, "label": _FAMILY_LABELS[key]}
                             for key in LESSON_FAMILIES],
                "lessons": [{"key": key, "title": LESSONS[key]["title"],
                             "summary": LESSONS[key]["summary"],
                             "family": LESSONS[key]["family"],
                             "views": lesson_views(key)} for key in keys],
            }
        if path == "/api/teach/lesson":
            key = payload.get("lesson")
            if not isinstance(key, str) or key not in LESSONS:
                raise ValueError("Choose a lesson from the Teach catalog.")
            view = payload.get("view", "2d")
            if view not in lesson_views(key):
                raise ValueError("This lesson has no {} view.".format(view if isinstance(view, str) else "such"))
            return {"html": render_lesson_3d(key) if view == "3d" else render_lesson(key)}
        raise ValueError("Unknown Teach endpoint.")
    if path == "/api/report/export":
        return _export_response(payload)
    if path == "/api/compare":
        before, _, _, _ = _plan(payload, "before")
        after, _, _, _ = _plan(payload, "after")
        return {"analysis": build_compare_sidecar(before, after),
                "html": render_compare(before, after)}
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
        return {"html": render_html_report(text, view_type=view, query_text=query)}
    if path == "/api/render":
        unit = "\u00b5s" if 0 < root["total_time"] < 1 else "ms"
        from .exploration import render_exploration
        return {"html": render_exploration(root, view, width=1200,
                    selected_id=payload.get("selected_id", ""), focus_id=payload.get("focus_id", ""),
                    collapsed_ids=payload.get("collapsed_ids"), metric=payload.get("metric", "self_time"))}

    raise ValueError("Unknown API endpoint.")


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True
    MAX_REQUEST_WORKERS = 32

    def __init__(self, port=0, store_path=None, no_restore=False, tool_groups_file=None, news_reader=None):
        from .workspace_store import WorkspaceStore
        from .live import LiveJobs
        self.store = WorkspaceStore(store_path)
        # --no-restore: start clean for this launch and leave the stored
        # session untouched (no restore, no autosave).
        self.no_restore = bool(no_restore)
        # Home news reads the network only through this reader: cmd_ui passes
        # news.fetch_feed; servers built without one (tests) never read feeds.
        self.news_reader = news_reader
        self.jobs = LiveJobs()
        from .editor_session import EditorSessions
        # One mysql/mariadb client process per Query editor tab; all are
        # ended when the server closes.
        self.editor = EditorSessions()
        from .lifecycle import LifecycleJobs
        # Dump, restore and clone jobs; their tools are stopped first on close.
        # tool_groups_file: the desktop app's record of tool process groups.
        self.lifecycle = LifecycleJobs(tool_groups_file)
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
        super().__init__(("127.0.0.1", port), WorkspaceHandler)
        self.origin = "http://127.0.0.1:%s" % self.server_address[1]
        self.launch_url = self.origin + "/#launch=" + self.bootstrap_secret


    def process_request(self, request, client_address):
        # ThreadingHTTPServer otherwise creates an unbounded number of threads
        # before the handler can reject an unauthenticated slow client.
        if not self._worker_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def server_close(self):
        # Lifecycle tools (dump, restore, clone) run in their own process
        # groups, beyond a supervisor's signal to this process group. Stop them
        # first and boundedly, before cleanup that can block on the server
        # (KILL QUERY in editor sessions, replication loops).
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is not None:
            lifecycle.shutdown()
        registry = getattr(self, "replication_loops", None)
        if registry is not None:
            registry.stop_all(wait=True)
        table_data = getattr(self, "table_data", None)
        if table_data is not None:
            table_data.close()
        self.jobs.close()
        self.editor.close()
        self.lifecycle.close()
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


def _unexpected_error_message(path):
    """The neutral 500 text for *path*. The response never carries the cause."""
    if path in _PLAN_PATHS:
        return "Could not render this plan. Try another view or the CLI."
    if path.startswith("/api/live/"):
        return "Could not read this from the server. Try Refresh or the CLI."
    return "Could not complete the request. Try again or use the CLI."


def _secret_values(value, store=None, found=None, depth=0):
    """Every non-empty string stored under a key that names a password (any
    depth), and the saved password of each connection that names a profile
    and sends none, so a traceback never carries one."""
    found = set() if found is None else found
    if depth > 12:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and "password" in key.lower() and isinstance(item, str) and item:
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
    elif isinstance(value, (list, tuple)):
        for item in value:
            _secret_values(item, store, found, depth + 1)
    return found


def _log_unexpected_error(path, exc_info, payload=None, store=None):
    """Write the traceback of an unexpected error to stderr, never to the
    response, with the request's passwords (typed or saved) removed."""
    import traceback
    try:
        text = "".join(traceback.format_exception(*exc_info))
        variants = set()
        for secret in _secret_values(payload, store):
            # Also as it reads inside a repr or JSON text of the exception.
            variants.update((secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]))
        for secret in sorted(variants, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
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
            index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
            # About myflames shows the installed package version.
            index = index.replace("__MYFLAMES_VERSION__", html.escape(__version__))
            index = index.replace("</head>", _LAUNCH_REFUSAL_STYLE + "</head>")
            index = index.replace('<div id="root"></div>', '<div id="root">' + _LAUNCH_REFUSAL_HTML + '</div>')
            self._send(200, index, "text/html; charset=utf-8")
            return
        # Only flat bundled assets are exposed, never arbitrary local files.
        name = path[len("/assets/"):] if path.startswith("/assets/") else ""
        # Earlier UI bundles referenced content-hashed logos. Keep those image
        # requests working for tabs open across the switch to a stable address.
        if name in ("myflames-BpuTIrWf.jpeg", "logo-BpuTIrWf.jpeg",
                    "myflames-icon-v2-CFba5YPm.png"):
            name = "myflames-logo.png"
        if name and "/" not in name and "\\" not in name and name not in (".", ".."):
            asset = ASSET_DIR / "assets" / name
            types = {".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml"}
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
                             "/api/profiles/list", "/api/profiles/save", "/api/profiles/delete",
                             "/api/profiles/forget-password",
                             "/api/live/start", "/api/live/status", "/api/live/cancel", "/api/connection/test",
                             "/api/live/top", "/api/live/slow-queries", "/api/live/running", "/api/live/sessions",
                             "/api/live/locks", "/api/live/innodb", "/api/live/error-log", "/api/live/cluster", "/api/live/proxysql",
                             "/api/live/kill", "/api/live/kill/prepare",
                             "/api/live/kill/bulk", "/api/live/kill/bulk/prepare",
                             "/api/live/table-columns",
                             "/api/live/health", "/api/live/waits", "/api/live/indexes",
                             "/api/live/tables", "/api/live/schemas",
                             "/api/live/io", "/api/live/config", "/api/live/instruments",
                             "/api/live/report", "/api/report/export",
                             "/api/live/explain-connection",
                             "/api/session/load", "/api/session/save", "/api/session/clear",
                             "/api/session/settings",
                             "/api/audit/list", "/api/audit/export", "/api/audit/settings",
                             "/api/audit/declined",
                             "/api/table-data/open", "/api/table-data/prepare", "/api/table-data/apply",
                             "/api/table-data/decline", "/api/table-data/status", "/api/table-data/cancel", "/api/table-data/close",
                             "/api/editor/open", "/api/editor/run", "/api/editor/status", "/api/editor/preview",
                             "/api/editor/stop", "/api/editor/commit", "/api/editor/rollback",
                             "/api/editor/close", "/api/editor/history", "/api/editor/export", "/api/analysis/prepare-query") + _LIFECYCLE_PATHS \
                + _REPLICATION_PATHS + _CATALOG_PATHS + _VARIABLE_PATHS + _ACTION_PATHS + _ACCOUNT_PATHS + _DIAGRAM_PATHS + _APP_UPDATE_PATHS \
                + _NEWS_PATHS + (_ACTION_PROGRESS_PATH, "/api/live/proxysql/digests"):
            self._send(404, {"error": "Not found."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Send application/json."})
            return
        from .workspace_store import SessionConflict, SessionTooLarge
        from .editor_session import EditorError
        session = self.path == "/api/session/save"
        payload = None
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= (MAX_SESSION_BODY if session else MAX_BODY) or self.headers.get("Transfer-Encoding"):
                self._send(413, {"error": "The workspace is larger than 64 MiB. Remove some plans so it can be saved."
                                 if session and size > 0 else "Request must be between 1 byte and 12 MiB."})
                return
            payload = json.loads(self._read_body(size).decode("utf-8"))
            result = api_response(self.path, payload, self.server)
            self._send(200, result)
        except SessionConflict as exc:
            self._send(409, {"error": str(exc), "code": "session_conflict", "revision": exc.revision})
        except SessionTooLarge as exc:
            self._send(413, {"error": str(exc), "code": "session_too_large"})
        except EditorError as exc:
            # busy, identity_mismatch, too_many_sessions, no_session, ...
            self._send(409 if exc.code in ("busy", "identity_mismatch", "too_many_sessions") else 400,
                       {"error": str(exc), "code": exc.code})
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
            self._send(400, {"error": "Could not complete the request: %s" % exc})
        except ConnectorError as exc:
            self._send(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
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
        parser.exit(2, "UI assets are missing. Reinstall myflames or build apps/workspace.\n")
    try:
        from .news import fetch_feed  # Home news: the only server that reads feeds
        # Set only by the desktop app: a file in its private runtime directory.
        server = WorkspaceServer(args.port, no_restore=args.no_restore,
                                 tool_groups_file=os.environ.get("MYFLAMES_TOOL_GROUPS_FILE") or None,
                                 news_reader=fetch_feed)
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
