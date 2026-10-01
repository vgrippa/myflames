import { useEffect, useState } from "react";
import { api, type Bundle } from "./api";
export function Library({
  onOpen,
}: {
  onOpen: (bundle: Bundle) => Promise<void>;
}) {
  const [items, setItems] = useState<Bundle[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState("");
  const [loading, setLoading] = useState(true);
  const filtered = items.filter((b) =>
    `${b.name} ${b.tags.join(" ")} ${b.notes}`
      .toLowerCase().includes(search.trim().toLowerCase()),
  );
  async function refresh() {
    const r = await api<{ items: Bundle[] }>("investigations/list", {});
    setItems(r.items);
  }
  useEffect(() => {
    refresh().catch((e) => setError(e.message)).finally(() => setLoading(false));
  }, []);
  return (
    <section>
      <div className="page-heading">
        <div>
        <div className="eyebrow">YOUR WORK</div>
        <h1>Saved investigations</h1>
        <p>
          Queries, experiments, notes, and baselines stored on this computer.
        </p>
        </div>
      </div>
      <label className="search library-search">
        <input
          aria-label="Search investigations"
          placeholder="Search names, notes, or tags"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </label>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <div className="library-grid">
        {filtered.map((b) => (
            <article className="card library-card" key={b.id}>
              <div className="eyebrow">{b.plans.length} EXPERIMENTS</div>
              <h2>{b.name}</h2>
              <p>{b.notes || "No notes yet."}</p>
              <div className="tags">
                {b.tags.map((t) => (
                  <span key={t}>{t}</span>
                ))}
              </div>
              <small>
                {b.updated_at && new Date(b.updated_at).toLocaleString()}
              </small>
              <div className="button-row">
                <button
                  className="button primary"
                  disabled={!!busy}
                  onClick={async () => {
                    setBusy(b.id!);
                    try {
                      await onOpen(b);
                    } catch (e) {
                      setError((e as Error).message);
                    } finally {
                      setBusy("");
                    }
                  }}
                >
                  {busy === b.id ? "Opening…" : "Open investigation"}
                </button>
                {deleting === b.id ? (
                  <>
                    <button
                      className="button danger"
                      onClick={async () => {
                        try {
                          await api("investigations/delete", { id: b.id });
                          await refresh();
                          setDeleting("");
                        } catch (e) {
                          setError((e as Error).message);
                        }
                      }}
                    >
                      Delete permanently
                    </button>
                    <button
                      className="text-button"
                      onClick={() => setDeleting("")}
                    >
                      Keep
                    </button>
                  </>
                ) : (
                  <button
                    className="text-button"
                    onClick={() => setDeleting(b.id!)}
                  >
                    Delete
                  </button>
                )}
              </div>
            </article>
          ))}
      </div>
      {loading && <p role="status" className="empty-small">Loading investigations…</p>}
      {!loading && !error && items.length > 0 && filtered.length === 0 && (
        <div className="card empty-small" role="status">
          <h2>No matching investigations</h2>
          <p>Try a different name, note, or tag.</p>
          <button className="button" onClick={() => setSearch("")}>Clear search</button>
        </div>
      )}
      {!loading && !error && items.length === 0 && (
        <div className="card empty-small">
          <h2>A place for your investigations</h2>
          <p>
            Import or capture a plan, then choose Save investigation. Nothing is
            saved automatically.
          </p>
        </div>
      )}
    </section>
  );
}
