# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""ProxySQL query digests, and where ProxySQL's query rules send each one.

:func:`read_digests` returns ``proxysql-digests-1.0`` (schema in
``docs/schemas/proxysql-digests-1.0.json``) for the ProxySQL place's Query
digests tab: the digests of ``stats_mysql_query_digest`` in one order (slowest
on average, most total time, most executions, highest rate, most rows sent),
the top 10, 25, 50 or 100, filtered by hostgroup, schema, user, digest or digest
text. ORDER BY and LIMIT run in Admin, so the read is bounded. It reads, never
writes.

Admin fills ``stats_mysql_query_digest`` from memory whenever a statement's text
names it, and the text ``stats_mysql_query_digest_reset`` makes that copy reset
every digest (ProxySQL_Admin.cpp ``GenericRefreshStatistics`` searches the
query text with ``strstr``). So nothing a person types is ever written into a
statement: text filters are hex blob literals (``CAST(X'..' AS TEXT)``) and
numbers are formatted as integers, and no statement here names the _reset table.

:class:`Routing` evaluates, here, where ProxySQL's active query rules would
send a digest, the way ``Query_Processor::process_query`` does
(lib/Query_Processor.cpp): the rules of ``runtime_mysql_query_rules`` in
rule_id order (ProxySQL_Admin.cpp loads ``WHERE active=1 ORDER BY rule_id``),
each one tested by ``rule_matches_query``: flagIN, username, schemaname,
client_addr, proxy_addr, proxy_port, digest, match_digest on the digest text and
match_pattern on the query text, ``negate_match_pattern`` turning either regex
result around, CASELESS in ``re_modifiers`` ignoring case. A match sets the
destination hostgroup when it is 0 or more and flagIN from ``attributes``
``flagOUTs`` (a random pick by weight) or flagOUT; ``apply`` stops the walk;
``mysql-query_processor_iterations`` restarts it after a flagOUT. Without
apply, ``mysql_query_rules_fast_routing`` (username, schemaname, flagIN, then
the same schema and flagIN for an empty username) has the last word; without
any destination the query goes to the user's ``default_hostgroup``. A rule's
``next_query_flagIN`` starts the session's following queries from that flagIN.
The walk follows every alternative an uncertain test or a weight opens, up to
BRANCH_LIMIT; work it leaves out is never judged a mismatch.

Patterns are never run in the workspace process: Python's ``re`` can
backtrack without end on a pattern PCRE or RE2 handles, and it holds the
interpreter while it does. :func:`test_patterns` runs them in a child process
(:func:`regex_worker`) with a time limit per pattern. A digest's hostgroup is where it ran
(``sess->current_hostgroup``); -1 is a reply from the query cache
(MySQL_Session.cpp sets it on a cache hit).

