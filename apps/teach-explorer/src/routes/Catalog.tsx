import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { algorithms } from "../data/algorithms";
import { AlgorithmCard } from "../components/AlgorithmCard";
import { FamilyFilter } from "../components/FamilyFilter";
import type { FamilyKey } from "../data/types";

export default function Catalog() {
  const [filter, setFilter] = useState<FamilyKey | "all">("all");
  const [query, setQuery] = useState("");

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    algorithms.forEach((a) => {
      c[a.family] = (c[a.family] || 0) + 1;
    });
    return c;
  }, []);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return algorithms.filter((a) => {
      if (filter !== "all" && a.family !== filter) return false;
      if (!q) return true;
      return (
        a.title.toLowerCase().includes(q) ||
        a.summary.toLowerCase().includes(q) ||
        a.name.toLowerCase().includes(q) ||
        a.tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [filter, query]);

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.18 }}
    >
      <section className="mb-8">
        <motion.h1
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          className="text-3xl md:text-4xl font-bold tracking-tight"
        >
          MySQL / MariaDB algorithms, one screen at a time.
        </motion.h1>
        <motion.p
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.05 }}
          className="mt-3 text-muted max-w-2xl text-sm md:text-base"
        >
          Twenty-one execution algorithms from <code>myflames/teach</code>, each with its complexity profile,
          a working animated lesson, and an Apples-to-Apples log–log comparison.
        </motion.p>
      </section>

      <div className="flex flex-col md:flex-row md:items-center gap-3 mb-6">
        <FamilyFilter value={filter} onChange={setFilter} counts={counts} />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search algorithms, tags…"
          className="flex-1 min-w-0 md:max-w-sm px-3 py-2 bg-panel/80 border border-border/60 rounded-lg text-sm placeholder:text-muted focus:outline-none focus:border-accent transition-colors"
        />
        <div className="text-xs text-muted">
          Showing <span className="text-ink font-mono">{visible.length}</span> / {algorithms.length}
        </div>
      </div>

      <motion.div
        layout
        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4"
      >
        {visible.map((algo, i) => (
          <AlgorithmCard key={algo.name} algo={algo} index={i} />
        ))}
      </motion.div>

      {visible.length === 0 && (
        <div className="text-center py-16 text-muted text-sm">
          No matches. Try clearing the filter.
        </div>
      )}
    </motion.div>
  );
}
