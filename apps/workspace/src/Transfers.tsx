// The Transfers panel of the Files browser: the transfer jobs of every SSH
// connection, read once on open and then every second while any job is
// queued or running (one request at a time, paused in a hidden tab), shown
// for this connection or for all of them, with progress, Cancel, Retry and
// Clear finished. A job that leaves the active states is reported once, so
// the browser can refresh the folder it wrote into.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Icon } from "./components";
import {
  ActionButton,
  ActionStatus,
  IconButton,
  Segmented,
  StatusPill,
  useAction,
  type ActionState,
  type Tone,
} from "./kit";
import { bytes } from "./PerfShared";
import {
  baseName,
  transferActive,
  transferCancel,
  transferRetry,
  transfers,
  transfersClear,
  type SshConnection,
  type Transfer,
  type TransferItem,
  type TransferState,
} from "./filesApi";

const POLL_MS = 1000;
const isActiveState = (state: TransferState | undefined) => state === "queued" || state === "running";
const itemCount = (n: number) => `${n.toLocaleString()} ${n === 1 ? "item" : "items"}`;

type Follower = { wake: () => void; dispose: () => void };

/** Calls *poll* now and then every POLL_MS while its last answer said a job
 * is active: one request at a time, none in a hidden tab, the request in
 * flight aborted when the tab hides or the panel goes away. *wake* polls at
 * once (a transfer just started). A failed read keeps the last answer and
 * tries again on the next tick. */
function followTransfers(poll: (signal: AbortSignal) => Promise<boolean>): Follower {
  let disposed = false;
  let timer = 0;
  let request: AbortController | null = null;
  let active = true; // Until the first answer says otherwise.
  const schedule = () => {
    window.clearTimeout(timer);
    if (!disposed && !document.hidden && active) timer = window.setTimeout(() => void run(), POLL_MS);
  };
  const run = async () => {
    if (disposed || document.hidden || request) return;
    const controller = new AbortController();
    request = controller;
    try {
      const busy = await poll(controller.signal);
      if (!disposed && !controller.signal.aborted) active = busy;
    } catch {
      /* The panel keeps the jobs it showed; the next tick reads again. */
    } finally {
      if (request === controller) request = null;
      schedule();
    }
  };
  const visibility = () => {
    window.clearTimeout(timer);
    if (document.hidden) request?.abort();
    else if (!request) void run();
  };
  document.addEventListener("visibilitychange", visibility);
  void run();
  return {
    wake: () => {
      if (disposed) return;
      active = true;
      if (!document.hidden && !request) {
        window.clearTimeout(timer);
        void run();
      }
    },
    dispose: () => {
      disposed = true;
      window.clearTimeout(timer);
      request?.abort();
      document.removeEventListener("visibilitychange", visibility);
    },
  };
}

/** The job list of every connection. *onFinished* gets the jobs that left
 * the queued and running states since the previous answer. */
export function useTransfers(onFinished: (jobs: Transfer[]) => void) {
  const [jobs, setJobs] = useState<Transfer[]>([]);
  const [loaded, setLoaded] = useState(false);
  const follower = useRef<Follower | null>(null);
  const states = useRef(new Map<string, TransferState>());
  // Jobs this page started, kept until an answer that was asked for after
  // them arrives (an answer already on its way does not list them yet).
  const started = useRef(new Map<string, { job: Transfer; seq: number }>());
  const polls = useRef(0);
  const finished = useRef(onFinished);
  finished.current = onFinished;
  useEffect(() => {
    const f = followTransfers(async (signal) => {
      const seq = ++polls.current;
      const answer = await transfers(undefined, signal);
      if (signal.aborted) return true;
      const fresh = answer.jobs || [];
      const ids = new Set(fresh.map((j) => j.id));
      const extra: Transfer[] = [];
      for (const [id, item] of started.current) {
        if (ids.has(id) || item.seq < seq) started.current.delete(id);
        else extra.push(item.job);
      }
      const merged = [...extra, ...fresh];
      const left = merged.filter((j) => !transferActive(j) && isActiveState(states.current.get(j.id)));
      states.current = new Map(merged.map((j) => [j.id, j.state]));
      setJobs(merged);
      setLoaded(true);
      if (left.length) finished.current(left);
      return merged.some(transferActive);
    });
    follower.current = f;
    return () => {
      follower.current = null;
      f.dispose();
    };
  }, []);
  const wake = useCallback(() => follower.current?.wake(), []);
  /** A job this page just started or retried. */
  const add = useCallback((job: Transfer) => {
    started.current.set(job.id, { job, seq: polls.current });
    states.current.set(job.id, job.state);
    setJobs((list) => [job, ...list.filter((j) => j.id !== job.id)]);
    follower.current?.wake();
  }, []);
  /** A newer state of a listed job (the Cancel answer); the next read still
   * reports it as finished when it left the active states. */
  const replace = useCallback((job: Transfer) => {
    setJobs((list) => list.map((j) => (j.id === job.id ? job : j)));
    follower.current?.wake();
  }, []);
  /** Drops finished jobs (of one connection, or all) after Clear finished. */
  const clear = useCallback((ssh?: string) => {
    const gone = (j: Transfer) => j.state === "finished" && (!ssh || j.ssh === ssh);
    setJobs((list) => {
      for (const j of list) if (gone(j)) {
        states.current.delete(j.id);
        started.current.delete(j.id);
      }
      return list.filter((j) => !gone(j));
    });
  }, []);
  return { jobs, loaded, wake, add, replace, clear };
}
export type TransfersModel = ReturnType<typeof useTransfers>;

