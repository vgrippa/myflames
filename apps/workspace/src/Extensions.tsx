// VillageSQL extension inventory. Every displayed value is server-controlled
// text; React owns the text nodes, and exports never generate HTML or SQL.
import { useState } from "react";
import {
  download, type Connection, type ExtensionUsage,
  type ExtensionVariable, type ExtensionsReport, type ServerExtension,
} from "./api";
import { readExtensions } from "./extensionsApi";
import { useConnectionReads, type Bound } from "./binding";
import { count, engineLabel } from "./PerfShared";
import { csvReport, type ExportColumn } from "./reportTableExport";
import { useOpenRead } from "./viewOnScreen";
import {
  DataTable, Disclosure, EmptyState, ExportMenu, FactStrip, Fields,
  InspectorSection, StatusPill, SummaryLine, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type Selection, type Tone,
} from "./kit";
import "./extensions.css";

const UNKNOWN = "Not collected";
const STATES: Record<ServerExtension["protocol_state"], [Tone, string]> = {
  current: ["ok", "Current"], deprecated: ["watch", "Deprecated"],
  unsupported: ["problem", "Unsupported"], unknown: ["unknown", "Unknown"],
};
const value = (v: string | number | null | undefined) => v == null ? UNKNOWN : String(v);
const yesNo = (v: boolean | null | undefined) => v == null ? UNKNOWN : v ? "On" : "Off";
const registeredCount = (names: string[] | null, truncated: boolean | null) => names === null || (!names.length && truncated) ? UNKNOWN : `${count(names.length)}${truncated ? "+" : ""}`;

function protocolText(row: ServerExtension) {
  return `${row.protocol === null ? "Unknown" : row.protocol} · ${STATES[row.protocol_state][1]}`;
}
function pendingText(row: ServerExtension) {
  if (!row.pending) return "None";
  return `${row.pending.version ?? "Version unknown"}${row.pending.last_error ? " · Failed" : " · Pending"}`;
}
/** A bounded read cannot establish the full count. Do not turn its sample
 * length into a fabricated total, or imply that an unread list was empty. */
function usageText<T>(usage: ExtensionUsage<T>, noun: "column" | "routine") {
  const shown = count(usage.sample.length);
  if (usage.count === null) return usage.truncated
    ? `Showing ${shown} ${noun}${usage.sample.length === 1 ? "" : "s"}; more may exist, the total is unknown.` : UNKNOWN;
  return `Showing ${shown} of ${count(usage.count)} ${noun}${usage.count === 1 ? "" : "s"}.`;
}
function buildText(report: ExtensionsReport) {
  const build = report.settings.build;
  if (!build) return UNKNOWN;
  return `${build.git_sha ?? "Commit not collected"}${build.is_dirty === null ? " · Worktree state not collected" : build.is_dirty ? " · Modified" : " · Clean"}`;
}
function Protocol({ row }: { row: ServerExtension }) {
  const [tone, label] = STATES[row.protocol_state];
  return <span className="extension-protocol"><span>{row.protocol === null ? "?" : row.protocol}</span><StatusPill tone={tone} label={label} /></span>;
}
function Names({ names, truncated }: { names: string[] | null; truncated: boolean | null }) {
  if (names === null || (!names.length && truncated)) return <p className="hint">Not collected.</p>;
  if (!names.length) return <p className="hint">None registered.</p>;
  return <><ul className="extension-names">{names.map((name, i) => <li key={i}><code><bdi>{name}</bdi></code></li>)}</ul>
    {truncated && <p className="hint">Showing the first {names.length === 1 ? "name" : `${count(names.length)} names`}; the server listed more.</p>}</>;
}
function Variables({ variables, truncated, extension = false }: { variables: ExtensionVariable[] | null; truncated: boolean | null; extension?: boolean }) {
  if (variables === null) return <p className="hint">Variables were not collected.</p>;
  if (!variables.length) return <p className="hint">No variables were read{extension ? " for this extension" : ""}.{truncated ? " The read was truncated." : ""}</p>;
  return <><dl className="extension-variables">{variables.map((variable, i) => <div key={i}>
    <dt><code><bdi>{variable.name}</bdi></code><span className="hint"> ({variable.kind})</span></dt>
    <dd><bdi>{value(variable.value)}</bdi></dd>
  </div>)}</dl>{truncated && <p className="hint">More variables may exist. The read was truncated.</p>}</>;
}

