import type { CoachFinding, CoachPatternsResponse } from "../api/types";
import { WARNING_COLOR, INFO_COLOR } from "../analytics/chartGeometry";

interface Props {
  data: CoachPatternsResponse;
}

function formatLastSeen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function FindingCard({ finding }: { finding: CoachFinding }) {
  const isWarning = finding.severity === "warning";
  const accentColor = isWarning ? WARNING_COLOR : INFO_COLOR;
  const icon = isWarning ? "⚠" : "→";

  return (
    <div className="pd-card" style={{ borderLeftColor: accentColor }}>
      <div className="pd-card-header">
        <span className="pd-card-icon" style={{ color: accentColor }}>{icon}</span>
        <span className="pd-card-title">{finding.title}</span>
        <span className="pd-card-metric" style={{ color: accentColor }}>{finding.metric}</span>
      </div>
      <p className="pd-card-body">{finding.body}</p>
      {finding.last_seen_at && (
        <p className="pd-card-lastseen">
          Last seen <time dateTime={finding.last_seen_at}>{formatLastSeen(finding.last_seen_at)}</time>
        </p>
      )}
      <div className="pd-card-footer">
        <p className="pd-card-recommendation">
          <span className="pd-card-rec-label">Fix: </span>
          {finding.recommendation}
        </p>
      </div>
    </div>
  );
}

export default function PatternDiscovery({ data }: Props) {
  if (data.findings.length === 0) {
    return (
      <div className="empty-state">
        No recurring patterns detected yet — needs more sessions with errors or loops.
      </div>
    );
  }

  return (
    <div className="pd-section">
      <p className="pd-subtitle">
        {data.findings.length} pattern{data.findings.length !== 1 ? "s" : ""} detected
        across {data.total_session_count} sessions over the last {data.weeks} weeks.
      </p>
      <div className="pd-list">
        {data.findings.map((f) => (
          <FindingCard key={f.title} finding={f} />
        ))}
      </div>
    </div>
  );
}
