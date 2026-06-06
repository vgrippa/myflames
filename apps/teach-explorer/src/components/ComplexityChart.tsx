import { motion } from "framer-motion";
import { useEffect, useMemo, useRef, useState } from "react";
import { evaluateCurve, formatN, sampleCurve } from "../data/complexity";
import type { Algorithm } from "../data/types";

interface Props {
  selected: Algorithm[];
  currentN: number;
  onChangeN: (n: number) => void;
  maxN: number;
  height?: number;
}

const MARGIN = { top: 28, right: 28, bottom: 64, left: 84 };
const MIN_N = 10;

export function ComplexityChart({
  selected,
  currentN,
  onChangeN,
  maxN,
  height = 460,
}: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(900);
  const [hover, setHover] = useState<{ n: number; x: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!wrapRef.current) return;
    const el = wrapRef.current;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && w > 0) setWidth(Math.max(360, Math.floor(w)));
    });
    ro.observe(el);
    setWidth(Math.max(360, Math.floor(el.getBoundingClientRect().width)));
    return () => ro.disconnect();
  }, []);

  const innerW = Math.max(80, width - MARGIN.left - MARGIN.right);
  const innerH = Math.max(120, height - MARGIN.top - MARGIN.bottom);

  const logMinX = Math.log10(MIN_N);
  const logMaxX = Math.log10(maxN);

  const allSamples = useMemo(
    () => selected.map((a) => ({ algo: a, samples: sampleCurve(a.curveKind, MIN_N, maxN, 180) })),
    [selected, maxN]
  );

  const yMax = useMemo(() => {
    let m = 1;
    for (const { samples } of allSamples) {
      for (const p of samples) m = Math.max(m, p.y);
    }
    return m * 1.25;
  }, [allSamples]);
  const yMin = 0.5;
  const logMinY = Math.log10(yMin);
  const logMaxY = Math.log10(yMax);

  const xScale = (n: number) => ((Math.log10(n) - logMinX) / (logMaxX - logMinX)) * innerW;
  const yScale = (y: number) =>
    innerH - ((Math.log10(Math.max(y, yMin)) - logMinY) / (logMaxY - logMinY)) * innerH;
  const xInverse = (px: number) =>
    Math.pow(10, logMinX + Math.min(1, Math.max(0, px / innerW)) * (logMaxX - logMinX));

  const xTicks = useMemo(() => {
    const ticks: number[] = [];
    for (let i = Math.ceil(logMinX); i <= Math.floor(logMaxX); i++) {
      ticks.push(Math.pow(10, i));
    }
    return ticks;
  }, [logMinX, logMaxX]);

  const yTicks = useMemo(() => {
    const ticks: number[] = [];
    const top = Math.ceil(logMaxY);
    const bot = Math.floor(logMinY);
    for (let i = bot; i <= top; i++) ticks.push(Math.pow(10, i));
    return ticks;
  }, [logMaxY, logMinY]);

  const handlePointer = (clientX: number) => {
    const svg = wrapRef.current?.querySelector("svg");
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * width - MARGIN.left;
    const clamped = Math.max(0, Math.min(innerW, px));
    return { px: clamped, n: Math.round(xInverse(clamped)) };
  };

  return (
    <div ref={wrapRef} className="relative w-full select-none">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label="Log-log complexity chart"
        onMouseDown={(e) => {
          const p = handlePointer(e.clientX);
          if (p) {
            onChangeN(p.n);
            setDragging(true);
          }
        }}
        onMouseMove={(e) => {
          const p = handlePointer(e.clientX);
          if (p) setHover({ n: p.n, x: p.px });
          if (dragging && p) onChangeN(p.n);
        }}
        onMouseUp={() => setDragging(false)}
        onMouseLeave={() => {
          setHover(null);
          setDragging(false);
        }}
        className={dragging ? "cursor-grabbing" : "cursor-crosshair"}
      >
        <defs>
          <clipPath id="plot-area">
            <rect x={0} y={0} width={innerW} height={innerH} />
          </clipPath>
        </defs>

        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {/* gridlines */}
          {xTicks.map((t) => (
            <line
              key={`gx-${t}`}
              x1={xScale(t)}
              x2={xScale(t)}
              y1={0}
              y2={innerH}
              stroke="#2a335a"
              strokeWidth="1"
              strokeDasharray="2 4"
              opacity="0.5"
            />
          ))}
          {yTicks.map((t) => (
            <line
              key={`gy-${t}`}
              x1={0}
              x2={innerW}
              y1={yScale(t)}
              y2={yScale(t)}
              stroke="#2a335a"
              strokeWidth="1"
              strokeDasharray="2 4"
              opacity="0.5"
            />
          ))}

          {/* axes */}
          <line x1={0} x2={innerW} y1={innerH} y2={innerH} stroke="#5a6594" strokeWidth="1.5" />
          <line x1={0} x2={0} y1={0} y2={innerH} stroke="#5a6594" strokeWidth="1.5" />

          {/* x tick marks + labels */}
          {xTicks.map((t) => (
            <g key={`xl-${t}`}>
              <line x1={xScale(t)} x2={xScale(t)} y1={innerH} y2={innerH + 6} stroke="#5a6594" />
              <text
                x={xScale(t)}
                y={innerH + 20}
                fontSize="11"
                fill="#c8cee6"
                textAnchor="middle"
                fontFamily="ui-monospace, Menlo, monospace"
              >
                {formatN(t)}
              </text>
            </g>
          ))}

          {/* y tick marks + labels */}
          {yTicks.map((t) => (
            <g key={`yl-${t}`}>
              <line x1={-6} x2={0} y1={yScale(t)} y2={yScale(t)} stroke="#5a6594" />
              <text
                x={-10}
                y={yScale(t) + 4}
                fontSize="11"
                fill="#c8cee6"
                textAnchor="end"
                fontFamily="ui-monospace, Menlo, monospace"
              >
                {t < 1 ? t.toFixed(1) : formatN(t)}
              </text>
            </g>
          ))}

          {/* curves */}
          <g clipPath="url(#plot-area)">
            {allSamples.map(({ algo, samples }) => {
              const d = samples
                .map(
                  (p, i) =>
                    `${i === 0 ? "M" : "L"} ${xScale(p.n).toFixed(2)} ${yScale(p.y).toFixed(2)}`
                )
                .join(" ");
              return (
                <motion.path
                  key={algo.name}
                  d={d}
                  fill="none"
                  stroke={algo.familyAccent}
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  initial={{ pathLength: 0, opacity: 0 }}
                  animate={{ pathLength: 1, opacity: 1 }}
                  transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
                />
              );
            })}
          </g>

          {/* current-n vertical marker */}
          <motion.line
            x1={xScale(currentN)}
            x2={xScale(currentN)}
            y1={0}
            y2={innerH}
            stroke="#5b8def"
            strokeWidth="1.5"
            strokeDasharray="4 4"
            initial={false}
            animate={{ x1: xScale(currentN), x2: xScale(currentN) }}
            transition={{ type: "spring", stiffness: 380, damping: 32 }}
          />
          <motion.g
            initial={false}
            animate={{ x: xScale(currentN) }}
            transition={{ type: "spring", stiffness: 380, damping: 32 }}
          >
            <circle r="8" fill="#5b8def" stroke="#0b1020" strokeWidth="2" cy={innerH} />
            <text
              y={innerH + 38}
              fontSize="11"
              fill="#5b8def"
              textAnchor="middle"
              fontFamily="ui-monospace, Menlo, monospace"
              fontWeight="600"
            >
              n = {formatN(currentN)}
            </text>
          </motion.g>

          {/* intersection dots at current n */}
          {selected.map((a) => {
            const y = evaluateCurve(a.curveKind, currentN);
            return (
              <motion.circle
                key={`mark-${a.name}`}
                r="5"
                fill={a.familyAccent}
                stroke="#0b1020"
                strokeWidth="2"
                initial={false}
                animate={{ cx: xScale(currentN), cy: yScale(y) }}
                transition={{ type: "spring", stiffness: 380, damping: 30 }}
              />
            );
          })}

          {/* hover guideline + tooltip */}
          {hover && !dragging && (
            <g pointerEvents="none">
              <line
                x1={hover.x}
                x2={hover.x}
                y1={0}
                y2={innerH}
                stroke="#e8ecf6"
                strokeWidth="1"
                opacity="0.35"
              />
              <HoverTooltip
                hover={hover}
                selected={selected}
                yScale={yScale}
                xScale={xScale}
                innerW={innerW}
              />
            </g>
          )}

          {/* axis titles */}
          <text
            x={innerW / 2}
            y={innerH + 54}
            fontSize="13"
            fill="#e8ecf6"
            textAnchor="middle"
            fontWeight="600"
          >
            rows · n (log scale)
          </text>
          <text
            transform={`translate(-64,${innerH / 2}) rotate(-90)`}
            fontSize="13"
            fill="#e8ecf6"
            textAnchor="middle"
            fontWeight="600"
          >
            relative cost (log scale)
          </text>
        </g>
      </svg>
    </div>
  );
}