// ---- Formatting ----------------------------------------------------------------

/** "~8 s left", "~3 min left", "~1.5 h left". */
export function etaText(eta: number | null): string {
  if (eta === null || !Number.isFinite(eta)) return "";
  const s = Math.max(1, Math.round(eta));
  if (s < 90) return `~${s} s left`;
  if (s < 90 * 60) return `~${Math.round(s / 60)} min left`;
  return `~${(s / 3600).toFixed(1)} h left`;
}
export function jobLabel(job: Transfer): string {
  const what = job.sources.length === 1 ? baseName(job.sources[0]) : itemCount(job.sources.length);
  return `${job.direction === "upload" ? "Upload" : "Download"} ${what} to ${job.target_dir}`;
}
function jobPercent(job: Transfer): number | null {
  return job.bytes_total > 0 ? Math.min(100, Math.floor((100 * job.bytes_done) / job.bytes_total)) : null;
}
function jobStatus(job: Transfer): { tone: Tone; label: string } {
  switch (job.state) {
    case "queued": return { tone: "off", label: "Queued" };
    case "running": return { tone: "info", label: job.phase === "scanning" ? "Scanning" : "Running" };
    case "failed": return { tone: "problem", label: "Failed" };
    case "cancelled": return { tone: "off", label: "Cancelled" };
    default:
      if (job.files_failed > 0) return { tone: "watch", label: "Done, some failed" };
      if (job.files_skipped > 0) return { tone: "watch", label: "Done, some skipped" };
      return { tone: "ok", label: "Done" };
  }
}
/** The numbers line: what moved, how fast, how long is left. */
function jobStats(job: Transfer): string {
  const parts: string[] = [];
  const active = transferActive(job);
  if (job.state === "queued") parts.push("Waiting to start");
  else if (active && job.phase === "scanning") {
    parts.push("Scanning…");
    if (job.files_total > 0) parts.push(`${job.files_total.toLocaleString()} files found`);
  } else if (active) {
    const pct = jobPercent(job);
    parts.push(job.bytes_total > 0 ? `${bytes(job.bytes_done)} of ${bytes(job.bytes_total)}` : bytes(job.bytes_done));
    if (pct !== null) parts.push(`${pct}%`);
    if (job.rate_bps > 0) parts.push(`${bytes(job.rate_bps)}/s`);
    const eta = etaText(job.eta_s);
    if (eta) parts.push(eta);
    if (job.files_total > 1) parts.push(`${job.files_done.toLocaleString()} of ${job.files_total.toLocaleString()} files`);
  } else {
    parts.push(bytes(job.bytes_done));
    if (job.files_total > 0) {
      parts.push(job.files_done === job.files_total
        ? `${job.files_total.toLocaleString()} ${job.files_total === 1 ? "file" : "files"}`
        : `${job.files_done.toLocaleString()} of ${job.files_total.toLocaleString()} files`);
    }
  }
  if (job.files_skipped > 0) parts.push(`${job.files_skipped.toLocaleString()} skipped`);
  if (job.files_failed > 0) parts.push(`${job.files_failed.toLocaleString()} failed`);
  return parts.join(" · ");
}
const ITEM_STATE: Record<TransferItem["state"], string> = {
  queued: "Queued", running: "Running", finished: "Done", failed: "Failed", cancelled: "Cancelled", skipped: "Skipped",
};