const EXPORT_COLUMNS: ExportColumn<ServerExtension>[] = [
  { label: "Name", value: (r) => r.name },
  { label: "Version", value: (r) => r.version },
  { label: "Protocol", value: (r) => r.protocol },
  { label: "Protocol state", value: (r) => r.protocol_state },
  { label: "Types", value: (r) => registeredCount(r.types, r.types_truncated) },
  { label: "Functions", value: (r) => registeredCount(r.functions, r.functions_truncated) },
  { label: "Columns using", value: (r) => r.columns.count },
  { label: "Pending", value: pendingText },
];
/** Export plain text only. Strip terminal controls even if a future backend
 * accidentally returns them; markup remains literal, useful diagnostic text. */
export function extensionsText(report: ExtensionsReport): string {
  const summary = report.summary;
  const lines = ["VillageSQL extensions", engineLabel(report), `Collected: ${report.taken_at}`,
    `Installed: ${value(summary.installed)}; pending: ${value(summary.pending)}; failing: ${value(summary.failing)}`,
    `Deprecated protocols: ${value(summary.deprecated_protocol)}; unsupported: ${value(summary.unsupported_protocol)}`,
    `Extension folder: ${value(report.settings.veb_dir)}`, `Preview extensions: ${yesNo(report.settings.preview_extensions)}`,
    `Server protocol: ${value(report.settings.server_protocol)}`, `Schema version: ${value(report.settings.schema_version)}`,
    `Codebase: ${value(report.settings.codebase)}`, `Build: ${buildText(report)}`,
    `Cache repairs: ${value(report.status.veb_cache_repaired)}; install and update requests: ${value(report.status.install_count)}; uninstalls: ${value(report.status.uninstall_count)}`,
    "Request counts do not establish successful installations."];
  if (report.reason) lines.push(report.reason);
  if (report.extensions === null) lines.push("Extension inventory was not collected.");
  else if (!report.extensions.length) lines.push("No extensions are installed.");
  for (const extension of report.extensions || []) {
    lines.push("", `${extension.name} ${value(extension.version)}`, `Protocol: ${protocolText(extension)}`, `SDK: ${value(extension.sdk_version)}`);
    if (extension.protocol_note) lines.push(extension.protocol_note);
    for (const key of ["types", "functions"] as const) {
      const names = extension[key];
      const truncated = extension[`${key}_truncated`];
      lines.push(`${key}: ${names === null || (!names.length && truncated) ? UNKNOWN : names.length ? names.join(", ") : "None registered"}${truncated ? " (truncated)" : ""}`);
    }
    lines.push(`Columns using: ${usageText(extension.columns, "column")}`);
    extension.columns.sample.forEach((row) => lines.push(`  ${row.schema}.${row.table}.${row.column}: ${row.data_type}`));
    lines.push(`Routines using: ${usageText(extension.routines, "routine")}`);
    extension.routines.sample.forEach((row) => lines.push(`  ${row.schema}.${row.routine} (${row.routine_type}), ${row.parameter ?? "return"}: ${row.data_type}`));
    lines.push(`Variables${extension.variables_truncated ? " (truncated)" : ""}:`);
    if (extension.variables === null) lines.push(`  ${UNKNOWN}`);
    else extension.variables.forEach((row) => lines.push(`  ${row.name} (${row.kind}): ${value(row.value)}`));
    lines.push(`Pending update: ${pendingText(extension)}`);
    if (extension.pending) {
      lines.push(`Requested: ${value(extension.pending.requested_at)}`, `Last error at: ${value(extension.pending.last_error_at)}`);
      if (extension.pending.last_error !== null) lines.push(extension.pending.last_error);
      if (extension.pending.error_truncated) lines.push("Last error truncated to 2,000 characters.");
    }
  }
  lines.push("", `Other variables${report.other_variables_truncated ? " (truncated)" : ""}:`);
  if (report.other_variables === null) lines.push(UNKNOWN);
  else report.other_variables.forEach((row) => lines.push(`${row.name} (${row.kind}): ${value(row.value)}`));
  if (report.notes.length) lines.push("", "Notes:", ...report.notes);
  return lines.join("\n").replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "") + "\n";
}

