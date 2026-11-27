# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""The send half of a reviewed write that a place plans from a fresh read
(ProxySQL and Cluster actions; db-operations skill, sections 1 and 8).

A place's planner turns a fresh report plus the request's inputs (action,
target, value) into a plan: the exact statements as they will be sent, the
``expect`` identity those statements depend on (captured at review time), an
optional ``typed_guard`` the person must type, and the text the sheet shows.
The workspace issues a review token bound to that whole plan
(``ui._review_tokens``). At send time :func:`execute` plans again from a
fresh read, refuses with "nothing was sent" when the statements or the
``expect`` identity differ from the reviewed plan, checks the typed guard,
consumes the token (``before_send``), writes the pending audit record, and
sends each statement in one query packet with its reviewed server identity
guard, repeated for every packet: one client invocation on standard input
(``MySQLConnection.run_write``: ``--comments``,
``--binary-mode``, no reconnect; batch mode stops at the first error, so a
later statement never runs after a failed one). It never retries.

Outcomes (``status``): ``done``; ``refused`` (nothing was sent); ``failed``
(nothing changed: the server answered the first statement with an error, or
the client printed an error before it read any statement, when it could not
connect or log in); ``partial`` (the server answered a later statement with
an error: the statements before it ran and the ones after it did not);
``unknown`` (any statement may have run). Only an error the client printed
with its number decides failed or partial. ``ERROR N at line L`` is how the
mysql client in batch mode reports the statement that started on line L
(client/mysql.cc ``put_info``; no line before the first statement is read),
and a server error number (any but the client's 2000 to 2999) at a line is
the server's answer. Everything else is unknown: a time limit, an interrupt,
a client error at a line (2013 lost connection, 2006 gone away), or a client
that ended without printing an error number (killed, crashed). Each one is
audited, an unknown one with outcome ``timeout`` or ``no_reply``.

While a send runs, the sheet that asked for it reads its :class:`Progress`
(``/api/live/action/progress`` with the request's ``progress_id``): the phase
(``received``, ``checking`` while the target is read again, ``sending`` once
the statements are with the server, ``reading`` once it answered, then
``finished``) and each statement's state: ``waiting``, ``sent`` (with the
server, no answer yet: it may run), ``done``, ``failed``, ``not_run`` (after a
failed statement), ``not_sent`` (refused before sending) or ``unknown``. A
statement is ``done`` only after the server answered. Progress holds no SQL
and no credentials, and a missing or reused ``progress_id`` only means no
progress is shown. The result carries the same final ``states``, ``effect``
(what changed: rows of what, before, now, from the read after the send) and,
from the plan, ``checks`` (what to look at when the outcome is unknown) and
``timeout_s``.
"""
import collections
import re
import threading
import time

from .connector import ConnectorError

#: "ERROR 1045 (28000) at line 2: ..." from the mysql client in batch mode:
#: the packet's input start line; SQL packet maps translate it to a statement
#: index before the common error handler. Shell retains its prefix offset.
_AT_LINE = re.compile(r"\bat line (\d+)\b")
_ERROR = re.compile(r"ERROR\s+(\d+)")


PROGRESS_SCHEMA_VERSION = "action-progress-1.0"
PHASES = ("received", "checking", "sending", "reading", "finished")
STATES = ("waiting", "sent", "done", "failed", "not_run", "not_sent", "unknown")
_PROGRESS_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class Progress(object):
    """What one send is doing now (see the module docstring)."""

    def __init__(self, count, timeout=None, idle_timeout=None):
        self._lock = threading.Lock()
        self.phase = "received"
        self.states = ["waiting"] * count
        self.status = None
        self.started = time.time()
        self.sent_at = None
        self.answered_at = None
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.touched = time.monotonic()
        self.output = []

    def emit(self, text, stream="stdout"):
        """Bounded, already-redacted Shell progress for the existing poller."""
        with self._lock:
            self.output.append({"stream": stream, "text": text[:2048]})
            self.output = self.output[-100:]
            self.touched = time.monotonic()

    def set(self, phase, states=None, status=None):
        with self._lock:
            self.phase = phase
            if states is not None:
                self.states = list(states)
            if status is not None:
                self.status = status
            if phase == "sending" and self.sent_at is None:
                self.sent_at = time.time()
            if phase in ("reading", "finished") and self.sent_at is not None and self.answered_at is None:
                self.answered_at = time.time()
            self.touched = time.monotonic()

    def fail(self):
        """The request ended with an error: statements with the server have an
        unknown outcome; statements that never left are not sent."""
        with self._lock:
            if self.phase == "finished":
                return
        sending = self.phase == "sending"
        self.set("finished", ["unknown" if sending else "not_sent"] * len(self.states),
                 "unknown" if sending else "refused")

    def snapshot(self):
        with self._lock:
            snapshot = {"schema_version": PROGRESS_SCHEMA_VERSION, "phase": self.phase, "states": list(self.states),
                        "status": self.status, "elapsed_s": round(time.time() - self.started, 1),
                        "waiting_s": round(time.time() - self.sent_at, 1) if self.sent_at and not self.answered_at
                        else None, "timeout_s": self.timeout, "output": list(self.output)}
            if self.idle_timeout is not None:
                snapshot["idle_timeout_s"] = self.idle_timeout
            return snapshot


class ProgressRegistry(object):
    """The workspace's progress of recent sends, by the client's progress_id
    (at most LIMIT, each kept TTL seconds after its last change)."""

    LIMIT = 64
    TTL = 600

    def __init__(self):
        self._lock = threading.Lock()
        self._items = collections.OrderedDict()

    def _prune(self):
        cutoff = time.monotonic() - self.TTL
        for key in [k for k, v in self._items.items() if v.touched < cutoff]:
            self._items.pop(key, None)
        while len(self._items) >= self.LIMIT:
            self._items.popitem(last=False)

    def start(self, progress_id, count, timeout=None, idle_timeout=None):
        """A new Progress, or None for a missing, invalid or reused id (the
        send goes on; only its progress is not shown)."""
        if not isinstance(progress_id, str) or not _PROGRESS_ID.match(progress_id):
            return None
        with self._lock:
            if progress_id in self._items:
                return None
            self._prune()
            progress = self._items[progress_id] = Progress(count, timeout, idle_timeout)
            return progress

    def get(self, progress_id):
        if not isinstance(progress_id, str) or not _PROGRESS_ID.match(progress_id):
            raise ValueError("Invalid progress_id.")
        with self._lock:
            progress = self._items.get(progress_id)
        if progress is None:
            return {"schema_version": PROGRESS_SCHEMA_VERSION, "phase": "unknown", "states": [], "status": None,
                    "elapsed_s": None, "waiting_s": None, "timeout_s": None}
        return progress.snapshot()


def _set(progress, phase, states=None, status=None):
    if progress is not None:
        progress.set(phase, states, status)


class Refusal(ValueError):
    """A check before sending refused; *outcome* is the audit outcome."""

    def __init__(self, message, outcome="review"):
        ValueError.__init__(self, message)
        self.outcome = outcome


class ActionUnavailable(ValueError):
    """A catalog refusal, already worded for the user (including its fix)."""


def worst_kind(statements, floor="risky"):
    """The verdict dbops.decide judges *statements* by: the worst kind of what
    dbops.classify says about each one and *floor*. ProxySQL Admin commands
    and cluster administration are never lighter than *floor*."""
    from . import dbops
    rank = {"read": 0, "write": 1, "risky": 2}
    worst = floor
    for statement in statements:
        kind = dbops.classify(statement).get("kind")
        if rank.get(kind, 2) > rank[worst]:
            worst = kind
    return {"kind": worst, "category": worst, "reasons": [], "statements": list(statements)}


def failed_line(text):
    """The 1-based input line the client named in its error, or None."""
    match = _AT_LINE.search(text or "")
    return int(match.group(1)) if match else None


def error_number(text):
    match = _ERROR.search(text or "")
    return int(match.group(1)) if match else None


def client_error(code):
    """True for a client library error number (2000 to 2999, include/errmsg.h
    CR_MIN_ERROR and CR_MAX_ERROR in MySQL and MariaDB); every other number
    is the server's (1000 to 1999, and 3000 and up in MySQL and MariaDB)."""
    return code is not None and 2000 <= code <= 2999


def server_message(text):
    """The server's own words from a client error line (after the last ': ')."""
    text = (text or "").strip()
    if "ERROR" in text and ": " in text:
        return text.split(": ", 2)[-1].strip()
    return text


def review_details(plan):
    """What a review token binds: the plan as reviewed, without its token and
    the policy text (the connection identity already carries type and
    read-only, so a policy change voids the token)."""
    # The HTTP plan response uses this same object. Publish the automatically
    # derived prefix for review, and bind it with the rest of the plan. Never
    # accept a caller-supplied guard instead of deriving it from expect.
    guard = session_guard(plan)
    if guard:
        plan["session_guard"] = guard
    return {key: value for key, value in plan.items() if key not in ("review_token", "policy")}


def session_guard(plan):
    """Derive a same-session guard in the sender's own language.

    ProxySQL Admin and Readyset do not expose these server identity variables.
    """
    from .dbops import target_session_guard
    expect = plan.get("expect") or {}
    identity = expect.get("server_identity") or expect.get("server")
    if plan.get("via") == "mysqlsh":
        from .cluster_shell import target_session_guard as shell_guard
        return shell_guard(identity)
    if expect.get("engine") not in (
            "mysql", "percona", "percona_server", "pxc", "mariadb"):
        return []
    if expect.get("kind") in ("galera", "pxc"):
        identity = dict(identity or {})
        identity.setdefault("wsrep_gcomm_uuid", None)
    return target_session_guard(expect["engine"], identity)


def check_reviewed(plan, single_line=False):
    """A reviewed plan sent back by the client, validated for shape before it
    is used: never trusted for anything the token does not cover."""
    if not isinstance(plan, dict):
        raise ValueError("Send the reviewed plan with the action.")
    statements = plan.get("statements")
    if not isinstance(plan.get("action"), str) or not isinstance(statements, list) or not statements \
            or not all(isinstance(s, str) and s.strip() for s in statements) \
            or len(statements) > 10:
        raise ValueError("The reviewed plan is not valid. Review the action again; nothing was sent.")
    if single_line and any("\n" in s or "\r" in s for s in statements):
        raise ValueError("Each reviewed Admin statement must be on one line. Nothing was sent. Review again.")
    if plan.get("via") != "mysqlsh":
        from .dbops import target_statement
        expect = plan.get("expect")
        engine = expect.get("engine") if isinstance(expect, dict) else None
        # SQL identity packets have explicit start-line maps. Admin interfaces
        # retain their one-input-line-per-statement error contract. A caller
        # on such an interface must not inherit SQL's multiline exception.
        admin = engine not in ("mysql", "percona", "percona_server", "pxc", "mariadb")
        for statement in statements:
            if admin and ("\n" in statement or "\r" in statement):
                raise ValueError("Each reviewed Admin statement must be on one line. Nothing was sent. Review again.")
            target_statement(statement)
    return plan


def result(schema_version, plan):
    statements = list(plan.get("statements") or [])
    checks = plan.get("checks")
    return {"schema_version": schema_version, "action": plan.get("action"), "status": None, "message": None,
            "statements": statements, "sent": [], "not_sent": [], "error": None,
            "after": None, "undo": plan.get("undo"), "next": None, "audit_id": None,
            "states": ["waiting"] * len(statements), "effect": None,
            "checks": [c for c in checks if isinstance(c, str)][:8] if isinstance(checks, list) else []}


def refused(out, message, outcome, audit=None, target=None, progress=None):
    out["status"], out["message"] = "refused", message
    out["outcome"] = outcome
    out["not_sent"] = list(out["statements"])
    out["states"] = ["not_sent"] * len(out["statements"])
    _set(progress, "finished", out["states"], "refused")
    if audit is not None:
        out["audit_id"] = audit.outcome(list(out.get("session_guard") or []) + out["statements"], "refused", outcome=outcome, message=message,
                                        target=target)
    return out


def execute(conn, reviewed, plan_fresh, schema_version, audit=None, typed=None, before_send=None, read_after=None,
            timeout=None, partial_text=None, done_text=None, progress=None, effect=None, verify_on_error=False,
            refused_text=None):
    """Send a reviewed plan after re-planning it from a fresh read.

    *plan_fresh* returns the plan for the same inputs from a read made now
    (ValueError when the action no longer applies). *read_after* returns the
    state after a successful send (shown in the result panel; a failure there
    leaves ``after`` None). *partial_text(ran, failed, error)* words a partial
    failure; *done_text(plan, after)* the success message; *effect(plan,
    after)* the rows of what changed (what, before, now). *progress* (a
    :class:`Progress`) follows each step. *refused_text(statement, error)*
    words a statement refusal when no partial change was observed.
    *verify_on_error* permits a state read after a first-statement server
    error; only MySQL Shell plans also read after a reply-less error.
    Returns the ``schema_version``
    envelope (see the module docstring).
    """
    from .dbops import may_have_run, target_guard_failed, shift_guard_error, target_guarded_script, target_not_connected
    from .connection_groups import ProfileReadError
    out = result(schema_version, reviewed)
    expect = reviewed.get("expect")
    _set(progress, "checking")
    try:
        fresh = plan_fresh()
    except ValueError as exc:
        return refused(out, "%s Nothing was sent." % str(exc).rstrip(), "changed", audit, expect, progress)
    if fresh.get("statements") != reviewed.get("statements") or fresh.get("expect") != expect \
            or fresh.get("via") != reviewed.get("via"):
        return refused(out, "The target changed after you reviewed it, so nothing was sent. Review the action "
                            "again.", "changed", audit, expect, progress)
    try:
        out["session_guard"] = session_guard(reviewed)
    except ValueError as exc:
        return refused(out, str(exc), "identity_unavailable", audit, expect, progress)
    statements = out["statements"]
    packet_lines = None
    script = "\n".join(out["session_guard"] + statements)
    if out["session_guard"] and fresh.get("via") != "mysqlsh":
        try:
            script, packet_lines = target_guarded_script(out["session_guard"], statements)
        except ValueError as exc:
            return refused(out, str(exc), "review", audit, expect, progress)
    guard = fresh.get("typed_guard")
    if guard and typed != guard:
        return refused(out, "Type %s exactly to confirm. Nothing was sent." % guard, "typed_guard", audit, expect,
                       progress)
    if before_send is not None:
        try:
            before_send()
        except ValueError as exc:
            return refused(out, str(exc), getattr(exc, "outcome", "review"), audit, expect, progress)
    if audit is not None:
        try:
            out["audit_id"] = audit.attempt(out["session_guard"] + statements, target=expect, verified=fresh.get("verified"))
        except Exception as exc:
            _set(progress, "finished", ["not_sent"] * len(statements), "refused")
            raise ValueError("Could not write the audit record (%s), so nothing was sent." % exc)
    _set(progress, "sending", ["sent"] * len(statements))
    try:
        conn.run_write(script, timeout=timeout)
    except KeyboardInterrupt as exc:
        _set(progress, "finished", ["unknown"] * len(statements), "unknown")
        if audit is not None:
            audit.fail(exc, statements)
        raise
    except (Refusal, ProfileReadError) as exc:
        # Login consent can change after the pending record is durable. No
        # subprocess ran: finish that same record rather than logging an error
        # or leaving a pending attempt that suggests an uncertain write.
        outcome = getattr(exc, "outcome", "changed")
        out.update(status="refused", outcome=outcome, message=str(exc), not_sent=list(statements),
                   states=["not_sent"] * len(statements))
        _set(progress, "finished", out["states"], "refused")
        if audit is not None:
            audit.complete("refused", outcome=outcome, message=out["message"])
        return out
    except ConnectorError as exc:
        from .dbops import redact, target_error_text
        pw = getattr(conn, "password", None) or (getattr(audit, "password", None) if audit is not None else None)
        guard_failure = target_guard_failed(exc, packet_lines) if out["session_guard"] else None
        if guard_failure:
            index = guard_failure.get("statement_index", 0)
            out.update(status="partial" if index else "refused", outcome=guard_failure["outcome"],
                       message=redact(guard_failure["message"], pw),
                       sent=statements[:index], not_sent=statements[index:],
                       states=["done"] * index + ["not_sent"] * (len(statements) - index))
            _set(progress, "finished", out["states"], out["status"])
            if audit is not None:
                audit.complete("error" if index else "refused", outcome=out["outcome"],
                               error=redact(target_error_text(exc), pw), message=out["message"])
            return out
        if out["session_guard"]:
            exc = shift_guard_error(exc, packet_lines)
        text = redact(str(exc), pw)
        out["error"] = redact(server_message(text), pw)
        code, line = error_number(text), failed_line(text)
        definite = code is not None and (target_not_connected(exc) if line is None else
                                        (not client_error(code) and 1 <= line <= len(statements)))
        if may_have_run(exc) or not definite:
            out["status"] = "unknown"
            out["states"] = ["unknown"] * len(statements)
            out["message"] = ("No reply arrived (%s), so whether %s ran is unknown. Nothing will be sent again: "
                              "refresh and check the state before you do anything else."
                              % (out["error"], "these statements" if len(statements) > 1 else "the statement"))
            # An AdminAPI exception can omit its call site after changing
            # the cluster. A read can show a partial change, but an unchanged
            # visible snapshot cannot prove that no operation ran elsewhere.
            if fresh.get("via") == "mysqlsh" and verify_on_error and read_after is not None:
                _set(progress, "reading")
                try:
                    out["after"] = read_after()
                except (ConnectorError, ValueError):
                    out["after"] = None
                if effect is not None and out["after"] is not None:
                    try:
                        out["effect"] = effect(fresh, out["after"])
                    except (KeyError, TypeError, ValueError):
                        out["effect"] = None
                if out["effect"]:
                    out["status"] = "partial"
                    out["message"] = ("An error was reported (%s), and part of the change is visible. "
                                      "The statement location is unknown; check the current state before "
                                      "doing anything else." % out["error"])
            _set(progress, "finished", out["states"], out["status"])
            if audit is not None:
                audit.complete("error" if out["status"] == "partial" else "unknown",
                               outcome="partial" if out["status"] == "partial" else
                               "timeout" if "timed out" in text else "no_reply", error=text,
                               message=out["message"], after=out["after"])
            return out
        if line is None:
            out["status"] = "failed"
            out["not_sent"] = list(statements)
            out["states"] = ["not_sent"] * len(statements)
            out["message"] = "Nothing was sent: the client could not connect or log in (%s)." % out["error"]
            _set(progress, "finished", out["states"], "failed")
            if audit is not None:
                audit.complete("error", outcome="not_connected", error=text, message=out["message"])
            return out
        if 1 < line:
            out["status"] = "partial"
            out["sent"], out["not_sent"] = statements[:line - 1], statements[line:]
            failed = statements[line - 1]
            out["message"] = (partial_text(out["sent"], failed, out["error"]) if partial_text else
                              "%s ran; %s failed: %s. The statements after it were not sent."
                              % ("The first statement" if line == 2 else "The first %d statements" % (line - 1),
                                 failed, out["error"]))
            outcome = "partial"
        else:
            out["not_sent"] = statements[1:]
            if verify_on_error and read_after is not None:
                _set(progress, "reading")
                try:
                    after = read_after()
                except (ConnectorError, ValueError):
                    out["status"] = "unknown"
                    out["states"] = ["unknown"] + ["not_run"] * (len(statements) - 1)
                    out["message"] = ("The server reported an error on %s (%s), and the state after it could not be "
                                      "read, so whether anything changed is unknown." % (statements[0], out["error"]))
                    _set(progress, "finished", out["states"], "unknown")
                    if audit is not None:
                        audit.complete("unknown", outcome="no_reply", error=text, message=out["message"])
                    return out
                out["after"] = after
                changes = None
                if effect is not None and after is not None:
                    try:
                        changes = effect(fresh, after)
                    except (KeyError, TypeError, ValueError):
                        changes = None
                if changes:
                    out["effect"] = changes
                    out["status"] = "partial"
                    out["sent"] = [statements[0]]
                    out["states"] = ["failed"] + ["not_run"] * (len(statements) - 1)
                    out["message"] = (partial_text(out["sent"], statements[0], out["error"]) if partial_text else
                                      "%s failed (%s), after part of the change took effect."
                                      % (statements[0], out["error"]))
                    _set(progress, "finished", out["states"], "partial")
                    if audit is not None:
                        audit.complete("error", outcome="partial", error=text, message=out["message"], after=after)
                    return out
            out["status"] = "failed"
            out["message"] = (refused_text(statements[0], out["error"]) if refused_text else
                              "The server refused %s: %s. Nothing changed." % (statements[0], out["error"]))
            outcome = "refused_by_server"
        out["states"] = (["done"] * (line - 1) + ["failed"] + ["not_run"] * (len(statements) - line))
        _set(progress, "finished", out["states"], out["status"])
        if audit is not None:
            audit.complete("error", outcome=outcome, error=text, message=out["message"])
        return out
    out["sent"] = list(statements)
    out["status"] = "done"
    out["states"] = ["done"] * len(statements)
    _set(progress, "reading", out["states"], "done")
    if read_after is not None:
        try:
            out["after"] = read_after()
        except (ConnectorError, ValueError):
            out["after"] = None
    if effect is not None and out["after"] is not None:
        try:
            out["effect"] = effect(fresh, out["after"])
        except (KeyError, TypeError, ValueError):
            out["effect"] = None
    out["message"] = done_text(fresh, out["after"]) if done_text else "Sent."
    out["next"] = fresh.get("next")
    if audit is not None:
        audit.complete("ok", outcome="applied", message=out["message"], after=out["after"])
    _set(progress, "finished", out["states"], "done")
    return out
