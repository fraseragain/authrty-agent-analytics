import type { CoachActionMarker } from "../api/types";
import { formatSignedUsd } from "../analytics/chartGeometry";

interface Props {
  markers: CoachActionMarker[];
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export default function CoachActionImpactList({ markers }: Props) {
  if (markers.length === 0) {
    return (
      <div className="empty-state">
        No actions applied yet — apply a fix from Pattern discoveries to start tracking impact.
      </div>
    );
  }

  const rows = [...markers].sort((a, b) => Date.parse(b.applied_at) - Date.parse(a.applied_at));

  return (
    <div className="coach-impact-list">
      {rows.map((m) => {
        const { impact } = m;
        const pctDelta = impact.after_avoidable_pct - impact.before_avoidable_pct;
        const usdDelta = impact.after_avoidable_usd - impact.before_avoidable_usd;
        const deltaClass = pctDelta < 0 ? "positive" : pctDelta > 0 ? "negative" : "";

        return (
          <div key={`${m.kind}:${m.key}:${m.applied_at}`} className="coach-impact-row">
            <div className="coach-impact-header">
              <span className="coach-impact-label">{m.label}</span>
              <span className="coach-impact-date">{formatDate(m.applied_at)}</span>
            </div>
            {impact.sufficient_data ? (
              <div className="coach-impact-body">
                <span className={`coach-impact-delta ${deltaClass}`}>
                  {impact.before_avoidable_pct.toFixed(1)}% → {impact.after_avoidable_pct.toFixed(1)}% avoidable
                </span>
                <span className="coach-impact-usd">{formatSignedUsd(usdDelta)} avoidable spend</span>
                {impact.confounded && (
                  <span
                    className="coach-impact-caveat"
                    title={`Another action was applied within ${impact.window_days} days — this comparison may be confounded.`}
                  >
                    ⚠ possibly confounded
                  </span>
                )}
              </div>
            ) : (
              <div className="coach-impact-pending">
                {impact.after_days}/{impact.window_days} days of data since applying — check back soon.
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