function ItemRow({ item }: { item: TransferItem }) {
  return (
    <li className={`files-item is-${item.state}`}>
      <span className="files-item-state">{ITEM_STATE[item.state]}</span>
      <bdi className="files-item-name" title={`${item.source} → ${item.target}`}>{baseName(item.source)}</bdi>
      <span className="files-item-bytes">{item.state === "running" ? `${bytes(item.done)} of ${bytes(item.bytes)}` : bytes(item.bytes)}</span>
      {item.error && <span className="files-item-error">{item.error}</span>}
    </li>
  );
}

function JobRow({ job, model, showConnection, expanded, onExpand, onEditConnection, onTrustHost }: {
  job: Transfer;
  model: TransfersModel;
  showConnection: boolean;
  expanded: boolean;
  onExpand: (open: boolean) => void;
  /** Present only for jobs of the connection on screen. */
  onEditConnection?: () => void;
  onTrustHost?: (retry: () => void) => void;
}) {
  const action = useAction();
  const detailsId = useId();
  const active = transferActive(job);
  const pct = jobPercent(job);
  const label = jobLabel(job);
  const status = jobStatus(job);
  const fill = active ? pct ?? 0 : job.state === "finished" ? 100 : pct ?? 0;
  const indeterminate = active && (job.state === "queued" || job.phase === "scanning" || pct === null);
  async function cancel() {
    const result = await action.run(() => transferCancel(job.id), { pending: "Cancelling…", done: "", write: true });
    if (result.ok) model.replace(result.value.job);
  }
  async function retry() {
    const result = await action.run(() => transferRetry(job.id), { pending: "Retrying…", done: "" });
    if (result.ok) model.add(result.value.job);
  }
  return (
    <li className={`files-job is-${job.state}`} data-job={job.id} data-state={job.state}>
      <div className="files-job-main">
        <span className="files-job-icon" aria-hidden="true"><Icon name={job.direction === "upload" ? "export" : "import"} size={16} /></span>
        <span className="files-job-label">
          {showConnection && <bdi className="files-job-conn">{job.ssh_name}</bdi>}
          <bdi title={job.sources.join("\n")}>{label}</bdi>
        </span>
        <StatusPill tone={status.tone} label={status.label} />
        <span className="files-job-actions">
          {active ? (
            <ActionButton small busy={action.pending} busyLabel="Cancelling…" onClick={() => void cancel()} aria-label={`Cancel: ${label}`}>
              Cancel
            </ActionButton>
          ) : job.state === "failed" || job.state === "cancelled" ? (
            <ActionButton small busy={action.pending} busyLabel="Retrying…" onClick={() => void retry()} aria-label={`Retry: ${label}`}>
              Retry
            </ActionButton>
          ) : null}
          <IconButton
            icon="disclosure"
            size={12}
            className={`files-job-expand${expanded ? " is-open" : ""}`}
            label={expanded ? "Hide files" : "Show files"}
            aria-expanded={expanded}
            aria-controls={detailsId}
            onClick={() => onExpand(!expanded)}
          />
        </span>
      </div>
      <div
        className={`files-progress${indeterminate ? " is-indeterminate" : ""}`}
        role="progressbar"
        aria-label={`${label}: progress`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : fill}
      >
        <span style={{ transform: `scaleX(${indeterminate ? 1 : fill / 100})` }} />
      </div>
      <p className="files-job-stats">{jobStats(job)}</p>
      {job.error && (
        <p className="files-job-error">
          {job.error}
          {job.error_code === "auth_failed" && onEditConnection && (
            <>
              {" "}
              <button type="button" className="text-button" onClick={onEditConnection}>Edit connection</button>
            </>
          )}
          {job.error_code === "host_key_unknown" && onTrustHost && (
            <>
              {" "}
              <button type="button" className="text-button" onClick={() => onTrustHost(() => void retry())}>Trust host…</button>
            </>
          )}
        </p>
      )}
      <ActionStatus state={action} onDismiss={action.reset} />
      <div id={detailsId} className="files-job-details" hidden={!expanded}>
        {expanded && (
          <>
            {job.current && (
              <p className="files-job-current">
                Now: <bdi>{job.current.source}</bdi> → <bdi>{job.current.target}</bdi> · {bytes(job.current.done)} of {bytes(job.current.bytes)}
              </p>
            )}
            {job.items.length > 0 ? (
              <ul className="files-items" aria-label="Files">
                {job.items.map((item, i) => <ItemRow key={`${item.source}\n${i}`} item={item} />)}
              </ul>
            ) : (
              !job.failed_items.length && <p className="files-job-current">No files listed yet.</p>
            )}
            {job.items_more > 0 && <p className="files-items-more">and {job.items_more.toLocaleString()} more</p>}
            {job.failed_items.length > 0 && (
              <>
                <h4 className="files-items-heading">Failed</h4>
                <ul className="files-items is-failed" aria-label="Failed files">
                  {job.failed_items.map((item, i) => <ItemRow key={`${item.source}\n${i}`} item={item} />)}
                </ul>
              </>
            )}
          </>
        )}
      </div>
    </li>
  );
}

