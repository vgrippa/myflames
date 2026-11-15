// Exercise the production pure functions without a browser or server.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

function productionFunctions(file, names, dependencies = {}, component) {
  const source = ts.createSourceFile(file, readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const statements = component ? source.statements.find(s => ts.isFunctionDeclaration(s) && s.name?.text === component)?.body?.statements : source.statements;
  const declarations = statements.filter(s => ts.isFunctionDeclaration(s) && names.includes(s.name?.text));
  assert.equal(declarations.length, names.length, "every requested production function was found");
  const javascript = ts.transpileModule(declarations.map(s => s.getText(source).replace(/^export /, "")).join("\n"),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  return new Function(...Object.keys(dependencies), `${javascript}\nreturn { ${names.join(",")} };`)(...Object.values(dependencies));
}

const locks = productionFunctions("Locks.tsx", ["killSessionState", "outcomeOf", "list", "outcomeText",
  "reconcileTransaction", "reconcileKillState", "reconcileKillNote", "killTransactionEvidence", "killPollDone",
  "killHeld", "killRollingBack", "killStateLine", "killEvidence", "clipStatement", "locksReadTruncated",
  "completeLockTransactions", "locksCanRecheck", "killReportMatches", "manualKillRefresh"],
{ number: n => n.toLocaleString("en-US") });
const sessions = productionFunctions("Sessions.tsx", ["sessionOutcome", "completeSessionList", "sessionsCanRecheck", "reconcileKillNote"], locks);
const before = { trx_id: "17", started: "2026-11-13 12:00:00", rows_locked: 2 };
const identity = { server_uuid: "reviewed-server" };
const answeredAt = Date.parse("2026-11-13T12:00:01Z");
const readAt = "2026-11-13T12:00:02Z";
const note = {
  answeredAt, reviewedServerIdentity: identity,
  status: "killed", mode: "query", connectionId: 41, message: "Original server message.",
  target: { connection_id: 41, info: "UPDATE t SET v = 1", trx_checked: true, trx: before },
  after: { present: true, command: "Query", trx_state: "ROLLING BACK", same_trx: true },
  outcome: [{ id: 42, waiting: true, blockedBy: [41], ddl: null, likely: false }], rollbackStale: true,
};
const emptyLocks = { scope: "all", trx_checked: true, transactions: [], blocking_tree: [], ddl_waits: [],
  captured_at: readAt, server_identity: identity,
  capabilities: { lock_waits: true, metadata_locks: true, data_locks_global_latch: false },
  metadata_locks: { locks: [], waits: [], truncated: false, waits_truncated: false } };
const fresh = { ...emptyLocks,
  transactions: [{ ...before, connection_id: 41, state: "RUNNING", rows_modified: 2, command: "Sleep", current_statement: null }],
  blocking_tree: [{ connection_id: 41, command: "Query", query: null, blocked: [{ connection_id: 42, blocked: [] }] }],
};
const cell = (n, label) => locks.killEvidence(n).find(r => r.label === label).after;
const original = structuredClone(note);
const finished = locks.reconcileKillNote(note, fresh);
assert.deepEqual(note, original, "reconciliation is pure");
assert.equal(finished.message, note.message);
assert.equal(finished.after.command, "Query", "session command takes precedence over transaction projection");
assert.equal(finished.after.transactionEvidence, "same");
assert.equal(finished.rollbackLeft, 2);
assert.equal(finished.rollbackStale, false);
assert.equal(locks.killRollingBack(finished), false);
assert.equal(locks.killStateLine(finished).text, "Query ended; transaction and locks still open.");
assert.equal(finished.update, "Checked again: its transaction is still open.");
assert.equal(locks.killHeld(finished), true);
assert.equal(locks.killHeld({ ...finished, outcome: [] }), true, "an open transaction stays amber without tracked waiters");
const initialRunning = { ...note, after: { present: true, command: "Query", trx_checked: true,
  trx_started: before.started, trx_state: "RUNNING", same_trx: true, statement: note.target.info } };
assert.equal(locks.killStateLine(initialRunning), null, "initial verification must also prove statement completion");
assert.equal(locks.killHeld(initialRunning), true);
assert.equal(locks.killPollDone(finished), true, "same open transaction needs no rollback polling");
const stillRunning = locks.reconcileKillNote(note, { ...fresh,
  blocking_tree: [{ ...fresh.blocking_tree[0], query: note.target.info }] });
assert.equal(stillRunning.after.same_trx, true);
assert.equal(locks.killStateLine(stillRunning), null, "same running statement does not prove Query ended");
const replaced = locks.reconcileKillNote(note, { ...fresh, transactions: [{ ...fresh.transactions[0], trx_id: "18" }] });
assert.equal(replaced.after.same_trx, false, "replacement within the same second");
assert.equal(replaced.update, "Checked again: it has started another transaction.");
const unidentified = locks.reconcileKillNote(note, { ...fresh, transactions: [{ ...fresh.transactions[0], started: null }] });
assert.equal(unidentified.after.transactionEvidence, "open");
assert.equal(locks.killHeld(unidentified), true);
const rolling = locks.reconcileKillNote(note, { ...fresh,
  transactions: [{ ...fresh.transactions[0], state: "ROLLING BACK", rows_modified: 0 }] });
assert.equal(rolling.after.trx_state, "ROLLING BACK");
assert.equal(rolling.update, "Checked again: still rolling back, about 0 changes left to undo.");
assert.equal(locks.killStateLine(rolling).text, "Rolling back: about 0 changes left to undo.");
assert.equal(locks.killPollDone(rolling), false);
const none = locks.reconcileKillNote({ ...rolling, mode: "connection" }, emptyLocks);
assert.equal(none.after.connectionEvidence, "unknown", "Locks never proves disconnect");
assert.equal(none.after.transactionEvidence, "none");
assert.equal(none.update, "Checked again: its transaction is no longer open.");
assert.equal(cell(none, "Transaction"), "rolled back");
assert.equal(cell(none, "Statement"), "unknown");
assert.equal(locks.killHeld(none), false);
assert.equal(cell(locks.reconcileKillNote({ ...note, after: null }, emptyLocks), "Transaction"), "none");
for (const partial of [{ scope: "waits" }, { scope: "deadlock" }, { trx_checked: false },
  { trx_checked: undefined }, { waits_truncated: true }, { blocking_tree_budget: { truncated: true } }]) {
  const unknown = locks.reconcileKillNote(rolling, { ...emptyLocks, ...partial });
  assert.equal(unknown.after.transactionEvidence, "unknown", JSON.stringify(partial));
  assert.equal(unknown.update, "Checked again: this list cannot show whether its transaction is still open.");
  assert.equal(locks.killPollDone(unknown), false, "unknown remains an open question even when this view cannot poll");
  assert.equal(unknown.rollbackStale, false, "unknown is not observed rollback");
  assert.equal(locks.killHeld(unknown), true);
  assert.equal(cell(unknown, "Transaction"), "unknown");
}
for (const connection_id of [0, null]) {
  const xa = locks.reconcileKillNote(note, { ...emptyLocks, transactions: [{ ...before, connection_id, rows_locked: 7 }] });
  assert.equal(xa.after.transactionEvidence, "survived");
  assert.equal(xa.update, "Checked again: its transaction is still open as a prepared XA transaction.");
  assert.equal(locks.killHeld(xa), true);
  assert.equal(locks.killStateLine(xa).text, "Prepared XA transaction and locks still open.");
  const later = locks.reconcileKillNote(xa, fresh);
  assert.equal(later.after.trx_survived, true, "a reused connection id cannot disprove detached XA survival");
  assert.equal(later.after.rows_locked, 7);
}
assert.equal(locks.reconcileKillNote(note, { ...emptyLocks,
  transactions: [{ ...before, trx_id: "999", connection_id: 0 }] }).after.transactionEvidence, "none");
const report = { taken_at: readAt, server_identity: identity, sessions: [], filters: { active_only: true }, capabilities: { process_privilege: true }, truncated: false };
const hidden = sessions.reconcileKillNote(note, report);
assert.equal(hidden.outcome[0].unknown, true);
assert.equal(locks.outcomeText(hidden.outcome[0]), "Connection 42: this read cannot show whether it still waits.");
assert.equal(hidden.after.connectionEvidence, "unknown");
assert.equal(hidden.after.transactionEvidence, "unknown");
assert.equal(locks.killPollDone(hidden), false);
assert.equal(locks.killHeld(hidden), true);
assert.equal(locks.killStateLine(hidden), null);
const hiddenAfterGone = sessions.reconcileKillNote({ ...note, after: { present: false } }, report);
assert.equal(hiddenAfterGone.after.connectionEvidence, "unknown");
assert.equal(locks.killHeld(hiddenAfterGone), true);
for (const filters of [{ active_only: true }, { user: "app" }, { host: "client" }, { db: "db" }, { min_seconds: 30 }, { locked_only: true }]) {
  assert.equal(sessions.sessionOutcome({ ...report, filters }, [42])[0].unknown, true);
  assert.equal(sessions.reconcileKillNote(note, { ...report, filters }).after.connectionEvidence, "unknown");
}
const edge = sessions.sessionOutcome({ ...report, blocking_edges: [{ waiter: 42, blocker: 99 }] }, [42])[0];
assert.equal(edge.waiting, true); assert.deepEqual(edge.blockedBy, [99]);
assert.equal(sessions.sessionOutcome({ ...report, blocking_edges: [] }, [42])[0].unknown, true);
const full = { ...report, filters: { active_only: false }, trx_checked: true };
for (const partial of [{ truncated: true }, { capabilities: { process_privilege: false } }]) {
  assert.equal(sessions.sessionOutcome({ ...full, ...partial }, [42])[0].unknown, true);
  assert.equal(sessions.reconcileKillNote(note, { ...full, ...partial }).after.connectionEvidence, "unknown");
}
const gone = sessions.reconcileKillNote(note, full);
assert.equal(gone.after.connectionEvidence, "gone");
assert.equal(gone.update, "Checked again: the connection has ended.");
assert.equal(locks.killStateLine(gone).tone, "ok");
assert.equal(cell(gone, "Transaction"), "rolled back");
const sessionRunning = { ...full, sessions: [{ id: 41, command: "Query", info: note.target.info,
  state: "updating", trx: { ...before, id: "17", state: "RUNNING" } }] };
const runningSession = sessions.reconcileKillNote(note, sessionRunning);
assert.equal(runningSession.after.same_trx, true);
assert.equal(locks.killStateLine(runningSession), null);
assert.equal(runningSession.after.command, "Query"); assert.equal(runningSession.after.state, "updating");
const killedCommand = { ...sessionRunning, sessions: [{ ...sessionRunning.sessions[0], command: "Killed" }] };
assert.equal(locks.killRollingBack(sessions.reconcileKillNote(note, killedCommand)), true);
assert.equal(locks.killRollingBack(sessions.reconcileKillNote(note, { ...killedCommand,
  sessions: [{ ...killedCommand.sessions[0], trx: null }] })), false);
const unchecked = sessions.reconcileKillNote(note, { ...killedCommand, trx_checked: false });
assert.equal(unchecked.after.transactionEvidence, "unknown");
assert.equal(locks.killPollDone(unchecked), false);
assert.doesNotMatch(unchecked.update, /finished|no longer open|ended/);
assert.equal(cell(unchecked, "Rows locked"), "unknown");
sessionRunning.sessions[0].trx.id = "18";
assert.equal(sessions.reconcileKillNote(note, sessionRunning).after.same_trx, false);
const sessionNone = sessions.reconcileKillNote(note, { ...full, sessions: [{ id: 41, trx: null }] });
assert.equal(sessionNone.after.transactionEvidence, "none");
assert.equal(sessionNone.update, "Checked again: its transaction is no longer open.");
const unavailable = { ...note, after: null, outcome: [] };
assert.ok(locks.killEvidence(unavailable).every(r => r.after === "unknown"));
assert.equal(locks.killHeld(unavailable), true); assert.equal(locks.killStateLine(unavailable), null);
assert.equal(locks.killHeld({ ...gone, outcome: [{ ...note.outcome[0], waiting: false, unknown: true }] }), true);
assert.equal(locks.killHeld({ ...gone, outcome: [{ ...note.outcome[0], waiting: true }] }), true);
assert.equal(locks.killHeld({ ...gone, outcome: [] }), false);
assert.ok(readFileSync(new URL("../src/Locks.tsx", import.meta.url), "utf8").includes('killHeld(note) ? " held" : ""'));

// Test the bound-report hook's acceptance, identity guard and commit acknowledgement.
function hookHarness() {
  const refs = [], deps = [], pending = [], cleanups = [];
  let refIndex = 0, effectIndex = 0;
  const { useKillReport } = productionFunctions("Locks.tsx", ["useKillReport"], {
    useRef: value => refs[refIndex++] ||= { current: value },
    useLayoutEffect: (callback, next) => {
      const i = effectIndex++;
      if (!deps[i] || next.some((v, j) => v !== deps[i][j])) { deps[i] = next; pending.push(callback); }
    },
  });
  return {
    render: (...args) => { refIndex = effectIndex = 0; return useKillReport(...args); },
    commit: () => { for (const effect of pending.splice(0)) { const cleanup = effect(); if (cleanup) cleanups.push(cleanup); } },
    unmount: () => cleanups.forEach(cleanup => cleanup()),
  };
}
const hook = hookHarness();
const current = { current: { ...note, on: { key: "server", label: "Server" } } };
const put = value => { current.current = value; };
let bound = { key: "server", report: fresh };
let readCommitted = hook.render(bound, current, put, locks.reconcileKillNote); hook.commit();
let settled = false;
const reading = readCommitted(async () => emptyLocks).then(() => { settled = true; });
await Promise.resolve(); assert.equal(settled, false);
bound = { key: "server", report: emptyLocks };
readCommitted = hook.render(bound, current, put, locks.reconcileKillNote); hook.commit();
await reading; assert.equal(current.current.after.transactionEvidence, "none");
// Refresh all directly supplies a bound report, without going through the loader.
bound = { key: "server", report: fresh };
hook.render(bound, current, put, locks.reconcileKillNote); hook.commit();
assert.equal(current.current.after.transactionEvidence, "same");
await readCommitted(async () => fresh);
const retained = current.current;
hook.render({ key: "other", report: emptyLocks }, current, put, locks.reconcileKillNote); hook.commit();
assert.equal(current.current, retained);
hook.render({ key: "server", report: emptyLocks }, current, put, locks.reconcileKillNote);
current.current = { ...retained, message: "Replacement" };
const replacement = current.current; hook.commit(); assert.equal(current.current, replacement);
assert.equal(await readCommitted(async () => null), null, "superseded reads need no commit acknowledgement");
const unmountedRead = readCommitted(async () => fresh);
await Promise.resolve(); hook.unmount(); await unmountedRead;

// An incomplete step waits for the next scheduled delay rather than ending polling.
const timers = [];
const follow = productionFunctions("Locks.tsx", ["followKill"], {
  KILL_FOLLOW_UP_S: [2, 4, 8, 16], window: { setTimeout: callback => (timers.push(callback), timers.length), clearTimeout: () => {} },
});
let reads = 0;
const stop = follow.followKill(async () => ++reads > 1);
timers.shift()(); await Promise.resolve(); assert.equal(timers.length, 1);
timers.shift()(); await Promise.resolve(); assert.equal(reads, 2); assert.equal(timers.length, 0); stop();
// 1. Processlist absence cannot resolve a failed detached XA lookup.
const failedXa = { ...note, mode: "connection", after: { present: false, trx_survival_checked: false } };
const xaUnknown = sessions.reconcileKillNote(failedXa, full);
assert.equal(xaUnknown.after.connectionEvidence, "gone");
assert.equal(xaUnknown.after.transactionEvidence, "unknown");
assert.equal(cell(xaUnknown, "Transaction"), "unknown");
assert.equal(locks.killHeld(xaUnknown), true, "unknown XA evidence keeps the note amber");
assert.equal(xaUnknown.update, "Checked again: the connection has ended; whether its transaction survived as a prepared XA transaction is unknown.");
assert.equal(sessions.reconcileKillNote(xaUnknown, full).after.transactionEvidence, "unknown", "uncertainty survives repeated Processlist reads");
assert.equal(sessions.reconcileKillNote(failedXa, sessionRunning).after.transactionEvidence, "unknown", "a reused connection cannot settle detached XA");
assert.equal(locks.reconcileKillNote(xaUnknown, emptyLocks).after.transactionEvidence, "none", "a complete transaction-id read can resolve XA uncertainty");
assert.equal(locks.reconcileKillNote(xaUnknown, fresh).after.transactionEvidence, "same", "a complete id read can also prove the original transaction remains attached");

// 2. Only complete wait sources prove absence; positive transaction state proves a wait.
for (const partial of [
  { capabilities: { ...emptyLocks.capabilities, lock_waits: false } },
  { capabilities: { ...emptyLocks.capabilities, metadata_locks: false } },
  { capabilities: {} }, { scope: "deadlock" }, { waits_truncated: true }, { row_locks_truncated: true },
  { blocking_tree_budget: { truncated: true } },
  { metadata_locks: { waits: [], truncated: true } },
  { metadata_locks: { waits: [], waits_truncated: true } },
  { metadata_locks: { waits: [{ truncated: true }] } },
  { ddl_waits: [{ connection_id: 99, blockers: [], blockers_truncated: true }] },
]) {
  const partialRead = { ...emptyLocks, ...partial };
  const outcome = locks.outcomeOf(partialRead, [42], new Map())[0];
  assert.equal(outcome.unknown, true, JSON.stringify(partial));
  assert.equal(locks.outcomeText(outcome), "Connection 42: this read cannot show whether it still waits.");
  const held = locks.reconcileKillNote(note, partialRead);
  assert.equal(locks.killHeld(held), true);
  assert.notEqual(locks.killStateLine(held)?.tone, "ok");
  const positive = locks.outcomeOf({ ...partialRead, transactions: [{ connection_id: 42, state: "LOCK WAIT" }] }, [42], new Map())[0];
  assert.equal(positive.waiting, true);
  assert.equal(positive.unknown, false, "positive LOCK WAIT wins over incomplete sources");
}
assert.equal(locks.outcomeOf(emptyLocks, [42], new Map())[0].unknown, false);
assert.equal(locks.outcomeOf(emptyLocks, [42], new Map())[0].waiting, false);

// 3. Keep confirmed XA until a complete Locks read no longer lists its transaction id.
const survived = locks.reconcileKillNote(note, { ...emptyLocks, transactions: [{ ...before, connection_id: 0, rows_locked: 7 }] });
for (const partial of [{ scope: "waits" }, { trx_checked: false }, { waits_truncated: true },
  { row_locks_truncated: true }, { blocking_tree_budget: { truncated: true } },
  { metadata_locks: { waits: [], truncated: true } }]) {
  const retainedXa = locks.reconcileKillNote(survived, { ...emptyLocks, ...partial });
  assert.equal(retainedXa.after.trx_survived, true, JSON.stringify(partial));
  assert.equal(retainedXa.after.rows_locked, 7);
}
assert.equal(sessions.reconcileKillNote(survived, full).after.trx_survived, true);
const clearedXa = locks.reconcileKillNote(survived, emptyLocks);
assert.equal(clearedXa.after.trx_survived, false);
assert.equal(clearedXa.after.transactionEvidence, "none");
assert.equal(cell(clearedXa, "Transaction"), "none", "XA may have committed; do not claim rollback");
assert.equal(clearedXa.update, "Checked again: its prepared XA transaction is no longer listed.");
assert.equal(locks.killHeld(clearedXa), false);
assert.equal(locks.reconcileKillNote(survived, { ...fresh, transactions: [{ ...fresh.transactions[0], trx_id: "18" }] }).after.transactionEvidence,
  "none", "a replacement transaction does not keep the old XA evidence alive");

// 4. The same production predicates gate both the first read and every timer.
for (const partial of [{ scope: "waits" }, { scope: "deadlock" }, { trx_checked: false }, { waits_truncated: true },
  { row_locks_truncated: true }, { metadata_locks: { waits: [], truncated: true } },
  { blocking_tree_budget: { truncated: true } },
  { capabilities: { ...emptyLocks.capabilities, data_locks_global_latch: true } }]) {
  const cannotCheck = { ...emptyLocks, ...partial };
  assert.equal(locks.locksCanRecheck(cannotCheck), false, JSON.stringify(partial));
  const manual = locks.reconcileKillNote(rolling, { ...fresh, ...partial,
    transactions: [{ ...fresh.transactions[0], state: "ROLLING BACK" }] });
  assert.equal(manual.refresh, true);
  assert.equal(manual.manualRefresh, true);
}
assert.equal(locks.locksCanRecheck(emptyLocks), true);
assert.equal(sessions.sessionsCanRecheck(full, rolling), true);
for (const partial of [report, { ...full, truncated: true }, { ...full, trx_checked: false },
  { ...full, filters: { active_only: false, min_seconds: 5 } }]) {
  assert.equal(sessions.sessionsCanRecheck(partial, rolling), false);
}
assert.equal(sessions.sessionsCanRecheck(full, xaUnknown), false, "Processlist cannot settle detached XA");
assert.ok(readFileSync(new URL("../src/Locks.tsx", import.meta.url), "utf8").includes('Refresh to check again.</p>'));

// Exercise the actual confirmation handlers, not a copy of their timer guards.
function confirmationHarness(view, initialReport, postRead = initialReport, answer = null) {
  let reportForCheck = initialReport, reads = 0;
  const scheduled = [], current = { current: null };
  const put = n => { current.current = n; };
  const target = { connection_id: 41, server_identity: identity, waiting: [42], via: { key: "server", label: "Server" } };
  const locksView = view === "Locks";
  const reconcile = locksView ? locks.reconcileKillNote : sessions.reconcileKillNote;
  const dependencies = {
    Date: { now: () => answeredAt },
    killTarget: target, target, report: initialReport, killing: false, connection: {}, policy: {},
    blockingOf: () => ({ ids: [42] }), outcomeOf: locks.outcomeOf,
    setKilling: () => {}, killSentAt: { current: 0 },
    // A kill-1.0 answer: sendKill sets refresh only for a refusal, never for a
    // kill that was sent (the fixture note's own Refresh flag is not an answer field).
    sendKill: async () => ({ ...(answer || rolling), refresh: false, on: { key: "server", label: "Server" } }),
    killMounted: { current: true }, reads: { currentKey: () => "server" },
    canRecheckKill: { current: (n, requireFresh = true) => locksView
      ? locks.locksCanRecheck(reportForCheck) && (!n || locks.killReportMatches(n, reportForCheck.captured_at, reportForCheck.server_identity))
      : sessions.sessionsCanRecheck(reportForCheck, n) && (!requireFresh || locks.killReportMatches(n, reportForCheck.taken_at, reportForCheck.server_identity)) },
    setKillTarget: () => {}, setTarget: () => {}, setKillLeft: () => {}, setKillNote: put, setNote: put,
    manualKillRefresh: locks.manualKillRefresh,
    window: { setTimeout: () => 0 }, killNoteRef: { current: null }, noteRef: { current: null },
    reportReconciled: async read => {
      const report = await read();
      put(reconcile(current.current, report));
      return report;
    },
    loadRef: { current: async () => { reads++; reportForCheck = postRead; return postRead; } },
    currentKillNote: current, killPollDone: locks.killPollDone, killRollingBack: locks.killRollingBack,
    cancelKillFollow: () => {}, killFollowCancel: { current: null },
    followKill: step => { scheduled.push(step); return () => {}; },
  };
  const { confirmKill } = productionFunctions(`${view}.tsx`, ["confirmKill"], dependencies, `${view}View`);
  return { confirm: () => confirmKill("connection", "reviewed"), scheduled, current,
    setReport: value => { reportForCheck = value; }, reads: () => reads };
}
const rollbackLocks = { ...fresh, transactions: [{ ...fresh.transactions[0], state: "ROLLING BACK" }] };
const rollbackSessions = { ...sessionRunning, sessions: [{ ...sessionRunning.sessions[0], command: "Killed" }] };
for (const [view, complete, incomplete] of [
  ["Locks", rollbackLocks, [
    { ...rollbackLocks, scope: "waits" }, { ...rollbackLocks, trx_checked: false },
    { ...rollbackLocks, waits_truncated: true }, { ...rollbackLocks, blocking_tree_budget: { truncated: true } },
    { ...rollbackLocks, capabilities: { ...rollbackLocks.capabilities, data_locks_global_latch: true } },
  ]],
  ["Sessions", rollbackSessions, [
    { ...rollbackSessions, filters: { active_only: true } }, { ...rollbackSessions, truncated: true },
    { ...rollbackSessions, trx_checked: false },
  ]],
]) {
  for (const rejected of [
    { ...complete, captured_at: "2026-11-13T12:00:00Z", taken_at: "2026-11-13T12:00:00Z" },
    { ...complete, server_identity: { server_uuid: "other-server" } },
  ]) {
    const ignored = confirmationHarness(view, complete, rejected);
    await ignored.confirm();
    assert.equal(ignored.scheduled.length, 0, `${view}: stale or other-server reads cannot start polling`);
    assert.equal(ignored.current.current.refreshing, false, `${view}: ignored evidence does not leave a spinner`);
    assert.equal(ignored.current.current.after.trx_state, "ROLLING BACK", "the kill answer's evidence stays intact");
  }
  for (const partial of incomplete) {
    const initiallyBlocked = confirmationHarness(view, partial);
    await initiallyBlocked.confirm();
    // The view is read once after the answer (its table shows the change, as
    // before); an incapable view schedules no timed follow-ups and keeps Refresh.
    assert.equal(initiallyBlocked.reads(), 1, `${view}: one post-kill read, as before`);
    assert.equal(initiallyBlocked.scheduled.length, 0, `${view}: no timed follow-ups for an incapable view`);
    assert.equal(initiallyBlocked.current.current.manualRefresh, true);
    assert.equal(initiallyBlocked.current.current.refresh, true);
    assert.equal(initiallyBlocked.current.current.answeredAt, answeredAt);
    assert.deepEqual(initiallyBlocked.current.current.reviewedServerIdentity, identity);
    const becomesPartial = confirmationHarness(view, complete, partial);
    await becomesPartial.confirm();
    assert.equal(becomesPartial.reads(), 1);
    assert.equal(becomesPartial.scheduled.length, 0, `${view}: the first read's incomplete result prevents scheduling`);
    // A refused, denied, gone or failed answer is read once and never polled.
    for (const status of ["denied", "gone", "error"]) {
      const notRun = confirmationHarness(view, complete, complete, { ...rolling, status, after: null });
      await notRun.confirm();
      assert.equal(notRun.reads(), 1, `${view}: a ${status} answer still gets the one post-answer read`);
      assert.equal(notRun.scheduled.length, 0, `${view}: a ${status} answer schedules no timed follow-ups`);
    }
    const pendingTimer = confirmationHarness(view, complete);
    await pendingTimer.confirm();
    assert.equal(pendingTimer.scheduled.length, 1, `${view}: complete rollback can schedule a follow-up`);
    pendingTimer.setReport(partial);
    assert.equal(await pendingTimer.scheduled[0](false), true, `${view}: a scope/capability change stops the timer`);
    assert.equal(pendingTimer.reads(), 1, `${view}: the stopped timer sends no additional read`);
    assert.equal(pendingTimer.current.current.manualRefresh, true);
  }
}

// 5. Ignore stale, simultaneous, unidentifiable and other-server reports, even when keys match.
for (const timestamp of ["2026-11-13T12:00:00Z", "2026-11-13T12:00:01Z", "invalid", undefined]) {
  assert.equal(locks.reconcileKillNote(note, { ...emptyLocks, captured_at: timestamp }), note);
  assert.equal(sessions.reconcileKillNote(note, { ...full, taken_at: timestamp }), note);
}
for (const server_identity of [undefined, { server_uuid: "another-server" },
  { server_id: "1", hostname: "reviewed-server", version: "11.4" }]) {
  assert.equal(locks.reconcileKillNote(note, { ...emptyLocks, server_identity }), note);
  assert.equal(sessions.reconcileKillNote(note, { ...full, server_identity }), note);
}
assert.equal(locks.reconcileKillNote({ ...note, answeredAt: undefined }, emptyLocks).after, note.after);
assert.equal(locks.killReportMatches({ ...note, reviewedServerIdentity: undefined }, readAt, identity), false);
const mariaIdentity = { server_id: "1", hostname: "db", version: "11.4" };
const mariaNote = { ...note, reviewedServerIdentity: mariaIdentity };
assert.equal(locks.killReportMatches(mariaNote, readAt, { version: "11.4", hostname: "db", server_id: "1" }), true);
for (const changed of [{ server_id: "2" }, { hostname: "other" }, { version: "11.8" }])
  assert.equal(locks.killReportMatches(mariaNote, readAt, { ...mariaIdentity, ...changed }), false);
const guardedHook = hookHarness();
const guardedCurrent = { current: { ...note, on: { key: "server", label: "Server" } } };
const putGuarded = value => { guardedCurrent.current = value; };
const beforeGuard = guardedCurrent.current;
for (const rejected of [{ ...emptyLocks, captured_at: "2026-11-13T12:00:00Z" },
  { ...emptyLocks, server_identity: { server_uuid: "other" } }]) {
  guardedHook.render({ key: "server", report: rejected }, guardedCurrent, putGuarded, locks.reconcileKillNote); guardedHook.commit();
  assert.equal(guardedCurrent.current, beforeGuard, "the bound-report effect ignores stale or other-server evidence");
}
guardedHook.unmount();
console.log("Kill reconciliation unit tests passed.");
