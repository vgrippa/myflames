// Server > Instruments: the Performance Schema setup (instruments-1.0 from
// myflames/ps_instruments.py), after Workbench's Easy Setup / Advanced:
//   - Performance Schema on or off, as one row;
//   - A toolbar preset picker, current state and a compact change banner;
//     the preset summary and its statements are in the inspector;
//   - What myflames can show: one checklist row per view;
//   - Advanced (collapsed): instrument groups and consumers as tables, and
//     setup_actors / setup_objects. Gain, loss, cost and the on/off statements
//     of the selected row are in the inspector.
// myflames only reads: every statement is text with a Copy button.
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import {
  api,
  type Connection,
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
import {
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
  sessions: ["sessions", "Sessions"],
  running: ["sessions", "Sessions"],
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
        <p className="hint">To keep it after a restart, under [mysqld] in my.cnf:</p>
        <CopySql sql={sql.on.config} label={`my.cnf lines that turn on ${label}`} />
      </InspectorSection>
      <InspectorSection title="Turn off">
        <p className="hint">Now, until the server restarts:</p>
        <CopySql sql={sql.off.runtime} label={`Turn off ${label} until restart`} />
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
}) {
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const [advanced, setAdvanced] = useState(false);
  const applied = report?.presets.find((p) => p.applied === true)?.key || "";
  const [preset, setPreset] = useState("");

  async function load() {
    await reads.run(
      "instruments",
      "Reading the Performance Schema setup…",
      (c) => api<InstrumentsReport>("live/instruments", { connection: c }),
      onReport,
    );
  }

  /** Select one row, open the section it is in, scroll to it, and focus it. */
  function reveal(row: string) {
    const inAdvanced = row.startsWith("group:") || row.startsWith("consumer:") || row === "actors" || row === "objects";
    if (inAdvanced) setAdvanced(true);
    selection.select(row, true);
    window.setTimeout(() => {
      const element = document.querySelector<HTMLElement>(`.instruments-panel [data-row="${CSS.escape(row)}"]`);
      if (!element) return;
      const focusable = element.matches("[data-row-key]") ? element : element.querySelector<HTMLElement>("[data-row-key]") || element;
      element.scrollIntoView({ block: "center" });
      focusable.focus({ preventScroll: true });
      element.classList.add("is-target");
      window.setTimeout(() => element.classList.remove("is-target"), 1600);
    });
  }

  // A link from another view: read the setup once if needed, then show the row.
  useEffect(() => {
    if (!target) return;
    // A setup read from another connection says nothing about this one.
    if (!report || other) {
      if (!disabled) void load();
      return;
    }
    reveal(target.row);
    onTargetDone?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.n, !!report, !!other]);

  const status = report?.status;
  const notes = report ? report.notes.filter((n) => !status || n !== status.text) : [];
  const features: Feature[] = report?.features || [];
  const views = features.filter((f) => !f.advisory);
  const ready = views.filter((f) => f.status === "ready").length;
  const partial = views.filter((f) => f.status === "partial").length;
  const offCount = views.filter((f) => f.status === "off").length;
  const shownPreset = report?.presets.find((p) => p.key === (preset || applied)) || report?.presets[0];

  const groupColumns: Column<InstrumentGroup>[] = [
    {
      key: "name",
      label: "Instrument group",
      sort: (g) => g.label,
      render: (g) => (
        <span className="cell-name">
          {g.label}
          {g.match && <code className="cell-muted">{g.match}</code>}
        </span>
      ),
    },
    {
      key: "now",
      label: "Now",
      sort: (g) => g.state,
      render: (g) => (
        <span className="cell-name">
          <StatusPill tone={groupTone(g)} label={groupNow(g)} />
          {!!g.changed && <Pill tone="warn">{`${count(g.changed)} changed from default`}</Pill>}
        </span>
      ),
    },
    { key: "default", label: "Default", sort: (g) => g.default, render: (g) => g.default },
    { key: "cost", label: "Cost", sort: (g) => g.cost?.level || "", render: (g) => <CostChip cost={g.cost} /> },
  ];
  const consumerColumns: Column<InstrumentConsumer>[] = [
    { key: "name", label: "Consumer", sort: (c) => c.name, render: (c) => <code>{c.name}</code> },
    {
      key: "now",
      label: "Now",
      sort: (c) => consumerNow(c),
      render: (c) => (
        <span className="cell-name">
          <StatusPill tone={consumerTone(c)} label={consumerNow(c)} />
          {c.enabled !== null && c.enabled !== c.default && <Pill tone="warn">Changed from default</Pill>}
        </span>
      ),
    },
    { key: "default", label: "Default", sort: (c) => (c.default ? 1 : 0), render: (c) => (c.default ? "On" : "Off") },
    { key: "cost", label: "Cost", sort: (c) => c.cost.level, render: (c) => <CostChip cost={c.cost} /> },
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
          <p>It only reads the setup tables. You run the statements yourself in a SQL client.</p>
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
      status={<ViewStatus loading={reads.loading} error={error} />}
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
          {shownPreset && (
            <div
              className={`instr-preset-status${shownPreset.applied === false ? " is-pending" : ""}`}
              data-preset={shownPreset.key}
              role="status"
            >
              <StatusPill
                tone={shownPreset.applied === true ? "ok" : shownPreset.applied === false ? "info" : "unknown"}
                label={shownPreset.applied === true ? "Already in place" : shownPreset.applied === false ? "Would change this server" : "Current setup unknown"}
              />
              <span className="instr-preset-summary" title={shownPreset.summary}>{shownPreset.summary}</span>
              <CostChip cost={shownPreset.cost} />
            </div>
          )}
          <SummaryLine tone={status.enabled ? "ok" : "off"} toneLabel={status.enabled ? "On" : "Off"} notes={notes}>
            Performance Schema {status.enabled ? "is on" : "is off"} · {ready} {ready === 1 ? "view" : "views"} ready
            {partial > 0 && `, ${partial} partly`}
            {offCount > 0 && `, ${offCount} off`}
          </SummaryLine>
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
              toneLabel={status.enabled ? "On" : "Off"}
              label="Performance Schema"
              detail={`${engineLabel(report)}${status.memory_bytes !== null ? ` · its own memory ${bytes(status.memory_bytes)}` : ""}`}
              value={`default ${status.default ? "ON" : "OFF"}`}
              attrs={{ "data-row": "ps", id: rowDomId("ps") }}
              className={status.enabled ? "is-on" : "is-off"}
            />
          </GroupedList>
          <GroupedList id="instr-features-h" title="What myflames can show" className="instr-features">
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
          </GroupedList>
          <Disclosure title="Advanced" open={advanced} onToggle={setAdvanced} className="instr-advanced">
            <section className="view-section" aria-labelledby="instr-groups-h">
              <div className="view-section-head">
                <h3 id="instr-groups-h">Instruments</h3>
                <span className="hint">What the server can measure (setup_instruments)</span>
              </div>
              <DataTable
                id="instruments.groups"
                label="Instrument groups"
                columns={groupColumns}
                rows={report.groups}
                rowKey={(g) => g.id}
                selection={selection}
                rowAttrs={(g) => ({ "data-row": g.id, id: rowDomId(g.id) })}
              />
            </section>
            <section className="view-section" aria-labelledby="instr-consumers-h">
              <div className="view-section-head">
                <h3 id="instr-consumers-h">Consumers</h3>
                <span className="hint">Where measurements are kept (setup_consumers)</span>
              </div>
              <DataTable
                id="instruments.consumers"
                label="Consumers"
                columns={consumerColumns}
                rows={report.consumers}
                rowKey={(c) => c.id}
                selection={selection}
                rowAttrs={(c) => ({ "data-row": c.id, id: rowDomId(c.id) })}
              />
            </section>
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
          </Disclosure>
        </>
      )}
    </ViewLayout>
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
          {p.restore_setup && (
            <>
              <p className="hint">setup_actors and setup_objects differ from the defaults; this restores them:</p>
              <CopySql sql={p.restore_setup} label={`${p.label}: restore setup_actors and setup_objects`} />
            </>
          )}
          {p.undo && (
            <>
              <p className="hint">When you are done:</p>
              <CopySql sql={p.undo} label={`${p.label}: undo statement`} />
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
