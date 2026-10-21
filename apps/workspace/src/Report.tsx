// Tuning > Full analysis: one read-only run of every Server view, on request
// (report-1.0 from myflames/report.py). One summary line and the server
// facts, Look at first, the findings to look at as one-line rows ranked by
// the report, Not judged for checks that could not run (with the reason),
// and the sections as a grouped list. The verdict, the numbers, the rule, the
// caveat, the statements and my.cnf lines to copy, and the statements behind
// a number are in the inspector; myflames never applies a recommendation.
//
// The report is bound to the connection it was read from (binding.ts): after
// a switch it stays on screen, greyed, with its server actions off. It is
// never saved; the workspace remembers only that it ran. Export turns the
// JSON into HTML, Markdown, JSON or text on the server (never from the page).
import {
  type AnalysisFinding,
  type AnalysisLink,
  type AnalysisReport,
  type AnalysisSource,
  type AnalysisStatus,
  type Connection,
  type TopStatement,
} from "./api";
import { connectionLabel } from "./binding";
import { LESSONS, uptime } from "./Health";
import { CopySql, engineLabel } from "./PerfShared";
import { StatementRows, coverageNotes, type DigestAction, type LabSettings } from "./SlowQueries";
import {
  Disclosure,
  FactStrip,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorSection,
  PrerequisiteGate,
  StatusPill,
  SummaryLine,
  isIdentifier,
  type InspectorContent,
  type Selection,
} from "./kit";
import "./report.css";

/** The connection a report was read from, without secrets: what an export names. */
export function analysisSource(c: Connection): AnalysisSource {
  const type = (c as Connection & { type?: string }).type;
  return {
    label: connectionLabel(c),
    host: c.host,
    port: Number(c.port) || 0,
    user: c.user,
    ...(type === "production" || type === "development" ? { type } : {}),
  };
}

/** What a run usually takes (Health, Waits and I/O sample a 10 s window). */
export const REPORT_ESTIMATE = "about 10 to 20 s";
export const STATUS_WORD: Record<AnalysisStatus, string> = {
  problem: "Problem",
  watch: "Watch",
  unknown: "Not judged",
  info: "No threshold",
  ok: "OK",
};

export function plural(n: number, one: string, many: string) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
export function clock(iso: string | null | undefined) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}
/** Mirrors report.coverage_lines: what the statements behind the numbers cover. */
export function attributionNotes(report: AnalysisReport): string[] {
  const a = report.attribution;
  if (!a) return [];
  if (!a.available) return [a.gate || "Statements could not be linked to these numbers.", ...(a.fix && a.fix !== a.gate ? [a.fix] : [])];
  const out = coverageNotes(a.coverage);
  out.unshift("Statement counters are cumulative since the server started or the digest table was truncated, not over Health's window.");
  const w = a.health_window;
  if (w && w.elapsed_s !== null && w.select_full_join !== null)
    out.push(`Health counted ${w.select_full_join.toLocaleString()} joins without an index in its ${Math.round(w.elapsed_s)} s window.`);
  if (a.coverage?.overflow)
    out.push(
      `The digest table is full: ${a.coverage.overflow.executions.toLocaleString()} executions were summed into its overflow row and are not attributed.`,
    );
  for (const t of a.full_scans || []) if (t.capped) out.push(`For ${t.schema}.${t.table} only the heaviest digests that name it were checked.`);
  return [...out, ...(a.caveats || [])];
}

