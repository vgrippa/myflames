import { useEffect, useRef, useState } from "react";
import { api, download, safePreview, type Bundle, type Comparison } from "./api";
import { Icon, ImportDialog } from "./components";
import {
  ConfirmSheet, EmptyState, GroupedList, GroupedRow, Pill,
  SearchField, SummaryLine, moveRowFocus,
} from "./kit";
import { relativeTime } from "./session";

/** A saved collection stays in the list while its details and actions share one pane. */
export function Library({ onOpen }: { onOpen: (bundle: Bundle) => Promise<void> }) {
  const [items, setItems] = useState<Bundle[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<Bundle | null>(null);
  const [importing, setImporting] = useState(false);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const actionLock = useRef(false);
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

  useEffect(() => {
    const controller = new AbortController();
    api<{ items: Bundle[] }>("investigations/list", {}, controller.signal)
      .then((r) => {
        if (!controller.signal.aborted)
          setItems([...r.items].sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")));
      })
      .catch((e) => { if (!controller.signal.aborted) setError((e as Error).message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
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
  async function open(bundle: Bundle) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(true);
    setError("");
    try { await onOpen(bundle); }
    catch (e) { setError((e as Error).message); }
    finally { actionLock.current = false; setBusy(false); }
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
    if (!baseline || !target || comparing) return;
    comparisonRead.current?.abort();
    const controller = new AbortController();
    comparisonRead.current = controller;
    setComparing(true);
    setComparison(null);
    setError("");
    try {
      const result = await api<Comparison>("compare", { before: baseline.raw, after: target.raw }, controller.signal);
      if (!controller.signal.aborted) setComparison(result);
    } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
    finally { if (!controller.signal.aborted) setComparing(false); }
  }
  function cancelDelete() {
    setDeleting(null);
    window.requestAnimationFrame(() => deleteButton.current?.focus());
  }
  async function remove() {
    if (!deleting || actionLock.current) return;
    const id = deleting.id;
    actionLock.current = true;
    setBusy(true);
    setError("");
    try {
      await api("investigations/delete", { id });
      setItems((current) => current.filter((b) => b.id !== id));
      setDeleting(null);
      // The removed row's detail vanishes; return focus to the surviving list.
      window.requestAnimationFrame(() => {
        const next = rows.current?.querySelector<HTMLButtonElement>("button") ||
          rows.current?.closest(".library")?.querySelector<HTMLInputElement>('input[type="search"]');
        next?.focus();
      });
    } catch (e) { setError((e as Error).message); }
    finally { actionLock.current = false; setBusy(false); }
  }
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
            {loading && <p className="empty-small" role="status">Loading investigations…</p>}
            {!loading && !!items.length && !filtered.length && <EmptyState icon="search" title="No matching investigations"
              action={<button className="button" onClick={() => changeSearch("")}>Clear search</button>}>
              Try a different name, note, or tag.
            </EmptyState>}
          </div>
        </aside>
        <section className="library-detail" id="investigation-detail" aria-label="Investigation details" aria-busy={busy}>
          {selected ? <>
            <header className="library-detail-head">
              <h2>{selected.name}</h2>
              {selected.updated_at && <p className="library-updated">Updated {relativeTime(selected.updated_at)}</p>}
              <div className="library-actions" role="group" aria-label="Investigation actions">
                <button className="button primary" disabled={busy} onClick={() => void open(selected)}>{busy ? "Opening…" : "Open all"}</button>
                <button className="button" disabled={busy || comparing || !baseline || !target}
                  title={!baseline ? "This investigation has no baseline." : !target ? "Add a second plan to compare." : `Compare ${target.name} to ${baseline.name}`}
                  onClick={() => void compare()}>{comparing ? "Comparing…" : "Compare to baseline"}</button>
                <button className="button" disabled={busy} onClick={() => download(JSON.stringify(selected, null, 2),
                  `${selected.name.replace(/[^a-z0-9_-]/gi, "-") || "investigation"}.json`, "application/json")}>Export</button>
                <button className="text-button" ref={deleteButton} disabled={busy} onClick={() => { setError(""); setDeleting(selected); }}>Delete</button>
              </div>
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
            {comparing && <p className="empty-small" role="status">Comparing saved plans…</p>}
            {comparison && <section className="library-comparison" aria-label="Baseline comparison">
              <div className="library-comparison-head"><h3>{baseline?.name} compared to {target?.name}</h3>
                <button className="text-button" onClick={() => setComparison(null)}>Close comparison</button></div>
              <iframe title="Baseline comparison" sandbox="allow-scripts" srcDoc={safePreview(comparison.html)} />
            </section>}
          </> : !loading && !items.length && !error ? <EmptyState icon="investigation" title="No investigations yet"
            action={<button className="button primary" onClick={() => { setError(""); setImporting(true); }}>Import plan</button>}>
            Open plans, then choose Save investigation in the top bar.
          </EmptyState> : !loading && !!items.length ? <EmptyState icon="investigation" title="Select an investigation" /> : null}
        </section>
      </div>
      {deleting && <ConfirmSheet title={`Delete “${deleting.name}”?`} description="This removes the saved collection from this computer."
        confirmLabel="Delete permanently" busy={busy} onCancel={cancelDelete} onConfirm={() => void remove()}>
        {error && <p role="alert" className="error">{error}</p>}
      </ConfirmSheet>}
      {importing && <ImportDialog busy={busy} error={error} onClose={() => { setImporting(false); setError(""); }}
        onFile={(file) => void importFile(file)} onPaste={(raw, name) => void importPlan(raw, name)} />}
    </section>
  );
}
