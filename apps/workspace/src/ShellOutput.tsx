// Live and completed MySQL Shell output for reviewed actions (ActionReview.tsx).
// Live output reads the bounded, redacted chunks already returned by
// /api/live/action/progress (reviewed_writes.Progress.emit). Completed output
// shows a collapsible "MySQL Shell output" block with Copy (kit copyToClipboard)
// and, for check_clusterset, leads with the backend's validated status facts
// above the raw output. Redacted display JSON is never machine evidence. All server text
// renders as plain React text nodes with nothing semantic stripped.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { copyToClipboard, Disclosure, StatusPill, type Tone } from "./kit";

export type ProgressOutputChunk = {
  stream: string;
  text: string;
};

export type ShellOutputResult = {
  action: string;
  shell_output?: string | null;
  shell_output_truncated?: boolean;
  shell_stderr?: string | null;
  shell_stderr_truncated?: boolean;
  verification?: { facts?: ShellStatusFact[] } | null;
};

type ShellStatusFact = { server: string; fact: string; actual: unknown; status: string };

export type OutputLine = {
  stream: "stdout" | "stderr";
  text: string;
};

export type ParsedShellCluster = {
  name: string;
  role: string | null;
  globalStatus: string | null;
  status: string | null;
  replicationStatus: string | null;
  gtidConsistency: string | null;
};

export type ParsedClusterSetStatus = {
  domainName: string | null;
  status: string;
  clusters: ParsedShellCluster[];
};

export const MAX_LIVE_LINES = 200;
export const MAX_RESULT_LINES = 400;
const MAX_DISPLAY_CHARS = 120_000;
const ANNOUNCE_THROTTLE_MS = 1500;
const BOTTOM_TOLERANCE_PX = 8;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function normalizeStream(stream: string): "stdout" | "stderr" {
  return stream === "stderr" ? "stderr" : "stdout";
}

function splitStringLines(text: string, stream: "stdout" | "stderr"): OutputLine[] {
  if (!text) return [];
  const trimmed = text.replace(/\r?\n$/, "");
  return trimmed.split(/\r?\n/).map((line) => ({ stream, text: line }));
}

function chunksToLines(chunks: readonly ProgressOutputChunk[]): OutputLine[] {
  const out: OutputLine[] = [];
  for (const chunk of chunks) {
    if (!chunk || typeof chunk.text !== "string") continue;
    const stream = normalizeStream(chunk.stream);
    if (chunk.text.length === 0) {
      out.push({ stream, text: "" });
      continue;
    }
    const trimmed = chunk.text.replace(/\r?\n$/, "");
    for (const part of trimmed.split(/\r?\n/)) {
      out.push({ stream, text: part });
    }
  }
  return out;
}

function capOutputLines(lines: OutputLine[], maxLines: number, maxChars = MAX_DISPLAY_CHARS) {
  const totalLines = lines.length;
  let slice = totalLines > maxLines ? lines.slice(-maxLines) : lines;
  let charTruncated = false;
  let chars = 0;
  let startIndex = slice.length;
  for (let i = slice.length - 1; i >= 0; i--) {
    const len = slice[i].text.length + 1;
    if (chars + len > maxChars && startIndex < slice.length) {
      charTruncated = true;
      break;
    }
    chars += len;
    startIndex = i;
  }
  if (startIndex > 0) {
    slice = slice.slice(startIndex);
  }
  if (slice.length === 1 && slice[0].text.length > maxChars) {
    charTruncated = true;
    slice = [{ ...slice[0], text: slice[0].text.slice(-maxChars) }];
  }
  const truncated = slice.length < totalLines || charTruncated;
  return { lines: slice, totalLines, truncated };
}

function latestAnnouncement(lines: readonly OutputLine[]): string {
  for (let i = lines.length - 1; i >= 0; i--) {
    const text = lines[i].text.trim();
    if (text) {
      const snippet = text.length > 200 ? `${text.slice(0, 200)}…` : text;
      return lines[i].stream === "stderr" ? `stderr: ${snippet}` : snippet;
    }
  }
  return "";
}

/** ShellWriter._status_output validates private JSON and preserves stable fact
 * labels while redacting names and values. Each cluster starts with globalStatus;
 * retain that boundary even when redaction makes two cluster names identical. */
