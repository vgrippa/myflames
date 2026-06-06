import { motion } from "framer-motion";
import { Link, useParams } from "react-router-dom";
import { findAlgorithm } from "../data/algorithms";
import { BigOBadge } from "../components/BigOBadge";
import { MiniSparkline } from "../components/MiniSparkline";
import { curveLabel } from "../data/complexity";

export default function Detail() {
  const { name } = useParams<{ name: string }>();
  const algo = name ? findAlgorithm(name) : undefined;

  if (!algo) {
    return (
      <div className="text-center py-20">
        <h1 className="text-xl font-semibold mb-2">Algorithm not found</h1>
        <Link to="/" className="text-accent text-sm">← back to catalog</Link>
      </div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      className="max-w-4xl mx-auto"
    >
      <Link
        to="/"
        className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink mb-4"
      >
        ← Catalog
      </Link>

      <div className="rounded-xl border border-border/60 bg-card/70 overflow-hidden">
        <div className="h-1.5" style={{ background: algo.familyAccent }} />
        <div className="p-6">
          <div className="flex items-start justify-between gap-4 flex-wrap mb-4">
            <div>
              <div className="text-[11px] uppercase tracking-wider text-muted">
                {algo.familyLabel}
                {algo.curriculumStep !== null && (
                  <span className="ml-2 px-1.5 py-0.5 rounded bg-accent/15 text-accent font-mono">
                    curriculum step {algo.curriculumStep}
                  </span>
                )}
              </div>
              <h1 className="text-2xl md:text-3xl font-bold mt-1">{algo.title}</h1>
              <code className="text-xs text-muted">{algo.name}</code>
            </div>
            <div className="flex gap-2">
              <a
                href={algo.lessonHref}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 px-3 py-2 rounded-md bg-accent text-white text-sm font-medium hover:opacity-90 transition-opacity"
              >
                Open full lesson ↗
              </a>
              <Link
                to={`/compare?compare=${algo.name}`}
                className="inline-flex items-center gap-1 px-3 py-2 rounded-md border border-border/60 hover:border-accent text-sm transition-colors"
              >
                Compare →
              </Link>
            </div>
          </div>

          <p className="text-sm text-ink/85 leading-relaxed mb-6">{algo.summary}</p>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6">
            <ComplexityCell label="Best" value={algo.complexity.best} tone="best" />
            <ComplexityCell label="Average" value={algo.complexity.avg} tone="avg" />
            <ComplexityCell label="Worst" value={algo.complexity.worst} tone="worst" />
          </div>

          <div className="rounded-lg border border-border/60 bg-panel/60 p-4">
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs uppercase tracking-wider text-muted">
                Curve · {curveLabel[algo.curveKind]}
              </div>
              <BigOBadge label={algo.complexity.avg} accent={algo.familyAccent} />
            </div>
            <div className="h-32">
              <MiniSparkline curveKind={algo.curveKind} color={algo.familyAccent} />
            </div>
            <div className="mt-2 text-[11px] text-muted">
              Indicative shape. For interactive log–log overlay against other algorithms, open the{" "}
              <Link to={`/compare?compare=${algo.name}`} className="text-accent">
                Compare view
              </Link>
              .
            </div>
          </div>

          {(algo.tags.length > 0 || algo.mysqlVersion) && (
            <div className="mt-5 flex flex-wrap items-center gap-2 text-[11px]">
              {algo.mysqlVersion && (
                <span className="px-2 py-0.5 rounded-md border border-border/60 text-muted font-mono">
                  {algo.mysqlVersion}
                </span>
              )}
              {algo.tags.map((t) => (
                <span
                  key={t}
                  className="px-2 py-0.5 rounded-md bg-panel/80 text-muted font-mono"
                >
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}

function ComplexityCell({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "best" | "avg" | "worst";
}) {
  const toneBorder = {
    best: "border-emerald-500/30",
    avg: "border-sky-500/30",
    worst: "border-rose-500/30",
  }[tone];
  return (
    <div className={`rounded-lg border ${toneBorder} bg-panel/50 p-3`}>
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 font-mono text-sm">{value}</div>
    </div>
  );
}
