import type { CoachWeek, CoachActionMarker } from "../api/types";
import {
  SUCCESS_COLOR, WARNING_COLOR, DANGER_COLOR, SUBAGENT_COLOR,
} from "../analytics/chartGeometry";

interface Props {
  weeks: CoachWeek[];
  markers: CoachActionMarker[];
}

function formatMarkerDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

const W = 600;
const H = 150;
const PL = 44;
const PT = 10;
const PW = W - PL - 12;
const PH = H - PT - 32;

const r = (v: number) => Math.round(v * 10) / 10;

type LayerKey = "efficient_usd" | "loop_waste_usd" | "error_retry_usd" | "alerted_usd";

const LAYERS: Array<{ key: LayerKey; color: string; label: string }> = [
  { key: "efficient_usd", color: SUCCESS_COLOR, label: "Efficient" },
  { key: "loop_waste_usd", color: WARNING_COLOR, label: "Loop waste" },
  { key: "error_retry_usd", color: DANGER_COLOR, label: "Error retries" },
  { key: "alerted_usd", color: SUBAGENT_COLOR, label: "Alerted sessions" },
];

// For daily granularity partial_days is always 7 (no-op), so this just returns w unchanged.
function projectWeek(w: CoachWeek): CoachWeek {
  if (!w.is_partial || w.partial_days >= 7) return w;
  const scale = 7 / w.partial_days;
  return {
    ...w,
    total_usd: w.total_usd * scale,
    efficient_usd: w.efficient_usd * scale,
    loop_waste_usd: w.loop_waste_usd * scale,
    error_retry_usd: w.error_retry_usd * scale,
    alerted_usd: w.alerted_usd * scale,
    avoidable_usd: w.avoidable_usd * scale,
    model_overkill_usd: w.model_overkill_usd * scale,
  };
}