export function ReportDiagnosticBody({
  report,
  hostLabel,
  selection,
}: {
  report: AnalysisReport;
  hostLabel?: string;
  selection: Selection;
}) {
  const findings = report.findings;
  const byId = new Map(findings.map((f) => [f.id, f]));
  const first = report.look_first ? byId.get(report.look_first) || null : null;
  const look = findings.filter((f) => f.id !== first?.id && (f.status === "problem" || f.status === "watch"));
  const info = findings.filter((f) => f.status === "info");
  const unknown = findings.filter((f) => f.status === "unknown");
  const failures = report.header.failures || [];
  const s = report.summary;

  const notes: string[] = [
    ...report.notes,
    ...attributionNotes(report),
    ...report.checklist.not_checked.map((n) => `Not checked: ${n.label}. ${n.reason}`),
  ];

  const row = (f: AnalysisFinding, prefix = "finding", detail?: string) => (
    <GroupedRow
      key={`${prefix}:${f.id}`}
      rowKey={`${prefix}:${f.id}`}
      selection={selection}
      tone={f.status}
      toneLabel={STATUS_WORD[f.status]}
      label={f.title}
      detail={detail}
      value={f.headline ? <span title={f.headline}>{f.headline}</span> : undefined}
      attrs={{ "data-finding": f.id }}
      className="report-finding"
      wrap
    />
  );

  return (
    <>
      <SummaryLine tone={s.status} toneLabel={STATUS_WORD[s.status]} notes={notes}>
        {s.text}
      </SummaryLine>
      <FactStrip
        label="Full analysis"
        facts={[
          ["Server", engineLabel({ engine: report.engine, server_version: report.server_version, server_product: report.server_product })],
          ["Host", hostLabel || ""],
          ["Uptime", uptime(report.uptime_s)],
          ["Taken", `${clock(report.taken_at)}, in ${(report.header.elapsed_s ?? 0).toFixed(1)} s`],
          ["Databases", report.header.scope.text],
          ["Views read", failures.length ? `${report.header.views.length - failures.length} of ${report.header.views.length}` : `all ${report.header.views.length}`],
        ]}
      />
      {!report.attribution.available && report.attribution.gate && (
        <PrerequisiteGate>{report.attribution.gate}</PrerequisiteGate>
      )}
      {first && (
        <GroupedList id="report-first-h" title="Look at first" className="report-first">
          {row(first, "first")}
        </GroupedList>
      )}
      <GroupedList
        id="report-findings-h"
        title="To look at"
        count={look.length}
        className="report-look"
        aside={<span>{plural(look.filter((f) => f.status === "problem").length, "problem", "problems")}, {look.filter((f) => f.status === "watch").length} to watch</span>}
      >
        {look.length ? (
          look.map((f) => row(f))
        ) : (
          <GroupedRow rowKey="none" tone={first ? "info" : "ok"} label={first ? "No other problem or watch finding" : "Nothing to look at: no problem or watch finding"} />
        )}
      </GroupedList>
      {(unknown.length > 0 || failures.length > 0) && (
        <GroupedList
          id="report-unknown-h"
          title="Not judged"
          count={unknown.length + failures.length}
          className="report-unknown"
          info={<p>Checks that could not run, with the reason: a missing privilege, Performance Schema off, or a view that failed. They never count as OK.</p>}
        >
          {failures.map((x) => (
            <GroupedRow
              key={`view:${x.view}`}
              rowKey={`view:${x.view}`}
              selection={selection}
              tone="unknown"
              toneLabel={x.status === "skipped" ? "Skipped" : "Failed"}
              label={x.label}
              detail={x.error}
              attrs={{ "data-view-failure": x.view }}
              className="report-finding"
            />
          ))}
          {unknown.map((f) => row(f, "finding", f.verdict))}
        </GroupedList>
      )}
      <GroupedList id="report-sections-h" title="Sections" count={report.sections.length} className="report-sections">
        {report.sections.map((section) => (
          <GroupedRow
            key={section.key}
            rowKey={`section:${section.key}`}
            selection={selection}
            tone={section.status}
            toneLabel={STATUS_WORD[section.status]}
            label={section.title}
            value={<span title={section.summary}>{section.summary}</span>}
            wrap
            attrs={{ "data-section": section.key }}
            className="report-section"
          />
        ))}
      </GroupedList>
      {info.length > 0 && (
        <Disclosure title="For information, no rule applies" count={info.length} className="report-info">
          <ul className="grouped-rows" data-rows>
            {info.map((f) => row(f))}
          </ul>
        </Disclosure>
      )}
    </>
  );
}

