import React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { CoachAction, CoachFinding, CoachPatternsResponse } from "../api/types";
import { WARNING_COLOR, INFO_COLOR } from "../analytics/chartGeometry";
import { applyCoachAction } from "../api/client";

interface Props {
  data: CoachPatternsResponse;
}

function formatLastSeen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function lastSeenValue(finding: CoachFinding): number {
  if (!finding.last_seen_at) return -Infinity;
  const parsed = Date.parse(finding.last_seen_at);
  return Number.isNaN(parsed) ? -Infinity : parsed;
}

function FindingCard({ finding }: { finding: CoachFinding }) {
  const isWarning = finding.severity === "warning";
  const accentColor = isWarning ? WARNING_COLOR : INFO_COLOR;
  const icon = isWarning ? "⚠" : "→";
  const queryClient = useQueryClient();
  const [pendingKey, setPendingKey] = React.useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (action: CoachAction) =>
      applyCoachAction(action.kind, action.key, action.kind === "claude_md" ? finding.recommendation : undefined),
    onMutate: (action) => setPendingKey(`${action.kind}:${action.key}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["coach-patterns"] });
    },
    onSettled: () => setPendingKey(null),
  });

  return (
    <div className="pd-card" style={{ borderLeftColor: accentColor }}>
      <div className="pd-card-header">
        <span className="pd-card-icon" style={{ color: accentColor }}>{icon}</span>
        <span className="pd-card-title">{finding.title}</span>
        <span className="pd-card-metric" style={{ color: accentColor }}>{finding.metric}</span>
        <span className="pd-card-lastseen-col">
          {finding.last_seen_at ? (
            <time dateTime={finding.last_seen_at}>{formatLastSeen(finding.last_seen_at)}</time>
          ) : "—"}
        </span>
      </div>
      <p className="pd-card-body">{finding.body}</p>
      <div className="pd-card-footer">
        <p className="pd-card-recommendation">
          <span className="pd-card-rec-label">Fix: </span>
          {finding.recommendation}
        </p>
        {finding.actions.length > 0 && (
          <div className="pd-card-actions">
            {finding.actions.map((action) => {
              const actionKey = `${action.kind}:${action.key}`;
              const isPending = mutation.isPending && pendingKey === actionKey;
              return (
                <button
                  key={actionKey}
                  type="button"
                  className={`pd-action-btn ${action.applied ? "applied" : ""}`}
                  disabled={action.applied || isPending}
                  onClick={() => mutation.mutate(action)}
                >
                  {action.applied ? "Applied ✓" : isPending ? "Applying…" : action.label}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default function PatternDiscovery({ data }: Props) {
  const [sortByLastSeen, setSortByLastSeen] = React.useState(false);

  if (data.findings.length === 0) {
    return (
      <div className="empty-state">
        No recurring patterns detected yet — needs more sessions with errors or loops.
      </div>
    );
  }

  const rows = sortByLastSeen
    ? [...data.findings].sort((a, b) => lastSeenValue(b) - lastSeenValue(a))
    : data.findings;

  return (
    <div className="pd-section">
      <p className="pd-subtitle">
        {data.findings.length} pattern{data.findings.length !== 1 ? "s" : ""} detected
        across {data.total_session_count} sessions over the last {data.weeks} weeks.
      </p>
      <div className="pd-columns">
        <span className="pd-col-pattern">Pattern</span>
        <span className="pd-col-metric">Metric</span>
        <button
          type="button"
          className={`pd-col-lastseen ${sortByLastSeen ? "sorted" : ""}`}
          onClick={() => setSortByLastSeen((v) => !v)}
          aria-pressed={sortByLastSeen}
        >
          Last seen {sortByLastSeen ? "▼" : ""}
        </button>
      </div>
      <div className="pd-list">
        {rows.map((f) => (
          <FindingCard key={f.title} finding={f} />
        ))}
      </div>
    </div>
  );
}