The evaluation is labelled "evaluated here": it uses Python's ``re``, while
ProxySQL uses PCRE (``mysql-query_processor_regex`` 1) or RE2 (2), so a pattern
can match differently; match_pattern is tested on the digest text, which keeps
no comments and has ``?`` for values, so a match_pattern result is approximate;
and a rule that tests what a digest does not record (proxy_addr, proxy_port,
client_addr when ``mysql-query_digests_track_hostname`` is off) is unknown. A
hostgroup other than the evaluated one is flagged; a transaction that stays on
one hostgroup (``transaction_persistent``), a ``/* hostgroup=N */`` comment, a
connection locked to a hostgroup, or rules loaded after these executions can
explain it. A few statements ProxySQL answers itself before any rule
(MySQL_Session.cpp ``handler_special_queries``: ``select @@version_comment limit
1``, which the mysql client sends after it connects, ``select USER()``,
``SELECT $$``; and ``SELECT @@version`` or ``SELECT VERSION()`` unless
``mysql-select_version_forwarding`` sends them on), so their digest names the
user's default hostgroup: verdict ``local``, seen live on 3.0.11.
"""
import binascii
import json
import re

from .connector import ConnectorError

SCHEMA_VERSION = "proxysql-digests-1.0"

#: The orders of the Query digests tab: id, label, ORDER BY expression, the
#: column it needs (sum_rows_sent arrived in ProxySQL 2.0.7). Ties fall back
#: to total time, executions and the digest, so the order is stable.
ORDERS = (
    ("slowest", "Slowest", "CAST(sum_time AS REAL) / MAX(count_star, 1)", None),
    ("time", "Most time", "sum_time", None),
    ("frequent", "Most frequent", "count_star", None),
    ("rate", "Highest rate", "CAST(count_star AS REAL) / MAX(last_seen - first_seen, 1)", None),
    ("rows", "Most rows sent", "sum_rows_sent", "sum_rows_sent"),
)
ORDER_IDS = tuple(o[0] for o in ORDERS)
LIMITS = (10, 25, 50, 100)
DEFAULT_ORDER = "time"
DEFAULT_LIMIT = 10
#: Digests read when the list is filtered by a rule: the rule is evaluated
#: here, so Admin cannot filter by it; the top RULE_SCAN rows in the chosen
#: order are read and the ones the rule applies to are kept.
RULE_SCAN = 500
#: Active rules evaluated per digest at most: past that, routing is not evaluated.
RULE_LIMIT = 500
#: Hostgroups, schemas and users the filters offer: the busiest combinations.
FACET_LIMIT = 200
FILTER_MAX = 200
HOSTGROUP_MAX = 2147483647
_DIGEST_HASH = re.compile(r"^0x[0-9A-Fa-f]{1,16}$")
#: Admin variables the evaluation depends on (runtime_global_variables).
VARIABLES = ("mysql-query_processor_regex", "mysql-query_processor_iterations", "mysql-query_digests",
             "mysql-query_digests_track_hostname", "mysql-select_version_forwarding")
REGEX_ENGINE = {"1": "PCRE", "2": "RE2"}
_NO_SUCH_COLUMN = "no such column"


def _int(value):
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def text_literal(value):
    """A SQLite text literal written as a hex blob: the text itself never
    appears in the statement (see the module docstring) and needs no quoting."""
    return "CAST(X'%s' AS TEXT)" % binascii.hexlify(value.encode("utf-8")).decode("ascii")


def digest_key(row):
    """The digest table's primary key (hostgroup, schemaname, username,
    client_address, digest) as one string: unique per row."""
    return "dg" + json.dumps([row.get(k) for k in
                             ("hostgroup", "schemaname", "username", "client_address", "digest")],
                            ensure_ascii=False, separators=(",", ":"))


def digest_row(r):
    """One stats_mysql_query_digest row; times in microseconds, seen times in
    Unix seconds. A column this ProxySQL lacks is None."""
    count = _int(r.get("count_star"))
    total = _int(r.get("sum_time"))
    first, last = _int(r.get("first_seen")), _int(r.get("last_seen"))
    span = (last - first) if first is not None and last is not None else None
    out = {"hostgroup": _int(r.get("hostgroup")), "schemaname": r.get("schemaname"),
           "username": r.get("username"), "client_address": r.get("client_address"),
           "digest": r.get("digest"), "digest_text": r.get("digest_text") or "", "count_star": count,
           "sum_time_us": total, "min_time_us": _int(r.get("min_time")), "max_time_us": _int(r.get("max_time")),
           "avg_time_us": (total // count) if total is not None and count else None,
           "first_seen": first, "last_seen": last, "seen_span_s": span,
           # Executions per second between the first and the last execution
           # (at least one second), as the Highest rate order computes it.
           "rate_qps": (float(count) / max(span, 1)) if count is not None and span is not None else None,
           "rows_sent": _int(r.get("sum_rows_sent")), "rows_affected": _int(r.get("sum_rows_affected"))}
    out["key"] = digest_key(out)
    return out


# ---- routing, evaluated here ------------------------------------------------------------

NO, MAYBE_NO, UNKNOWN, MAYBE_YES, YES = "no", "maybe_no", "unknown", "maybe_yes", "yes"
#: Digest texts of statements ProxySQL answers itself, before the rules (see
#: the module docstring): exact text, and text compared ignoring case.
LOCAL_EXACT = ("select @@version_comment limit ?", "select USER()")
LOCAL_CASELESS = ("select $$",)
#: SELECT @@version and SELECT VERSION(): answered by ProxySQL when
#: mysql-select_version_forwarding is 0 (never forward), 2 or 3 (a backend's
#: version when known; 3, the default, forwards the query when none is known).
LOCAL_VERSION = ("select @@version", "select version()")
VERSION_FORWARDING_LOCAL = ("0", "2", "3")
#: Alternatives followed per digest at most (uncertain rules, flagOUT weights).
BRANCH_LIMIT = 64
#: Patterns are tested in a separate Python process (see regex_worker): a
#: pattern that backtracks without end must never hold up the workspace. Each
#: pattern gets this many seconds over all the texts; one that takes longer is
#: unknown, and the worker starts again after it, at most WORKER_RESTARTS times.
PATTERN_TIMEOUT = 2.0
WORKER_RESTARTS = 3
WORKER_ARG = "__myflames_regex_worker"
#: The worker's first answer also waits for it to start.
WORKER_START = 10.0
ITERATION_LIMIT = 10


def answered_locally(text, version_forwarding=None):
    """Whether ProxySQL replies to this digest itself, before any rule."""
    text = (text or "").strip()
    if text in LOCAL_EXACT or text.lower() in LOCAL_CASELESS:
        return True
    return text.lower() in LOCAL_VERSION and (version_forwarding or "").strip() in VERSION_FORWARDING_LOCAL


def regex_worker(stdin=None, stdout=None):
    """The child that tests patterns (``python -I -c`` from a checkout, or the
    packaged backend with WORKER_ARG): reads ``{"patterns": [[pattern,
    caseless], ...], "texts": [...]}``, writes ``ready``, then one line per
    pattern, one character per text: 1 found, 0 not found, e a pattern
    Python's re cannot compile."""
    import json
    import sys
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    job = json.loads(stdin.read() or "{}")
    stdout.write("ready\n")
    stdout.flush()
    texts = job.get("texts") or []
    for pattern, caseless in job.get("patterns") or []:
        try:
            compiled = re.compile(pattern, re.IGNORECASE if caseless else 0)
        except (re.error, OverflowError, RecursionError, TypeError, ValueError):
            stdout.write("e" * len(texts) + "\n")
            stdout.flush()
            continue
        line = []
        for text in texts:
            try:
                line.append("1" if compiled.search(text) else "0")
            except (RecursionError, OverflowError):
                line.append("e")
        stdout.write("".join(line) + "\n")
        stdout.flush()


def _worker_argv():
    import os
    import sys
    if getattr(sys, "frozen", False):
        # The packaged backend (apps/desktop/scripts/backend-entry.py) runs
        # regex_worker when its first argument is WORKER_ARG.
        return [sys.executable, WORKER_ARG]
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); from myflames.proxysql_digests "
                                        "import regex_worker; regex_worker()" % parent]


