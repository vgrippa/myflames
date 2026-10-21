// Tuning place: a scorecard over the statuses the Server views already decide
// (Health, Parameters, Tables and the Full analysis), plus the Full analysis
// itself in its own tab.
//
// The scorecard reads only reports of the connection in use. Unknown checks
// are shown with the reason and the view that measures them, and never count
// toward the score (tuningRules.ts has the scoring rule). Suggested statements
// are copied or opened in the Query editor; nothing on this page runs them.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  download,
  type AnalysisExport,
  type AnalysisFinding,
  type AnalysisLink,
  type AnalysisReport,
  type AnalysisSource,
  type ConfigReport,
  type Connection,
  type HealthReport,
  type SessionsReport,
  type TablesReport,
  type TopStatement,
} from "./api";
import { connectionKey, type Bound, type ConnectionReads } from "./binding";
import { Icon } from "./components";
import { CopySql } from "./PerfShared";
import { buildReportInspector, REPORT_ESTIMATE, ReportDiagnosticBody } from "./Report";
import type { ServerKind } from "./session";
import { StatementRows, StatementsBehind, type DigestAction, type LabSettings, type OpenTop } from "./SlowQueries";
import {
  EmptyState,
  ExportMenu,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorSection,
  Popover,
  SearchField,
  Segmented,
  StatusPill,
  ViewLayout,
  ViewStatus,
  type InspectorContent,
  type Selection,
} from "./kit";
import {
  CATEGORY_ORDER,
  coverageText,
  evaluateTuning,
  GRADE_RULE,
  GRADE_SCALE,
  gradeRangeText,
  gradeReason,
  letterGrade,
  PROBLEM_GRADE_CAP,
  SCORE_RULE,
  STATUS_TEXT,
  summaryText,
  TUNING_CATEGORIES,
  tuningHtml,
  tuningMarkdown,
  type AnalysisReportInput,
  type TuningCategoryKey,
  tuningInputs,
  type BoundLike,
  type TuningCheck,
  type TuningMeasureWith,
  type TuningReport,
} from "./tuningRules";
import "./tuning.css";

export type TuningTab = "scorecard" | "analysis";
type ListFilter = "attention" | "unknown" | "all";

const ANALYSIS_FORMATS: [string, string][] = [
  ["html", "HTML"],
  ["markdown", "Markdown"],
  ["json", "JSON"],
  ["text", "Text"],
];
const MEASURE_VIEW: Record<TuningMeasureWith, { view: ServerKind | "report"; label: string }> = {
  health: { view: "health", label: "Open Health" },
  config: { view: "config", label: "Open Parameters" },
  tables: { view: "tables", label: "Open Tables" },
  report: { view: "report", label: "Run full analysis" },
};

interface TuningViewProps {
  connection?: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  /** The Full analysis of the place (Server owns the read). */
  report?: Bound<AnalysisReport> | null;
  /** Bound reports: each is used only while its key is the connection in use. */
  health?: BoundLike<HealthReport>;
  config?: BoundLike<ConfigReport>;
  tables?: BoundLike<TablesReport>;
  /** Deprecated: unbound reports from the first call site, used only when the bound prop is absent. */
  healthReport?: HealthReport | null;
  configReport?: ConfigReport | null;
  tablesReport?: TablesReport | null;
  sessionsReport?: SessionsReport | null;
  source?: AnalysisSource | null;
  reads?: ConnectionReads;
  /** Refresh all can own the analysis read while this view is not mounted. */
  refreshRunning?: boolean;
  /** Start of the in-flight read, owned by Server so remounting does not reset it. */
  startedAt?: number;
  wasRun?: boolean;
  settings?: LabSettings;
  onDigest?: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
  onOpenLink?: (link: AnalysisLink, finding: AnalysisFinding, from: string) => void;
  onOpenLesson?: (lesson: string) => void;
  onRunReport?: () => void | Promise<void>;
  onOpenSql?: (sql: string) => void;
  onOpenView?: (view: ServerKind | string) => void;
  /** Top statements ordered by a counter (Joins without an index). */
  onOpenTop?: OpenTop;
  /** Open on a tab; each new n switches to it again (links to the Full analysis). */
  tabRequest?: { tab: TuningTab; n: number } | null;
  /** Refresh all is reading the Server views: the Full analysis waits for it,
   * so it never starts a second worker pool beside the global queue. */
  runBlocked?: boolean;
}

