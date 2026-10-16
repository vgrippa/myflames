// Server > InnoDB (innodb-1.0 from myflames/innodb_status.py): SHOW ENGINE
// INNODB STATUS parsed into sections, never shown raw by default. Each section
// is a group of a few one-line rows (value and status dot) with its one-line
// summary; every number, the rules, and the links (the deadlock to Locks, old
// transactions to Sessions) are in the inspector. The raw text is fetched only
// when asked, behind "Show raw output", for support.
import type { ReactNode } from "react";
import { api, type Connection, type InnodbItem, type InnodbReport, type InnodbSection } from "./api";
import type { ServerKind } from "./session";
import { useConnectionReads, type Bound } from "./binding";
import { engineLabel } from "./PerfShared";
import { uptime } from "./Health";
import { seconds } from "./Locks";
import {
  Disclosure,
  EmptyState,
  FactStrip,
  Fields,
  GroupedRow,
  InspectorSection,
  PrerequisiteGate,
  StatusPill,
  SummaryLine,
  ViewLayout,
  ViewStatus,
  toneRank,
  type InspectorContent,
  type Selection,
  type Tone,
} from "./kit";

/** Rows shown per section before "more in the inspector". */
const KEY_ROWS = 4;

function tone(status: string | null | undefined): Tone {
  return status === "ok" || status === "watch" || status === "problem" || status === "info" ? status : "unknown";
}

/** The rows a section shows: graded items first, then facts, at most KEY_ROWS. */
function keyItems(section: InnodbSection) {
  const graded = section.items.filter((i) => i.status);
  const facts = section.items.filter((i) => !i.status);
  return [...graded, ...facts].slice(0, Math.max(KEY_ROWS, graded.length));
}

/** Scalar values of a section's data as label/value rows (nested keys joined with a dot). */
function flatData(data: Record<string, unknown> | null, prefix = ""): [string, string][] {
  if (!data) return [];
  const rows: [string, string][] = [];
  for (const [key, value] of Object.entries(data)) {
    const name = prefix ? `${prefix}.${key}` : key;
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) rows.push([name, `${value.length} ${value.length === 1 ? "entry" : "entries"}`]);
    else if (typeof value === "object") rows.push(...flatData(value as Record<string, unknown>, name));
    else rows.push([name, typeof value === "number" ? value.toLocaleString() : String(value)]);
  }
  return rows;
}

type Trx = {
  trx_id?: string | null;
  connection_id?: number | null;
  state?: string | null;
  active_s?: number | null;
  age_s?: number | null;
  user?: string | null;
  host?: string | null;
  statement?: string | null;
  query?: string | null;
};

function SectionDetails({ section }: { section: InnodbSection }): ReactNode {
  const data = (section.data || {}) as Record<string, unknown>;
  if (section.key === "transactions" && Array.isArray(data.transactions) && data.transactions.length) {
    const list = data.transactions as Trx[];
    const view = data.oldest_read_view as { trx_id?: string; connection_id?: number | null; active_s?: number | null } | null;
    return (
      <>
        {view && (
          <InspectorSection title="Holding purge back">
            <p className="innodb-read-view">
              Transaction {view.trx_id}
              {view.connection_id != null ? ` (connection ${view.connection_id})` : ""} has the oldest read view
              {view.active_s != null ? `, active ${seconds(view.active_s)}` : ""}: purge cannot remove the undo records it may
              still need.
            </p>
          </InspectorSection>
        )}
      <InspectorSection title={`Transactions (oldest first, ${list.length})`}>
        <ul className="innodb-trx">
          {list.map((t, i) => (
            <li key={`${t.trx_id}-${i}`}>
              <strong>{t.trx_id ? `Transaction ${t.trx_id}` : "Transaction"}</strong>
              {t.connection_id != null && <span className="hint"> · connection {t.connection_id}</span>}
              {(t.active_s ?? t.age_s) != null && <span className="hint"> · active {seconds(t.active_s ?? t.age_s)}</span>}
              {t.state && <span className="hint"> · {t.state}</span>}
              {(t.statement || t.query) && (
                <pre className="inspector-sql">
                  <code>{t.statement || t.query}</code>
                </pre>
              )}
            </li>
          ))}
        </ul>
      </InspectorSection>
      </>
    );
  }
  if (section.key === "deadlock" && Array.isArray(data.transactions) && data.transactions.length) {
    const list = data.transactions as (Trx & { victim?: boolean; n?: number })[];
    return (
      <InspectorSection title="Transactions in the cycle">
        <ul className="innodb-trx">
          {list.map((t, i) => (
            <li key={i}>
              <strong>{t.trx_id ? `Transaction ${t.trx_id}` : `Transaction ${i + 1}`}</strong>
              {t.connection_id != null && <span className="hint"> · connection {t.connection_id}</span>}
              {t.victim && <span className="hint"> · rolled back</span>}
              {t.statement && (
                <pre className="inspector-sql">
                  <code>{t.statement}</code>
                </pre>
              )}
            </li>
          ))}
        </ul>
      </InspectorSection>
    );
  }
  if (section.key === "foreign_key_error" && typeof data.statement === "string") {
    return (
      <InspectorSection title="Statement">
        <pre className="inspector-sql">
          <code>{data.statement}</code>
        </pre>
      </InspectorSection>
    );
  }
  return null;
}

