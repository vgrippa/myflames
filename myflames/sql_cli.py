"""Run a CLI script through the Query editor's planner and session engine."""
import argparse
import io
import json
import math
import sqlite3
import sys
from decimal import Decimal

from . import dbops, editor_session as editor
from .connector import ConnectorError, terminal_text
from .live import connection_options
from .workspace_cli import (_add_connection_args, _bounded_integer,
                            _connection_settings, _resolve_connection, _table)
from .workspace_store import PROFILE_FIELDS, WorkspaceStore


def _timeout(value):
    try:
        seconds = float(value)
    except ValueError:
        seconds = float("nan")
    if not math.isfinite(seconds) or not 0 <= seconds <= 86400:
        raise argparse.ArgumentTypeError("Choose seconds from 0 to 86400 (0 disables the time limit).")
    return seconds


def _input(args):
    if args.execute is not None:
        text = args.execute
    elif args.file is not None and args.file != "-":
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read(editor.MAX_SQL + 1)
    else:
        if sys.stdin.isatty():
            raise ValueError("Pass -e SQL, -f FILE, or pipe SQL on stdin.")
        text = sys.stdin.read(editor.MAX_SQL + 1)
    return {"sql": text, "scope": "all"}


def _note(message, password=None):
    # SQL and server errors can contain both secrets and terminal controls.
    sys.stderr.write("myflames: %s\n" % terminal_text(dbops.redact(message, password)))


def _envelope(status, run=None, message=None, audit_id=None):
    run = run or {}
    return {"schema_version": "sql-1.0", "status": status,
            "results": run.get("results", []), "counts": run.get("counts"),
            "error": run.get("error"), "stopped": run.get("stopped"),
            "message": message or run.get("message"),
            "audit_id": audit_id or run.get("audit_id")}


def _numeric_kind(column, value):
    """Trust column metadata, never the appearance of an untyped value."""
    kind = (column.get("type") or "").lower().split("(")[0].split(" ")[0]
    if column.get("binary") or kind not in editor.NUMERIC_TYPES or value is None:
        return None
    return kind if isinstance(value, str) and editor._NUMBER.fullmatch(value) else None


def _json_value(column, value):
    kind = _numeric_kind(column, value)
    # Preserve exact decimal and BIGINT text, as in editor exports. An
    # unexpected oversized integer also must not lose precision in clients.
    if kind in ("tinyint", "smallint", "mediumint", "int", "integer", "year"):
        try:
            number = int(value)
            return number if abs(number) <= (1 << 53) - 1 else value
        except ValueError:
            return value
    if kind in ("float", "double", "real"):
        number = float(value)
        if not math.isfinite(number) or (number == 0 and Decimal(value) != 0):
            return value
        return number
    return value


def _emit(body, fmt, quiet, password, output=None, insert_table="result"):
    from .cli import _write_output
    data = io.StringIO()
    body = dbops.redact_value(body, password)
    if fmt == "json":
        # Build a projection; exporting must not change the stored result.
        body = dict(body, results=[dict(result, rows=[
            [_json_value(column, value) for column, value in zip(result["columns"], row)]
            for row in result["rows"]]) if result["type"] == "rows" else result
            for result in body["results"]])
        data.write(json.dumps(body, ensure_ascii=False, allow_nan=False) + "\n")
    for result in body["results"]:
        kind = result["type"]
        if kind == "rows":
            if fmt == "table":
                # Quote text so SQL NULL, empty text, and the text 'NULL' differ.
                columns = [terminal_text(c["name"]) for c in result["columns"]]
                rows = [["NULL" if v is None else v if _numeric_kind(c, v) else json.dumps(v, ensure_ascii=False)
                         for c, v in zip(result["columns"], row)] for row in result["rows"]]
                data.write(_table(columns, rows) if columns else "")
            elif fmt != "json":
                # The user names the target; result remains the documented
                # default for result sets without a source table.
                data.write(editor.export_result("sql" if fmt == "insert" else fmt, result,
                                                      table=insert_table if fmt == "insert" else None)["content"])
            if (result.get("truncated") or result.get("warnings")) and not quiet:
                # label describes a stopped or truncated result; otherwise count the rows.
                _note("Statement %d: %s; %d warning(s)." % (
                    result["statement"] + 1, result.get("label") or "%d row(s)" % len(result["rows"]),
                    result.get("warnings") or 0))
        elif kind == "ok" and not quiet:
            _note("Statement %d: %s row(s) affected; %s warning(s).%s" % (
                result["statement"] + 1, result.get("affected_rows") or 0,
                result.get("warnings") or 0, " " + result["info"] if result.get("info") else ""))
        elif kind == "error" and not result.get("interrupted"):
            pos = result.get("position") or {}
            _note("Statement %d, line %s, column %s: ERROR %s (%s): %s" % (
                result["statement"] + 1, pos.get("line", "?"), pos.get("column", "?"),
                result.get("code"), result.get("sqlstate"), result.get("message")))
    _write_output(data.getvalue(), output, quiet=quiet)
    if body.get("message"):
        _note(body["message"])
    if body.get("stopped"):
        _note(body["stopped"]["message"])


