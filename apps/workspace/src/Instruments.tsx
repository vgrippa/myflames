// Server > Instruments: loads setup on open and offers Features, Instruments,
// Consumers and Actors and objects. Individual rows support bounded selection;
// exact runtime SQL uses the shared review/confirmation runner. Startup option
// lines stay copy-only. Linked rows and selected-row details use the inspector.
import { createContext, useContext, useEffect, useLayoutEffect, useState, type ReactNode } from "react";
import {
  api,
  type Connection,
  type Instrument,
  type InstrumentConsumer,
  type InstrumentCost,
  type InstrumentFeature,
  type InstrumentGroup,
  type InstrumentsPreset,
  type InstrumentsReport,
  type InstrumentSql,
  type InstrumentsSetup,
} from "./api";
import { CopySql, bytes, count, engineLabel } from "./PerfShared";
import { uptime } from "./Health";
import type { ServerKind } from "./session";
import { useConnectionReads, type Bound } from "./binding";
import { useOpenRead } from "./viewOnScreen";
import {
  copyToClipboard,
  DataTable,
  Disclosure,
  EmptyState,
  Fields,
  GroupedList,
  GroupedRow,
  InfoPopover,
  InspectorSection,
  Pill,
  PrerequisiteGate,
  Select,
  SearchField,
  Segmented,
  StatusPill,
  SummaryLine,
  ViewLayout,
  ViewStatus,
  type Column,
  type InspectorContent,
  type Selection,
  type Tone,
} from "./kit";
import "./instruments.css";
import { ReviewedSqlAction, ReviewedSqlProvider, ReviewedSqlStatus } from "./ReviewedSqlAction";

/** Set by the Server place: opens Instruments at one row. */
export const InstrumentsLinkContext = createContext<((row: string) => void) | null>(null);

/** A link from another view's "instrument is off" note to its Instruments row. */
export function InstrumentsLink({ row, children = "How to turn it on" }: { row: string; children?: ReactNode }) {
  const open = useContext(InstrumentsLinkContext);
  if (!open) return null;
  return (
    <button type="button" className="text-button instr-link" data-instruments-row={row} onClick={() => open(row)}>
      {children}
    </button>
  );
}

/** The DOM id of a row ("group:wait/synch" becomes "instr-group-wait-synch"). */
export function rowDomId(row: string) {
  return `instr-${row.replace(/[^a-z0-9]+/gi, "-")}`;
}

/** How a "Show" link names a row. */
function rowLabel(row: string) {
  if (row === "ps") return "Performance Schema";
  if (row === "actors" || row === "objects") return `setup_${row}`;
  return row.replace(/^(group|consumer):/, "");
}

const STATE_TEXT: Record<string, string> = {
  on: "On",
  off: "Off",
  partial: "Partly on",
  counted: "On, not timed",
  absent: "Not on this server",
  unknown: "Unknown",
};
const FEATURE_STATUS: Record<string, [Tone, string]> = {
  ready: ["ok", "Ready"],
  partial: ["watch", "Partly"],
  off: ["problem", "Off"],
  unavailable: ["watch", "Unavailable"],
  not_needed: ["info", "Not needed"],
  unknown: ["unknown", "Unknown"],
};
// Advisory coverage is optional and never contributes to view readiness.
type Feature = InstrumentsReport["features"][number] & { advisory?: boolean };
function featureStatus(feature: Feature): [Tone, string] {
  return feature.advisory ? ["info", "Optional"] : FEATURE_STATUS[feature.status] || ["unknown", feature.status];
}
/** Views a feature can open in the Server place. */
const VIEWS: Record<string, [ServerKind, string]> = {
  top: ["top", "Top statements"],
  sessions: ["sessions", "Processlist"],
  running: ["sessions", "Processlist"],
  locks: ["locks", "Locks"],
  waits: ["waits", "Waits"],
  indexes: ["indexes", "Indexes"],
  io: ["io", "I/O"],
};

function CostChip({ cost }: { cost: InstrumentCost | null }) {
  if (!cost) return <Pill>?</Pill>;
  return (
    <span className={`instr-cost is-${cost.level}`}>
      <Pill tone={cost.level === "high" ? "danger" : cost.level === "moderate" ? "warn" : "ok"} title={cost.text}>
        {cost.level === "high" ? "High cost" : cost.level === "moderate" ? "Moderate cost" : "Low cost"}
      </Pill>
    </span>
  );
}

function UsedBy({ features, onOpenView }: { features: InstrumentFeature[]; onOpenView: (view: ServerKind) => void }) {
  if (!features.length) return <p className="hint">No myflames view reads it today.</p>;
  return (
    <div className="instr-used-by">
      {features.map((f) => {
        const view = VIEWS[f.view];
        const text = `${f.label}${f.required ? "" : " (optional)"}`;
        return view ? (
          <button key={f.key} type="button" className="button small instr-feature" onClick={() => onOpenView(view[0])}>
            {text}
          </button>
        ) : (
          <Pill key={f.key}>{text}</Pill>
        );
      })}
    </div>
  );
}

