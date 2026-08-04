import type { CoachSummary } from "../api/types";
import { formatUsd, SUCCESS_COLOR, WARNING_COLOR, DANGER_COLOR, SUBAGENT_COLOR, INFO_COLOR, NEUTRAL_COLOR } from "../analytics/chartGeometry";

interface Props {
  summary: CoachSummary;
}

const CX = 70;
const CY = 70;
const R = 50;
const SW = 20;
const CIRC = 2 * Math.PI * R;

interface Segment {
  label: string;
  value: number;
  color: string;
}

function DonutArc({ offset, fraction, color }: { offset: number; fraction: number; color: string }) {
  const len = fraction * CIRC;
  if (len <= 0) return null;
  return (
    <circle
      cx={CX} cy={CY} r={R}
      fill="none"
      stroke={color}
      strokeWidth={SW}
      strokeDasharray={`${len} ${CIRC - len}`}
      strokeDashoffset={-offset * CIRC}
      transform={`rotate(-90 ${CX} ${CY})`}
    />
  );
}

export default function AvoidableBreakdown({ summary }: Props) {
  const total = summary.total_usd;

  if (total <= 0) {
    return <div className="empty-state">No spend data yet.</div>;
  }

  const efficient = Math.max(total - summary.avoidable_usd, 0);

  const allSegments: Segment[] = [
    { label: "Efficient spend", value: efficient, color: SUCCESS_COLOR },
    { label: "Loop waste", value: summary.loop_waste_usd, color: WARNING_COLOR },
    { label: "Error retries", value: summary.error_retry_usd, color: DANGER_COLOR },
    { label: "Alerted sessions", value: summary.alerted_usd, color: SUBAGENT_COLOR },
    { label: "Model overkill", value: summary.model_overkill_usd, color: INFO_COLOR },
  ];

  const segments = allSegments.filter((s) => s.value > 0);

  let offset = 0;
  const arcs = segments.map((s) => {
    const fraction = s.value / total;
    const arc = { fraction, offset, color: s.color };
    offset += fraction;
    return arc;
  });

  return (
    <div className="avoidable-donut">
      <svg width={140} height={140} viewBox="0 0 140 140" aria-hidden="true">
        <circle cx={CX} cy={CY} r={R} fill="none" stroke={NEUTRAL_COLOR} strokeWidth={SW} opacity={0.25} />
        {arcs.map((a, i) => (
          <DonutArc key={i} offset={a.offset} fraction={a.fraction} color={a.color} />
        ))}
        <text x={CX} y={CY - 6} textAnchor="middle" fontSize={11} fill="var(--text-muted)">avoidable</text>
        <text x={CX} y={CY + 10} textAnchor="middle" fontSize={13} fontWeight="600" fill="var(--text)">
          {summary.avoidable_pct.toFixed(0)}%
        </text>
      </svg>
      <div className="avoidable-legend">
        {allSegments.map((s) => (
          <div key={s.label} className={`avoidable-legend-row ${s.value <= 0 ? "avoidable-legend-row--zero" : ""}`}>
            <span className="avoidable-legend-swatch" style={{ background: s.color }} />
            <span className="avoidable-legend-label">{s.label}</span>
            <span className="avoidable-legend-value">{formatUsd(s.value)}</span>
            <span className="avoidable-legend-pct">({total > 0 ? ((s.value / total) * 100).toFixed(0) : 0}%)</span>
          </div>
        ))}
        <div className="avoidable-legend-total">
          <span className="avoidable-legend-total-label">Total</span>
          <span className="avoidable-legend-total-value">{formatUsd(total)}</span>
        </div>
      </div>
    </div>
  );
}