def _run_worker(patterns, texts, start, out):
    """One worker over patterns[start:]: fills *out* and returns (index
    reached, stalled). The child is always killed and reaped, whatever
    happens here (an interrupt included); stdin is written by its own thread,
    so a child that never reads cannot block this one past WORKER_START."""
    import json
    import os
    import queue
    import subprocess
    import threading
    env = dict((k, v) for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH", "TMPDIR", "TEMP", "LANG"))
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(_worker_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    try:
        lines = queue.Queue()
        payload = json.dumps({"patterns": patterns[start:], "texts": texts}).encode("utf-8")

        def feed():
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        def drain():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    # The worker's text stdout ends lines with \r\n on Windows.
                    lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
            except (OSError, ValueError):
                pass
            lines.put(None)
        for target in (feed, drain):
            threading.Thread(target=target, daemon=True).start()
        try:
            ready = lines.get(timeout=WORKER_START)
        except queue.Empty:
            ready = None
        if ready != "ready":
            raise OSError("the pattern worker did not start")
        index = start
        while index < len(patterns):
            try:
                line = lines.get(timeout=PATTERN_TIMEOUT)
            except queue.Empty:
                return index, True
            if line is None:
                return index, False
            out[index] = line
            index += 1
        return index, False
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def test_patterns(patterns, texts):
    """``{pattern index: line}`` for *patterns* ([(pattern, caseless)]) over
    *texts*, each line one character per text (1, 0, e, or t: took longer than
    PATTERN_TIMEOUT), from regex_worker in a separate process. Raises OSError
    when no worker can start."""
    out = {}
    start = 0
    restarts = 0
    while start < len(patterns):
        index, stalled = _run_worker(patterns, texts, start, out)
        if index >= len(patterns):
            break
        # This pattern took too long (or the worker died on it): unknown.
        out[index] = "t" * len(texts)
        start = index + 1
        restarts += 1
        if restarts > WORKER_RESTARTS or not stalled:
            for rest in range(start, len(patterns)):
                out[rest] = "t" * len(texts)
            break
    return out


class _Rule(object):
    """One active runtime rule, as rule_matches_query reads it."""

    def __init__(self, row, patterns):
        self.rule_id = _int(row.get("rule_id"))
        self.flag_in = _int(row.get("flagIN")) or 0
        flag_out = _int(row.get("flagOUT"))
        self.flag_out = flag_out if flag_out is not None and flag_out >= 0 else None
        self.username = row.get("username") or ""
        self.schemaname = row.get("schemaname") or ""
        self.client_addr = row.get("client_addr") or ""
        self.proxy_addr = row.get("proxy_addr") or ""
        port = _int(row.get("proxy_port"))
        self.proxy_port = port if port is not None and port >= 0 else None
        digest = (row.get("digest") or "").strip()
        try:
            # strtoull(digest, NULL, 0): 0x for hex, else decimal.
            self.digest = int(digest, 0) if digest else None
        except ValueError:
            self.digest = None
        self.match_digest = row.get("match_digest")
        self.match_pattern = row.get("match_pattern")
        self.negate = _int(row.get("negate_match_pattern")) == 1
        modifiers = [m.strip().upper() for m in (row.get("re_modifiers") or "").split(",")]
        self.caseless = any(m.startswith("CASELESS") for m in modifiers)
        self.digest_index = self.pattern_index = None
        if self.match_digest is not None:
            self.digest_index = len(patterns)
            patterns.append((self.match_digest, self.caseless))
        if self.match_pattern is not None:
            self.pattern_index = len(patterns)
            patterns.append((self.match_pattern, self.caseless))
        dest = _int(row.get("destination_hostgroup"))
        self.destination = dest if dest is not None and dest >= 0 else None
        self.apply = _int(row.get("apply")) == 1
        # Sets the flagIN the session's next query starts with (MySQL_Session.cpp
        # keeps it for the following queries of that session).
        following = _int(row.get("next_query_flagIN"))
        self.next_flag = following if following is not None and following >= 0 else None
        self.comment = row.get("comment") or ""
        # attributes.flagOUTs: [{"id": n, "weight": n}] picks a flagOUT at random
        # by weight (MySQL_Query_Processor.cpp new_query_rule); flagOUT, when
        # set, replaces the pick (Query_Processor.cpp process_query).
        self.flag_outs = []
        try:
            import json
            attributes = json.loads(row.get("attributes") or "{}")
            for item in (attributes.get("flagOUTs") or []) if isinstance(attributes, dict) else ():
                ident, weight = item.get("id"), item.get("weight")
                if isinstance(ident, int) and isinstance(weight, int) and not isinstance(ident, bool) \
                        and ident >= 0 and weight > 0:
                    self.flag_outs.append(ident)
        except (ValueError, AttributeError, TypeError):
            self.flag_outs = []

    def changes(self):
        """Whether matching this rule can change where a query goes."""
        return self.destination is not None or self.flag_out is not None or bool(self.flag_outs) or self.apply

    def test(self, d, flag_in, found):
        """(result, reasons): YES or NO when every condition could be checked
        exactly, MAYBE_YES or MAYBE_NO when match_pattern was checked on the
        digest text, UNKNOWN when a condition cannot be checked from a digest.
        *found(index)* is a pattern's character for this digest text."""
        if self.flag_in != flag_in:
            return NO, []
        if self.username and (d.get("username") or "") != self.username:
            return NO, []
        if self.schemaname and (d.get("schemaname") or "") != self.schemaname:
            return NO, []
        unknown = []
        if self.client_addr:
            seen = d.get("client_address") or ""
            if not seen or "%" in self.client_addr:
                unknown.append("client_addr %s" % self.client_addr)
            elif seen != self.client_addr:
                return NO, []
        if self.proxy_addr:
            unknown.append("proxy_addr %s" % self.proxy_addr)
        if self.proxy_port is not None:
            unknown.append("proxy_port %d" % self.proxy_port)
        if self.digest is not None and d.get("digest"):
            try:
                if int(d["digest"], 16) != self.digest:
                    return NO, []
            except ValueError:
                unknown.append("digest")
        if self.digest_index is not None:
            mark = found(self.digest_index)
            if mark not in ("0", "1"):
                unknown.append("match_digest (%s)" % _MARK_WORD.get(mark, "not tested"))
            elif (mark == "1") == self.negate:
                return NO, []
        approx = False
        if self.pattern_index is not None:
            mark = found(self.pattern_index)
            if mark not in ("0", "1"):
                unknown.append("match_pattern (%s)" % _MARK_WORD.get(mark, "not tested"))
            else:
                approx = True
                if (mark == "1") == self.negate:
                    return (UNKNOWN if unknown else MAYBE_NO), unknown
        if unknown:
            return UNKNOWN, unknown
        return (MAYBE_YES if approx else YES), []

    def summary(self):
        return {"rule_id": self.rule_id, "destination_hostgroup": self.destination, "apply": self.apply,
                "flagIN": self.flag_in, "flagOUT": self.flag_out, "username": self.username or None,
                "schemaname": self.schemaname or None, "match_digest": self.match_digest,
                "match_pattern": self.match_pattern, "negate_match_pattern": self.negate,
                "caseless": self.caseless, "comment": self.comment}


_MARK_WORD = {"e": "a pattern Python cannot read", "t": "a pattern that took too long to test here"}


class _Path(object):
    """One way through the rules: the walk ProxySQL takes when every
    uncertain test goes as *assumed* says."""

    def __init__(self, index=0, flag_in=0, budget=0):
        self.index, self.flag_in, self.budget = index, flag_in, budget
        self.dest = self.decisive = None
        self.chain, self.passed, self.assumed = [], [], []
        self.stopped = False
        # Work this evaluation left out (a restart past ITERATION_LIMIT, a
        # flagOUT weight past BRANCH_LIMIT): the path may end elsewhere.
        self.truncated = False
        self.unread_variables = set()

    def copy(self):
        other = _Path(self.index, self.flag_in, self.budget)
        other.dest, other.decisive, other.stopped = self.dest, self.decisive, self.stopped
        other.truncated = self.truncated
        other.unread_variables = set(self.unread_variables)
        other.chain, other.passed, other.assumed = list(self.chain), list(self.passed), list(self.assumed)
        return other


class Routing(object):
    """The active runtime rules, fast routing, the users' default hostgroups
    and the Admin variables one evaluation needs. Any of them None (unread)
    leaves what depends on it unknown."""

    def __init__(self, rules_rows, fast_rows, user_rows, variables):
        self.variables = variables or {}
        self.readable = rules_rows is not None
        active = [r for r in rules_rows or () if _int(r.get("active")) in (None, 1) and _int(r.get("rule_id")) is not None]
        active.sort(key=lambda r: _int(r.get("rule_id")))
        self.limited = len(active) > RULE_LIMIT
        self.patterns = []
        self.rules = [] if self.limited else [_Rule(r, self.patterns) for r in active]
        self.fast = None if fast_rows is None else dict(
            ((r.get("username") or "", r.get("schemaname") or "", _int(r.get("flagIN")) or 0),
             _int(r.get("destination_hostgroup"))) for r in fast_rows)
        self.users = None
        if user_rows is not None:
            self.users = {}
            for r in user_rows:
                # runtime_mysql_users has a frontend and a backend row per user.
                if _int(r.get("frontend")) == 0:
                    continue
                self.users[r.get("username") or ""] = {"default_hostgroup": _int(r.get("default_hostgroup")),
                                                       "transaction_persistent": _int(r.get("transaction_persistent")),
                                                       "active": _int(r.get("active")) != 0}
        iterations = _int(self.variables.get("mysql-query_processor_iterations"))
        self.iterations_read = iterations is not None and iterations >= 0
        # Restarts followed here at most (ProxySQL's default is 0); a path that
        # would restart again is marked truncated, never judged a mismatch.
        self.iterations = min(iterations, ITERATION_LIMIT) if iterations and iterations > 0 else 0
        self.iterations_cut = bool(iterations and iterations > ITERATION_LIMIT)
        self.next_flags = {}
        for rule in self.rules:
            if rule.next_flag is not None:
                self.next_flags.setdefault(rule.next_flag, []).append(rule.rule_id)
        self.engine = REGEX_ENGINE.get(str(self.variables.get("mysql-query_processor_regex") or "").strip())
        self.marks = {}
        self.worker_error = None

    def prepare(self, texts):
        """Test every pattern on *texts* at once, in the worker process."""
        wanted = [t for t in dict.fromkeys(texts or ()) if t not in self.marks]
        if not wanted or not self.patterns or self.worker_error:
            for text in wanted:
                self.marks.setdefault(text, "")
            return
        try:
            lines = test_patterns(self.patterns, wanted)
        except OSError as exc:
            self.worker_error = "patterns could not be tested here (%s)" % exc
            lines = {}
        for column, text in enumerate(wanted):
            self.marks[text] = "".join((lines.get(i) or "")[column:column + 1] or "?" for i in range(len(self.patterns)))

    def context(self):
        """What the report says about the evaluation (the place's ⓘ text)."""
        note = None
        if not self.readable:
            note = "runtime_mysql_query_rules could not be read, so routing is not evaluated."
        elif self.limited:
            note = "More than %d active rules, so routing is not evaluated here." % RULE_LIMIT
        elif self.worker_error:
            note = "Routing is not fully evaluated: %s." % self.worker_error
        return {"evaluated": self.readable and not self.limited, "regex_engine": self.engine,
                "regex_variable": self.variables.get("mysql-query_processor_regex"),
                "iterations": self.iterations, "fast_routing_read": self.fast is not None,
                "users_read": self.users is not None,
                "track_hostname": (self.variables.get("mysql-query_digests_track_hostname") or "").lower() == "true",
                "active_rules": len(self.rules), "note": note}

    def _fast(self, d, flag_in):
        """mysql_query_rules_fast_routing: (user, schema, flagIN), then the
        same schema and flagIN for any user (an entry with an empty username;
        Query_Processor.cpp search_rules_fast_routing_dest_hg)."""
        if self.fast is None:
            return None
        for user in (d.get("username") or "", ""):
            hit = self.fast.get((user, d.get("schemaname") or "", flag_in))
            if hit is not None and hit >= 0:
                return hit
        return None

    def _walk(self, d, found, flag_in=0):
        """Every path through the rules (see _Path) from *flag_in*; the first
        one takes each uncertain test at its best guess (match_pattern on the
        digest text as it came out, unknown as not matched), the others flip
        one of them or take another flagOUT weight. At most BRANCH_LIMIT."""
        paths, todo, cut = [], [_Path(flag_in=flag_in, budget=self.iterations)], False
        while todo:
            path = todo.pop()
            while path.index < len(self.rules) and not path.stopped:
                rule = self.rules[path.index]
                path.index += 1
                result, reasons = rule.test(d, path.flag_in, found)
                if result == NO:
                    continue
                take = result in (YES, MAYBE_YES)
                if result != YES and rule.changes():
                    if len(paths) + len(todo) + 1 < BRANCH_LIMIT:
                        other = path.copy()
                        other.assumed.append((rule.rule_id, "skipped" if take else "matched"))
                        if take:
                            other.passed.append((rule, result, reasons))
                        else:
                            self._take(other, rule, result, todo, paths)
                        todo.append(other)
                    else:
                        cut = True
                if not take:
                    path.passed.append((rule, result, reasons))
                    continue
                self._take(path, rule, result, todo, paths)
            paths.append(path)
        return paths, cut

    def _take(self, path, rule, result, todo, paths):
        """Apply a matching rule to *path* (process_query): flagOUT weights
        (one branch per other choice), flagOUT, destination, apply, and a
        restart while iterations remain."""
        path.chain.append((rule, result))
        flagged = False
        if rule.flag_outs:
            for choice in rule.flag_outs[1:]:
                if len(paths) + len(todo) + 1 < BRANCH_LIMIT:
                    other = path.copy()
                    other.assumed.append((rule.rule_id, "weight %d" % choice))
                    other.flag_in = choice
                    self._after_flag(other, rule)
                    todo.append(other)
                else:
                    path.truncated = True
            path.flag_in = rule.flag_outs[0]
            if len(rule.flag_outs) > 1:
                path.assumed.append((rule.rule_id, "weight %d" % rule.flag_outs[0]))
            flagged = True
        if rule.flag_out is not None:
            path.flag_in = rule.flag_out
            flagged = True
        if rule.destination is not None:
            path.dest, path.decisive = rule.destination, rule.rule_id
        if rule.apply:
            path.stopped = True
        elif flagged:
            self._restart(path)

    def _restart(self, path):
        if not self.iterations_read:
            # Zero is a known no-restart budget; an unread value is not zero.
            path.unread_variables.add("mysql-query_processor_iterations")
        elif path.budget:
            path.budget -= 1
            path.index = 0
        elif self.iterations_cut:
            path.truncated = True

    def _after_flag(self, path, rule):
        if rule.flag_out is not None:
            path.flag_in = rule.flag_out
        if rule.destination is not None:
            path.dest, path.decisive = rule.destination, rule.rule_id
        if rule.apply:
            path.stopped = True
        else:
            self._restart(path)

    def _finish(self, d, path, default):
        source = "rule" if path.decisive is not None else None
        dest = path.dest
        if not path.stopped:
            hit = self._fast(d, path.flag_in)
            if hit is not None:
                dest, source = hit, "fast_routing"
        if dest is None and default is not None:
            dest, source = default, "default"
        return dest, source

    def evaluate(self, d):
        """The ``routing`` object of one digest (see the module docstring)."""
        observed = d.get("hostgroup")
        user = (self.users or {}).get(d.get("username") or "") if self.users is not None else None
        default = user["default_hostgroup"] if user else None
        out = {"observed": observed, "expected": None, "source": None, "rules": [], "decisive": None,
               "approximate": False, "possible": [], "unknown_rules": [], "default_hostgroup": default,
               "verdict": "unknown", "text": "", "why": []}
        if not self.readable or self.limited:
            out["text"] = ("The query rules could not be read." if not self.readable
                           else "More than %d active rules: not evaluated here." % RULE_LIMIT)
            return out
        text = d.get("digest_text") or ""
        if text not in self.marks:
            self.prepare([text])
        marks = self.marks.get(text) or ""
        unread_variables = set()

        def found(index):
            if self.engine is None:
                unread_variables.add("mysql-query_processor_regex")
            return marks[index:index + 1] or "?"
        paths, cut = self._walk(d, found)
        main = paths[0]
        dest, source = self._finish(d, main, default)
        decisive = main.decisive if source == "rule" else None
        approximate = any(res == MAYBE_YES for _, res in main.chain)
        out.update(expected=dest, source=source, decisive=decisive, rules=[r.rule_id for r, _ in main.chain],
                   approximate=approximate,
                   unknown_rules=[{"rule_id": r.rule_id, "why": "; ".join(why)} for r, res, why in main.passed
                                  if res == UNKNOWN])
        weighted = [p for p in paths[1:] if p.assumed and all(how.startswith("weight") for _, how in p.assumed)]
        flipped = [p for p in paths[1:] if p not in weighted]
        reaching = [p for p in flipped if self._finish(d, p, default)[0] == observed and observed != dest]
        possible = []
        for p in reaching:
            for rid, how in p.assumed:
                if not how.startswith("weight") and rid not in possible:
                    possible.append(rid)
        # A rule's next_query_flagIN makes the session's following queries
        # start from that flagIN (Query_Processor.cpp process_query).
        following = []
        if observed != dest and observed != -1:
            for flag, setters in sorted(self.next_flags.items()):
                if flag == 0:
                    continue
                more, more_cut = self._walk(d, found, flag)
                cut = cut or more_cut
                if any(self._finish(d, p, default)[0] == observed for p in more):
                    following.extend(rid for rid in setters if rid not in following)
                paths.extend(more)
        for rid in following:
            if rid not in possible:
                possible.append(rid)
        out["possible"] = possible
        for path in paths:
            unread_variables.update(path.unread_variables)
        incomplete = cut or any(p.truncated for p in paths)
        by_weight = any(self._finish(d, p, default)[0] == observed for p in weighted)
        who = d.get("username") or "the user"
        why = out["why"]
        for name in sorted(unread_variables):
            why.append("%s could not be read with a supported value; routing depends on it." % name)
        if any(how.startswith("weight") for _, how in main.assumed) or weighted:
            why.append("A rule on the way picks its flagOUT at random by weight (attributes flagOUTs), so the "
                       "hostgroup can differ between executions.")
        if not main.stopped and source == "fast_routing":
            why.append("No rule with apply = 1 matched, so mysql_query_rules_fast_routing chose hostgroup %d." % dest)
        if not main.stopped and self.fast is None and source != "rule":
            why.append("mysql_query_rules_fast_routing could not be read; it may choose another hostgroup.")
        if approximate:
            why.append("match_pattern tests the query text; here it is tested on the digest text, which has no "
                       "comments and ? for values, so the result is approximate.")
        for item in out["unknown_rules"]:
            why.append("Rule %d tests %s, which a digest does not record or myflames could not test." % (
                item["rule_id"], item["why"]))
        if cut:
            why.append("Not every alternative was followed (more than %d)." % BRANCH_LIMIT)
        if any(p.truncated for p in paths):
            why.append("Some ways through the rules go on further than myflames follows (more than %d restarts of "
                       "mysql-query_processor_iterations, or more than %d flagOUT choices)." % (ITERATION_LIMIT,
                                                                                               BRANCH_LIMIT))
        if source == "rule":
            route = "Rule %d sends it to hostgroup %d" % (decisive, dest)
        elif source == "fast_routing":
            route = "Fast routing sends it to hostgroup %d" % dest
        elif source == "default":
            route = "No active rule sets a hostgroup, so it goes to %s's default hostgroup %d" % (who, dest)
        else:
            route = ""
        if observed == -1:
            out["verdict"] = "cache"
            out["text"] = "Answered from the query cache (hostgroup -1)%s." % (
                "; %s%s" % (route[0].lower(), route[1:]) if route else "")
        elif unread_variables:
            out["text"] = "%s could not be read with a supported value, so routing is not judged." % \
                ", ".join(sorted(unread_variables))
        elif dest is None:
            out["verdict"] = "unknown"
            out["text"] = ("No active rule sets a hostgroup, and %s's default hostgroup could not be read." % who
                           if self.users is None else
                           "No active rule sets a hostgroup, and %s is not in runtime_mysql_users." % who)
        elif dest == observed or by_weight:
            out["verdict"] = "match"
            out["text"] = "%s, where it ran." % route if dest == observed else \
                "%s, or to hostgroup %s by another flagOUT weight, where it ran." % (route, observed)
        elif answered_locally(text, self.variables.get("mysql-select_version_forwarding")) \
                and default is not None and observed == default:
            out["verdict"] = "local"
            out["text"] = ("ProxySQL answers this statement itself, before any rule, so the digest names %s's "
                           "default hostgroup %d." % (who, default))
            why.insert(0, "MySQL_Session.cpp replies to it without a backend (handler_special_queries; SELECT "
                          "@@version and SELECT VERSION() by mysql-select_version_forwarding).")
            if text.lower() in LOCAL_VERSION and (self.variables.get("mysql-select_version_forwarding") or "").strip() == "3":
                why.append("With mysql-select_version_forwarding = 3 ProxySQL answers when it knows a backend's "
                           "version and sends the query on otherwise.")
        elif possible:
            first = next(r for r in self.rules if r.rule_id == possible[0])
            out["verdict"] = "possible"
            if first.rule_id in following and first.rule_id not in [rid for p in reaching for rid, _ in p.assumed]:
                out["text"] = "%s here; it ran on hostgroup %s, where it goes after rule %d sets the next query's " \
                              "flagIN." % (route, observed, first.rule_id)
                why.insert(0, "Rule %d sets next_query_flagIN, so the queries after it in the same session start "
                              "from another flagIN." % first.rule_id)
            else:
                out["text"] = "%s here; it ran on hostgroup %s, where rule %d may have sent it." % (
                    route, observed, first.rule_id)
                why.insert(0, "Rule %d %s, so it may have matched these executions." % (
                    first.rule_id, "tests the query text (match_pattern), which the digest does not keep"
                    if first.match_pattern is not None else "tests what a digest does not record"))
        elif incomplete or (self.fast is None and not main.stopped):
            # What was left out could send it to the hostgroup it ran on.
            out["verdict"] = "unknown"
            out["text"] = "%s here, but it ran on hostgroup %s; %s, so this is not judged." % (
                route, observed, "mysql_query_rules_fast_routing could not be read and can choose another hostgroup"
                if not incomplete else "not every way through the rules was followed here")
        else:
            out["verdict"] = "mismatch"
            out["text"] = "%s, but it ran on hostgroup %s." % (route, observed)
            persistent = bool(user) and user.get("transaction_persistent") == 1
            why.append("ProxySQL can choose another hostgroup when %s, a /* hostgroup=N */ comment sets it, a "
                       "SET statement locked the connection to a hostgroup, or the rules were loaded after these "
                       "executions." % ("a transaction stays on the hostgroup where it began "
                                        "(transaction_persistent is 1 for %s)" % who
                                        if persistent else "the statement runs inside a transaction"))
        return out


def read_routing(reads, statements=None):
    """The reads :class:`Routing` needs, through *reads* (proxysql._Reads):
    runtime rules, fast routing, users' default hostgroups (named columns,
    never the password) and the Admin variables. Returns (Routing, rule rows)."""
    variables = reads.table("SELECT variable_name, variable_value FROM runtime_global_variables WHERE "
                            "variable_name IN (%s)" % ", ".join("'%s'" % v for v in VARIABLES),
                            "runtime_global_variables")
    values = None if variables is None else dict((r.get("variable_name"), r.get("variable_value")) for r in variables)
    rules = reads.table("SELECT * FROM runtime_mysql_query_rules", "runtime_mysql_query_rules")
    fast = reads.table("SELECT * FROM runtime_mysql_query_rules_fast_routing", "runtime_mysql_query_rules_fast_routing")
    users = reads.table("SELECT username, active, default_hostgroup, transaction_persistent, frontend FROM "
                        "runtime_mysql_users", "runtime_mysql_users")
    return Routing(rules, fast, users, values), rules


# ---- the Query digests tab ----------------------------------------------------------------

def _text_filter(value, what):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > FILTER_MAX or "\x00" in value:
        raise ValueError("The %s filter must be text of at most %d characters." % (what, FILTER_MAX))
    return value


def check_filters(order=None, limit=None, hostgroup=None, schema=None, user=None, text=None, digest=None,
                  rule_id=None):
    """The validated request: an order id, a limit from LIMITS, and filters."""
    order = DEFAULT_ORDER if order in (None, "") else order
    if order not in ORDER_IDS:
        raise ValueError("Choose an order: %s." % ", ".join(ORDER_IDS))
    limit = DEFAULT_LIMIT if limit is None else limit
    if isinstance(limit, bool) or limit not in LIMITS:
        raise ValueError("Choose how many digests to list: %s." % ", ".join(str(n) for n in LIMITS))
    if hostgroup is not None and (isinstance(hostgroup, bool) or not isinstance(hostgroup, int)
                                  or not -1 <= hostgroup <= HOSTGROUP_MAX):
        raise ValueError("The hostgroup filter must be a whole number from -1 to %d." % HOSTGROUP_MAX)
    if digest not in (None, "") and (not isinstance(digest, str) or not _DIGEST_HASH.match(digest)):
        raise ValueError("The digest filter must be a digest such as 0x9C012E222EE5AA4E.")
    if digest:
        digest = "0x" + digest[2:].upper()
    if rule_id is not None and (isinstance(rule_id, bool) or not isinstance(rule_id, int)
                                or not 0 <= rule_id <= HOSTGROUP_MAX):
        raise ValueError("The rule filter must be a rule_id.")
    text = _text_filter(text, "text")
    # Schema and user: None is every one; any text, the empty one included
    # (a client with no default schema), is matched exactly.
    return {"order": order, "limit": limit, "hostgroup": hostgroup, "schema": _exact_filter(schema, "schema"),
            "user": _exact_filter(user, "user"), "text": text.strip() if text else None, "digest": digest or None,
            "rule_id": rule_id}


def _exact_filter(value, what):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > FILTER_MAX or "\x00" in value:
        raise ValueError("The %s filter must be text of at most %d characters." % (what, FILTER_MAX))
    return value


def digest_sql(filters, limit):
    """The bounded Admin statement for *filters* (see the module docstring:
    no person's text appears in it)."""
    expression = next(o[2] for o in ORDERS if o[0] == filters["order"])
    where = []
    if filters["hostgroup"] is not None:
        where.append("hostgroup = %d" % filters["hostgroup"])
    for column, name in (("schemaname", "schema"), ("username", "user"), ("digest", "digest")):
        if filters[name] is not None:
            where.append("%s = %s" % (column, text_literal(filters[name])))
    if filters["text"]:
        # lower() folds ASCII letters only (SQLite without ICU).
        where.append("instr(lower(digest_text), lower(%s)) > 0" % text_literal(filters["text"]))
    return ("SELECT * FROM stats_mysql_query_digest%s ORDER BY %s DESC, sum_time DESC, count_star DESC, digest "
            "LIMIT %d" % ((" WHERE " + " AND ".join(where)) if where else "", expression, int(limit)))


FACET_SQL = ("SELECT hostgroup, schemaname, username, COUNT(*) AS digests, SUM(count_star) AS executions, "
             "SUM(sum_time) AS sum_time, MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen "
             "FROM stats_mysql_query_digest GROUP BY hostgroup, schemaname, username "
             "ORDER BY executions DESC LIMIT %d" % (FACET_LIMIT + 1))


def _facets(rows):
    """Filters' choices and the totals of every digest, from the grouped read."""
    if rows is None:
        return None, None, None
    complete = len(rows) <= FACET_LIMIT
    rows = rows[:FACET_LIMIT]
    out = {"hostgroups": {}, "schemas": {}, "users": {}}
    totals = {"digests": 0, "executions": 0, "sum_time_us": 0}
    first = last = None
    for r in rows:
        n, ex, t = _int(r.get("digests")) or 0, _int(r.get("executions")) or 0, _int(r.get("sum_time")) or 0
        totals["digests"] += n
        totals["executions"] += ex
        totals["sum_time_us"] += t
        for name, value in (("hostgroups", _int(r.get("hostgroup"))), ("schemas", r.get("schemaname") or ""),
                            ("users", r.get("username") or "")):
            entry = out[name].setdefault(value, {"value": value, "digests": 0, "executions": 0})
            entry["digests"] += n
            entry["executions"] += ex
        f, l = _int(r.get("first_seen")), _int(r.get("last_seen"))
        first = f if first is None or (f is not None and f < first) else first
        last = l if last is None or (l is not None and l > last) else last
    facets = dict((name, sorted(values.values(), key=lambda e: (-e["executions"], str(e["value"]))))
                  for name, values in out.items())
    facets["complete"] = complete
    return facets, (totals if complete else None), {"first_seen": first, "last_seen": last}


def read_digests(conn, order=None, limit=None, hostgroup=None, schema=None, user=None, text=None, digest=None,
                 rule_id=None):
    """The ``proxysql-digests-1.0`` report. Read-only; see the module docstring."""
    from . import proxysql
    from .perf_common import now
    filters = check_filters(order, limit, hostgroup, schema, user, text, digest, rule_id)
    out = {"schema_version": SCHEMA_VERSION, "captured_at": now(), "available": False, "is_proxysql": False,
           "version": None, "message": None, "filters": filters,
           "orders": [{"id": o[0], "label": o[1], "available": True, "why": None} for o in ORDERS],
           "limits": list(LIMITS), "digests": [], "more": False, "scanned": None, "facets": None, "totals": None,
           "window": None, "rules": [], "rule_hits": {}, "routing": None, "notes": [], "statements": []}
    ok, detail = proxysql.identify(conn)
    if not ok:
        out["message"] = ("This connection is not a ProxySQL Admin interface: %s. ProxySQL Admin listens on port "
                          "6032 unless admin-mysql_ifaces says otherwise." % detail)
        return out
    out.update(available=True, is_proxysql=True, version=detail)
    reads = proxysql._Reads(conn)
    routing, _ = read_routing(reads)
    hits = reads.table("SELECT * FROM stats_mysql_query_rules", "stats_mysql_query_rules")
    out["rule_hits"] = dict((str(_int(r.get("rule_id"))), _int(r.get("hits"))) for r in hits or ()) \
        if hits is not None else None
    out["rules"] = [r.summary() for r in routing.rules]
    out["routing"] = routing.context()
    if (routing.variables.get("mysql-query_digests") or "").lower() == "false":
        out["notes"].append("mysql-query_digests is false: ProxySQL records no new digests.")
    out["facets"], out["totals"], out["window"] = _facets(reads.table(FACET_SQL, "stats_mysql_query_digest (groups)"))
    if out["facets"] is not None and not out["facets"]["complete"]:
        out["notes"].append("The filters list the %d busiest hostgroup, schema and user combinations." % FACET_LIMIT)
    by_rule = filters["rule_id"] is not None
    if by_rule and not out["routing"]["evaluated"]:
        raise ValueError(out["routing"]["note"] or "Routing is not evaluated, so digests cannot be listed by rule.")
    if by_rule and filters["rule_id"] not in [r.rule_id for r in routing.rules]:
        raise ValueError("Rule %d is not active in runtime, so it applies to no digest." % filters["rule_id"])
    want = RULE_SCAN if by_rule else filters["limit"]
    # One more row than shown says whether more digests match.
    sql = digest_sql(filters, want + (0 if by_rule else 1))
    try:
        rows = proxysql.select(conn, sql)
    except ConnectorError as exc:
        needs = next(o[3] for o in ORDERS if o[0] == filters["order"])
        if needs and _NO_SUCH_COLUMN in str(exc):
            for item in out["orders"]:
                if item["id"] == filters["order"]:
                    item.update(available=False, why="This ProxySQL has no %s column (ProxySQL 2.0.7 added it)."
                                % needs)
            raise ValueError("This ProxySQL has no %s column (ProxySQL 2.0.7 added it), so digests cannot be "
                             "ordered by rows sent." % needs)
        raise
    out["statements"] = reads.statements + [sql]
    for label, why in reads.missing:
        out["notes"].append("Not read: %s (%s)." % (label, why))
    shown = []
    items = [digest_row(row) for row in rows]
    routing.prepare([item["digest_text"] for item in items])
    for item in items:
        item["routing"] = routing.evaluate(item)
        if by_rule and filters["rule_id"] not in item["routing"]["rules"]:
            continue
        shown.append(item)
    if by_rule:
        out["scanned"] = len(rows)
        out["more"] = len(shown) > filters["limit"] or len(rows) >= RULE_SCAN
    else:
        out["more"] = len(shown) > filters["limit"]
    out["digests"] = shown[:filters["limit"]]
    return out


def cross_check(digests, rules, routing):
    """Fill the proxysql-1.0 report: each digest gets ``key`` and ``routing``,
    each rule ``digests`` (the keys of the digests it applies to, evaluated
    here), ``digest_count``, and ``possible_digests`` (digests that ran on its
    destination hostgroup while a condition of it could not be tested on the
    digest, such as match_pattern on the query text)."""
    applies, possible = {}, {}
    routing.prepare([d.get("digest_text") or "" for d in digests])
    for d in digests:
        d["key"] = digest_key(d)
        d["routing"] = routing.evaluate(d)
        for rid in d["routing"]["rules"]:
            applies.setdefault(rid, []).append(d["key"])
        for rid in d["routing"]["possible"]:
            possible.setdefault(rid, []).append(d["key"])
    evaluated = routing.readable and not routing.limited
    for rule in rules:
        keys = applies.get(rule["rule_id"], [])
        rule["digests"] = keys if evaluated else None
        rule["digest_count"] = len(keys) if evaluated else None
        rule["possible_digests"] = possible.get(rule["rule_id"], []) if evaluated else None