export function clusterSetStatusFromFacts(facts: ShellStatusFact[] | undefined): ParsedClusterSetStatus | null {
  const observed = (Array.isArray(facts) ? facts : []).filter((f) =>
    isRecord(f) && f.status === "verified" && asString(f.server) && asString(f.actual));
  const set = observed.find((f) => f.fact === "MySQL Shell ClusterSet status");
  const status = asString(set?.actual);
  if (!set || !status) return null;
  const clusters: ParsedShellCluster[] = [];
  let current: ParsedShellCluster | null = null;
  for (const fact of observed) {
    const value = asString(fact.actual);
    if (fact.fact === "MySQL Shell globalStatus") {
      current = { name: fact.server, role: null, globalStatus: value, status: null,
        replicationStatus: null, gtidConsistency: null };
      clusters.push(current);
    } else if (current && fact.server === current.name) {
      if (fact.fact === "MySQL Shell status") current.status = value;
      else if (fact.fact === "MySQL Shell clusterRole") current.role = value;
      else if (fact.fact === "MySQL Shell clusterSetReplicationStatus") current.replicationStatus = value;
      else if (fact.fact === "MySQL Shell transactionSetConsistencyStatus") current.gtidConsistency = value;
    }
  }
  if (clusters.length === 0) return null;
  clusters.sort((a, b) => {
    const aPrimary = a.role === "PRIMARY" ? 0 : 1;
    const bPrimary = b.role === "PRIMARY" ? 0 : 1;
    return aPrimary - bPrimary;
  });
  return {
    domainName: set.server,
    status,
    clusters,
  };
}

function setStatusTone(status: string): Tone {
  if (status === "HEALTHY") return "ok";
  if (status === "AVAILABLE") return "watch";
  if (status === "UNAVAILABLE") return "problem";
  return "unknown";
}

function clusterStatusTone(status: string | null): Tone {
  if (!status) return "unknown";
  if (status === "OK") return "ok";
  if (
    status === "OK_FENCED_WRITES" ||
    status === "OK_PARTIAL" ||
    status === "OK_NO_TOLERANCE" ||
    status === "OK_NO_TOLERANCE_PARTIAL" ||
    status === "OK_MISCONFIGURED"
  ) {
    return "watch";
  }
  if (
    status === "OK_NOT_REPLICATING" ||
    status === "OK_NOT_CONSISTENT" ||
    status === "NOT_OK" ||
    status === "INVALIDATED" ||
    status === "NO_QUORUM" ||
    status === "OFFLINE" ||
    status === "ERROR"
  ) {
    return "problem";
  }
  return "unknown";
}

function roleLabel(role: string | null): string {
  if (role === "PRIMARY") return "Primary";
  if (role === "REPLICA") return "Replica";
  return role || "Unknown";
}

function replicationSummary(cluster: ParsedShellCluster): string {
  if (cluster.replicationStatus) return cluster.replicationStatus;
  if (cluster.role === "PRIMARY") return "Primary";
  return "Not reported";
}

function buildCopyText(stdout: string, stderr: string): string {
  if (stdout && stderr) {
    return `${stdout}${stdout.endsWith("\n") ? "" : "\n"}[stderr]\n${stderr}`;
  }
  return stdout || stderr;
}

function OutputLines({ lines }: { lines: readonly OutputLine[] }) {
  return (
    <>
      {lines.map((line, i) => (
        <div
          key={i}
          className={`shell-output-line${line.stream === "stderr" ? " is-stderr" : ""}`}
          data-stream={line.stream}
        >
          {line.stream === "stderr" && (
            <span className="shell-output-stream">stderr</span>
          )}
          <span className="shell-output-text">{line.text}</span>
        </div>
      ))}
    </>
  );
}