export function TuningView({
  connection,
  disabled,
  runBlocked,
  onBack,
  selection,
  report: boundReport,
  health,
  config,
  tables,
  healthReport,
  configReport,
  tablesReport,
  source,
  reads,
  refreshRunning,
  startedAt,
  wasRun,
  settings,
  onDigest,
  onOpenLink,
  onOpenLesson,
  onRunReport,
  onOpenSql,
  onOpenView,
  onOpenTop,
  tabRequest,
}: TuningViewProps) {
  const [tab, setTab] = useState<TuningTab>(tabRequest?.tab || "scorecard");
  const [category, setCategory] = useState<TuningCategoryKey | "all">("all");
  const [filter, setFilter] = useState<ListFilter>("attention");
  const [query, setQuery] = useState("");
  const [exporting, setExporting] = useState("");
  const [exported, setExported] = useState("");
  const [exportError, setExportError] = useState("");
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (tabRequest) setTab(tabRequest.tab);
  }, [tabRequest?.tab, tabRequest?.n]); // eslint-disable-line react-hooks/exhaustive-deps

  const activeKey = connection ? connectionKey(connection) : reads?.key || "";
  const { input, other } = useMemo(
    () =>
      tuningInputs({
        activeKey,
        report: boundReport,
        health,
        config,
        tables,
        legacy: { health: healthReport, config: configReport, tables: tablesReport },
      }),
    [activeKey, boundReport, health, config, tables, healthReport, configReport, tablesReport],
  );
  const tuning: TuningReport = useMemo(() => evaluateTuning(input), [input]);
  const analysis = boundReport?.report ?? null;
  const analysisOther = boundReport && boundReport.key !== activeKey ? boundReport.label : null;
  const loading = reads?.loading || "";
  const running = !!loading || !!refreshRunning;

  // While the analysis runs, count the seconds (it usually takes 10 to 20).
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);
  const elapsed = running && startedAt !== undefined ? Math.max(0, Math.round((now - startedAt) / 1000)) : 0;

  // A new Full analysis starts on its Look at first pick, on that tab only.
  const selectRef = useRef(selection.select);
  selectRef.current = selection.select;
  const hasSelection = !!selection.key;
  useEffect(() => {
    if (tab === "analysis" && analysis?.look_first && !hasSelection) selectRef.current(`first:${analysis.look_first}`, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysis, tab]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tuning.checks.filter((c) => {
      if (category !== "all" && c.category !== category) return false;
      if (filter === "attention" && !(c.applies && (c.status === "problem" || c.status === "watch"))) return false;
      if (filter === "unknown" && !(c.status === "unknown")) return false;
      if (!q) return true;
      return [c.title, c.value, c.verdict, c.unknownReason || "", c.fix?.sql || "", ...c.settings.map((s) => s.name)]
        .join(" ")
        .toLowerCase()
        .includes(q);
    });
  }, [tuning.checks, category, filter, query]);
  const inCategory = (c: TuningCheck) => category === "all" || c.category === category;
  const counts = {
    attention: tuning.checks.filter((c) => inCategory(c) && c.applies && (c.status === "problem" || c.status === "watch")).length,
    unknown: tuning.checks.filter((c) => inCategory(c) && c.status === "unknown").length,
    all: tuning.checks.filter(inCategory).length,
  };

  const run = () => {
    if (disabled || runBlocked || running || !onRunReport) return;
    setExported("");
    setExportError("");
    void onRunReport();
  };
  // The scorecard updates when the report arrives; it stays on this tab.
  const open = (view: TuningMeasureWith) => {
    if (view === "report") run();
    else onOpenView?.(MEASURE_VIEW[view].view);
  };

  function exportScorecard(format: "html" | "markdown" | "json") {
    const label = other || undefined;
    const date = tuning.generatedAt.slice(0, 10);
    if (format === "json") download(JSON.stringify(tuning, null, 2), `tuning-${date}.json`, "application/json");
    else if (format === "markdown") download(tuningMarkdown(tuning, label), `tuning-${date}.md`, "text/markdown");
    else download(tuningHtml(tuning, label), `tuning-${date}.html`, "text/html");
    setExported(`tuning-${date}.${format === "markdown" ? "md" : format}`);
  }
  async function exportAnalysis(format: string) {
    if (!analysis || exporting) return;
    setExporting(format);
    setExported("");
    setExportError("");
    try {
      // The file names the connection it was read from (never a password).
      const file = await api<AnalysisExport>("report/export", { report: analysis, format, ...(source ? { source } : {}) });
      download(file.content, file.filename, file.mime);
      setExported(file.filename);
    } catch (e) {
      setExportError((e as Error).message);
    } finally {
      setExporting("");
    }
  }

  // ---- Inspector ------------------------------------------------------------------
  let inspector: InspectorContent | null = null;
  const key = selection.key || "";
  if (tab === "scorecard" && key.startsWith("check:")) {
    const check = tuning.checks.find((c) => c.key === key.slice(6));
    if (check)
      inspector = checkInspector(check, {
        actionsOff: disabled || !!other,
        other: !!other,
        // Statements behind a number: only from a Full analysis of the connection in use.
        analysis: boundReport && boundReport.key === activeKey ? boundReport.report : null,
        activeKey,
        connection,
        settings,
        onDigest,
        onOpenTop,
        onOpenSql,
        onMeasure: open,
        canMeasure: (v) => (v === "report" ? !!onRunReport : !!onOpenView),
      });
  } else if (tab === "analysis" && analysis && /^(finding|first|section|view):/.test(key)) {
    inspector = buildReportInspector({
      report: analysis,
      selectionKey: key,
      onSelectKey: selection.select,
      boundKey: boundReport?.key,
      actionsOff: disabled || !!analysisOther,
      connection,
      settings,
      onDigest,
      onOpenLink,
      onOpenLesson,
    });
  }

  const exportMenu =
    tab === "scorecard" ? (
      <ExportMenu
        label="Export the tuning scorecard"
        className="report-export"
        disabled={!!other}
        title={other ? `The scorecard is from ${other}: run the full analysis to load this server`
          : `The scorecard as shown: ${coverageText(tuning)}; nothing is read again.`}
        formats={(["html", "markdown", "json"] as const).map(format => ({
          key: format,
          label: format === "html" ? "HTML" : format === "markdown" ? "Markdown" : "JSON",
          onSelect: () => exportScorecard(format),
        }))}
      />
    ) : analysis ? (
      <ExportMenu
        label="Export the full analysis"
        className="report-export"
        disabled={!!exporting}
        title={`Made from the report as read${analysisOther ? ` on ${analysisOther}` : ""}; nothing is read again.`}
        formats={ANALYSIS_FORMATS.map(([format, label]) => ({
          key: format, label, onSelect: () => void exportAnalysis(format),
        }))}
      />
    ) : null;

  // Scorecard | Full analysis: the shared segmented control in the header row,
  // with tab semantics (tuning-tab-*/tuning-panel-* ids, one tab stop).
  const tabs = (
    <Segmented<TuningTab>
      label="Tuning"
      tabs="tuning"
      className="tuning-tabs"
      value={tab}
      onChange={setTab}
      options={[
        { value: "scorecard", label: "Scorecard" },
        { value: "analysis", label: "Full analysis" },
      ]}
    />
  );

  return (
    <ViewLayout
      id="tuning"
      title="Tuning"
      className="tuning-panel server-aligned-values"
      onBack={onBack}
      info={
        tab === "scorecard" ? (
          <>
            <p>
              A score from the checks the Server views already judge: Health, Parameters, Tables and the Full
              analysis. Each check shows what was measured, the rule and what to do.
            </p>
            <p>{SCORE_RULE}</p>
            <p>{GRADE_RULE}</p>
            <p>Suggested statements are for you to copy or open in the Query editor; nothing here runs them.</p>
          </>
        ) : (
          <>
            <p>
              Runs every Server view once on the connection in use, {REPORT_ESTIMATE}, and ranks what it finds: problems
              first, then what to watch, with the rule each one uses.
            </p>
            <p>It only reads. Suggested statements and my.cnf lines are for you to copy; myflames never applies them.</p>
          </>
        )
      }
      controls={
        <>
          {tabs}
          {exportMenu}
          {tab === "scorecard" && onRunReport && (
            <button
              type="button"
              className="button"
              data-action="run-analysis"
              disabled={disabled || !!runBlocked || running}
              title={`Runs every Server view once, ${REPORT_ESTIMATE}. It only reads.`}
              onClick={run}
            >
              {running ? <span className="spinner" aria-hidden="true" /> : <Icon name="analysis" size={15} />}
              Run full analysis
            </button>
          )}
        </>
      }
      onRefresh={tab === "analysis" && onRunReport ? run : undefined}
      refreshDisabled={disabled || !!runBlocked}
      loading={running}
      otherConnection={tab === "scorecard" ? other : analysisOther}
      // The scorecard has no Refresh: the full analysis reads this server.
      otherAction={tab === "scorecard" && onRunReport ? { label: "Run full analysis", onClick: run, disabled: disabled || !!runBlocked || running } : undefined}
      inspector={inspector}
      inspectorEmpty={
        <p>
          {tab === "scorecard"
            ? "Select a check to see what was measured, the rule and what to do."
            : "Select a finding or a section to see the verdict, the numbers, the rule and what to do."}
        </p>
      }
      status={
        <>
          <ViewStatus
            loading={running ? `Running every Server view once: ${elapsed} s so far, ${REPORT_ESTIMATE} in all. It only reads.` : ""}
            error={reads?.error || exportError}
          />
          {exported && <p className="hint" role="status">Saved {exported}</p>}
        </>
      }
    >
      {tab === "scorecard" ? (
        <div className="tuning tuning-view" id="tuning-panel-scorecard" role="tabpanel" aria-labelledby="tuning-tab-scorecard">
          <p className="visually-hidden" role="status">
            {summaryText(tuning)}
          </p>
          <ScoreHeader tuning={tuning} running={running || !!runBlocked} disabled={disabled || !!other} onMeasure={open} canRun={!!onRunReport} />
          <ul className="tuning-categories" aria-label="Categories">
            {tuning.categories.map((c) => {
              const active = category === c.key;
              return (
                <li key={c.key}>
                  <button
                    type="button"
                    className={`tuning-category is-${c.status}${active ? " is-active" : ""}`}
                    aria-pressed={active}
                    data-category={c.key}
                    title={`${TUNING_CATEGORIES[c.key].description} ${Math.round(c.weight * 100)}% of the score.`}
                    onClick={() => setCategory(active ? "all" : c.key)}
                  >
                    <span className="tuning-category-name">{c.name}</span>
                    <span className="tuning-category-score">
                      {c.score === null ? <span className="tuning-category-none">Not measured</span> : `${c.score}%`}
                    </span>
                    <span className="tuning-meter" aria-hidden="true">
                      <span style={{ width: `${c.score ?? 0}%` }} />
                    </span>
                    <span className="tuning-category-foot">
                      {c.measured} of {c.total} measured
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="tuning-filters">
            <Segmented<ListFilter>
              label="Show"
              value={filter}
              onChange={setFilter}
              options={[
                { value: "attention", label: "Needs attention", count: counts.attention },
                { value: "unknown", label: "Not measured", count: counts.unknown },
                { value: "all", label: "All checks", count: counts.all },
              ]}
            />
            <SearchField label="Search checks" value={query} onChange={setQuery} placeholder="Search checks or SQL" />
          </div>
          {visible.length === 0 ? (
            <p className="tuning-empty" role="status">
              {query
                ? "No check matches the search."
                : filter === "attention"
                  ? tuning.measured
                    ? `No problem or watch among the ${tuning.measured} measured checks${category !== "all" ? " in this category" : ""}.`
                    : "Nothing measured yet, so nothing to flag."
                  : filter === "unknown"
                    ? "Every check here was measured."
                    : "No check in this category."}
            </p>
          ) : (
            CATEGORY_ORDER.filter((k) => visible.some((c) => c.category === k)).map((k) => (
              <GroupedList key={k} id={`tuning-group-${k}`} title={TUNING_CATEGORIES[k].name} className="tuning-group">
                {visible
                  .filter((c) => c.category === k)
                  .map((c) => (
                    <GroupedRow
                      key={c.key}
                      rowKey={`check:${c.key}`}
                      selection={selection}
                      tone={c.applies ? c.status : "off"}
                      toneLabel={c.applies ? STATUS_TEXT[c.status] : "Does not apply"}
                      label={c.title}
                      detail={c.status === "unknown" ? (c.source ? c.unknownReason : undefined) : c.verdict}
                      value={
                        c.status === "unknown" ? (
                          <span className="tuning-needs" title={c.unknownReason}>{needsText(c)}</span>
                        ) : (
                          <span title={c.value}>{c.value}</span>
                        )
                      }
                      attrs={{ "data-check": c.key, "data-status": c.applies ? c.status : "not-applicable" }}
                      className="tuning-check"
                      wrap
                    />
                  ))}
              </GroupedList>
            ))
          )}
        </div>
      ) : (
        <div className="report-panel tuning-analysis tuning-view" id="tuning-panel-analysis" role="tabpanel" aria-labelledby="tuning-tab-analysis">
          {analysis ? (
            <ReportDiagnosticBody report={analysis} hostLabel={boundReport?.label} selection={selection} />
          ) : (
            !running && (
              <EmptyState
                icon="analysis"
                title="No full analysis yet"
                action={
                  onRunReport && (
                    <button type="button" className="button primary" disabled={disabled || !!runBlocked} onClick={run}>
                      Run full analysis
                    </button>
                  )
                }
              >
                {wasRun
                  ? `A full analysis ran in your last session. Reports are not saved: run it again, ${REPORT_ESTIMATE}. It only reads.`
                  : `Runs every Server view once and ranks what to look at first, ${REPORT_ESTIMATE}. It only reads.`}
              </EmptyState>
            )
          )}
        </div>
      )}
    </ViewLayout>
  );
}

const MEASURE_NAME: Record<TuningMeasureWith, string> = {
  health: "Health",
  config: "Parameters",
  tables: "Tables",
  report: "Full analysis",
};
/** A short value for an unknown row: what would measure it, or why it was not judged. */
function needsText(c: TuningCheck): string {
  if (!c.applies) return "Does not apply";
  if (c.source) return "Not judged";
  return `Needs ${c.measureWith.map((v) => MEASURE_NAME[v]).join(" or ")}`;
}

function ScoreHeader({
  tuning,
  running,
  disabled,
  canRun,
  onMeasure,
}: {
  tuning: TuningReport;
  running: boolean;
  disabled: boolean;
  canRun: boolean;
  onMeasure: (view: TuningMeasureWith) => void;
}) {
  const unknown = tuning.checks.filter((c) => c.applies && c.status === "unknown");
  const needsAnalysis = unknown.some((c) => c.measureWith.includes("report"));
  const onlyParameters = unknown.length > 0 && unknown.every((c) => c.measureWith.length === 1 && c.measureWith[0] === "config");
  const reason = gradeReason(tuning);
  // A problem lowered the grade below the score's own range.
  const capped = tuning.grade !== null && tuning.score !== null && letterGrade(tuning.score) !== tuning.grade;
  return (
    <section className={`tuning-score is-${tuning.status}`} aria-labelledby="tuning-score-label">
      <div className="tuning-score-figure">
        <span
          className={`tuning-score-number${tuning.provisional ? " is-provisional" : ""}`}
          data-score={tuning.score ?? ""}
          aria-hidden={tuning.score === null ? "true" : undefined}
        >
          {tuning.score === null ? "—" : tuning.score}
          {tuning.score !== null && <span className="tuning-score-unit">%</span>}
        </span>
        <span className="tuning-grade-line">
          <span className="tuning-grade" data-grade={tuning.grade || ""}>
            {tuning.grade ? `Grade ${tuning.grade}` : tuning.score === null ? "No score" : "Provisional"}
          </span>
          <GradeLegend grade={tuning.grade} />
        </span>
      </div>
      <div className="tuning-score-text">
        <h3 id="tuning-score-label" className="tuning-score-label">
          <span className={`status-dot is-${tuning.status}`} aria-hidden="true" />
          {tuning.label}
        </h3>
        {reason && (
          <p className="tuning-grade-reason" data-capped={capped ? "true" : "false"}>
            {reason}
          </p>
        )}
        <p className="tuning-score-coverage">
          {coverageText(tuning)}
          {tuning.counts.problem > 0 && ` · ${tuning.counts.problem} problem${tuning.counts.problem === 1 ? "" : "s"}`}
          {tuning.counts.watch > 0 && ` · ${tuning.counts.watch} to watch`}
          {tuning.counts.ok > 0 && ` · ${tuning.counts.ok} OK`}
        </p>
        {unknown.length > 0 && (
          <p className="tuning-score-hint">
            {tuning.provisional
              ? "Not graded until at least half the checks, and one in each category, are measured."
              : `${unknown.length} not measured; they are left out of the score.`}{" "}
            {needsAnalysis && canRun && (
              <button type="button" className="text-button" disabled={disabled || running} onClick={() => onMeasure("report")}>
                Run full analysis
              </button>
            )}
            {onlyParameters && (
              <button type="button" className="text-button" disabled={disabled} onClick={() => onMeasure("config")}>
                Open Parameters
              </button>
            )}
          </p>
        )}
      </div>
    </section>
  );
}

/** ⓘ next to the grade: what each letter means, the cap and when there is no
 * grade, all from tuningRules.ts (GRADE_SCALE, PROBLEM_GRADE_CAP). */
function GradeLegend({ grade }: { grade: TuningReport["grade"] }) {
  return (
    <Popover label="How the grade works" className="tuning-grade-help" width={300}>
      <h3 className="popover-title">How the grade works</h3>
      <table className="tuning-grade-legend">
        <caption className="visually-hidden">Grades and the scores they cover</caption>
        <thead>
          <tr>
            <th scope="col">Grade</th>
            <th scope="col">Score</th>
          </tr>
        </thead>
        <tbody>
          {GRADE_SCALE.map((g) => (
            <tr key={g.grade} data-grade={g.grade} className={g.grade === grade ? "is-current" : undefined}
              aria-current={g.grade === grade ? "true" : undefined}>
              <th scope="row">{g.grade}</th>
              <td>
                {gradeRangeText(g.grade)}
                {g.grade === grade && <span className="tuning-grade-current"> · this server</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>A problem caps the grade at {PROBLEM_GRADE_CAP}, whatever the score.</p>
      <p>No grade (provisional) until at least half the checks, and one in each category, are measured.</p>
    </Popover>
  );
}

function checkInspector(
  check: TuningCheck,
  ctx: {
    actionsOff: boolean;
    other: boolean;
    analysis: AnalysisReportInput | null;
    activeKey: string;
    connection?: Connection;
    settings?: LabSettings;
    onDigest?: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
    onOpenTop?: OpenTop;
    onOpenSql?: (sql: string) => void;
    onMeasure: (view: TuningMeasureWith) => void;
    canMeasure: (view: TuningMeasureWith) => boolean;
  },
): InspectorContent {
  const fix = check.fix;
  const measured = check.status !== "unknown";
  const attributed = ctx.analysis?.attribution?.full_joins?.statements || [];
  const joins = check.key === "joins_without_index" && measured && (check.status === "problem" || check.status === "watch");
  return {
    key: `check:${check.key}`,
    title: check.title,
    meta: (
      <>
        <StatusPill tone={check.applies ? check.status : "off"} label={check.applies ? STATUS_TEXT[check.status] : "Does not apply"} />
        <span className="hint">{TUNING_CATEGORIES[check.category].name}</span>
      </>
    ),
    actions:
      fix?.sql && ctx.onOpenSql ? (
        <div className="inspector-links">
          <button
            type="button"
            className="button"
            disabled={ctx.actionsOff}
            title="Opens the statements in a Query editor tab; they run only when you run them there"
            onClick={() => ctx.onOpenSql?.(fix.sql!)}
          >
            <Icon name="query" size={14} /> Open in Query editor
          </button>
        </div>
      ) : undefined,
    body: (
      <>
        <InspectorSection title={measured ? "Measured" : check.applies ? "Not measured" : "Does not apply"}>
          {measured ? (
            <>
              <p className="tuning-measured-value">{check.value}</p>
              {check.verdict && <p>{check.verdict}</p>}
            </>
          ) : (
            <>
              <p>{check.unknownReason}</p>
              {check.applies && (
                <div className="button-row">
                  {check.measureWith
                    .filter((v) => ctx.canMeasure(v))
                    .map((v) => (
                      <button key={v} type="button" className="button" disabled={ctx.actionsOff} onClick={() => ctx.onMeasure(v)}>
                        {MEASURE_VIEW[v].label}
                      </button>
                    ))}
                </div>
              )}
            </>
          )}
          {check.evidence.length > 0 && <Fields label="Numbers" rows={check.evidence.map((n) => [n.label, n.text])} />}
          {check.source && (
            <p className="hint">
              From {check.source.label}
              {check.source.takenAt ? `, read at ${new Date(check.source.takenAt).toLocaleTimeString()}` : ""}.
            </p>
          )}
        </InspectorSection>
        {fix && (
          <InspectorSection title="What to do">
            <p>{fix.text}</p>
            {fix.sql && <CopySql sql={fix.sql} label={`Statements for ${check.title}`} />}
            {fix.optionFile && (
              <>
                <p className="hint">Option file line, to copy (myflames does not edit files):</p>
                <CopySql sql={fix.optionFile} label={`Option file line for ${check.title}`} />
              </>
            )}
            {fix.caveat && <p className="hint">{fix.caveat}</p>}
          </InspectorSection>
        )}
        {check.rule && (
          <InspectorSection title="Rule">
            <p>{check.rule}</p>
          </InspectorSection>
        )}
        {check.why && (
          <InspectorSection title="Why it matters">
            <p>{check.why}</p>
          </InspectorSection>
        )}
        {joins && (
          <InspectorSection title="Statements behind it">
            {attributed.length && ctx.connection && ctx.settings ? (
              <StatementRows
                statements={attributed}
                order="full_join"
                connection={ctx.connection}
                disabled={ctx.actionsOff}
                settings={ctx.settings}
                onAction={(action, row, rank) => ctx.onDigest?.(row, rank, action, ctx.activeKey)}
              />
            ) : ctx.connection && ctx.settings && ctx.onDigest && ctx.onOpenTop ? (
              <StatementsBehind
                connection={ctx.connection}
                disabled={ctx.actionsOff}
                other={ctx.other}
                from={ctx.activeKey}
                order="full_join"
                settings={ctx.settings}
                onDigest={ctx.onDigest}
                onOpenTop={ctx.onOpenTop}
              />
            ) : (
              <p className="hint">Top statements ordered by joins without an index lists them.</p>
            )}
          </InspectorSection>
        )}
        {check.settings.length > 0 && (
          <InspectorSection title="Settings">
            <Fields
              label="Settings"
              rows={check.settings.map((s) => [
                s.name,
                s.default && s.default !== s.value ? `${s.value} (default ${s.default})` : s.value,
              ])}
            />
          </InspectorSection>
        )}
        {check.docs && (
          <p className="hint">
            <a href={check.docs} target="_blank" rel="noreferrer noopener">
              Reference manual
            </a>
          </p>
        )}
      </>
    ),
  };
}