export default function AvoidableSpendChart({ weeks, markers }: Props) {
  if (weeks.length === 0) return <div className="empty-state">No data yet.</div>;

  const displayWeeks = weeks.map(projectWeek);

  const n = displayWeeks.length;
  const maxTotal = Math.max(...displayWeeks.map((w) => w.total_usd), 0.01);
  const xAt = (i: number) => PL + (n <= 1 ? PW / 2 : (i / (n - 1)) * PW);
  const yAt = (v: number) => PT + PH - (v / maxTotal) * PH;

  const partialIdx = weeks[n - 1]?.is_partial ? n - 1 : -1;

  // Weekend shading — shade the column around each Sat/Sun data point.
  const dayStep = n <= 1 ? PW : PW / (n - 1);
  const halfStep = dayStep / 2;
  const weekendRects = weeks
    .map((w, i) => {
      const dow = new Date(w.week + "T12:00:00Z").getUTCDay(); // 0=Sun, 6=Sat
      if (dow !== 0 && dow !== 6) return null;
      const cx = r(xAt(i));
      return { x: r(Math.max(PL, cx - halfStep)), width: r(Math.min(W - 12, cx + halfStep) - Math.max(PL, cx - halfStep)) };
    })
    .filter((v): v is { x: number; width: number } => v !== null);

  const solidPaths: Array<{ color: string; label: string; areaPath: string }> = [];
  const dashedPaths: Array<{ color: string; areaPath: string }> = [];
  const lower = new Array(n).fill(0);

  for (const layer of LAYERS) {
    const upper = displayWeeks.map((w, i) => lower[i] + (w[layer.key] as number));

    if (partialIdx >= 0) {
      const solidEnd = partialIdx;
      const topSolid = upper.slice(0, solidEnd + 1).map((v, i) => `${r(xAt(i))},${r(yAt(v))}`);
      const botSolid = lower.slice(0, solidEnd + 1).map((v, i) => `${r(xAt(i))},${r(yAt(v))}`).reverse();
      solidPaths.push({
        color: layer.color,
        label: layer.label,
        areaPath: `M ${topSolid.join(" L ")} L ${botSolid.join(" L ")} Z`,
      });

      if (partialIdx > 0) {
        const px = partialIdx;
        const topDash = [upper[px - 1], upper[px]].map((v, j) => `${r(xAt(px - 1 + j))},${r(yAt(v))}`);
        const botDash = [lower[px - 1], lower[px]].map((v, j) => `${r(xAt(px - 1 + j))},${r(yAt(v))}`).reverse();
        dashedPaths.push({
          color: layer.color,
          areaPath: `M ${topDash.join(" L ")} L ${botDash.join(" L ")} Z`,
        });
      }
    } else {
      const topPts = upper.map((v, i) => `${r(xAt(i))},${r(yAt(v))}`);
      const botPts = lower.map((v, i) => `${r(xAt(i))},${r(yAt(v))}`).reverse();
      solidPaths.push({
        color: layer.color,
        label: layer.label,
        areaPath: `M ${topPts.join(" L ")} L ${botPts.join(" L ")} Z`,
      });
    }

    for (let i = 0; i < n; i++) lower[i] = upper[i];
  }

  const yTicks = [maxTotal, maxTotal / 2, 0].map((v) => ({
    y: r(yAt(v)),
    label: `$${v.toFixed(2)}`,
  }));

  const MINBAR_PX = 3; // minimum visible height in SVG units for near-zero days
  const minBarMarkers = displayWeeks
    .map((w, i) => ({ x: r(xAt(i)), h: (w.total_usd / maxTotal) * PH, hasData: w.total_usd > 0 }))
    .filter((m) => m.hasData && m.h < MINBAR_PX);

  // Show ~8 x-axis labels across the range
  const step = Math.max(1, Math.ceil(n / 8));
  const xIdxs: number[] = [];
  for (let i = 0; i < n; i += step) xIdxs.push(i);
  if (n > 0 && xIdxs[xIdxs.length - 1] !== n - 1) xIdxs.push(n - 1);

  // One SVG line per unique x position (deduplicated), individual pill per applied action
  const individualMarkers = markers
    .flatMap((m) => {
      const idx = weeks.findIndex((w) => w.week === m.week);
      if (idx < 0) return [];
      const x = r(xAt(idx));
      if (x < PL) return [];
      return [{ x, pct: (x / W) * 100, key: `${m.kind}:${m.key}`, label: m.label, applied_at: m.applied_at }];
    });

  const uniqueXPositions = Array.from(new Set(individualMarkers.map((m) => m.x)));

  // Assign stack index per x position so pills at the same date don't overlap
  const xStackIdx = new Map<number, number>();
  const pillMarkers = individualMarkers.map((m) => {
    const idx = xStackIdx.get(m.x) ?? 0;
    xStackIdx.set(m.x, idx + 1);
    return { ...m, stackIdx: idx };
  });

  const PILL_H = 26; // px per stacked pill row
  const maxStack = pillMarkers.length > 0
    ? Math.max(...pillMarkers.map((p) => p.stackIdx + 1))
    : 0;

  return (
    <div className="sot">
      <div className="sot-plot">
        <svg className="sot-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Avoidable spend over time">
          {weekendRects.map((rect) => (
            <rect key={rect.x} x={rect.x} y={PT} width={rect.width} height={PH} fill="var(--muted)" opacity={0.15} />
          ))}
          {yTicks.map((t) => (
            <line key={t.label} className="sot-grid" x1={PL} y1={t.y} x2={W - 12} y2={t.y} />
          ))}
          {solidPaths.map((p) => (
            <path key={p.label} d={p.areaPath} fill={p.color} fillOpacity={0.55} />
          ))}
          {dashedPaths.map((p, i) => (
            <path key={i} d={p.areaPath} fill={p.color} fillOpacity={0.25} />
          ))}
          {partialIdx >= 0 && (
            <line
              x1={r(xAt(partialIdx))} y1={PT}
              x2={r(xAt(partialIdx))} y2={PT + PH}
              stroke="var(--muted)"
              strokeWidth={1}
              strokeDasharray="3 3"
              opacity={0.5}
            />
          )}
          {uniqueXPositions.map((x) => (
            <line
              key={x}
              x1={x} y1={PT}
              x2={x} y2={PT + PH}
              stroke="var(--success)"
              strokeWidth={1.5}
              opacity={0.7}
            />
          ))}
          {minBarMarkers.map((m) => (
            <rect
              key={m.x}
              x={m.x - 3} y={PT + PH - MINBAR_PX}
              width={6} height={MINBAR_PX}
              fill={SUCCESS_COLOR}
              opacity={0.6}
            />
          ))}
        </svg>
        {yTicks.map((t) => (
          <span className="sot-ylabel" key={t.label} style={{ top: `${((t.y - PT) / PH) * 100}%` }}>
            {t.label}
          </span>
        ))}
      </div>
      <div className="sot-xaxis">
        {xIdxs.map((i) => (
          <span key={i} style={weeks[i].is_partial ? { opacity: 0.55 } : undefined}>
            {weeks[i].week_label}
          </span>
        ))}
      </div>
      {pillMarkers.length > 0 && (
        <div className="sot-skill-pills" style={{ height: maxStack * PILL_H }}>
          {pillMarkers.map((m) => (
            <span
              key={`${m.key}-${m.stackIdx}`}
              className="sot-skill-pill"
              style={{ left: `${m.pct}%`, top: m.stackIdx * PILL_H }}
            >
              {m.label}
              <span className="sot-skill-pill-date">{formatMarkerDate(m.applied_at)}</span>
            </span>
          ))}
        </div>
      )}
      <div className="chip-legend card-bottom-legend">
        {LAYERS.map((l) => (
          <span key={l.key}><i style={{ background: l.color }} />{l.label}</span>
        ))}
      </div>
    </div>
  );
}