export type TransferScope = "this" | "all";

export function TransfersPanel({ connection, model, open, onToggle, scope, onScope, starting, onStartingDismiss, onEditConnection, onTrustHost }: {
  connection: SshConnection;
  onEditConnection?: () => void;
  /** Opens the host-key prompt; `retry` runs after the host is trusted. */
  onTrustHost?: (retry: () => void) => void;
  model: TransfersModel;
  open: boolean;
  onToggle: (open: boolean) => void;
  scope: TransferScope;
  onScope: (scope: TransferScope) => void;
  /** Starting a transfer (the request that creates the job). */
  starting: Pick<ActionState, "phase" | "message" | "startedAt">;
  onStartingDismiss: () => void;
}) {
  const bodyId = useId();
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const clearing = useAction();
  const shown = scope === "this" ? model.jobs.filter((j) => j.ssh === connection.id) : model.jobs;
  const queue = shown.filter(transferActive);
  const failed = shown.filter((j) => j.state === "failed").length;
  const done = shown.filter((j) => j.state === "finished").length;
  const cancelled = shown.filter((j) => j.state === "cancelled").length;
  const totals = queue.reduce((t, j) => ({ done: t.done + j.bytes_done, total: t.total + j.bytes_total }), { done: 0, total: 0 });
  const overall = !open && queue.length && totals.total > 0 ? Math.min(100, Math.floor((100 * totals.done) / totals.total)) : null;
  const counts = [`Queue ${queue.length}`, `Failed ${failed}`, `Done ${done}`, ...(cancelled ? [`Cancelled ${cancelled}`] : [])].join(" · ");
  async function clearFinished() {
    const ssh = scope === "this" ? connection.id : undefined;
    const result = await clearing.run(() => transfersClear(ssh), { pending: "Clearing finished transfers…", done: "" });
    if (result.ok) model.clear(ssh);
  }
  return (
    <section className={`files-transfers${open ? " is-open" : ""}`} aria-label="Transfers">
      <div className="files-transfers-head">
        <h3 className="files-transfers-title">
          <button type="button" className="files-transfers-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => onToggle(!open)}>
            <span className="files-chevron" aria-hidden="true"><Icon name="disclosure" size={12} /></span>
            Transfers
            <span className="files-transfers-counts">{counts}{overall !== null ? ` · ${overall}%` : ""}</span>
          </button>
        </h3>
        <Segmented
          label="Transfers shown"
          value={scope}
          onChange={onScope}
          options={[
            { value: "this", label: "This connection" },
            { value: "all", label: "All connections" },
          ]}
        />
        <ActionButton
          small
          busy={clearing.pending}
          busyLabel="Clearing…"
          aria-disabled={!done || undefined}
          title={done ? "Remove finished transfers from the list" : "No finished transfers"}
          onClick={() => void clearFinished()}
        >
          Clear finished
        </ActionButton>
      </div>
      <div id={bodyId} className="files-transfers-body" hidden={!open}>
        <ActionStatus state={starting} onDismiss={onStartingDismiss} />
        <ActionStatus state={clearing} onDismiss={clearing.reset} />
        {shown.length ? (
          <ul className="files-jobs" aria-label={scope === "this" ? `Transfers for ${connection.name}` : "Transfers for all connections"}>
            {shown.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                model={model}
                showConnection={scope === "all"}
                onEditConnection={job.ssh === connection.id ? onEditConnection : undefined}
                onTrustHost={job.ssh === connection.id ? onTrustHost : undefined}
                expanded={expanded.has(job.id)}
                onExpand={(on) => setExpanded((set) => {
                  const next = new Set(set);
                  if (on) next.add(job.id);
                  else next.delete(job.id);
                  return next;
                })}
              />
            ))}
          </ul>
        ) : (
          model.loaded && (
            <p className="files-transfers-empty">
              {scope === "this" ? "No transfers for this connection yet. Select files and use Upload or Download, or drag them to the other pane." : "No transfers yet."}
            </p>
          )
        )}
      </div>
    </section>
  );
}