def _authorize(plan, safety, options, args, registry, audit, session=None, review=None):
    """Use editor tokens and declines, including an unsent audit on refusal."""
    decision = editor._decision(plan, safety, session)
    if review is not None:
        decision["alter_preflight"] = review
        decision["alter_binding"] = editor._alter_binding(session, review)
        decision["warnings"] += review.get("warnings", [])
    audit.policy = dbops.policy_summary(decision)
    statements = [u["send"] for u in plan["units"]]
    writes = [u["send"] for u in plan["units"] if u["kind"] != "read"]
    digest = editor._review_digest("cli", dbops.connection_identity(options, safety), statements, decision)
    name = safety.get("profile_name") or "%s:%s" % (options.get("host"), options.get("port"))
    payload = {}
    decline_message = None
    while True:
        # A real refusal (for example an expired token) finishes its audit.
        # A subsequent answer needs a fresh trail for start_run/_finish;
        # AuditTrail is intentionally single-use.
        if audit.finished:
            audit = dbops.AuditTrail(audit.store, audit.operation, audit.surface,
                                     connection=audit.connection, safety=audit.safety,
                                     password=audit.password, policy=dbops.policy_summary(decision))
        if decision["action"] == "confirm" and not payload:
            # Preparing a confirmation is not a refusal. The CLI owns the
            # prompt; pass only its actual answer to the editor's token gate.
            token = registry.issue_token(digest)
            blocked = {"status": "needs_confirmation",
                       "confirmation": editor._confirmation(decision, token, statements, name, options)}
        else:
            blocked = editor._gate(registry, decision, payload, digest, audit, statements, writes, name, options)
        if blocked is None:
            if writes:
                problem = dbops.cli_confirm(None, '', assume_yes=args.yes, ask=False)
                if problem:
                    audit_id = audit.outcome(statements, problem[0], outcome=problem[1], message=problem[2])
                    return {"status": problem[0], "message": problem[2], "audit_id": audit_id}, decision, audit
            return None, decision, audit
        if blocked["status"] != "needs_confirmation":
            if decline_message:
                blocked["message"] = decline_message
            return blocked, decision, audit
        confirmation = blocked["confirmation"]
        lines = ["myflames: %s [%s:%s, database %s] (%s) as %s: %s\n%s" % (
            name, options.get("host"), options.get("port"), options.get("database") or "none",
            safety["type"], options.get("user") or "default user", confirmation["text"], "\n".join(statements))]
        lines.extend("myflames: " + warning for warning in confirmation.get("warnings") or [])
        if review is not None:
            lines.append("myflames: ALTER preflight: " + json.dumps(review, ensure_ascii=False))
        expected = confirmation.get("typed_target")
        prompt = "Type the database name %s to drop it: " % expected if expected is not None else "Run these statements? [y/N] "
        problem = dbops.cli_confirm("\n".join(lines), prompt,
            expected=expected, supplied=args.confirm_target,
            mismatch_message="DROP requires the exact database name; use --confirm-target NAME with --yes. Nothing was sent.",
            assume_yes=args.yes, quiet=args.quiet, passwords=(audit.password,),
            no_terminal_message="Writes require --yes when stdin is not a terminal; nothing was sent.")
        if problem and problem[1] == "no_terminal":
            audit_id = audit.outcome(statements, problem[0], outcome=problem[1], message=problem[2])
            return {"status": problem[0], "message": problem[2], "audit_id": audit_id}, decision, audit
        if problem and problem[0] == "refused":
            # A missing/mismatched --confirm-target is a refusal, not a user
            # decline. Keep that distinction in the shared CLI audit contract.
            audit = dbops.AuditTrail(audit.store, audit.operation, audit.surface,
                                     connection=audit.connection, safety=audit.safety,
                                     password=audit.password, policy=dbops.policy_summary(decision))
            audit_id = audit.outcome(statements, problem[0], outcome=problem[1], message=problem[2])
            return {"status": problem[0], "message": problem[2], "audit_id": audit_id}, decision, audit
        accepted = problem is None
        decline_message = problem[2] if problem else None
        # A fresh gate call consumes the exact editor token; an expired token
        # issues a new confirmation instead of authorizing another script.
        payload = {"confirmed": accepted, "confirm_token": confirmation["token"], "decline": not accepted,
                   "typed_target": expected if accepted else None}


