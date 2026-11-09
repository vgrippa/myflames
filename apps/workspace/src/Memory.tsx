// Workload > Resources: tracked allocations, with honest collection coverage.
import { useEffect, useState, type ReactNode } from "react";
import { memoryReport, type Connection, type MemoryReport, type MemoryValue, type MemorySession, type MemoryAccount, type MemoryCategory, type MemoryEvent } from "./api";
import { useConnectionReads, type Bound } from "./binding";
import { useOpenRead } from "./viewOnScreen";
import { WINDOW_INTERVALS } from "./session";
import { bytes, WindowSelect } from "./PerfShared";
import { InstrumentsLink } from "./Instruments";
import { DataTable, Disclosure, Fields, InspectorSection, PrerequisiteGate, Segmented, StatusPill, SummaryLine, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type Selection } from "./kit";

type Group = "categories" | "sessions" | "accounts";
/** A session or account below 0 freed memory another thread allocated (MySQL
 * 5.7, 8.0 before 8.0.23 and MariaDB charge a free to the freeing thread):
 * never shown as a negative byte count. */
const BELOW_ZERO = "Below 0 (freed by another thread)";
const rate = (n: number | null) => n === null ? "Not collected" : `${n > 0 ? "+" : ""}${bytes(n)}/s`;
const value = (n: number | null, attributed = false) => n === null ? "Not collected" : attributed && n < 0 ? BELOW_ZERO : bytes(n);
const exact = (n: number | null, attributed = false) => n === null ? "Not collected" : attributed && n < 0 ? BELOW_ZERO : `${n.toLocaleString()} B`;
// Inspector values: binary units first, the exact bytes beside them in
// muted text (visible to keyboard and screen-reader users, not only on hover).
const byteField = (n: number | null, attributed = false) => n === null || (attributed && n < 0)
  ? value(n, attributed) : <><span className="memory-value">{value(n, attributed)}</span> <span className="cell-muted">{exact(n, attributed)}</span></>;
const RULE_NOTES = [
  "Rule of thumb: Growth triggers above both 1% of the starting total and 16 MiB across the window.",
  "Rule of thumb: The 25% rule judges only temporary tables, sort, join and hash buffers, and network.",
  "Rule of thumb: The 1 GiB session rule can be overstated: memory another thread freed can still count toward the session that allocated it.",
];
/** General rules are stated once in Notes; inspectors show row-specific reasons. */
export function memoryNotes(report: MemoryReport): string[] {
  return [...RULE_NOTES, ...report.findings.filter(f => !f.key.includes(":")).map(f => f.reason), ...report.notes];
}
const initialGroup = (report?: MemoryReport): Group => report && !report.collection.events && !report.categories.length && report.collection.sessions ? "sessions" : "categories";
const text = (s: string | null) => s === null ? <span className="cell-muted">NULL</span> : s === "" ? <span aria-label="empty string">''</span> : <bdi>{s}</bdi>;
const accountKey = (r: MemoryAccount) => JSON.stringify([r.user, r.host]);
const accountName = (r: MemoryAccount) => <>{text(r.user)}@{text(r.host)}</>;
const numbers = <T extends MemoryValue,>(attributed = false): Column<T>[] => [
  { key: "current", label: "Current", num: true, sort: r => r.current_bytes, render: r => <span title={r.collection_reason}>{value(r.current_bytes, attributed)}</span> },
  // Short headers keep the trailing columns in view at 720 px; the inspector names them in full.
  { key: "high", label: "Sum of highs", title: "Sum of event highs: upper bounds, not a recorded peak", num: true, sort: r => r.high_bytes, render: r => <span title={r.collection_reason}>{value(r.high_bytes, attributed)}</span> },
];
/** What a category holds beyond its name (engine facts: memory.py CATEGORIES). */
const CATEGORY_ABOUT: Record<string, string> = {
  join: "Join buffers (JOIN_CACHE, MySQL 5.7 and MariaDB) and hash join and BKA buffers (hash_join on MySQL 8.0, hashed_operation from 8.2). hashed_operation also serves other hashed operations, such as INTERSECT and EXCEPT.",
};

