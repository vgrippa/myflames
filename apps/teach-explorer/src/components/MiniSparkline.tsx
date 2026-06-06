import { sampleCurve } from "../data/complexity";
import type { CurveKind } from "../data/types";

interface Props {
  curveKind: CurveKind;
  color: string;
}

const W = 200;
const H = 40;

export function MiniSparkline({ curveKind, color }: Props) {
  const samples = sampleCurve(curveKind, 100, 1e9, 48);
  const xs = samples.map((p) => Math.log10(p.n));
  const ys = samples.map((p) => Math.log10(Math.max(p.y, 1e-3)));
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;
  const PAD = 2;
  const path = samples
    .map((_p, i) => {
      const x = PAD + ((xs[i] - xMin) / xSpan) * (W - PAD * 2);
      const y = H - PAD - ((ys[i] - yMin) / ySpan) * (H - PAD * 2);
      return `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full h-full"
      preserveAspectRatio="none"
      aria-hidden
    >
      <defs>
        <linearGradient id={`spark-${curveKind}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${path} L ${W - PAD} ${H - PAD} L ${PAD} ${H - PAD} Z`} fill={`url(#spark-${curveKind})`} />
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}
