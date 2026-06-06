import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { algorithms } from "../data/algorithms";
import { ComplexityChart } from "../components/ComplexityChart";
import { evaluateCurve, formatN } from "../data/complexity";
import type { FamilyKey } from "../data/types";

const DEFAULT_SELECTION = ["full_scan", "btree", "hash"];

const N_PRESETS = [
  { label: "1K", n: 1_000 },
  { label: "100K", n: 100_000 },
  { label: "1M", n: 1_000_000 },
  { label: "100M", n: 100_000_000 },
  { label: "1B", n: 1_000_000_000 },
];

const MAX_N_OPTIONS = [
  { label: "1M", n: 1e6 },
  { label: "1B", n: 1e9 },
  { label: "1T", n: 1e12 },
];

function readSearch(search: string, key: string): string | null {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  return params.get(key);
}

function readSelectionFromSearch(search: string): string[] {
  const v = readSearch(search, "compare");
  if (!v) return DEFAULT_SELECTION;
  return v
    .split(",")
    .map(decodeURIComponent)
    .filter((n) => algorithms.some((a) => a.name === n));
}

export default function Compare() {
  const location = useLocation();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<string[]>(() =>
    readSelectionFromSearch(location.search)
  );
  const [currentN, setCurrentN] = useState<number>(() => {
    const fromUrl = readSearch(location.search, "n");
    const parsed = fromUrl ? parseInt(fromUrl, 10) : NaN;
    return Number.isFinite(parsed) && parsed >= 10 ? parsed : 1_000_000;
  });
  const [maxN, setMaxN] = useState<number>(() => {
    const fromUrl = readSearch(location.search, "max");
    const parsed = fromUrl ? parseFloat(fromUrl) : NaN;
    return Number.isFinite(parsed) && parsed >= 1e3 ? parsed : 1e9;
  });
  const [nInput, setNInput] = useState<string>(String(currentN));

  useEffect(() => {
    setNInput(String(currentN));
  }, [currentN]);

  useEffect(() => {
    const params = new URLSearchParams();
    params.set("compare", selected.map(encodeURIComponent).join(","));
    params.set("n", String(currentN));
    params.set("max", String(maxN));
    navigate(`/compare?${params.toString()}`, { replace: true });
  }, [selected, currentN, maxN, navigate]);

  const selectedAlgos = useMemo(
    () =>
      selected
        .map((n) => algorithms.find((a) => a.name === n))
        .filter((a): a is NonNullable<typeof a> => Boolean(a)),
    [selected]
  );

  const grouped = useMemo(() => {
    const g: Record<FamilyKey, typeof algorithms> = {
      scan_family: [],
      index_family: [],
      join_family: [],
      cache_family: [],
      planner_family: [],
    };
    algorithms.forEach((a) => g[a.family].push(a));
    return g;
  }, []);

  const toggle = (name: string) =>
    setSelected((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]
    );

  const setCurrentNClamped = (n: number) => {
    const clamped = Math.max(10, Math.min(maxN, Math.round(n)));
    setCurrentN(clamped);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.25 }}
      className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-6"
    >
      <aside className="rounded-xl border border-border/60 bg-card/60 p-3 lg:max-h-[calc(100vh-180px)] lg:overflow-y-auto">
        <div className="flex items-center justify-between mb-2 px-1">
          <h2 className="text-sm font-semibold">Algorithms</h2>
          <button
            onClick={() => setSelected([])}
            className="text-[11px] text-muted hover:text-ink"
          >
            clear
          </button>
        </div>
        {(Object.keys(grouped) as FamilyKey[]).map((fk) => (
          <div key={fk} className="mb-3">
            <div className="text-[10px] uppercase tracking-wider text-muted px-1 mb-1">
              {grouped[fk][0]?.familyLabel}
            </div>
            <ul className="space-y-0.5">
              {grouped[fk].map((a) => {
                const on = selected.includes(a.name);
                return (
                  <li key={a.name}>
                    <button
                      onClick={() => toggle(a.name)}
                      className={`w-full text-left px-2 py-1.5 rounded-md text-xs flex items-center gap-2 transition-colors ${
                        on ? "bg-card text-ink" : "text-muted hover:text-ink hover:bg-card/60"
                      }`}
                    >
                      <span
                        className="w-2.5 h-2.5 rounded-full border"
                        style={{
                          background: on ? a.familyAccent : "transparent",
                          borderColor: a.familyAccent,
                        }}
                      />
                      <span className="font-mono truncate">{a.name}</span>
                      <span className="ml-auto font-mono text-[10px] text-muted">
                        {a.complexity.avg}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </aside>

      <section>
        <header className="mb-4 flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Compare time complexity</h1>
            <p className="text-sm text-muted mt-1">
              Click or drag on the chart to move <code>n</code>. Hover for exact values. Pick algorithms on the left.
            </p>
          </div>
          <button
            onClick={async () => {
              await navigator.clipboard.writeText(window.location.href);
            }}
            className="text-xs px-3 py-1.5 rounded-md border border-border/60 hover:border-accent transition-colors"
          >
            Copy share link
          </button>
        </header>

        {/* controls strip */}
        <div className="rounded-xl border border-border/60 bg-card/60 p-3 mb-3">
          <div className="flex flex-col md:flex-row md:items-center gap-3 flex-wrap">
            {/* n input */}
            <div className="flex items-center gap-2">
              <label className="text-xs text-muted whitespace-nowrap">rows (n)</label>
              <input
                type="text"
                inputMode="numeric"
                value={nInput}
                onChange={(e) => setNInput(e.target.value.replace(/[^\d]/g, ""))}
                onBlur={() => {
                  const n = parseInt(nInput || "0", 10);
                  if (Number.isFinite(n) && n >= 10) setCurrentNClamped(n);
                  else setNInput(String(currentN));
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                }}
                className="w-28 px-2 py-1 bg-panel/80 border border-border/60 rounded-md text-sm font-mono text-right focus:outline-none focus:border-accent"
              />
            </div>

            {/* preset chips */}
            <div className="flex items-center gap-1">
              {N_PRESETS.filter((p) => p.n <= maxN).map((p) => {
                const active = currentN === p.n;
                return (
                  <button
                    key={p.label}
                    onClick={() => setCurrentNClamped(p.n)}
                    className={`px-2 py-1 rounded-md text-xs font-mono border transition-colors ${
                      active
                        ? "bg-accent/20 border-accent text-ink"
                        : "border-border/60 text-muted hover:text-ink hover:border-accent/60"
                    }`}
                  >
                    {p.label}
                  </button>
                );
              })}
            </div>

            <div className="md:ml-auto flex items-center gap-2">
              <label className="text-xs text-muted whitespace-nowrap">max x-axis</label>
              <div className="flex rounded-md border border-border/60 overflow-hidden">
                {MAX_N_OPTIONS.map((m) => (
                  <button
                    key={m.label}
                    onClick={() => {
                      setMaxN(m.n);
                      if (currentN > m.n) setCurrentN(Math.round(m.n));
                    }}
                    className={`px-2.5 py-1 text-xs font-mono ${
                      maxN === m.n
                        ? "bg-accent/20 text-ink"
                        : "text-muted hover:text-ink"
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* big log-scale slider */}
          <div className="mt-3 flex items-center gap-3">
            <span className="text-[10px] text-muted font-mono w-8 text-right">10</span>
            <input
              type="range"
              min={Math.log10(10)}
              max={Math.log10(maxN)}
              step={0.005}
              value={Math.log10(currentN)}
              onChange={(e) =>
                setCurrentNClamped(Math.round(Math.pow(10, parseFloat(e.target.value))))
              }
              className="flex-1 accent-accent h-2"
            />
            <span className="text-[10px] text-muted font-mono w-12">{formatN(maxN)}</span>
          </div>
        </div>

        {/* chart */}
        <div className="rounded-xl border border-border/60 bg-card/60 p-4">
          {selectedAlgos.length === 0 ? (
            <div className="h-[460px] grid place-items-center text-muted text-sm">
              Pick at least one algorithm from the left.
            </div>
          ) : (
            <>
              <ComplexityChart
                selected={selectedAlgos}
                currentN={currentN}
                onChangeN={setCurrentNClamped}
                maxN={maxN}
              />
              {/* legend */}
              <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t border-border/40">
                {selectedAlgos.map((a) => (
                  <button
                    key={`leg-${a.name}`}
                    onClick={() => toggle(a.name)}
                    title="Remove from chart"
                    className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-panel/80 text-xs font-mono hover:bg-panel transition-colors"
                  >
                    <span
                      className="w-3 h-1.5 rounded-sm"
                      style={{ background: a.familyAccent }}
                    />
                    {a.name}
                    <span className="text-muted">{a.complexity.avg}</span>
                    <span className="text-muted hover:text-rose-400 ml-0.5">×</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        {/* ranking panel */}
        {selectedAlgos.length > 0 && (
          <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
            {selectedAlgos
              .map((a) => ({ a, y: evaluateCurve(a.curveKind, currentN) }))
              .sort((p, q) => p.y - q.y)
              .map(({ a, y }, i) => (
                <motion.div
                  key={a.name}
                  layout
                  className="rounded-lg border border-border/60 bg-panel/60 p-3 flex items-center gap-3"
                >
                  <div className="text-xs font-mono text-muted w-5">#{i + 1}</div>
                  <div className="w-2.5 h-8 rounded-full" style={{ background: a.familyAccent }} />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{a.title}</div>
                    <div className="text-[11px] text-muted font-mono">
                      {a.name} · {a.complexity.avg}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="text-[10px] text-muted">cost @ n={formatN(currentN)}</div>
                    <div className="text-sm font-mono">{formatN(y)}</div>
                  </div>
                </motion.div>
              ))}
          </div>
        )}
      </section>
    </motion.div>
  );
}
