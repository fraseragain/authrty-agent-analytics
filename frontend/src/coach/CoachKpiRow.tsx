import type { CoachSummary } from "../api/types";
import { formatUsd } from "../analytics/chartGeometry";

interface Props {
  summary: CoachSummary;
}

export default function CoachKpiRow({ summary }: Props) {
  const vs = summary.vs_prev_period_pct;
  const isUp = vs !== null && vs > 0;

  return (
    <div className="coach-kpi-row">
      <div className="coach-kpi">
        <span className="kpi-label">Total spend</span>
        <span className="kpi-value">{formatUsd(summary.total_usd)}</span>
      </div>
      <div className="coach-kpi">
        <span className="kpi-label">Avoidable spend</span>
        <span className="kpi-value">{formatUsd(summary.avoidable_usd)}</span>
      </div>
      <div className="coach-kpi">
        <span className="kpi-label">Avoidable %</span>
        <span className="kpi-value">{summary.avoidable_pct.toFixed(1)}%</span>
      </div>
      <div className="coach-kpi">
        <span className="kpi-label">vs last period</span>
        {vs !== null ? (
          <span className={`kpi-value kpi-delta ${isUp ? "negative" : "positive"}`}>
            {isUp ? "▲" : "▼"} {Math.abs(vs).toFixed(1)}%
          </span>
        ) : (
          <span className="kpi-value" style={{ color: "var(--text-muted)" }}>—</span>
        )}
      </div>
    </div>
  );
}
