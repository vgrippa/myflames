import { useEffect, useRef, useState } from "react";
import {
  api, download, getInvestigation, listInvestigations, safePreview,
  type Bundle, type Comparison, type InvestigationCursor, type InvestigationSummary,
} from "./api";
import { Icon, ImportDialog } from "./components";
import {
  ActionButton, ActionStatus, ConfirmSheet, EmptyState, GroupedList, GroupedRow, Pill,
  SearchField, SummaryLine, errorText, moveRowFocus, useAction, useSince,
} from "./kit";
import { relativeTime } from "./session";

/** Rows per `investigations/list` page. */
const PAGE = 25;
/** Typing pauses this long before the server searches every saved investigation. */
const SEARCH_DELAY_MS = 250;

/** A saved collection stays in the list while its details and actions share one pane. */
export function Library({ onOpen }: { onOpen: (bundle: Bundle) => Promise<void> }) {
  // Summaries only; Open, Compare and Export read the complete bundle.
  const [items, setItems] = useState<InvestigationSummary[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [nextCursor, setNextCursor] = useState<InvestigationCursor | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  /** Whether anything is saved at all (null until the unfiltered list says), so an empty search reads as no match. */
  const [anySaved, setAnySaved] = useState<boolean | null>(null);
  const listRead = useRef<AbortController | null>(null);
  const moreRead = useRef(false);
  const firstRead = useRef(true);
  const latest = useRef({ items: [] as InvestigationSummary[], nextCursor: null as InvestigationCursor | null, search: "" });
  // Open all, Export and Delete: one at a time, each saying what it does and
  // what happened (the line at the top of the details).
  const act = useAction();
  const [actKind, setActKind] = useState<"" | "open" | "export" | "delete">("");
  // What Open all or Export acted on: Try again repeats it on that one, even
  // after another investigation was selected.
  const [actItem, setActItem] = useState<InvestigationSummary | Bundle | null>(null);
  const busy = act.pending;
  const [deleting, setDeleting] = useState<InvestigationSummary | null>(null);
  // The Delete sheet was closed while the deletion had no reply yet.
  const [deleteLeft, setDeleteLeft] = useState(false);
  const [importing, setImporting] = useState(false);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const comparingSince = useSince(comparing);
  const actionLock = useRef(false);
  const importingRef = useRef(false);
  importingRef.current = importing;
  const comparisonRead = useRef<AbortController | null>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const rows = useRef<HTMLDivElement>(null);
  const filtered = items.filter((b) =>
    `${b.name} ${b.tags.join(" ")} ${b.notes}`.toLowerCase().includes(search.trim().toLowerCase()),
  );
  // Filtering and deleting always leave a visible selection, never a hidden detail.
  const selected = filtered.find((b) => b.id === selectedId) || filtered[0];
  const baseline = selected?.plans.find((p) => p.id === selected.baseline_id);
  const candidates = selected?.plans.filter((p) => p.id !== baseline?.id) || [];
  const target = candidates.find((p) => p.id === targetId) || candidates[0];
  latest.current = { items, nextCursor, search };

  // The server searches every saved investigation; the rows already loaded
  // are filtered above as well, so typing narrows the list at once.
  useEffect(() => {
    listRead.current?.abort();
    const controller = new AbortController();
    listRead.current = controller;
    setNextCursor(null);
    setLoading(true);
    const text = search.trim();
    const timer = window.setTimeout(() => {
      listInvestigations(PAGE, null, text, controller.signal)
        .then((r) => {
          if (controller.signal.aborted) return;
          setItems(r.items);
          setNextCursor(r.next_cursor);
          if (!text) setAnySaved(r.items.length > 0);
          else if (r.items.length) setAnySaved(true);
        })
        .catch((e) => { if (!controller.signal.aborted) setError((e as Error).message); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, firstRead.current || !text ? 0 : SEARCH_DELAY_MS);
    firstRead.current = false;
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [search]);
  function loadMore() {
    const controller = listRead.current;
    if (!controller || !nextCursor || loading || moreRead.current) return;
    moreRead.current = true;
    setLoadingMore(true);
    listInvestigations(PAGE, nextCursor, search.trim(), controller.signal)
      .then((r) => {
        if (controller.signal.aborted) return;
        setItems((rows) => {
          const ids = new Set(rows.map((b) => b.id));
          return rows.concat(r.items.filter((b) => !ids.has(b.id)));
        });
        setNextCursor(r.next_cursor);
      })
      .catch((e) => { if (!controller.signal.aborted) setError((e as Error).message); })
      .finally(() => { moreRead.current = false; setLoadingMore(false); });
  }
  useEffect(() => {
    setComparison(null);
    setComparing(false);
    comparisonRead.current?.abort();
    return () => comparisonRead.current?.abort();
  }, [selected?.id, target?.id]);

  function changeSearch(value: string) {
    setSearch(value);
    setSelectedId("");
    setTargetId("");
    setError("");
  }
  async function open(bundle: InvestigationSummary | Bundle) {
    if (actionLock.current) return;
    actionLock.current = true;
    setActKind("open");
    setActItem(bundle);
    setError("");
    // An imported file can be anything: read its fields defensively, and the
    // lock is released however the open ends.
    const count = Array.isArray(bundle?.plans) ? bundle.plans.length : 0;
    const name = typeof bundle?.name === "string" && bundle.name ? bundle.name : "the investigation";
    let r: Awaited<ReturnType<typeof act.run>>;
    try {
      r = await act.run(async () => { await onOpen(await getInvestigation(bundle)); }, {
        pending: count ? `Opening ${count} ${count === 1 ? "plan" : "plans"} of “${name}”…` : `Opening “${name}”…`,
        done: "",
        failed: (e) => `Could not open “${name}”: ${errorText(e)}`,
      });
    } finally {
      actionLock.current = false;
    }
    // An import shows its error inside the Import dialog.
    if (!r.ok && r.reason === "failed" && importingRef.current) {
      setError(errorText(r.error));
      act.reset();
    }
  }
  async function importPlan(raw: string, name: string) {
    try {
      const parsed = JSON.parse(raw);
      const id = "imported-plan";
      await open(parsed?.schema_version === "investigation-1.0" ? parsed : {
        schema_version: "investigation-1.0", name: "Untitled investigation", notes: "", tags: [],
        baseline_id: id, plans: [{ id, name: name || "Imported plan", raw }],
      });
    } catch (e) { setError((e as Error).message); }
  }
  async function importFile(file?: File) {
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) { setError("Choose a file smaller than 10 MiB."); return; }
    try { await importPlan(await file.text(), file.name); }
    catch (e) { setError((e as Error).message); }
  }
  async function compare() {
    if (!selected || !baseline || !target || comparing) return;
    comparisonRead.current?.abort();
    const controller = new AbortController();
    comparisonRead.current = controller;
    setComparing(true);
    setComparison(null);
    setError("");
    try {
      const bundle = await getInvestigation(selected, controller.signal);
      const before = bundle.plans.find((p) => p.id === baseline.id);
      const after = bundle.plans.find((p) => p.id === target.id);
      if (!before || !after) throw new Error("This investigation changed after the list was loaded. Open Investigations again to see it.");
      if (controller.signal.aborted) return;
      const result = await api<Comparison>("compare", { before: before.raw, after: after.raw }, controller.signal);
      if (!controller.signal.aborted) setComparison(result);
    } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
    finally { if (!controller.signal.aborted) setComparing(false); }
  }
  async function exportBundle(item: InvestigationSummary) {
    if (actionLock.current) return;
    actionLock.current = true;
    setActKind("export");
    setActItem(item);
    setError("");
    try {
      await act.run(async () => {
        const bundle = await getInvestigation(item);
        const file = `${bundle.name.replace(/[^a-z0-9_-]/gi, "-") || "investigation"}.json`;
        download(JSON.stringify(bundle, null, 2), file, "application/json");
        return file;
      }, {
        pending: `Preparing the export of “${item.name}”…`,
        done: (file) => `Exported “${item.name}” as ${file}.`,
        failed: (e) => `Could not export “${item.name}”: ${errorText(e)}`,
      });
    } finally {
      actionLock.current = false;
    }
  }
  function cancelDelete() {
    setDeleting(null);
    window.requestAnimationFrame(() => deleteButton.current?.focus());
  }
  async function remove() {
    if (!deleting || actionLock.current) return;
    const id = deleting.id;
    const name = deleting.name;
    actionLock.current = true;
    setActKind("delete");
    setDeleteLeft(false);
    setError("");
    let r: Awaited<ReturnType<typeof act.run>>;
    try {
      r = await act.run(async () => {
        await api("investigations/delete", { id });
        // A page loaded while the delete was pending stays: filter the current rows.
        setItems((current) => current.filter((b) => b.id !== id));
        const now = latest.current;
        if (!now.search.trim() && !now.nextCursor && now.items.every((b) => b.id === id)) setAnySaved(false);
        setDeleting(null);
        // The removed row's detail vanishes; return focus to the surviving list.
        window.requestAnimationFrame(() => {
          const next = rows.current?.querySelector<HTMLButtonElement>("button") ||
            rows.current?.closest(".library")?.querySelector<HTMLInputElement>('input[type="search"]');
          next?.focus();
        });
      }, {
        pending: `Deleting “${name}”…`,
        done: `Deleted “${name}” from this computer.`,
        failed: (e) => `Could not delete “${name}”: ${errorText(e)}`,
      });
    } finally {
      actionLock.current = false;
    }
    if (!r.ok && r.reason === "failed") {
      // The sheet is still open: the error shows in it, where Delete can be
      // tried again. A closed sheet stays closed: the line above says why.
      if (deletingRef.current) {
        setError(errorText(r.error));
        act.reset();
      } else setDeleting(null);
    }
    setDeleteLeft(false);
  }
  const deletingRef = useRef<InvestigationSummary | null>(null);
  deletingRef.current = deleteLeft ? null : deleting;
  const retry = actKind === "export" && actItem ? () => void exportBundle(actItem as InvestigationSummary)
    : actKind === "open" && actItem ? () => void open(actItem) : undefined;
  return (
    <section className="library" aria-labelledby="investigations-title">
      <h1 id="investigations-title" className="visually-hidden">Investigations</h1>
      <SummaryLine>Saved collections of plans, notes, and tags on this computer.</SummaryLine>
      {error && !importing && !deleting && <p role="alert" className="error">{error}</p>}
      <div className="library-split">
        <aside className="library-master" aria-label="Saved investigations">
          <div className="library-tools">
            <SearchField label="Search investigations" placeholder="Name, notes, or tags" value={search} onChange={changeSearch} />
          </div>
          <div className="library-list" data-rows ref={rows}>
            {filtered.map((b) => (
              <button
                key={b.id} className={`library-item${selected?.id === b.id ? " is-selected" : ""}`}
                data-row-key={b.id} aria-current={selected?.id === b.id ? "true" : undefined}
                aria-controls="investigation-detail" tabIndex={selected?.id === b.id ? 0 : -1} disabled={busy}
                onClick={() => { setSelectedId(b.id!); setTargetId(""); setError(""); }}
                onKeyDown={(e) => {
                  const next = moveRowFocus(e);
                  if (next?.dataset.rowKey) { setSelectedId(next.dataset.rowKey); setTargetId(""); setError(""); }
                }}
              >
                <Icon name="investigation" />
                <span className="library-item-text">
                  <strong title={b.name}>{b.name}</strong>
                  <small title={`${b.tags.join(", ")}${b.updated_at ? ` · Updated ${new Date(b.updated_at).toLocaleString()}` : ""}`}>
                    {b.plans.length} {b.plans.length === 1 ? "plan" : "plans"}
                    {b.updated_at && <> · <time dateTime={b.updated_at}>{relativeTime(b.updated_at)}</time></>}
                    {b.tags.length > 0 && ` · ${b.tags.join(", ")}`}
                  </small>
                </span>
              </button>
            ))}
            {loading && !filtered.length && <p className="empty-small" role="status">Loading investigations…</p>}
            {nextCursor && !loading && <button className="text-button" disabled={busy || loadingMore}
              onClick={loadMore}>{loadingMore ? "Loading investigations…" : "Load more investigations"}</button>}
            {!loading && anySaved !== false && !!search.trim() && !filtered.length && <EmptyState icon="search" title="No matching investigations"
              action={<button className="button" onClick={() => changeSearch("")}>Clear search</button>}>
              Try a different name, note, or tag.
            </EmptyState>}
          </div>
        </aside>
        <section className="library-detail" id="investigation-detail" aria-label="Investigation details" aria-busy={busy}>
          {/* With nothing left to select (the last one was deleted), the outcome shows here. */}
          {!selected && act.phase !== "idle" && <ActionStatus className="library-status library-status-empty" state={act} onDismiss={act.reset} />}
          {selected ? <>
            <header className="library-detail-head">
              <h2>{selected.name}</h2>
              {selected.updated_at && <p className="library-updated">Updated {relativeTime(selected.updated_at)}</p>}
              <div className="library-actions" role="group" aria-label="Investigation actions">
                {/* Only the clicked action shows that it works; the others wait. */}
                <ActionButton primary busy={busy && actKind === "open"} busyLabel="Opening…" disabled={busy && actKind !== "open"}
                  onClick={() => void open(selected)}>Open all</ActionButton>
                <ActionButton busy={comparing} busyLabel="Comparing…" disabled={!comparing && (busy || !baseline || !target)}
                  title={!baseline ? "This investigation has no baseline." : !target ? "Add a second plan to compare." : `Compare ${target.name} to ${baseline.name}`}
                  onClick={() => void compare()}>Compare to baseline</ActionButton>
                <ActionButton busy={busy && actKind === "export"} busyLabel="Exporting…" disabled={busy && actKind !== "export"}
                  onClick={() => void exportBundle(selected)}>Export</ActionButton>
                <button className="text-button" ref={deleteButton} disabled={busy} onClick={() => { setError(""); setDeleting(selected); }}>Delete</button>
              </div>
              {/* Under the buttons, so its line never moves them: what Open all,
                  Export or Delete is doing and what it did. */}
              <ActionStatus className="library-status" state={act} onRetry={retry} onDismiss={act.reset} />
            </header>
            <section className="library-notes" aria-labelledby="investigation-notes-title">
              <h3 className="section-label" id="investigation-notes-title">Notes</h3>
              <p>{selected.notes || "No notes yet."}</p>
            </section>
            <section className="library-tags" aria-labelledby="investigation-tags-title">
              <h3 className="section-label" id="investigation-tags-title">Tags</h3>
              <div>{selected.tags.length ? selected.tags.map((t) => <Pill key={t}>{t}</Pill>) : <span className="muted">No tags</span>}</div>
            </section>
            <div className="library-plans" data-rows>
              <GroupedList id="investigation-plans-title" title="Plans" count={selected.plans.length}
                footer={baseline && target ? "Select a plan to compare to the baseline." : undefined}>
                {selected.plans.map((p) => <GroupedRow key={p.id} rowKey={p.id} label={p.name} attrs={{ title: p.name }}
                  detail={p.capture?.mode === "explain" ? "Estimated" : p.capture ? "Captured" : "Imported"}
                  value={selected.plans.length > 1 && baseline?.id === p.id ? <Pill>Baseline</Pill> : undefined}
                  selection={baseline?.id !== p.id && baseline && selected.plans.length > 1 ? { key: target?.id || "", select: setTargetId } : undefined} />)}
              </GroupedList>
            </div>
            <ActionStatus className="library-compare-status" state={{ phase: comparing ? "pending" : "idle",
              message: `Comparing ${target?.name || "the plan"} to the baseline…`, startedAt: comparingSince }}
              onStop={() => { comparisonRead.current?.abort(); setComparing(false); }} stopLabel="Stop" />
            {comparison && <section className="library-comparison" aria-label="Baseline comparison">
              <div className="library-comparison-head"><h3>{baseline?.name} compared to {target?.name}</h3>
                <button className="text-button" onClick={() => setComparison(null)}>Close comparison</button></div>
              <iframe title="Baseline comparison" sandbox="allow-scripts" srcDoc={safePreview(comparison.html)} />
            </section>}
          </> : !loading && anySaved === false && !error ? <EmptyState icon="investigation" title="No investigations yet"
            action={<button className="button primary" onClick={() => { setError(""); setImporting(true); }}>Import plan</button>}>
            Open plans, then choose Save investigation in the top bar.
          </EmptyState> : !loading && anySaved !== false ? <EmptyState icon="investigation" title="Select an investigation" /> : null}
        </section>
      </div>
      {deleting && !deleteLeft && <ConfirmSheet title={`Delete “${deleting.name}”?`} description="This removes the saved collection from this computer."
        confirmLabel="Delete permanently" busy={busy && actKind === "delete"} busyLabel="Deleting…"
        progress="Removing it from this computer" onLeave={() => setDeleteLeft(true)}
        leaveText="Still deleting. Close does not stop it; the outcome shows in Investigations."
        onCancel={cancelDelete} onConfirm={() => void remove()}>
        {error && <p role="alert" className="error">{error}</p>}
      </ConfirmSheet>}
      {importing && <ImportDialog busy={busy} error={error} onClose={() => { setImporting(false); setError(""); }}
        onFile={(file) => void importFile(file)} onPaste={(raw, name) => void importPlan(raw, name)} />}
    </section>
  );
}
