import { motion } from "framer-motion";
import type { FamilyKey } from "../data/types";

const FAMILY_ORDER: Array<{ key: FamilyKey | "all"; label: string; color: string }> = [
  { key: "all", label: "All", color: "#5b8def" },
  { key: "scan_family", label: "Scan", color: "#ef4444" },
  { key: "index_family", label: "Index", color: "#2563eb" },
  { key: "join_family", label: "Join", color: "#a855f7" },
  { key: "cache_family", label: "Cache", color: "#f59e0b" },
  { key: "planner_family", label: "Planner", color: "#ec4899" },
];

interface Props {
  value: FamilyKey | "all";
  onChange: (v: FamilyKey | "all") => void;
  counts: Record<string, number>;
}

export function FamilyFilter({ value, onChange, counts }: Props) {
  return (
    <div className="flex flex-wrap gap-1.5 p-1 rounded-lg bg-panel/80 border border-border/60 w-fit">
      {FAMILY_ORDER.map((f) => {
        const active = value === f.key;
        const n = f.key === "all" ? Object.values(counts).reduce((a, b) => a + b, 0) : counts[f.key] || 0;
        return (
          <button
            key={f.key}
            onClick={() => onChange(f.key)}
            className="relative px-3 py-1.5 text-xs font-medium rounded-md text-ink/80 hover:text-ink"
          >
            {active && (
              <motion.span
                layoutId="family-pill"
                className="absolute inset-0 rounded-md"
                style={{ background: `${f.color}26`, border: `1px solid ${f.color}66` }}
                transition={{ type: "spring", stiffness: 380, damping: 32 }}
              />
            )}
            <span className="relative inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: f.color }} />
              {f.label}
              <span className="text-muted font-mono text-[10px]">{n}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
