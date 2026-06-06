interface Props {
  label: string;
  tone?: "best" | "avg" | "worst";
  accent?: string;
}

const toneMap = {
  best: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  avg: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  worst: "bg-rose-500/15 text-rose-300 border-rose-500/30",
} as const;

export function BigOBadge({ label, tone = "avg", accent }: Props) {
  const style = accent
    ? { borderColor: `${accent}55`, color: accent, background: `${accent}1a` }
    : undefined;
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[11px] font-mono ${
        accent ? "" : toneMap[tone]
      }`}
      style={style}
    >
      {label}
    </span>
  );
}