def _run(args, payload):
    plan = editor._plan_run(payload, None)
    writes = [u["send"] for u in plan["units"] if u["kind"] != "read"]
    store = WorkspaceStore()
    profile = store.find_profile(args.profile) if args.profile else None
    planned = {f: profile[f] for f in PROFILE_FIELDS if profile and profile.get(f) not in (None, "")}
    planned.update(_connection_settings(args, None))
    options = connection_options(planned)
    safety = dbops.connection_safety(profile, planned)
    password = None if args.password == "__PROMPT__" else args.password
    audit = dbops.AuditTrail(store, "sql", "cli", connection={k: options.get(k) for k in ("host", "port", "user")},
                             safety=safety, password=password)
    registry = editor.EditorSessions(reap_every=0)
    session = None
    run = None
    try:
        decision = editor._decision(plan, safety)
        alter = editor._alter_unit(plan) is not None and decision["action"] != "refuse"
        if alter:
            # Read-only and script-shape checks precede credentials. An ALTER
            # review reads metadata before asking, without executing its guard.
            problem = dbops.cli_confirm(None, "", assume_yes=args.yes, ask=False)
            blocked = None
            if problem:
                audit_id = audit.outcome(writes, problem[0], outcome=problem[1], message=problem[2])
                blocked = {"status": problem[0], "message": problem[2], "audit_id": audit_id}
        else:
            blocked, decision, audit = _authorize(plan, safety, options, args, registry, audit)
        if blocked:
            return 1, _envelope(blocked["status"], message=blocked["message"], audit_id=blocked.get("audit_id")), password
        full = connection_options(_resolve_connection(args))
        password = full.get("password")
        audit.password = password
        # Password resolution re-reads the profile. A concurrent profile edit
        # must not send the confirmed script to another endpoint or database,
        # or let a newly read-only connection keep its old policy.
        current_profile = store.get("profile", profile["id"]) if profile else None
        if ({k: v for k, v in full.items() if k != "password"} != options
                or dbops.connection_safety(current_profile, full) != safety):
            message = "The connection settings changed after review; nothing was sent. Run the command again."
            audit_id = audit.outcome(writes, "refused", outcome="connection_changed", message=message) if writes else None
            return 1, _envelope("refused", message=message, audit_id=audit_id), password
        session = editor.EditorSession("cli", dbops.connection_identity(options, safety), full,
                                       safety["type"], safety["read_only"])
        session.start(full)
        again = editor._plan_run(payload, session)
        writes = [u["send"] for u in again["units"] if u["kind"] != "read"]
        new_decision = editor._decision(again, safety, session)
        review = editor._alter_review(again, session) if alter else None
        if (alter or [u["send"] for u in again["units"]] != [u["send"] for u in plan["units"]]
                or any(new_decision[k] != decision[k] for k in ("action", "kind"))):
            blocked, decision, audit = _authorize(again, safety, options, args, registry, audit, session, review)
            if blocked:
                return 1, _envelope(blocked["status"], message=blocked["message"],
                                    audit_id=blocked.get("audit_id")), password
        run = editor.Run(again["units"], args.limit, full, again["text"], again["offsets"], again["scope"],
                         audit=audit if writes else None,
                         timeout=args.timeout or None, alter_context=review)
        current_profile = store.get("profile", profile["id"]) if profile else None
        if dbops.connection_safety(current_profile, full) != safety:
            message = "The connection policy changed after review; nothing was sent. Run the command again."
            audit_id = audit.outcome(writes, "refused", outcome="connection_changed", message=message) if writes else None
            return 1, _envelope("refused", message=message, audit_id=audit_id), password
        session.start_run(run)
        while not run.done.wait(0.2):
            pass
        snapshot = session.snapshot(run)
        code = 0 if snapshot["state"] == "complete" else (2 if snapshot["state"] == "session_ended" else 1)
        if not args.quiet and session.in_transaction is not False:
            _note("Closing the session rolls back any open transaction; include COMMIT in the script to keep it.")
        return code, _envelope(snapshot["state"], snapshot), password
    except KeyboardInterrupt as exc:
        if session is not None:
            session.end(options=full)
        if run is not None and session.run is run:
            run.done.wait(5)
        if writes:
            if run is None and not audit.pending:
                audit.outcome(writes, "declined", outcome="declined", message="Cancelled before sending.")
            else:
                audit.fail(exc, writes)
        return 1, _envelope("stopped", message="Cancelled; check the audit trail for the write outcome.",
                            audit_id=audit.id), password
    except (ValueError, TypeError, OSError, ConnectorError, sqlite3.Error, EOFError) as exc:
        if writes:
            if isinstance(exc, EOFError) and run is None and not audit.pending:
                audit.outcome(writes, "declined", outcome="declined", message="Password prompt cancelled.")
            else:
                audit.fail(exc, writes)
        code = 1 if isinstance(exc, editor.EditorError) and exc.code == "audit_failed" else 2
        return code, _envelope("error", message=dbops.redact(str(exc), password), audit_id=audit.id), password
    finally:
        if session is not None:
            session.end()
            if run is not None and session.run is run:
                run.done.wait(5)
        registry.close()