function GainLoseCost({ gain, lose, cost }: { gain: string; lose: string; cost: InstrumentCost | null }) {
  return (
    <dl className="instr-gls">
      {gain && (
        <div>
          <dt>You gain, when it is on</dt>
          <dd>{gain}</dd>
        </div>
      )}
      {lose && (
        <div>
          <dt>You lose, when it is off</dt>
          <dd>{lose}</dd>
        </div>
      )}
      {cost && (
        <div>
          <dt>Cost (rule of thumb)</dt>
          <dd>{cost.text}</dd>
        </div>
      )}
    </dl>
  );
}

function Toggle({ sql, label }: { sql: InstrumentSql; label: string }) {
  return (
    <>
      <InspectorSection title="Turn on">
        <p className="hint">Now, until the server restarts:</p>
        <CopySql sql={sql.on.runtime} label={`Turn on ${label} until restart`} />
          <ReviewedSqlAction sql={sql.on.runtime} label={`Turn on ${label}`} />
        <p className="hint">To keep it after a restart, under [mysqld] in my.cnf:</p>
        <CopySql sql={sql.on.config} label={`my.cnf lines that turn on ${label}`} />
      </InspectorSection>
      <InspectorSection title="Turn off">
        <p className="hint">Now, until the server restarts:</p>
        <CopySql sql={sql.off.runtime} label={`Turn off ${label} until restart`} />
          <ReviewedSqlAction sql={sql.off.runtime} label={`Turn off ${label}`} />
        <p className="hint">To keep it off after a restart:</p>
        <CopySql sql={sql.off.config} label={`my.cnf line that turns off ${label}`} />
      </InspectorSection>
    </>
  );
}

function groupNow(g: InstrumentGroup) {
  if (g.total === null) return STATE_TEXT.unknown;
  const counts = g.total > 1 ? ` (${count(g.enabled)} of ${count(g.total)})` : "";
  return `${STATE_TEXT[g.state] || g.state}${g.state === "absent" ? "" : counts}`;
}
function groupTone(g: InstrumentGroup): Tone {
  if (g.total === null) return "unknown";
  return g.state === "on" ? "ok" : g.state === "partial" || g.state === "counted" ? "watch" : g.state === "off" ? "off" : "unknown";
}
function consumerNow(c: InstrumentConsumer) {
  if (c.enabled === null) return "Unknown";
  if (c.enabled && c.blocked_by.length) return `On, but blocked by ${c.blocked_by.join(", ")}`;
  return c.enabled ? "On" : "Off";
}
function consumerTone(c: InstrumentConsumer): Tone {
  if (c.enabled === null) return "unknown";
  return c.enabled ? (c.blocked_by.length ? "watch" : "ok") : "off";
}
function setupTone(setup: InstrumentsSetup): [Tone, string] {
  return setup.state === "default" ? ["ok", "Default"] : setup.state === "empty" ? ["problem", "Empty"] : ["watch", "Changed"];
}

type InstrumentAction = "enable" | "counted" | "disable";

/** Pure, report-bound SQL generation. No caller-supplied name becomes SQL unless
 * it exists in the report. Timed and untimed rows always use separate UPDATEs. */
