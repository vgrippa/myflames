"""Bounded, read-only observations from an editor tab's own connection.

Never insert a helper between script statements: diagnostics and ROW_COUNT
belong to the user. Read the history ring before the normal post-run helpers,
and refuse ambiguous attribution when that ring has lost entries.

The transaction meter's poll is an observation: it holds run_lock (one
statement on the session pipe at a time) but never makes a user operation
fail as busy. A user operation waits for it (_acquire_run_lock), and the
poll skips its remaining read while one waits.
"""
import time

from . import analysis_query
from .connector import ConnectorError
from .locks import SYSTEM_NOW, blocking_transactions_query
from .server_info import parse_version_tuple

TAG = "/*myflames:editor*/ "
HISTORY_LIMIT = 1000
BLOCKING_LIMIT = 500
#: How long a user operation waits for a meter poll that holds the session:
#: the poll's read in flight (one helper, at most editor_session.HELPER_TIMEOUT,
#: 30 s, after which the session ends) plus a margin; the poll sends no
#: further read once a user operation waits.
OBSERVATION_WAIT = 35.0
FIELDS = ("rows_examined", "rows_sent", "rows_affected", "no_index_used", "no_good_index_used",
          "created_tmp_tables", "created_tmp_disk_tables", "sort_merge_passes", "select_full_join",
          "select_scan", "lock_time", "timer_wait", "errors", "warnings")


def unsigned(value):
    """Keep BIGINT counters exact across JSON and JavaScript; NULL stays NULL."""
    text = str(value) if value is not None else ""
    return str(int(text)) if text and len(text) <= 20 and all("0" <= c <= "9" for c in text) else None


def helper_rows(step):
    if step.real_errors() or not step.marker:
        return []
    for entry in step.entries:
        if entry.get("type") == "rows":
            names = [c["name"].lower() for c in entry["columns"]]
            return [dict(zip(names, row)) for row in entry["rows"]]
    return []


def unavailable(reason, unit):
    return dict(analysis_eligibility(unit), available=False, reason=reason)


def analysis_eligibility(unit):
    """Project the sent SQL locally, independently of execution success."""
    prepared = analysis_query.prepare(unit["send"])
    return {"analyzable": not bool(prepared["problem"]),
            "analyze_reason": prepared["problem"]}