#: Results come from the client's --xml output, which has no column types
#: (mysql and mariadb print --column-type-info only in table mode), and a
#: second metadata query per statement is not acceptable.
TYPES_NOTE = ("Column types: results carry no column types (the mysql and mariadb clients print none with --xml), "
              "so every value arrives as text. json keeps numbers as strings, table quotes them, and insert writes "
              "numbers and hex-shown binary values as quoted text. NULL stays NULL in every format.")


def cmd_sql(argv):
    parser = argparse.ArgumentParser(prog="myflames sql", add_help=False,
                                     description="Run SQL in one Query editor session; stop at the first error.",
                                     epilog=TYPES_NOTE)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("-e", "--execute", metavar="SQL", help="Run this SQL script.")
    source.add_argument("-f", "--file", metavar="FILE", help="Read a UTF-8 SQL script (use - for stdin).")
    _add_connection_args(parser)
    parser.add_argument("--format", choices=("table", "csv", "json", "tsv", "markdown", "insert"), default="table",
                        help="Choose the result format (default: table; insert uses --insert-table).")
    parser.add_argument("--insert-table", default="result", metavar="NAME",
                        help="Name the INSERT export table (default: result; quoted as one identifier).")
    parser.add_argument("-o", "--output", metavar="PATH", help="Write results to PATH instead of stdout.")
    parser.add_argument("--confirm-target", metavar="NAME", help="Type the exact DROP DATABASE/SCHEMA target when using --yes.")
    parser.add_argument("--json", action="store_true", help="Emit the run as machine-readable JSON (overrides --format).")
    parser.add_argument("--limit", type=_bounded_integer(1, editor.MAX_LIMIT), default=editor.DEFAULT_LIMIT,
                        help="Keep at most this many rows per result set (default: 1000).")
    parser.add_argument("--timeout", type=_timeout, default=0, help="Stop the run after this many seconds (default: 0, unlimited).")
    parser.add_argument("--yes", action="store_true", help="Confirm writes; required for a confirming policy with non-terminal stdin.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress status messages; keep results and errors.")
    args = parser.parse_args(argv)
    fmt = "json" if args.json else args.format
    password = None if args.password == "__PROMPT__" else args.password
    try:
        # Validate export-only settings before SQL can make any server changes.
        if fmt == "insert":
            editor.export_result("sql", {"columns": [], "rows": []}, table=args.insert_table)
        code, body, password = _run(args, _input(args))
    except (ValueError, TypeError, OSError, sqlite3.Error, EOFError) as exc:
        code, body = 2, _envelope("error", message=dbops.redact(str(exc), password))
    # An unsent failure is a CLI diagnostic, not a SQL run payload. Keep
    # envelopes for statement errors and partial runs, even with exit 2.
    if code and not body["results"] and not (body.get("counts") or {}).get("run"):
        _note(body.get("message") or "No statements were sent.", password)
        raise SystemExit(2)
    _emit(body, fmt, args.quiet, password, args.output, args.insert_table)
    raise SystemExit(code)