export function buildInstrumentChange(
  report: Pick<InstrumentsReport, "instruments" | "consumers">,
  kind: "instruments" | "consumers",
  selected: string[],
  action: InstrumentAction,
): { sql: string; config: string; alsoEnables: string[]; skippedBuiltin: string[] } {
  // Doubling quotes reads the same with or without NO_BACKSLASH_ESCAPES; a
  // backslash would not, so such a name is refused (none exists in either engine).
  const quote = (name: string) => "'" + name.replace(/'/g, "''") + "'";
  const optionQuote = (name: string) => name.replace(/'/g, "\\'");
  const names = [...new Set(selected)].sort();
  const sql: string[] = [];
  const config: string[] = [];
  const alsoEnables: string[] = [];
  const skippedBuiltin: string[] = [];
  const on = action !== "disable";
  function update(table: string, values: string, rows: string[]) {
    for (let at = 0; at < rows.length; at += 500)
      sql.push(`UPDATE performance_schema.${table} SET ${values} WHERE NAME IN (${rows.slice(at, at + 500).map(quote).join(", ")});`);
  }
  if (kind === "instruments") {
    const available = new Map((report.instruments || []).map(row => [row.name, row]));
    const timed: string[] = [], untimed: string[] = [];
    for (const name of names) {
      const row = available.get(name);
      if (!row) throw new Error(`Instrument is not in this report: ${name}`);
      if (name.includes("\\")) throw new Error(`Instrument name contains a backslash: ${name}`);
      if (!on && row.builtin) { skippedBuiltin.push(name); continue; }
      (row.timed_applies ? timed : untimed).push(name);
      const value = !on ? "OFF" : action === "counted" && row.timed_applies ? "COUNTED" : "ON";
      config.push(`performance-schema-instrument='${optionQuote(name)}=${value}'`);
    }
    const enabled = `ENABLED = '${on ? "YES" : "NO"}'`;
    update("setup_instruments", `${enabled}, TIMED = '${action === "enable" ? "YES" : "NO"}'`, timed);
    update("setup_instruments", enabled, untimed);
  } else {
    if (action === "counted") throw new Error("Consumers have no timing option.");
    const available = new Map(report.consumers.map(row => [row.name, row]));
    const chosen = new Set<string>();
    function include(name: string) {
      const row = available.get(name);
      if (!row || row.enabled === null) throw new Error(`Consumer is not readable in this report: ${name}`);
      if (chosen.has(name)) return;
      chosen.add(name);
      if (on) row.requires.forEach(include);
    }
    names.forEach(include);
    const all = [...chosen].sort();
    alsoEnables.push(...all.filter(name => !names.includes(name)));
    update("setup_consumers", `ENABLED = '${on ? "YES" : "NO"}'`, all);
    config.push(...all.map(name => `performance-schema-consumer-${optionQuote(name)}=${on ? "ON" : "OFF"}`));
  }
  return { sql: sql.join("\n"), config: config.join("\n"), alsoEnables, skippedBuiltin };
}

const RUNTIME_WARNING = "Runtime changes are lost at restart. Copy the option file lines to keep them under [mysqld].";
function SelectedChanges({ report, kind, names, clear }: {
  report: InstrumentsReport; kind: "instruments" | "consumers"; names: string[]; clear(): void;
}) {
  const actions: [InstrumentAction, string][] = kind === "instruments"
    ? [["enable", "Enable"], ["counted", "Enable without timing"], ["disable", "Disable"]]
    : [["enable", "Enable"], ["disable", "Disable"]];
  // A refresh can remove a selected row before its clearing effect runs.
  const available = new Set(kind === "instruments" ? (report.instruments || []).map(row => row.name)
    : report.consumers.filter(row => row.enabled !== null).map(row => row.name));
  const picked = names.filter(name => available.has(name));
  const plans = actions.map(([action, label]) => {
    try { return { label, ...buildInstrumentChange(report, kind, picked, action), error: "" }; }
    catch (error) {
      // Missing consumer dependencies must refuse a plan, rather than crash the
      // view or silently execute a partial enable script.
      return { label, sql: "", config: "", alsoEnables: [], skippedBuiltin: [], error: (error as Error).message };
    }
  });
  const skipped = plans.find(plan => plan.skippedBuiltin.length)?.skippedBuiltin.length || 0;
  return <div className="instr-batch" data-instrument-actions={kind}>
    <div className="instr-actions">
      <span role="status">{count(picked.length)} selected</span>
      {plans.map(plan => plan.sql
        ? <ReviewedSqlAction key={plan.label} sql={plan.sql} label={plan.label}
            warning={[RUNTIME_WARNING, plan.alsoEnables.length ? `Also enables ${plan.alsoEnables.join(", ")}.` : "",
              plan.skippedBuiltin.length ? `Skips ${plan.skippedBuiltin.length} built-in memory instruments, which cannot be disabled.` : ""].filter(Boolean).join(" ")} />
        : <button key={plan.label} type="button" className="button small" disabled>{plan.label}</button>)}
      <button type="button" className="text-button" disabled={!names.length} onClick={clear}>Clear selection</button>
    </div>
    {[...new Set(plans.map(plan => plan.error).filter(Boolean))].map(error => <p key={error} role="alert">{error} Refresh the setup before reviewing this change.</p>)}
    {!!skipped && <p className="hint">Disable skips {count(skipped)} built-in memory instruments, which cannot be turned off.</p>}
    {!!names.length && <Disclosure title="Keep after restart">
      <p className="hint">Copy only. Add under [mysqld] in the server option file.</p>
      {plans.filter(plan => plan.config).map(plan => <CopySql key={plan.label} sql={plan.config} label={`${plan.label}: option file lines`} />)}
    </Disclosure>}
  </div>;
}

function enabledText(value: boolean | null) { return value === null ? "Unknown" : value ? "Yes" : "No"; }
function defaultText(row: Instrument) {
  if (row.default_enabled === null) return "Unknown";
  return !row.default_enabled ? "Disabled" : row.timed_applies && row.default_timed === false ? "Enabled, not timed" : "Enabled";
}

export function InstrumentsView({
  connection,
  disabled,
  onBack,
  selection,
  report: bound,
  onReport,
  target,
  onTargetDone,
  onOpenView,
  onOpenSql,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<InstrumentsReport> | null;
  onReport: (report: Bound<InstrumentsReport>) => void;
  /** A row another view asked to show; n changes on every request. */
  target: { row: string; n: number } | null;
  /** Called once the target row is shown, so it is not shown again. */
  onTargetDone?: () => void;
  onOpenView: (view: ServerKind) => void;
  onOpenSql?: (sql: string) => void;
}) {
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const [tab, setTab] = useState("features");
  const [search, setSearch] = useState("");
  const [group, setGroup] = useState("");
  const [state, setState] = useState("all");
  const [instrumentNames, setInstrumentNames] = useState<string[]>([]);
  const [consumerNames, setConsumerNames] = useState<string[]>([]);
  const applied = report?.presets.find((p) => p.applied === true)?.key || "";
  const [preset, setPreset] = useState("");

  async function load() {
    await reads.run(
      "instruments",
      "Reading the Performance Schema setup…",
      (c, signal) => api<InstrumentsReport>("live/instruments", { connection: c }, signal),
      onReport,
    );
  }

  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: loading || reads.running(), read: () => { void load(); } });
  useEffect(() => {
    setInstrumentNames([]);
    setConsumerNames([]);
  }, [reads.key, bound?.report]);

  const [revealRequest, setRevealRequest] = useState(0);
  useLayoutEffect(() => {
    if (!revealRequest) return;
    // Details now replace the list. Focus their Back control after rendering,
    // including when a retained view reveals the same setting a second time.
    document.querySelector<HTMLButtonElement>(".instruments-panel .detail-back")?.focus();
  }, [revealRequest]);

  /** Select a row and reveal its full-width details. */
  function reveal(row: string) {
    if (row.startsWith("group:")) {
      setTab("instruments");
      setGroup(row.slice(6));
      setSearch("");
      setState("all");
    } else if (row.startsWith("instrument:")) setTab("instruments");
    else if (row.startsWith("consumer:")) setTab("consumers");
    else if (row === "actors" || row === "objects") setTab("setup");
    selection.select(row, true);
    setRevealRequest(n => n + 1);
  }

  // A link from another view: read the setup once if needed, then show the row.
  useEffect(() => {
    if (!target) return;
    // A setup read from another connection says nothing about this one.
    if (!report || other || disabled) return;
    reveal(target.row);
    onTargetDone?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.n, bound?.key, reads.key, disabled]);

  const status = report?.status;
  const notes = report ? report.notes.filter((n) => !status || n !== status.text) : [];
  const features: Feature[] = report?.features || [];
  const views = features.filter((f) => !f.advisory);
  const ready = views.filter((f) => f.status === "ready").length;
  const partial = views.filter((f) => f.status === "partial").length;
  const offCount = views.filter((f) => f.status === "off").length;
  const shownPreset = report?.presets.find((p) => p.key === (preset || applied)) || report?.presets[0];

  const shownInstruments = (report?.instruments || []).filter(row =>
    row.name.toLowerCase().includes(search.toLowerCase()) && (!group || row.group === group) &&
    (state === "all" || state === "enabled" && row.enabled || state === "disabled" && !row.enabled ||
      state === "different" && (row.default_enabled !== null && row.enabled !== row.default_enabled ||
        row.timed_applies && row.default_timed !== null && row.timed !== row.default_timed)));
  /** A visible checkbox per row: clicking the row still selects only that row,
   * the checkbox adds or removes it; the header picks every row shown. */
  function pickColumn<T>(nameOf: (row: T) => string, shown: T[], names: string[], setNames: (names: string[]) => void,
    disabledOf: (row: T) => boolean = () => false, cap = 2000): Column<T> {
    const shownNames = shown.filter(row => !disabledOf(row)).slice(0, cap).map(nameOf);
    const all = shownNames.length > 0 && shownNames.every(name => names.includes(name));
    return {
      key: "pick", name: "Select", width: 40, minWidth: 40,
      label: <input type="checkbox" className="instr-pick" aria-label="Select every row shown" checked={all}
        disabled={!shownNames.length} onChange={e => setNames(e.target.checked ? [...new Set([...names, ...shownNames])] : names.filter(name => !shownNames.includes(name)))} />,
      render: row => <input type="checkbox" className="instr-pick" aria-label={`Select ${nameOf(row)}`} checked={names.includes(nameOf(row))}
        disabled={disabledOf(row)} onChange={e => setNames(e.target.checked ? [...names, nameOf(row)] : names.filter(name => name !== nameOf(row)))} />,
    };
  }
  const instrumentColumns: Column<Instrument>[] = [
    pickColumn<Instrument>(row => row.name, shownInstruments, instrumentNames, setInstrumentNames),
    { key: "name", label: "Name", primary: true, sort: row => row.name, render: row => <code>{row.name}</code> },
    { key: "enabled", label: "Enabled", sort: row => Number(row.enabled), render: row => enabledText(row.enabled) },
    { key: "timed", label: "Timed", sort: row => row.timed === null ? -1 : Number(row.timed), render: row => row.timed_applies ? enabledText(row.timed) : "" },
    { key: "default", label: "Default", sort: defaultText, render: defaultText },
    { key: "group", label: "Group", sort: row => row.group, render: row => report?.groups.find(g => g.key === row.group)?.label || row.group },
  ];
  const consumerColumns: Column<InstrumentConsumer>[] = [
    pickColumn<InstrumentConsumer>(c => c.name, report?.consumers || [], consumerNames, setConsumerNames, c => c.enabled === null),
    { key: "name", label: "Name", primary: true, sort: c => c.name, render: c => <code>{c.name}</code> },
    { key: "enabled", label: "Enabled", sort: c => Number(c.enabled), render: c => enabledText(c.enabled) },
    { key: "effective", label: "Effective", sort: c => Number(c.effective), render: c => c.blocked_by.length
      ? `Blocked by ${c.blocked_by.join(", ")}` : enabledText(c.effective) },
    { key: "default", label: "Default", sort: c => Number(c.default), render: c => enabledText(c.default) },
    { key: "requires", label: "Requires", sort: c => c.requires.join(", "), render: c => c.requires.join(", ") },
  ];

  let inspector: InspectorContent | null = null;
  const key = selection.key;
  if (report && status && key === "ps") {
    inspector = {
      key,
      title: "Performance Schema",
      meta: <StatusPill tone={status.enabled ? "ok" : "off"} label={status.enabled ? "On" : "Off"} />,
      body: (
        <>
          <InspectorSection title="State">
            <p>{status.text}</p>
            <Fields
              label="Performance Schema"
              rows={[
                ["Server", engineLabel(report)],
                ["Uptime", uptime(report.uptime_s)],
                ["Default", status.default ? "ON" : "OFF"],
                status.memory_bytes !== null && ["Its own memory", bytes(status.memory_bytes)],
                status.lost.length > 0 && ["Lost (no room)", status.lost.join(", ")],
              ]}
            />
          </InspectorSection>
          {status.enable && (
            <InspectorSection title="Turn it on">
              <p className="hint">Under [mysqld] in my.cnf, then restart:</p>
              <CopySql sql={status.enable.config} label="my.cnf lines that turn Performance Schema on" />
              {status.enable.persist && (
                <>
                  <p className="hint">{status.enable.persist_text}</p>
                  <CopySql sql={status.enable.persist} label="Persist performance_schema = ON, then restart" />
                </>
              )}
            </InspectorSection>
          )}
          {status.disable && (
            <InspectorSection title="Turn it off">
              <p className="hint">{status.cost}</p>
              <CopySql sql={status.disable.config} label="my.cnf lines that turn Performance Schema off" />
            </InspectorSection>
          )}
        </>
      ),
    };
  } else if (report && key.startsWith("preset:")) {
    const p = report.presets.find((x) => `preset:${x.key}` === key);
    if (p) inspector = presetInspector(key, p, !!status?.enabled, report.read_only_note);
  } else if (report && key.startsWith("feature:")) {
    const f = features.find((x) => `feature:${x.key}` === key);
    if (f) {
      const [tone, label] = featureStatus(f);
      const view = VIEWS[f.view];
      inspector = {
        key,
        title: f.label,
        meta: <StatusPill tone={tone} label={label} />,
        actions:
          view && f.status !== "not_needed" ? (
            <button type="button" className="button" onClick={() => onOpenView(view[0])}>
              Open {view[1]}
            </button>
          ) : undefined,
        body: (
          <>
            <InspectorSection title="What it shows">
              <p>{f.explain}</p>
            </InspectorSection>
            {f.missing.length > 0 && (
              <InspectorSection title="Missing">
                <p className="instr-missing">Missing: {f.missing.join("; ")}.</p>
              </InspectorSection>
            )}
            {f.optional_missing.length > 0 && (
              <InspectorSection title="Optional">
                <p className="hint">Optional, not on: {f.optional_missing.join("; ")}.</p>
              </InspectorSection>
            )}
            {f.rows.length > 0 && (
              <InspectorSection title="Turn it on">
                <div className="inspector-links">
                  {f.rows.map((row) => (
                    <button key={row} type="button" className="text-button" onClick={() => reveal(row)}>
                      Show {rowLabel(row)}
                    </button>
                  ))}
                </div>
              </InspectorSection>
            )}
          </>
        ),
      };
    }
  } else if (report && key.startsWith("instrument:")) {
    const row = report.instruments?.find(item => `instrument:${item.name}` === key);
    const g = report.groups.find(item => item.key === row?.group);
    if (row) inspector = {
      key, title: row.name, meta: <StatusPill tone={row.enabled ? "ok" : "off"} label={row.enabled ? "Enabled" : "Disabled"} />,
      body: <>
        <InspectorSection title="On this server">
          <Fields label="Instrument" rows={[
            ["Enabled", enabledText(row.enabled)], ["Timed", row.timed_applies ? enabledText(row.timed) : "Does not apply"],
            ["Default", defaultText(row)], ["Group", g?.label || row.group], ["Properties", row.properties || "None"],
          ]} />
          {row.builtin && <p className="hint">Built-in memory instrument. It cannot be turned off.</p>}
        </InspectorSection>
        {g && <InspectorSection title="Gain, loss and cost"><GainLoseCost gain={g.gain} lose={g.lose} cost={g.cost} /></InspectorSection>}
      </>,
    };
  } else if (report && key.startsWith("group:")) {
    const g = report.groups.find((x) => x.id === key);
    if (g)
      inspector = {
        key,
        title: g.label,
        meta: (
          <>
            {g.match && <code>{g.match}</code>}
            <StatusPill tone={groupTone(g)} label={groupNow(g)} />
          </>
        ),
        body: (
          <>
            <InspectorSection title="Used by myflames">
              <UsedBy features={g.features} onOpenView={onOpenView} />
            </InspectorSection>
            <InspectorSection title="Gain, loss and cost">
              <GainLoseCost gain={g.gain} lose={g.lose} cost={g.cost} />
            </InspectorSection>
            {((g.kinds && g.kinds.length > 0) || !!g.builtin || (g.changed_names && g.changed_names.length > 0)) && (
              <InspectorSection title="On this server">
                {g.kinds && g.kinds.length > 0 && (
                  <p className="hint">{g.kinds.map((k) => `${k.kind}: ${count(k.enabled)} of ${count(k.total)} on`).join(", ")}</p>
                )}
                {!!g.builtin && (
                  <p className="hint">
                    Plus {count(g.builtin)} memory/performance_schema/* instruments, always on: the server ignores changes to them.
                  </p>
                )}
                {g.changed_names && g.changed_names.length > 0 && (
                  <p className="hint">
                    Changed from the default: <code>{g.changed_names.join(", ")}</code>
                    {g.changed && g.changed > g.changed_names.length ? " and more" : ""}.
                  </p>
                )}
              </InspectorSection>
            )}
            {g.sql && <Toggle sql={g.sql} label={g.label} />}
          </>
        ),
      };
  } else if (report && key.startsWith("consumer:")) {
    const c = report.consumers.find((x) => x.id === key);
    if (c)
      inspector = {
        key,
        title: c.name,
        meta: <StatusPill tone={consumerTone(c)} label={consumerNow(c)} />,
        body: (
          <>
            <InspectorSection title="Used by myflames">
              <UsedBy features={c.features} onOpenView={onOpenView} />
            </InspectorSection>
            {c.requires.length > 0 && (
              <InspectorSection title="Depends on">
                <p className="hint">Records only while these are on: {c.requires.join(", ")}.</p>
              </InspectorSection>
            )}
            <InspectorSection title="Gain, loss and cost">
              <GainLoseCost gain={c.gain} lose={c.lose} cost={c.cost} />
            </InspectorSection>
            <Toggle sql={c.sql} label={c.name} />
          </>
        ),
      };
  } else if (report && (key === "actors" || key === "objects")) {
    const setup = key === "actors" ? report.actors : report.objects;
    if (setup) {
      const [tone, label] = setupTone(setup);
      inspector = {
        key,
        title: `setup_${key}`,
        meta: <StatusPill tone={tone} label={label} />,
        body: (
          <>
            <InspectorSection title="What it is">
              <p>{setup.explain}</p>
            </InspectorSection>
            <InspectorSection title="On this server">
              <p className="hint">
                Default: {setup.default_text} This server has {setup.rows.length} {setup.rows.length === 1 ? "row" : "rows"}.
              </p>
            </InspectorSection>
            {setup.restore && (
              <InspectorSection title="Restore the defaults">
                <p className="hint">TRUNCATE empties only this configuration table:</p>
                <CopySql sql={setup.restore} label={`Restore the default setup_${key} rows`} />
          <ReviewedSqlAction sql={setup.restore} label={`Restore setup_${key} defaults`} />
              </InspectorSection>
            )}
          </>
        ),
      };
    }
  } else if (report && key === "topic:rules") {
    inspector = {
      key,
      title: "How changes apply",
      body: (
        <ul className="instr-rules">
          {report.rules.map((r) => (
            <li key={r}>{r}</li>
          ))}
          <li>{report.privileges}</li>
        </ul>
      ),
    };
  }

  return (
    <ReviewedSqlProvider connection={connection} disabled={disabled || !!other || !report?.status.enabled} onComplete={() => { setInstrumentNames([]); setConsumerNames([]); void load(); }}>
    <ViewLayout
      id="instruments"
      title="Instruments"
      className="instruments-panel"
      onBack={onBack}
      info={
        <>
          <p>
            Which Performance Schema instruments and consumers are on, what each gives myflames and you, what it
            costs, and the statements that turn it on or off, with ready-made presets.
          </p>
          <p>This view reads the setup tables when opened. Refresh retries or reads them again. Apply actions show the exact SQL and require confirmation.</p>
        </>
      }
      controls={report && shownPreset ? (
        <>
          <label className="toolbar-field instr-presets">
            <span>Preset</span>
            <Select
              label="Preset"
              value={shownPreset.key}
              onChange={setPreset}
              options={report.presets.map((p): [string, string] => [p.key, `${p.label}${p.applied === true ? " (current)" : ""}`])}
            />
          </label>
          {!applied && <Pill title="The setup matches none of the presets">Custom</Pill>}
          <InfoPopover label="About presets">
            <p>
              <strong>{shownPreset.label}:</strong> {shownPreset.summary}
            </p>
            <p>
              Cost: <CostChip cost={shownPreset.cost} />
            </p>
            <p>Ready-made sets of statements for this engine. Each takes effect at once and lasts until the server restarts; the my.cnf lines keep it.</p>
            <p>{report.read_only_note}</p>
          </InfoPopover>
          <button
            type="button"
            className="button instr-show-statements"
            data-row-key={`preset:${shownPreset.key}`}
            onClick={() => selection.select(`preset:${shownPreset.key}`, true)}
          >
            Show statements
          </button>
        </>
      ) : undefined}
      onRefresh={() => void load()}
      refreshDisabled={disabled}
      loading={loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a preset, a view or an instrument to see its statements, gain and cost.</p>}
      status={<><ViewStatus loading={reads.loading} error={error} onStop={reads.stop} onRetry={reads.error ? () => void load() : undefined} doneAt={reads.doneAt} /><ReviewedSqlStatus /></>}
    >
      {!report && !loading && !error && (
        <EmptyState
          icon="instruments"
          title="No setup loaded"
          action={
            <button type="button" className="button primary" disabled={disabled} onClick={() => void load()}>
              Load instruments
            </button>
          }
        >
          See what each view needs, what each instrument costs, and ready-made presets. It only reads.
        </EmptyState>
      )}
      {report && status && (
        <>
          <SummaryLine tone={status.enabled ? (offCount > 0 ? "problem" : partial > 0 ? "watch" : "ok") : "problem"} notes={notes}>
            Performance Schema {status.enabled ? "is on" : "is off"} · {ready} {ready === 1 ? "view" : "views"} ready
            {partial > 0 && `, ${partial} partly`}
            {offCount > 0 && `, ${offCount} off`}
          </SummaryLine>
          {shownPreset && shownPreset.applied !== true && (
            <div
              className={`instr-preset-status${shownPreset.applied === false ? " is-pending" : ""}`}
              data-preset={shownPreset.key}
              role="status"
            >
              <StatusPill
                tone={shownPreset.applied === false ? "watch" : "unknown"}
              />
              <span className="instr-preset-summary" title={shownPreset.summary}>
                {shownPreset.applied === false ? `Preset ${shownPreset.label} would change this server` : "Current setup unknown"} · {shownPreset.summary}
              </span>
              <CostChip cost={shownPreset.cost} />
            </div>
          )}
          {report.unavailable && report.unavailable !== "performance_schema_off" && (
            <PrerequisiteGate>
              The setup tables could not be read, so the current state is unknown (see the notes). The statements still apply.
            </PrerequisiteGate>
          )}
          <GroupedList id="instr-ps-h" title="Server" className="instr-server">
            <GroupedRow
              rowKey="ps"
              selection={selection}
              tone={status.enabled ? "ok" : "off"}
              toneLabel={status.enabled ? undefined : "Off"}
              label="Performance Schema"
              detail={`${engineLabel(report)}${status.memory_bytes !== null ? ` · its own memory ${bytes(status.memory_bytes)}` : ""}`}
              value={`default ${status.default ? "ON" : "OFF"}`}
              attrs={{ "data-row": "ps", id: rowDomId("ps") }}
              className={status.enabled ? "is-on" : "is-off"}
            />
          </GroupedList>
          <Segmented label="Instruments sections" value={tab} onChange={setTab} className="instr-tabs"
            options={[{ value: "features", label: "Features" }, { value: "instruments", label: "Instruments" },
              { value: "consumers", label: "Consumers" }, { value: "setup", label: "Actors and objects" }]} />
          {tab === "features" && <GroupedList id="instr-features-h" title="What myflames can show" className="instr-features">
            {features.map((f) => {
              const [tone, label] = featureStatus(f);
              const reason = f.missing.length
                ? `needs ${f.missing[0]}${f.missing.length > 1 ? ` and ${f.missing.length - 1} more` : ""}`
                : f.optional_missing.length
                  ? `${f.optional_missing.length} optional ${f.optional_missing.length === 1 ? "group" : "groups"} off`
                  : "";
              return (
                <GroupedRow
                  key={f.key}
                  rowKey={`feature:${f.key}`}
                  selection={selection}
                  tone={tone}
                  toneLabel={label}
                  label={f.label}
                  detail={reason || undefined}
                  attrs={{ "data-feature": f.key }}
                />
              );
            })}
          </GroupedList>}
          {tab === "instruments" && (!status.enabled ? <PrerequisiteGate>{status.text}</PrerequisiteGate> : <section className="view-section instr-list" aria-label="Instruments">
            <div className="instr-filters">
              <SearchField label="Search instruments" value={search} onChange={setSearch} />
              <Select label="Instrument group" value={group} onChange={setGroup}
                options={[["", "All groups"], ...report.groups.map((g): [string, string] => [g.key, g.label])]} />
              <Select label="Instrument state" value={state} onChange={setState}
                options={[["all", "All states"], ["enabled", "Enabled"], ["disabled", "Disabled"], ["different", "Different from default"]]} />
            </div>
            <SelectedChanges report={report} kind="instruments" names={instrumentNames} clear={() => setInstrumentNames([])} />
            <div className="instr-actions">
              <button type="button" className="text-button" disabled={!shownInstruments.length}
                onClick={() => setInstrumentNames(shownInstruments.slice(0, 2000).map(row => row.name))}>Select all shown</button>
              <span className="hint">{count(shownInstruments.length)} shown</span>
              {shownInstruments.length > 2000 && <span className="hint">Select all shown is limited to the first 2,000 matching instruments.</span>}
            </div>
            <DataTable id="instruments.list" label="Instruments" columns={instrumentColumns} rows={shownInstruments}
              rowKey={row => `instrument:${row.name}`} selection={selection} selectionHighlight="always"
              multiSelection={{ keys: instrumentNames.map(name => `instrument:${name}`), select: keys => setInstrumentNames(keys.map(key => key.slice(11))) }}
              onActivate={row => reveal(`instrument:${row.name}`)}
              rowAttrs={row => ({ "data-row": `instrument:${row.name}` })}
              rowContextMenu={row => [
                {
                  label: "Enable instrument",
                  icon: "check",
                  disabled: row.enabled === true,
                  onClick: () => setInstrumentNames([row.name]),
                },
                {
                  label: "Disable instrument",
                  icon: "block",
                  disabled: row.enabled === false || !!row.builtin,
                  onClick: () => setInstrumentNames([row.name]),
                },
                ...(row.timed_applies ? [
                  {
                    label: row.timed ? "Disable timing (Counted only)" : "Enable timing",
                    icon: "waits",
                    onClick: () => setInstrumentNames([row.name]),
                  },
                ] : []),
                { divider: true as const },
                {
                  label: "Explain & Help (What this event does)",
                  icon: "info",
                  onClick: () => reveal(`instrument:${row.name}`),
                },
                {
                  label: `Filter by group (${report?.groups.find(g => g.key === row.group)?.label || row.group})`,
                  icon: "search",
                  onClick: () => { setGroup(row.group); setSearch(""); },
                },
                { divider: true as const },
                {
                  label: `Copy instrument name (${row.name})`,
                  icon: "copy",
                  onClick: () => void copyToClipboard(row.name),
                },
                ...(onOpenSql ? [
                  {
                    label: "Query in Editor (performance_schema)",
                    icon: "query",
                    onClick: () => onOpenSql(`SELECT * FROM performance_schema.setup_instruments WHERE NAME = '${row.name}';`),
                  },
                ] : []),
              ]} />
          </section>)}
          {tab === "consumers" && (!status.enabled ? <PrerequisiteGate>{status.text}</PrerequisiteGate> : <section className="view-section instr-list" aria-label="Consumers">
            <SelectedChanges report={report} kind="consumers" names={consumerNames} clear={() => setConsumerNames([])} />
            <DataTable id="instruments.consumers" label="Consumers" columns={consumerColumns} rows={report.consumers}
              rowKey={c => c.id} selection={selection} selectionHighlight="always"
              multiSelection={{ keys: consumerNames.map(name => `consumer:${name}`), select: keys => setConsumerNames(keys.map(key => key.slice(9))) }}
              onActivate={c => reveal(c.id)} rowAttrs={c => ({ "data-row": c.id, id: rowDomId(c.id) })}
              rowContextMenu={c => [
                {
                  label: "Enable consumer",
                  icon: "check",
                  disabled: c.enabled === true || c.enabled === null,
                  onClick: () => setConsumerNames([c.name]),
                },
                {
                  label: "Disable consumer",
                  icon: "block",
                  disabled: c.enabled === false || c.enabled === null,
                  onClick: () => setConsumerNames([c.name]),
                },
                { divider: true as const },
                {
                  label: "Inspect consumer",
                  icon: "info",
                  onClick: () => reveal(c.id),
                },
                {
                  label: `Copy consumer name (${c.name})`,
                  icon: "copy",
                  onClick: () => void copyToClipboard(c.name),
                },
                ...(onOpenSql ? [
                  {
                    label: "Query in Editor (setup_consumers)",
                    icon: "query",
                    onClick: () => onOpenSql(`SELECT * FROM performance_schema.setup_consumers WHERE NAME = '${c.name}';`),
                  },
                ] : []),
              ]} />
          </section>)}
          {tab === "setup" && <>
            {(report.actors || report.objects) && (
              <GroupedList id="instr-setup-h" title="Who and what is recorded" className="instr-setup">
                {(["actors", "objects"] as const).map((k) => {
                  const setup = report[k];
                  if (!setup) return null;
                  const [tone, label] = setupTone(setup);
                  return (
                    <GroupedRow
                      key={k}
                      rowKey={k}
                      selection={selection}
                      tone={tone}
                      toneLabel={label}
                      label={<code>setup_{k}</code>}
                      value={`${setup.rows.length} ${setup.rows.length === 1 ? "row" : "rows"}`}
                      attrs={{ "data-row": k, id: rowDomId(k) }}
                    />
                  );
                })}
              </GroupedList>
            )}
            <button
              type="button"
              className="text-button instr-rules-button"
              data-row-key="topic:rules"
              onClick={() => selection.select("topic:rules", true)}
            >
              How changes apply
            </button>
          </>}
        </>
      )}
    </ViewLayout>
    </ReviewedSqlProvider>
  );
}

function presetInspector(key: string, p: InstrumentsPreset, psOn: boolean, readOnly: string): InspectorContent {
  return {
    key,
    title: p.label,
    meta: (
      <>
        <CostChip cost={p.cost} />
        {p.applied === true && <StatusPill tone="ok" label="Already in place" />}
        {p.applied === false && <StatusPill tone="info" label="Would change this server" />}
      </>
    ),
    body: (
      <div className="instr-preset" data-preset={p.key}>
        <InspectorSection title="What it changes">
          <p>{p.summary}</p>
          <p className="hint">
            <strong>Gain:</strong> {p.gain}
          </p>
          <p className="hint">
            <strong>Cost:</strong> {p.cost.text}
          </p>
        </InspectorSection>
        <InspectorSection title="Statements to run">
          {!psOn && <p className="hint">Performance Schema is off: turn it on in my.cnf and restart first; the statements then apply.</p>}
          <p className="hint">Now, until the server restarts:</p>
          <CopySql sql={p.runtime} label={`${p.label}: runtime statements`} />
          <ReviewedSqlAction sql={p.runtime} label={`Apply ${p.label}`} disabled={!psOn} warning={p.cost.text} />
          {p.restore_setup && (
            <>
              <p className="hint">setup_actors and setup_objects differ from the defaults; this restores them:</p>
              <CopySql sql={p.restore_setup} label={`${p.label}: restore setup_actors and setup_objects`} />
          <ReviewedSqlAction sql={p.restore_setup} label={`Restore setup for ${p.label}`} disabled={!psOn} />
            </>
          )}
          {p.undo && (
            <>
              <p className="hint">When you are done:</p>
              <CopySql sql={p.undo} label={`${p.label}: undo statement`} />
          <ReviewedSqlAction sql={p.undo} label={`Undo ${p.label}`} disabled={!psOn} />
            </>
          )}
          <p className="hint">To keep it after a restart (my.cnf):</p>
          <CopySql sql={p.config} label={`${p.label}: my.cnf lines`} />
        </InspectorSection>
        {p.notes.length > 0 && (
          <InspectorSection title="Notes">
            {p.notes.map((n) => (
              <p key={n} className="hint perf-caveat">
                {n}
              </p>
            ))}
          </InspectorSection>
        )}
        <p className="hint instr-never">{readOnly}</p>
      </div>
    ),
  };
}