export function buildReportInspector({
  report,
  selectionKey,
  onSelectKey,
  boundKey,
  actionsOff = false,
  connection,
  settings,
  onDigest,
  onOpenLink,
  onOpenLesson,
}: {
  report: AnalysisReport;
  selectionKey: string;
  onSelectKey: (key: string, focus?: boolean) => void;
  boundKey?: string | null;
  actionsOff?: boolean;
  connection?: Connection;
  settings?: LabSettings;
  onDigest?: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
  onOpenLink?: (link: AnalysisLink, finding: AnalysisFinding, from: string) => void;
  onOpenLesson?: (lesson: string) => void;
}): InspectorContent | null {
  const byId = new Map(report.findings.map((f) => [f.id, f]));
  const failures = report.header.failures || [];
  const [type, name] = selectionKey.split(/:(.*)/s);

  function openFromInspector(key: string) {
    onSelectKey(key, true);
    window.setTimeout(() => {
      const title = document.querySelector<HTMLElement>(".view-inspector .inspector-title h3");
      if (!title) return;
      title.tabIndex = -1;
      title.focus();
    });
  }

  function attribution(finding: AnalysisFinding) {
    const a = report.attribution;
    const act = (action: DigestAction, r: TopStatement, rank: number) => {
      if (boundKey && onDigest) onDigest(r, rank, action, boundKey);
    };
    const about = (
      <p>
        Statements Performance Schema counted the same events for, read with the analysis. Their counters run since the
        server started or the digest table was truncated, not over Health's window.
      </p>
    );
    if (!a.available)
      return (
        <InspectorSection title="Statements behind it" info={about}>
          <p className="hint">{a.gate || "Statements could not be linked to this number."}</p>
        </InspectorSection>
      );
    if (finding.attribution === "full_joins") {
      const statements = a.full_joins?.statements || [];
      return (
        <InspectorSection title="Statements behind it" info={about}>
          {statements.length && connection && settings ? (
            <StatementRows
              statements={statements}
              order="full_join"
              connection={connection}
              disabled={actionsOff}
              settings={settings}
              onAction={act}
            />
          ) : (
            <p className="hint">No recorded statement has joins without an index.</p>
          )}
        </InspectorSection>
      );
    }
    return (
      <InspectorSection title="Statements that name each table" info={about}>
        {(a.full_scans || []).map((t) => {
          const table = { schema: t.schema, table: t.table };
          return (
            <div key={`${t.schema}.${t.table}`} className="report-scan-table" data-table={`${t.schema}.${t.table}`}>
              <h5>
                <code>
                  {t.schema}.{t.table}
                </code>{" "}
                <span className="hint">{t.label}</span>
              </h5>
              {t.error ? (
                <p className="error">Could not read: {t.error}</p>
              ) : t.statements.length && connection && settings ? (
                <StatementRows
                  statements={t.statements}
                  order="no_index"
                  table={table}
                  connection={connection}
                  disabled={actionsOff}
                  settings={settings}
                  onAction={act}
                />
              ) : (
                <p className="hint">No recorded statement names it and reads without an index.</p>
              )}
            </div>
          );
        })}
      </InspectorSection>
    );
  }

  function findingInspector(f: AnalysisFinding): InspectorContent {
    const lesson = f.learn && LESSONS[f.learn] && onOpenLesson ? f.learn : "";
    const action = f.action;
    return {
      key: selectionKey,
      title: f.title,
      meta: (
        <>
          <StatusPill tone={f.status} label={STATUS_WORD[f.status]} />
          {f.headline && <span>{f.headline}</span>}
        </>
      ),
      actions:
        (f.links.length && onOpenLink) || lesson ? (
          <div className="inspector-links">
            {onOpenLink &&
              f.links.map((link, i) => (
                <button
                  key={i}
                  type="button"
                  className="button"
                  title={link.label}
                  disabled={link.view === "top" && actionsOff}
                  onClick={() => onOpenLink(link, f, boundKey || "")}
                >
                  {link.label}
                </button>
              ))}
            {lesson && (
              <button type="button" className="button" onClick={() => onOpenLesson?.(lesson)}>
                Lesson: {LESSONS[lesson]}
              </button>
            )}
          </div>
        ) : undefined,
      body: (
        <>
          <InspectorSection title={f.status === "unknown" ? "Why it was not judged" : "Verdict"}>
            <p>{f.verdict || "No verdict."}</p>
          </InspectorSection>
          {f.numbers.length > 0 && (
            <InspectorSection title="Numbers">
              <Fields label={`${f.title}: numbers`} rows={f.numbers.map((n) => [isIdentifier(n.label) ? <code>{n.label}</code> : n.label, n.text])} />
            </InspectorSection>
          )}
          {f.settings.length > 0 && (
            <InspectorSection title="Settings">
              <Fields
                label={`${f.title}: settings`}
                rows={f.settings.map((v) => [
                  <code key="n">{v.name}</code>,
                  <>
                    {v.display ?? v.value ?? "?"}
                    {v.default !== null && v.default !== undefined && <span className="hint"> · default {v.default}</span>}
                  </>,
                ])}
              />
            </InspectorSection>
          )}
          {f.rule && (
            <InspectorSection title="Rule">
              <p>Rule of thumb: {f.rule}</p>
            </InspectorSection>
          )}
          {f.why && (
            <InspectorSection title="Why it matters">
              <p>{f.why}</p>
            </InspectorSection>
          )}
          {action && (action.text || action.sql || action.config) && (
            <InspectorSection title="What to do">
              {action.text && <p>{action.text}</p>}
              {action.sql && <CopySql sql={action.sql} label={`${f.title}: statement`} />}
              {action.config && <CopySql sql={action.config} label={`${f.title}: my.cnf line`} />}
              {(action.sql || action.config) && <p className="hint">Suggested only: myflames never runs or applies it.</p>}
              {f.caveat && <p className="hint perf-caveat">Caveat: {f.caveat}</p>}
            </InspectorSection>
          )}
          {!action && f.caveat && (
            <InspectorSection title="Caveat">
              <p className="hint perf-caveat">{f.caveat}</p>
            </InspectorSection>
          )}
          {f.attribution && attribution(f)}
        </>
      ),
    };
  }

  if (type === "finding" || type === "first") {
    const f = byId.get(name);
    if (f) return findingInspector(f);
  } else if (type === "section") {
    const section = report.sections.find((x) => x.key === name);
    if (section) {
      const own = section.findings.map((id) => byId.get(id)).filter((f): f is AnalysisFinding => !!f);
      return {
        key: selectionKey,
        title: section.title,
        meta: <StatusPill tone={section.status} label={STATUS_WORD[section.status]} />,
        body: (
          <>
            <InspectorSection title="Summary">
              <p>{section.summary}</p>
              {section.notes.length > 0 && section.notes.join(" ") !== section.summary && (
                <ul className="report-notes">
                  {section.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              )}
            </InspectorSection>
            {own.length > 0 && (
              <InspectorSection title={`Findings (${own.length})`}>
                <ul className="report-section-findings">
                  {own.map((f) => (
                    <li key={f.id} data-finding={f.id}>
                      <button type="button" className="report-section-link" onClick={() => openFromInspector(`finding:${f.id}`)}>
                        <StatusPill tone={f.status} label={STATUS_WORD[f.status]} />
                        <span className="report-section-title">{f.title}</span>
                        {f.headline && <span className="report-section-value">{f.headline}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              </InspectorSection>
            )}
            <p className="hint">From {section.views.join(", ")}</p>
          </>
        ),
      };
    }
  } else if (type === "view") {
    const failure = failures.find((x) => x.view === name);
    const view = report.header.views.find((v) => v.key === name);
    if (failure) {
      return {
        key: selectionKey,
        title: failure.label,
        meta: <StatusPill tone="unknown" label={failure.status === "skipped" ? "Skipped" : "Failed"} />,
        body: (
          <>
            <InspectorSection title={failure.status === "skipped" ? "Why it was skipped" : "Why it failed"}>
              <p>{failure.error}</p>
            </InspectorSection>
            <p className="hint">The sections this view feeds say what is missing; nothing else stopped.</p>
            {view && view.notes.length > 0 && (
              <InspectorSection title="Notes">
                <ul className="report-notes">
                  {view.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              </InspectorSection>
            )}
          </>
        ),
      };
    }
  }

  return null;
}