class EditorInsights(object):
    def _init_insights(self):
        self.transaction_meter = None
        self._transaction_seen_at = None
        self._meter_sampled_at = None
        self._meter_last_poll = None
        # run_lock is held by a meter poll, not a user operation (under self.lock).
        self._observing = False
        # User operations waiting for that poll to finish (under self.lock).
        self._users_waiting = 0

    def _acquire_run_lock(self, timeout=OBSERVATION_WAIT):
        """Take run_lock for a user operation (Run, Commit, Roll back, a plan,
        safe updates, the ALTER review). False means another user operation
        holds it, and the caller answers busy as before.

        A meter poll holding it is waited for, never reported as busy: the
        poll flips _observing and run_lock together under self.lock, so this
        check cannot mistake one for the other.
        """
        waiting = False
        deadline = time.monotonic() + timeout
        with self.cond:
            try:
                while True:
                    if self.run_lock.acquire(False):
                        return True
                    remaining = deadline - time.monotonic()
                    if not self._observing or remaining <= 0:
                        return False
                    if not waiting:
                        waiting = True
                        self._users_waiting += 1
                    self.cond.wait(min(0.5, remaining))
            finally:
                if waiting:
                    self._users_waiting -= 1

    def _user_waiting(self):
        with self.lock:
            return self._users_waiting > 0

    def _thread_expression(self):
        version = parse_version_tuple(self.server_version or "", leading=True, default=None)
        if self.engine == "mysql" and version is not None and version >= (8, 0, 16):
            return "PS_CURRENT_THREAD_ID()"
        return "(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID = CONNECTION_ID() LIMIT 1)"

    def _history_boundary(self, run):
        reason = "Statement history could not be read; check Server > Instruments."
        for unit in run.units:
            unit["server_stats"] = unavailable(reason, unit)
        thread = self._thread_expression()
        # MAX returns one row even when the ring is empty. Only this thread is read.
        sql = ("SELECT @@performance_schema AS performance_schema, "
               "(SELECT ENABLED FROM performance_schema.setup_consumers WHERE NAME = "
               "'events_statements_current') AS current_consumer, "
               "(SELECT ENABLED FROM performance_schema.setup_consumers WHERE NAME = "
               "'events_statements_history') AS history_consumer, "
               "(SELECT INSTRUMENTED FROM performance_schema.threads WHERE THREAD_ID = %s) AS instrumented, "
               "(SELECT COALESCE(MAX(EVENT_ID), 0) FROM performance_schema.events_statements_history "
               "WHERE THREAD_ID = %s) AS event_id" % (thread, thread))
        rows = helper_rows(self._helper(TAG + sql))
        if not rows:
            return None
        row = rows[0]
        if row.get("performance_schema") != "1":
            reason = "Performance Schema is off; check Server > Instruments."
        elif any(row.get(key) != "YES" for key in ("current_consumer", "history_consumer", "instrumented")):
            reason = "Statement history collection or this thread's instrumentation is off; check Server > Instruments."
        elif unsigned(row.get("event_id")) is not None:
            return int(row["event_id"])
        for unit in run.units:
            unit["server_stats"] = unavailable(reason, unit)
        return None

    def _collect_statement_history(self, run, boundary):
        if boundary is None or self.ended:
            return
        # Top-level statements can have a TRANSACTION parent. A stored-program
        # child can too if it starts a transaction newer than its caller: the
        # statement nesting level, available in 5.7 too, must also be zero.
        sql = ("SELECT EVENT_ID, LEFT(SQL_TEXT, 4096) AS sql_text, NESTING_EVENT_TYPE, NESTING_EVENT_LEVEL, " + ", ".join(name.upper() for name in FIELDS) +
               " FROM performance_schema.events_statements_history WHERE THREAD_ID = " + self._thread_expression() +
               " AND EVENT_ID > %d AND NESTING_EVENT_LEVEL = 0 AND (NESTING_EVENT_TYPE IS NULL OR NESTING_EVENT_TYPE <> 'STATEMENT')"
               " AND SQL_TEXT IS NOT NULL AND LEFT(SQL_TEXT, 20) <> '/*myflames:editor*/ '"
               " ORDER BY EVENT_ID DESC LIMIT %d" % (boundary, HISTORY_LIMIT))
        rows = helper_rows(self._helper(TAG + sql))
        self._attribute_history(run, rows, boundary)

    def _attribute_history(self, run, rows, boundary):
        units = [u for u in run.units if u.get("status") not in (None, "pending", "running", "not_run")]
        # Validate again at the parsing boundary. A hostile or stale answer
        # must not attach a helper, a nested statement or an old event to SQL.
        rows = [r for r in rows if unsigned(r.get("event_id")) is not None
                and int(r["event_id"]) > boundary and r.get("nesting_event_type") != "STATEMENT"
                and unsigned(r.get("nesting_event_level")) == "0"
                and r.get("sql_text") and not r["sql_text"].startswith(TAG)]
        rows.sort(key=lambda row: int(row["event_id"]))
        ids = [r["event_id"] for r in rows]
        if len(set(ids)) != len(ids):
            return
        exact = lambda unit, row: unit.get("single", True) and unit["send"].strip() == row["sql_text"].strip()
        pairs = []
        if len(units) == len(rows) and all(exact(u, r) for u, r in zip(units, rows)):
            pairs = list(zip(units, rows))
        else:
            # Lost/truncated SQL, disabled instruments and stored procedures
            # can leave holes. Only a unique full-text match is attributable.
            for row in rows:
                matches = [u for u in units if exact(u, row)]
                if len(matches) == 1 and sum(exact(matches[0], r) for r in rows) == 1:
                    pairs.append((matches[0], row))
        for unit in units:
            unit["server_stats"] = unavailable("This statement was not retained unambiguously in the thread's history; check Server > Instruments.", unit)
        for unit, row in pairs:
            stats = {key: unsigned(row.get(key)) for key in FIELDS}
            if any(stats[key] is None for key in FIELDS if key != "timer_wait"):
                continue
            unit["server_stats"] = dict(stats, available=True, reason=None, event_id=unsigned(row["event_id"]),
                                        **analysis_eligibility(unit))

    def _refresh_transaction_meter(self, observation=False):
        """Sample the open transaction. *observation* (a poll) stops before
        any read while a user operation waits, and keeps the previous sample:
        that operation samples again when it ends (refresh_state)."""
        now = time.monotonic()
        with self.lock:
            if self.in_transaction is not True or self.ended:
                self.transaction_meter = None
                self._transaction_seen_at = None
                return
            if self._transaction_seen_at is None:
                self._transaction_seen_at = now
            observed = max(0, int(now - self._transaction_seen_at))
        meter = {"age_seconds": observed, "age_source": "observed", "rows_modified": None,
                 "rows_locked": None, "lock_structs": None, "blocking": None, "blocking_truncated": False,
                 "note": "Age is measured since this tab first observed the transaction."}
        if observation and self._user_waiting():
            return
        # trx_started is the OS zone's local time (M/storage/innobase/handler/
        # i_s.cc:325-348, Ma i_s.cc:211-235), NOW() the session time_zone's
        # (M/sql/item_timefunc.cc:2462, Ma item_timefunc.cc:2239-2244). Compare
        # both in the OS zone without changing the session's time_zone.
        step = self._helper(TAG +
            "SELECT trx_id, GREATEST(0, TIMESTAMPDIFF(SECOND, trx_started, " + SYSTEM_NOW + ")) AS age_seconds, "
            "trx_rows_modified AS rows_modified, trx_rows_locked AS rows_locked, trx_lock_structs AS lock_structs "
            "FROM information_schema.INNODB_TRX WHERE trx_mysql_thread_id = CONNECTION_ID() LIMIT 1")
        rows = helper_rows(step)
        if rows:
            row = rows[0]
            age = unsigned(row.get("age_seconds"))
            if age is not None:
                meter.update(age_seconds=int(age), age_source="innodb")
            for key in ("rows_modified", "rows_locked", "lock_structs"):
                meter[key] = unsigned(row.get(key))
            meter["note"] = None
            trx = unsigned(row.get("trx_id"))
            if trx is not None:
                if observation and self._user_waiting():
                    return
                query = blocking_transactions_query(self.engine, parse_version_tuple(self.server_version or ""),
                                                    trx, self._thread_expression(), BLOCKING_LIMIT + 1)
                step = self._helper(TAG + query)
                if step.real_errors() or not step.marker:
                    meter["note"] = "Blocking sessions could not be read; open Server > Locks to check."
                else:
                    waits = {unsigned(row.get("waiting_id")) for row in helper_rows(step)} - {None}
                    meter["blocking"] = min(len(waits), BLOCKING_LIMIT)
                    meter["blocking_truncated"] = len(waits) > BLOCKING_LIMIT
        else:
            denied = any(error.get("code") in (1142, 1227) for error in step.real_errors())
            meter["note"] = (("PROCESS is required to read InnoDB transaction details; " if denied else
                              "No InnoDB transaction details were available; ") +
                             "age is measured since this tab first observed it.")
        with self.lock:
            self.transaction_meter = meter
            self._meter_sampled_at = time.monotonic()

    def refresh_transaction_meter(self):
        """Poll only an idle open transaction; never queue behind user SQL.

        Holding run_lock protects the session pipe; _observing marks the hold
        as an observation, so user operations wait for it instead of being
        refused. Polls do not renew the editor's idle timeout, and duplicate
        requests cost no database reads.
        """
        with self.lock:
            now = time.monotonic()
            if self._users_waiting or (self._meter_last_poll is not None and now - self._meter_last_poll < 10):
                return
            if not self.run_lock.acquire(False):
                return
            self._observing = True
            self._meter_last_poll = now
        try:
            self._refresh_transaction_meter(observation=True)
        except ConnectorError:
            pass  # the session ended while reading; its status says so
        finally:
            with self.lock:
                self._observing = False
                self.run_lock.release()
                self.cond.notify_all()

    def _transaction_meter_status(self):
        if self.transaction_meter is None:
            return None
        meter = dict(self.transaction_meter)
        meter["age_seconds"] += max(0, int(time.monotonic() - self._meter_sampled_at))
        return meter