export function ExtensionsView({ connection, disabled, onBack, selection, report: bound, onReport }: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<ExtensionsReport> | null;
  onReport: (report: Bound<ExtensionsReport>) => void;
}) {
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const [exportResult, setExportResult] = useState<{ key: string; text: string } | null>(null);
  async function load() {
    setExportResult(null);
    await reads.run("extensions", "Reading extensions…", readExtensions, onReport);
  }
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: loading || reads.running(), read: () => { void load(); } });
  const rows = report?.extensions || [];
  const columns: Column<ServerExtension>[] = [
    { key: "name", label: "Name", flex: true, sort: (r) => r.name, render: (r) => <code><bdi>{r.name}</bdi></code> },
    { key: "version", label: "Version", sort: (r) => r.version, render: (r) => <bdi>{value(r.version)}</bdi> },
    { key: "protocol", label: "Protocol", sort: (r) => r.protocol, render: (r) => <Protocol row={r} /> },
    { key: "types", label: "Types", num: true, sort: (r) => r.types?.length ?? null, render: (r) => registeredCount(r.types, r.types_truncated) },
    { key: "functions", label: "Functions", num: true, sort: (r) => r.functions?.length ?? null, render: (r) => registeredCount(r.functions, r.functions_truncated) },
    { key: "columns", label: "Columns using", num: true, sort: (r) => r.columns.count, render: (r) => r.columns.count === null ? UNKNOWN : count(r.columns.count) },
    { key: "pending", label: "Pending", sort: pendingText, render: (r) => <bdi>{pendingText(r)}</bdi> },
  ];
  const selected = rows.find((extension) => extension.name === selection.key);
  let inspector: InspectorContent | null = null;
  if (selected) {
    inspector = {
      key: selected.name,
      title: <bdi>{selected.name}</bdi>,
      meta: `Protocol ${protocolText(selected)}`,
      body: <>
        <InspectorSection title="Registration">
          <Fields rows={[["Version", <bdi>{value(selected.version)}</bdi>], ["Protocol", protocolText(selected)], ["SDK", <bdi>{value(selected.sdk_version)}</bdi>]]} />
          {selected.protocol_state === "unsupported" && <p className="hint">Protocol 4 is unsupported. VillageSQL 0.0.8 refuses extensions that use it.</p>}
          {selected.protocol_state === "deprecated" && <p className="hint">Protocols 1 and 3 are deprecated. Check for an extension update.</p>}
          <h4>Types</h4><Names names={selected.types} truncated={selected.types_truncated} />
          <h4>Functions</h4><Names names={selected.functions} truncated={selected.functions_truncated} />
        </InspectorSection>
        <InspectorSection title="Columns using its types">
          <p className="hint">{usageText(selected.columns, "column")}</p>
          <ul className="extension-usage">{selected.columns.sample.map((row, i) => <li key={i}>
            <code><bdi>{row.schema}.{row.table}.{row.column}</bdi></code><span><bdi>{row.data_type}</bdi></span>
          </li>)}</ul>
        </InspectorSection>
        <InspectorSection title="Routines using its types">
          <p className="hint">{usageText(selected.routines, "routine")}</p>
          <ul className="extension-usage">{selected.routines.sample.map((row, i) => <li key={i}>
            <code><bdi>{row.schema}.{row.routine}</bdi></code>
            <span><bdi>{row.routine_type}</bdi> · <bdi>{row.parameter ?? "Return value"}</bdi> · <bdi>{row.data_type}</bdi></span>
          </li>)}</ul>
        </InspectorSection>
        <InspectorSection title="Variables"><Variables variables={selected.variables} truncated={selected.variables_truncated} extension /></InspectorSection>
        <InspectorSection title="Pending update">
          {selected.pending ? <>
            <Fields rows={[["Version", <bdi>{value(selected.pending.version)}</bdi>], ["Requested at", <bdi>{value(selected.pending.requested_at)}</bdi>], ["Last error at", <bdi>{value(selected.pending.last_error_at)}</bdi>]]} />
            {selected.pending.last_error !== null && <pre className="extension-error">{selected.pending.last_error}</pre>}
            {selected.pending.error_truncated && <p className="hint">Last error truncated to 2,000 characters.</p>}
          </> : <p className="hint">No update is pending.</p>}
        </InspectorSection>
      </>,
    };
  }
  function exportReport(format: "csv" | "txt") {
    if (!report || bound?.key !== reads.key) return;
    const filename = `myflames-extensions.${format}`;
    download(format === "csv" ? csvReport(EXPORT_COLUMNS, rows) : extensionsText(report), filename, format === "csv" ? "text/csv;charset=utf-8" : "text/plain;charset=utf-8");
    setExportResult({ key: reads.key, text: `Exported ${filename}.` });
  }
  const summary = report?.summary;
  const warnings = summary && summary.deprecated_protocol !== null && summary.unsupported_protocol !== null
    ? summary.deprecated_protocol + summary.unsupported_protocol : null;
  const summaryUnknown = summary && Object.values(summary).some((counter) => counter === null);
  const brief = (n: number | null) => n === null ? "?" : count(n);
  return <ViewLayout id="extensions" title="Extensions" className="extensions-panel" onBack={onBack}
    info={<><p>Installed VillageSQL extensions, their registered types and functions, and pending updates.</p><p>This view only reads. Usage lists are bounded and include objects visible to your account.</p></>}
    controls={<ExportMenu label="Export extensions" disabled={!report || bound?.key !== reads.key} formats={[
      { key: "csv", label: "CSV", onSelect: () => exportReport("csv") },
      { key: "text", label: "Text report", onSelect: () => exportReport("txt") },
    ]} />}
    onRefresh={() => void load()} refreshDisabled={disabled} loading={loading} otherConnection={other}
    inspector={inspector} inspectorEmpty={<p>Select an extension to see its registration, dependencies, variables and pending update.</p>}
    status={<><ViewStatus loading={reads.loading} error={reads.error} errorCode={reads.errorCode} onStop={reads.stop}
      onRetry={reads.error ? () => void load() : undefined} doneAt={reads.doneAt} />
      <span className="hint" role="status">{exportResult?.key === reads.key ? exportResult.text : ""}</span></>}>
    {report && summary && <>
      <SummaryLine notes={report.notes} tone={summary.failing || summary.unsupported_protocol ? "problem" : warnings || summary.pending ? "watch" : summaryUnknown ? "unknown" : "ok"}>
        {brief(summary.installed)} installed · {brief(summary.pending)} pending updates · {brief(summary.failing)} failing updates · {brief(warnings)} protocol warnings
      </SummaryLine>
      <FactStrip label="Extension settings" facts={[
        ["Extension folder", <bdi>{value(report.settings.veb_dir)}</bdi>], ["Preview extensions", yesNo(report.settings.preview_extensions)],
        ["Server protocol", value(report.settings.server_protocol)], ["Build", <bdi>{buildText(report)}</bdi>],
      ]} />
      {!report.available ? <EmptyState icon="extensions" title="Extensions are unavailable">{report.reason || "This view is available on VillageSQL connections."}</EmptyState>
        : report.extensions === null ? <EmptyState icon="extensions" title="Extension inventory was not collected">Open the notes for this read, then Refresh to retry.</EmptyState>
        : !rows.length ? <EmptyState icon="extensions" title="No extensions are installed.">INSTALL EXTENSION needs EXTENSION_ADMIN.</EmptyState>
        : <DataTable id="extensions.list" label="Extensions" rows={rows} columns={columns} rowKey={(row) => row.name}
          selection={selection} defaultSort={{ key: "name", dir: "asc" }} empty="No extensions are installed." />}
      <Disclosure title="Server details">
        <Fields rows={[["Codebase", <bdi>{value(report.settings.codebase)}</bdi>], ["Schema version", value(report.settings.schema_version)],
          ["Cache repairs", value(report.status.veb_cache_repaired)], ["Install and update requests", value(report.status.install_count)], ["Uninstall statements", value(report.status.uninstall_count)]]} />
        <p className="hint">Request counts do not establish successful installations.</p>
        <h4>Other dotted variables</h4><Variables variables={report.other_variables} truncated={report.other_variables_truncated} />
      </Disclosure>
    </>}
  </ViewLayout>;
}