export function MemoryView({ connection, disabled, onBack, selection, interval, onIntervalChange, report: bound, onReport }: {
  connection: Connection; disabled: boolean; onBack?: () => void; selection: Selection;
  interval: number; onIntervalChange: (seconds: number) => void;
  report: Bound<MemoryReport> | null; onReport: (report: Bound<MemoryReport>) => void;
}) {
  const [group, setGroup] = useState<Group>(() => initialGroup(bound?.report));
  const reads = useConnectionReads(connection);
  const report = bound?.report;
  useEffect(() => {
    if (initialGroup(report) === "sessions") setGroup(current => current === "categories" ? "sessions" : current);
  }, [report]);
  const findingPill = (key: string) => {
    const finding = report?.findings.find(f => f.key === key);
    return finding ? <StatusPill tone={finding.status === "not_judged" ? "unknown" : finding.status}
      label={finding.status === "not_judged" ? "Not judged" : undefined} title={finding.reason} /> : null;
  };
  const other = bound && bound.key !== reads.key ? bound.label : null;
  async function load() {
    await reads.run(`memory:${interval}`, `Sampling memory for ${interval} s…`,
      (c, signal) => memoryReport(c, interval, signal), onReport);
  }
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled,
    busy: !!reads.loading || reads.running(), read: () => { void load(); } });
  const categoryColumns: Column<MemoryCategory>[] = [
    { key: "name", label: "Category", width: 180, sort: r => r.label, render: r => <span className="cell-name"><bdi>{r.label}</bdi>{findingPill(`category:${r.key}`)}</span> },
    ...numbers<MemoryCategory>(),
    { key: "share", label: "Share (%)", title: "Share of tracked memory (non-negative category bytes)", num: true, sort: r => r.share_pct, render: r => r.share_pct === null ? "Not collected" : `${r.share_pct.toFixed(1)}%` },
    { key: "growth", label: "Growth / s", num: true, sort: r => r.growth_bytes_per_s, render: r => rate(r.growth_bytes_per_s) },
  ];
  const sessionColumns: Column<MemorySession>[] = [
    { key: "id", label: "Session", num: true, sort: r => r.processlist_id, render: r => <span className="cell-name"><span>{r.processlist_id ?? "Unknown"}</span>{findingPill(`session:${r.processlist_id}`)}</span> },
    { key: "user", label: "User / host", flex: true, sort: r => `${r.user ?? ""}@${r.host ?? ""}`, render: accountName },
    ...numbers<MemorySession>(true).map(c => c.key === "high" && report?.sessions.some(r => r.source === "information_schema.PROCESSLIST") ? { ...c, label: "Session high" } : c),
  ];
  const accountColumns: Column<MemoryAccount>[] = [
    { key: "name", label: "Account", flex: true, sort: accountKey, render: accountName }, ...numbers<MemoryAccount>(true),
  ];
  const eventColumns: Column<MemoryEvent>[] = [
    { key: "name", label: "Event", flex: true, sort: r => r.event, render: r => <bdi><code>{r.event}</code></bdi> }, ...numbers<MemoryEvent>(),
  ];
  let selected: MemoryValue | undefined;
  let title: ReactNode = null;
  let what = "";
  let about: string[] = [];
  let attributed = false;
  if (report) {
    const category = report.categories.find(r => selection.key === `category:${r.key}`);
    const session = report.sessions.find(r => selection.key === `session:${r.processlist_id}`);
    const account = report.accounts.find(r => selection.key === `account:${accountKey(r)}`);
    const event = report.events.find(r => selection.key === `event:${r.event}`);
    selected = category || session || account || event;
    attributed = !!(session || account);
    if (category) {
      title = <bdi>{category.label}</bdi>; what = "A group of memory instruments identified by known event-name prefixes.";
      about = [CATEGORY_ABOUT[category.key], category.engine_note].filter((s): s is string => !!s);
    }
    if (session) { title = <>Session {session.processlist_id}: {accountName(session)}</>; what = "Tracked allocations attributed to this visible connection, not its process RSS; memory another thread freed can still count here."; }
    if (account) { title = accountName(account); what = "Tracked allocations attributed to this user and host account; memory another account's thread freed can still count here."; }
    if (event) { title = <bdi>{event.event}</bdi>; what = "An allocation instrument reported by the server."; }
  }
  const threads = report?.thread_coverage;
  const selectedFindings = report?.findings.filter(f => f.key === selection.key) ?? [];
  const inspector: InspectorContent | null = report && selected ? {
    key: selection.key, title,
    body: <>
      <InspectorSection title="What it is"><p>{what}</p>{about.map(note => <p key={note}><bdi>{note}</bdi></p>)}</InspectorSection>
      <InspectorSection title="Why it matters"><p>Compare repeated windows with the workload; a short increase alone does not prove a leak.</p></InspectorSection>
      <InspectorSection title="Numbers">{selected.collection_reason && <p><bdi>{selected.collection_reason}</bdi></p>}<Fields rows={[
        ["Current", byteField(selected.current_bytes, attributed)],
        [selected.source === "information_schema.PROCESSLIST" ? "Session high" : "Sum of event highs", byteField(selected.high_bytes, attributed)],
        ...("growth_bytes_per_s" in selected ? [["Growth", rate((selected as MemoryCategory).growth_bytes_per_s)] as [string, string]] : []),
        ...("elapsed_s" in selected ? [["Category window", (selected as MemoryCategory).elapsed_s === null ? "Not collected" : `${(selected as MemoryCategory).elapsed_s} s`] as [string, string]] : []),
        ...("thread_id" in selected ? [["Thread ID", (selected as MemorySession).thread_id ?? "Not collected"] as [string, number | string]] : []),
      ]} /></InspectorSection>
      <InspectorSection title="Server context"><Fields rows={[
        ["Tracked total", byteField(report.current_bytes)], ["Sum of event highs", byteField(report.high_bytes)],
        ["Total growth", rate(report.growth_bytes_per_s)],
        ["Window", report.elapsed_s === null ? "Not collected" : `${report.elapsed_s} s (${report.window_status})`],
        ["Buffer pool setting", byteField(report.innodb_buffer_pool_size)],
        ["Performance Schema tracked share", (() => { const share = report.categories.find(c => c.key === "performance_schema")?.share_pct; return share == null ? "Not collected" : `${share.toFixed(1)}%`; })()],
        ["Buffer pool tracked share", report.buffer_pool_share_pct === null ? "Not collected" : `${report.buffer_pool_share_pct.toFixed(1)}%`],
        ["Memory instruments enabled", report.coverage ? `${report.coverage.enabled} of ${report.coverage.total}` : "Not collected"],
        ["Foreground threads instrumented", threads ? `${threads.foreground - threads.uninstrumented} of ${threads.foreground}` : "Not collected"],
      ]} /></InspectorSection>
      {selectedFindings.length > 0 && <InspectorSection title="Rules of thumb">
        {selectedFindings.map(f => <p key={f.key}>{findingPill(f.key)} <bdi>{f.reason}</bdi></p>)}
      </InspectorSection>}
      <InspectorSection title="Source"><p><bdi><code>{selected.source}</code></bdi></p>
        <p>Shares use the sum of non-negative category bytes. Performance Schema has its own sizing settings; its share is context.</p>
        {selected.source !== "information_schema.PROCESSLIST" && <p>Each event high is an upper bound (current bytes plus the headroom of every thread, account and host), not a recorded peak, so their sum is not the server's peak usage. TRUNCATE of a memory summary table resets highs to current bytes.</p>}</InspectorSection>
    </>,
  } : null;
  return <ViewLayout id="memory" title="Memory" onBack={onBack} inspector={inspector}
    inspectorEmpty={<p>Select a category, session, account or event to inspect its numbers and source.</p>}
    info={<><p>Tracked allocator bytes, not process RSS or machine RAM. The window compares two samples; negative growth means allocations decreased.</p>
      <p>Event highs are upper bounds, not recorded peaks. Instruments enabled later miss earlier allocations; unknown prefixes remain in Other.</p></>}
    controls={<WindowSelect value={interval} options={WINDOW_INTERVALS} onChange={onIntervalChange} disabled={!!reads.loading} />}
    onRefresh={() => void load()} refreshDisabled={disabled} loading={!!reads.loading} otherConnection={other}
    status={<ViewStatus loading={reads.loading} error={reads.error} onStop={reads.stop} onRetry={reads.error ? () => void load() : undefined}
      doneAt={reads.doneAt} slowAfter={(interval + 30) * 1000} />}>
    {report && <>
      <SummaryLine tone={report.status === "not_judged" ? "unknown" : report.status} toneLabel={report.status === "not_judged" ? "Not judged" : undefined}
        notes={memoryNotes(report).map((n, i) => <bdi key={i}>{n}</bdi>)}>
        {value(report.current_bytes)} tracked · {report.high_bytes === null ? "sum of highs not collected" : `${value(report.high_bytes)} sum of highs`} · {rate(report.growth_bytes_per_s)}
      </SummaryLine>
      {report.gate && <PrerequisiteGate action={!other ? <InstrumentsLink row={report.gate_row ?? (report.performance_schema ? "group:memory" : "ps")}>Open Instruments</InstrumentsLink> : undefined}>
        <bdi>{report.gate}</bdi>
      </PrerequisiteGate>}
      <section className="view-section">
        <div className="view-section-head"><Segmented<Group> label="Group memory by" value={group} onChange={setGroup} options={[
          { value: "categories", label: "By category" }, { value: "sessions", label: "By session" }, { value: "accounts", label: "By account" },
        ]} /></div>
        {group === "categories" ? <DataTable id="memory.categories" label="Memory by category" columns={categoryColumns} rows={report.categories}
          rowKey={r => `category:${r.key}`} selection={selection} empty="Memory categories not collected; see notes." />
          : group === "sessions" ? <DataTable id="memory.sessions" label="Memory by session" columns={sessionColumns} rows={report.sessions}
            rowKey={r => `session:${r.processlist_id}`} selection={selection} empty={report.collection.sessions ? "No visible sessions." : "Session memory not collected; see notes."} />
          : <DataTable id="memory.accounts" label="Memory by account" columns={accountColumns} rows={report.accounts}
            rowKey={r => `account:${accountKey(r)}`} selection={selection} empty={report.collection.accounts ? "No accounts recorded." : "Account memory not collected; see notes."} />}
      </section>
      {group === "categories" && report.collection.events && <Disclosure title="Top memory events" count={report.events.length}>
        <DataTable id="memory.events" label="Top memory events" columns={eventColumns} rows={report.events}
          rowKey={r => `event:${r.event}`} selection={selection} empty="No memory events recorded." />
      </Disclosure>}
    </>}
  </ViewLayout>;
}
