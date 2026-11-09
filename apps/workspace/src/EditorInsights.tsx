import type { EditorStatement } from "./api";
import { InfoPopover, StatusPill, type Tone } from "./kit";

const count = (text: string) => BigInt(text).toLocaleString();

/** A failed statement is a clear Problem; scan and spill counters are Watch
 * signals, never a claim that a particular access path is incorrect. */
export function StatementInsight({ statement, onAnalyze, disabled, busy, onOpenInstruments, numbered = false }: {
  statement: EditorStatement;
  onAnalyze: () => void;
  disabled: boolean;
  busy: boolean;
  onOpenInstruments?: () => void;
  numbered?: boolean;
}) {
  const stats = statement.server_stats;
  if (!stats?.available) return <div className="editor-insight editor-insight-missing" data-statement={statement.index}>
    {numbered && <span>Statement {statement.index + 1}:</span>}
    <span>Server stats not collected</span>
    <InfoPopover label="Why server stats were not collected">
      <p><bdi>{stats?.reason || "This run has no statement history sample; check Server > Instruments."}</bdi></p>
      {onOpenInstruments && <button className="button small" onClick={onOpenInstruments}>Server &gt; Instruments</button>}
    </InfoPopover>
  </div>;
  const examined = BigInt(stats.rows_examined), sent = BigInt(stats.rows_sent);
  const expensive = sent > 0n && examined > 10000n && examined > sent * 100n;
  const disk = BigInt(stats.created_tmp_disk_tables) > 0n;
  const join = BigInt(stats.select_full_join) > 0n;
  const tone: Tone = BigInt(stats.errors) > 0n ? "problem" : expensive || disk || join ? "watch" : "ok";
  // Divide as integers first, so even BIGINT maxima keep an exact comparison.
  const ratio = sent > 0n ? ` (${(Number(examined * 10n / sent) / 10).toLocaleString(undefined, { maximumFractionDigits: 1 })} per row)` : "";
  const facts = [`Examined ${count(stats.rows_examined)} rows for ${count(stats.rows_sent)} returned${ratio}`];
  if (BigInt(stats.rows_affected) > 0n) facts.push(`${count(stats.rows_affected)} rows changed`);
  if (BigInt(stats.select_scan) > 0n) facts.push("full scan");
  if (join) facts.push("join without an index");
  facts.push(disk ? `${count(stats.created_tmp_disk_tables)} temporary tables on disk` : "no temporary tables on disk");
  return <div className="editor-insight" data-statement={statement.index}>
    {numbered && <span>Statement {statement.index + 1}:</span>}
    <StatusPill tone={tone} />
    <span className="editor-insight-text" title={facts.join(" · ")}>{facts.join(" · ")}</span>
    <InfoPopover label="Statement stats and verdict">
      <p>Watch: more than 10,000 examined and over 100 examined per returned row, a disk temporary table, or a join without an index. Problem: the server recorded an error. A full scan alone does not imply a problem.</p>
      <details><summary>Server counters</summary><dl className="editor-insight-counters">
        {Object.entries(stats).filter(([key]) => !["available", "reason"].includes(key)).map(([key, value]) =>
          <div key={key}><dt>{key}</dt><dd><bdi>{value === null ? "Not collected" : String(value)}</bdi></dd></div>)}
      </dl></details>
      <p>Timers are in picoseconds. History is read after the script, before session helpers; a small history ring can lose earlier statements. Server totals can exceed retained result rows.</p>
    </InfoPopover>
    <button className="button small ghost" disabled={disabled} aria-busy={busy || undefined}
      onClick={onAnalyze}>{busy ? "Analyzing…" : "Explain analyze"}</button>
  </div>;
}