/** Live, bounded, monospace Shell progress region shown while an action runs. */
export function LiveShellOutput({ chunks }: { chunks?: readonly ProgressOutputChunk[] | null }) {
  const rawLines = chunks && chunks.length > 0 ? chunksToLines(chunks) : [];
  const capped = capOutputLines(rawLines, MAX_LIVE_LINES);
  const latest = latestAnnouncement(capped.lines);

  const logRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const [scrolledUp, setScrolledUp] = useState(false);

  const [announcement, setAnnouncement] = useState("");
  const lastAnnouncedAt = useRef(0);
  const lastAnnouncedText = useRef("");
  const pendingText = useRef("");
  const timerRef = useRef<number | null>(null);

  useLayoutEffect(() => {
    const el = logRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [chunks]);

  useEffect(() => {
    if (!latest || latest === lastAnnouncedText.current) return;
    pendingText.current = latest;
    const now = Date.now();
    const elapsed = now - lastAnnouncedAt.current;
    if (lastAnnouncedAt.current === 0 || elapsed >= ANNOUNCE_THROTTLE_MS) {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      lastAnnouncedAt.current = now;
      lastAnnouncedText.current = latest;
      setAnnouncement(latest);
      return;
    }
    if (timerRef.current === null) {
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        const next = pendingText.current;
        if (next && next !== lastAnnouncedText.current) {
          lastAnnouncedAt.current = Date.now();
          lastAnnouncedText.current = next;
          setAnnouncement(next);
        }
      }, ANNOUNCE_THROTTLE_MS - elapsed);
    }
  }, [latest, capped.totalLines]);

  useEffect(() => () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  if (rawLines.length === 0) return null;

  function handleScroll() {
    const el = logRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_TOLERANCE_PX;
    stickRef.current = atBottom;
    setScrolledUp((prev) => (prev === !atBottom ? prev : !atBottom));
  }

  function jumpToLatest() {
    const el = logRef.current;
    stickRef.current = true;
    setScrolledUp(false);
    if (el) el.scrollTop = el.scrollHeight;
  }

  return (
    <section className="shell-output-live" aria-label="MySQL Shell progress">
      <div className="shell-output-head">
        <span className="shell-output-title">MySQL Shell progress</span>
        {scrolledUp && (
          <button type="button" className="text-button shell-output-follow" onClick={jumpToLatest}>
            Jump to latest
          </button>
        )}
      </div>
      {capped.truncated && (
        <p className="hint shell-output-cap">
          Showing the last {capped.lines.length.toLocaleString()} {capped.lines.length === 1 ? "line" : "lines"} (of {capped.totalLines.toLocaleString()}).
        </p>
      )}
      <div
        ref={logRef}
        className="shell-output-log"
        tabIndex={0}
        aria-label="MySQL Shell output lines"
        onScroll={handleScroll}
      >
        <OutputLines lines={capped.lines} />
      </div>
      <div className="shell-output-sr" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>
    </section>
  );
}