function HoverTooltip({
  hover,
  selected,
  yScale,
  xScale,
  innerW,
}: {
  hover: { n: number; x: number };
  selected: Algorithm[];
  yScale: (y: number) => number;
  xScale: (n: number) => number;
  innerW: number;
}) {
  const TOOLTIP_W = 180;
  const lineH = 16;
  const padTop = 26;
  const tooltipH = padTop + selected.length * lineH + 6;
  const showRight = hover.x + 14 + TOOLTIP_W < innerW;
  const tx = showRight ? hover.x + 14 : hover.x - 14 - TOOLTIP_W;

  // Snap-to-curve dots so the user sees exact intersection points.
  const dots = selected.map((a) => ({
    a,
    cx: xScale(hover.n),
    cy: yScale(evaluateCurve(a.curveKind, hover.n)),
  }));

  return (
    <g>
      {dots.map(({ a, cx, cy }) => (
        <circle
          key={`hd-${a.name}`}
          cx={cx}
          cy={cy}
          r="4"
          fill={a.familyAccent}
          stroke="#0b1020"
          strokeWidth="1.5"
          opacity="0.9"
        />
      ))}
      <rect
        x={tx}
        y={6}
        width={TOOLTIP_W}
        height={tooltipH}
        rx="6"
        fill="#121830"
        stroke="#3d4870"
      />
      <text
        x={tx + 10}
        y={22}
        fontSize="11"
        fill="#9aa3c0"
        fontFamily="ui-monospace, Menlo, monospace"
      >
        n = {formatN(hover.n)}{" "}
        <tspan fill="#5b8def">({hover.n.toLocaleString()})</tspan>
      </text>
      {selected
        .map((a) => ({ a, y: evaluateCurve(a.curveKind, hover.n) }))
        .sort((p, q) => p.y - q.y)
        .map(({ a, y }, i) => (
          <g key={`hl-${a.name}`}>
            <circle cx={tx + 14} cy={padTop + i * lineH + 6} r="4" fill={a.familyAccent} />
            <text
              x={tx + 24}
              y={padTop + i * lineH + 10}
              fontSize="11"
              fill="#e8ecf6"
              fontFamily="ui-monospace, Menlo, monospace"
            >
              {a.name}
            </text>
            <text
              x={tx + TOOLTIP_W - 10}
              y={padTop + i * lineH + 10}
              fontSize="11"
              fill="#c8cee6"
              fontFamily="ui-monospace, Menlo, monospace"
              textAnchor="end"
            >
              {formatN(y)}
            </text>
          </g>
        ))}
    </g>
  );
}