export function InnodbView({
  connection,
  disabled,
  onBack,
  selection,
  report: bound,
  onReport,
  onOpenView,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<InnodbReport> | null;
  onReport: (report: Bound<InnodbReport>) => void;
  onOpenView: (view: ServerKind | string) => void;
}) {
  const reads = useConnectionReads(connection);
  const loading = reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;

  async function load(raw = false) {
    await reads.run(
      `innodb:${raw}`,
      raw ? "Reading the raw InnoDB status…" : "Reading SHOW ENGINE INNODB STATUS…",
      (c) => api<InnodbReport>("live/innodb", { connection: c, raw }),
      onReport,
    );
  }

  const sections = report?.sections || [];
  const flagged = sections.filter((s) => s.status === "watch" || s.status === "problem").sort((a, b) => toneRank(a.status) - toneRank(b.status));
  const [type, name, itemKey] = selection.key.split(":");

  let inspector: InspectorContent | null = null;
  if (report && selection.key === "raw") {
    inspector = {
      key: "raw",
      title: "Raw output",
      meta: <span>SHOW ENGINE INNODB STATUS</span>,
      body: report.raw ? (
        <>
          <pre className="inspector-sql innodb-raw">{report.raw.text}</pre>
          {report.raw.truncated && <p className="hint">The server cut the output at its size limit.</p>}
        </>
      ) : (
        <p className="hint">Not read yet.</p>
      ),
    };
  } else if (report && type === "section") {
    const section = sections.find((s) => s.key === name);
    if (section) {
      const graded = section.items.filter((i) => i.rule);
      const rows = flatData(section.data);
      inspector = {
        key: selection.key,
        title: section.title,
        meta: <StatusPill tone={tone(section.status)} />,
        actions: section.links.length ? (
          <>
            {section.links.map((l) => (
              <button key={l.view} type="button" className="button" onClick={() => onOpenView(l.view)}>
                {l.label}
              </button>
            ))}
          </>
        ) : undefined,
        body: (
          <>
            <InspectorSection title="Summary">
              <p>{section.summary}</p>
            </InspectorSection>
            {section.present && section.items.length > 0 && (
              <InspectorSection title="Values">
                <Fields
                  label={`${section.title}: values`}
                  rows={section.items.map((i: InnodbItem) => [
                    <span key="l" className={i.key === itemKey ? "innodb-item-selected" : undefined}>
                      {i.label}
                    </span>,
                    <>
                      {i.text}
                      {i.status && (
                        <>
                          {" "}
                          <StatusPill tone={tone(i.status)} />
                        </>
                      )}
                    </>,
                  ])}
                />
              </InspectorSection>
            )}
            {graded.length > 0 && (
              <InspectorSection title="Rules">
                <ul className="innodb-rules">
                  {graded.map((i) => (
                    <li key={i.key}>
                      <strong>{i.label}:</strong> {i.rule}
                    </li>
                  ))}
                </ul>
              </InspectorSection>
            )}
            <SectionDetails section={section} />
            {rows.length > 0 && (
              <Disclosure title="Every parsed number" count={rows.length} className="innodb-data">
                <Fields label={`${section.title}: parsed data`} rows={rows.map(([k, v]) => [<code key="k">{k}</code>, v])} />
              </Disclosure>
            )}
          </>
        ),
      };
    }
  }

  return (
    <ViewLayout
      id="innodb"
      title="InnoDB"
      className="innodb-panel"
      onBack={onBack}
      info={
        <>
          <p>
            SHOW ENGINE INNODB STATUS, parsed into sections: latches, transactions and purge, the latest deadlock and
            foreign key error, file I/O, the change buffer, the adaptive hash index, the redo log, the buffer pool and
            row operations.
          </p>
          <p>Its averages cover the window since the server last printed the status. It needs the PROCESS privilege and only reads.</p>
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={disabled}
      loading={!!loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a row to see its section's numbers, rules and related views.</p>}
      status={<ViewStatus loading={loading} error={error} />}
    >
      {!report && !loading && !error && (
        <EmptyState
          icon="engine"
          title="No InnoDB status yet"
          action={
            <button type="button" className="button primary" disabled={disabled} onClick={() => void load()}>
              Load InnoDB status
            </button>
          }
        >
          Transactions, purge, the redo log and the buffer pool from SHOW ENGINE INNODB STATUS. It only reads.
        </EmptyState>
      )}
      {report && !report.available && (
        <PrerequisiteGate>{report.error?.message || "SHOW ENGINE INNODB STATUS is not available on this connection."}</PrerequisiteGate>
      )}
      {report && report.available && (
        <>
          <SummaryLine tone={tone(report.summary.status)} notes={report.notes}>
            {flagged.length
              ? `${flagged.length} of ${sections.length} sections need attention`
              : "Nothing needs attention"}
            {report.header?.window_s != null && <span className="summary-muted"> · {report.header.window_s} s window</span>}
          </SummaryLine>
          <FactStrip
            label="Server"
            facts={[
              ["Server", engineLabel(report)],
              ["Uptime", uptime(report.uptime_s)],
              ...(report.header?.printed_at ? ([["Printed", report.header.printed_at]] as [string, string][]) : []),
            ]}
          />
          {report.parse && !report.parse.complete && (
            <PrerequisiteGate>
              The status output was incomplete{report.parse.truncated_by_server ? " (the server cut it at its size limit)" : ""}, so some
              sections may be missing.
            </PrerequisiteGate>
          )}
          {sections.map((section) => {
            const id = `innodb-${section.key}`;
            const rows = section.present ? keyItems(section) : [];
            const more = section.present ? section.items.length - rows.length : 0;
            return (
              <Disclosure
                key={`${bound?.key}:${section.key}:${section.status}`}
                id={id}
                className={`innodb-section is-${section.status}`}
                open={section.status === "watch" || section.status === "problem"}
                title={
                  <>
                    <span className="innodb-section-title">{section.title}</span>
                    {section.present ? <StatusPill tone={tone(section.status)} /> : <StatusPill tone="off" label="Not in output" />}
                    <span className="innodb-section-summary" title={section.summary}>{section.summary}</span>
                  </>
                }
              >
                <ul className="grouped-rows">
                  {section.present ? (
                    rows.length ? (
                      rows.map((i) => (
                        <GroupedRow
                          key={i.key}
                          rowKey={`section:${section.key}:${i.key}`}
                          selection={selection}
                          tone={i.status ? tone(i.status) : undefined}
                          label={i.label}
                          value={<span title={i.text}>{i.text}</span>}
                          attrs={{ "data-section": section.key, "data-item": i.key }}
                        />
                      ))
                    ) : (
                      <GroupedRow
                        rowKey={`section:${section.key}`}
                        selection={selection}
                        label="Details"
                        attrs={{ "data-section": section.key }}
                      />
                    )
                  ) : (
                    <GroupedRow
                      rowKey={`section:${section.key}`}
                      selection={selection}
                      label={<span className="cell-muted">{section.summary || "Not printed by this server"}</span>}
                      attrs={{ "data-section": section.key }}
                    />
                  )}
                  {more > 0 && (
                    <GroupedRow
                      rowKey={`section:${section.key}:more`}
                      selection={selection}
                      label={<span className="cell-muted">{more} more</span>}
                      attrs={{ "data-section": section.key, "data-item": "more" }}
                    />
                  )}
                </ul>
              </Disclosure>
            );
          })}
          <p className="grouped-footer innodb-raw-line">
            <button
              type="button"
              className="text-button"
              data-row-key="raw"
              disabled={!!other && !report.raw}
              onClick={async () => {
                selection.select("raw", true);
                if (!report.raw && !other) await load(true);
              }}
            >
              Show raw output
            </button>{" "}
            for support (the full SHOW ENGINE INNODB STATUS text).
          </p>
        </>
      )}
    </ViewLayout>
  );
}