function ClusterSetCheckSummary({ parsed }: { parsed: ParsedClusterSetStatus }) {
  return (
    <section className="shell-check-summary" aria-label="MySQL Shell ClusterSet status">
      <div className="shell-check-head">
        <h3>
          {parsed.domainName ? `ClusterSet ${parsed.domainName}` : "ClusterSet"}
          <span className="shell-check-source">MySQL Shell</span>
        </h3>
        <StatusPill tone={setStatusTone(parsed.status)} label={parsed.status} />
      </div>
      {parsed.clusters.length > 0 ? (
        <div className="shell-check-table-wrap">
          <table className="shell-check-clusters">
            <thead>
              <tr>
                <th scope="col">Cluster</th>
                <th scope="col">Role</th>
                <th scope="col">Status</th>
                <th scope="col">Replication</th>
              </tr>
            </thead>
            <tbody>
              {parsed.clusters.map((cluster, i) => {
                const statusWord = cluster.globalStatus || cluster.status || "Unknown";
                return (
                  <tr key={i} data-cluster={cluster.name}>
                    <th scope="row">
                      <span className="shell-check-cluster-name">{cluster.name}</span>
                    </th>
                    <td>{roleLabel(cluster.role)}</td>
                    <td>
                      <StatusPill tone={clusterStatusTone(cluster.globalStatus || cluster.status)} label={statusWord} />
                      {cluster.status && cluster.globalStatus && cluster.status !== cluster.globalStatus && (
                        <span className="shell-check-sub">{cluster.status}</span>
                      )}
                    </td>
                    <td>
                      <span>{replicationSummary(cluster)}</span>
                      {cluster.gtidConsistency && (
                        <span className="shell-check-sub">
                          GTID consistency: {cluster.gtidConsistency}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="hint">No clusters reported in status output.</p>
      )}
    </section>
  );
}

function truncatedStreamNames(stdoutTruncated: boolean, stderrTruncated: boolean): string {
  if (stdoutTruncated && stderrTruncated) return "stdout and stderr";
  if (stdoutTruncated) return "stdout";
  if (stderrTruncated) return "stderr";
  return "";
}

/** Result Shell summary (for check_clusterset) and collapsible "MySQL Shell output" block with Copy. */
export function ShellActionOutput({ result }: { result: ShellOutputResult }) {
  const stdout = typeof result.shell_output === "string" ? result.shell_output : "";
  const stderr = typeof result.shell_stderr === "string" ? result.shell_stderr : "";
  const stdoutTruncated = result.shell_output_truncated === true;
  const stderrTruncated = result.shell_stderr_truncated === true;
  const backendTruncated = stdoutTruncated || stderrTruncated;
  const truncatedStreams = truncatedStreamNames(stdoutTruncated, stderrTruncated);
  const isCheck = result.action === "check_clusterset" || result.action.startsWith("check_clusterset:");
  const parsed = isCheck ? clusterSetStatusFromFacts(result.verification?.facts) : null;

  const allLines = [
    ...splitStringLines(stdout, "stdout"),
    ...splitStringLines(stderr, "stderr"),
  ];
  const capped = capOutputLines(allLines, MAX_RESULT_LINES);
  const copyText = buildCopyText(stdout, stderr);

  const [copied, setCopied] = useState<{ text: string; message: string }>({ text: "", message: "" });
  const copySeq = useRef(0);
  const mounted = useRef(true);
  const currentText = useRef(copyText);
  currentText.current = copyText;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    copySeq.current += 1;
    setCopied({ text: "", message: "" });
  }, [copyText]);

  if (!isCheck && !parsed && allLines.length === 0 && !backendTruncated) return null;

  const copyStatus = copied.text === copyText ? copied.message : "";
  const shownLineWord = capped.lines.length === 1 ? "line" : "lines";
  const totalLineWord = capped.totalLines === 1 ? "line" : "lines";

  return (
    <div className="shell-result-output">
      {parsed && <ClusterSetCheckSummary parsed={parsed} />}
      {isCheck && !parsed && (
        <p className="hint shell-output-cap" role="status">
          No structured MySQL Shell status facts were returned. Inspect the Shell output for details.
        </p>
      )}
      {isCheck && stdoutTruncated && (
        <p className="hint shell-output-cap" role="status">
          MySQL Shell stdout exceeded the 1 MiB retention limit. Only its retained tail is available below.
        </p>
      )}
      {(allLines.length > 0 || backendTruncated) && (
        <Disclosure
          title={backendTruncated ? "MySQL Shell output (retained tail)" : "MySQL Shell output"}
          count={capped.totalLines}
          className="shell-output-details"
        >
          <div className="shell-output-bar">
            {backendTruncated && capped.truncated ? (
              <span className="hint shell-output-cap">
                Showing the last {capped.lines.length.toLocaleString()} {shownLineWord} of the {capped.totalLines.toLocaleString()}-line retained tail ({truncatedStreams} exceeded the 1 MiB limit; earlier output was discarded). Copy copies only the retained tail.
              </span>
            ) : backendTruncated ? (
              <span className="hint shell-output-cap">
                Showing the retained tail ({capped.totalLines.toLocaleString()} {totalLineWord}; {truncatedStreams} exceeded the 1 MiB limit, earlier output was discarded). Copy copies only the retained tail.
              </span>
            ) : capped.truncated ? (
              <span className="hint shell-output-cap">
                Showing the last {capped.lines.length.toLocaleString()} {shownLineWord} (of {capped.totalLines.toLocaleString()}). Copy copies the full output.
              </span>
            ) : (
              <span className="hint">
                {capped.totalLines.toLocaleString()} {totalLineWord}
              </span>
            )}
            {copyText.length > 0 && (
              <span className="shell-output-copy">
                <button
                  type="button"
                  className="text-button"
                  aria-label={backendTruncated ? "Copy retained MySQL Shell output tail" : "Copy MySQL Shell output"}
                  onClick={() => {
                    const seq = ++copySeq.current;
                    const target = copyText;
                    void copyToClipboard(target).then((ok) => {
                      if (mounted.current && copySeq.current === seq && currentText.current === target) {
                        setCopied({
                          text: target,
                          message: ok ? (backendTruncated ? "Copied retained tail." : "Copied.") : "Select the text to copy it.",
                        });
                      }
                    });
                  }}
                >
                  {backendTruncated ? "Copy retained tail" : "Copy"}
                </button>
                {copyStatus && <span className="hint" role="status">{copyStatus}</span>}
              </span>
            )}
          </div>
          {capped.lines.length > 0 && (
            <div className="shell-output-log is-result" tabIndex={0} aria-label={backendTruncated ? "Retained MySQL Shell output tail" : "MySQL Shell output"}>
              <OutputLines lines={capped.lines} />
            </div>
          )}
        </Disclosure>
      )}
    </div>
  );
}
