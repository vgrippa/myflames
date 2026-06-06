import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import type { Algorithm } from "../data/types";
import { BigOBadge } from "./BigOBadge";
import { MiniSparkline } from "./MiniSparkline";

interface Props {
  algo: Algorithm;
  index: number;
}

export function AlgorithmCard({ algo, index }: Props) {
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        delay: Math.min(index * 0.025, 0.4),
        duration: 0.32,
        ease: [0.22, 1, 0.36, 1],
      }}
    >
      <Link
        to={`/algo/${algo.name}`}
        className="group relative block h-full rounded-xl border border-border/60 bg-card/70 hover:bg-card transition-all p-4 overflow-hidden"
      >
        <span
          aria-hidden
          className="absolute inset-x-0 top-0 h-[3px]"
          style={{ background: algo.familyAccent }}
        />
        <div className="flex items-start justify-between gap-2 mb-2">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-wider text-muted">
              {algo.familyLabel}
              {algo.curriculumStep !== null && (
                <span className="ml-1.5 px-1.5 py-0.5 rounded bg-accent/15 text-accent font-mono">
                  T{algo.curriculumStep}
                </span>
              )}
            </div>
            <h3 className="text-sm font-semibold mt-0.5 truncate">{algo.title}</h3>
          </div>
          <BigOBadge label={algo.complexity.avg} accent={algo.familyAccent} />
        </div>
        <p className="text-xs text-muted leading-relaxed line-clamp-2 mb-3">
          {algo.summary}
        </p>
        <div className="h-10 -mx-1">
          <MiniSparkline curveKind={algo.curveKind} color={algo.familyAccent} />
        </div>
        <div className="mt-2 flex items-center justify-between text-[10px] text-muted">
          <code className="font-mono text-ink/80">{algo.name}</code>
          <span className="opacity-0 group-hover:opacity-100 transition-opacity text-accent">
            Open →
          </span>
        </div>
      </Link>
    </motion.div>
  );
}
