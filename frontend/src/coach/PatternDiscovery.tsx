import React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, X } from "lucide-react";
import type { CoachAction, CoachFinding, CoachPatternsResponse } from "../api/types";
import { WARNING_COLOR, INFO_COLOR, formatUsd } from "../analytics/chartGeometry";
import { applyCoachAction } from "../api/client";

interface Props {
  data: CoachPatternsResponse;
}

interface ToastState {
  id: number;
  kind: string;
  label: string;
  detail: string;
  hostPath: string | null;
}

let toastId = 0;

function fileUrl(hostPath: string): string {
  return "file://" + hostPath.split("/").map(encodeURIComponent).join("/");
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

function CoachToast({ toast, onDismiss }: { toast: ToastState; onDismiss: (id: number) => void }) {
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    timerRef.current = setTimeout(() => onDismiss(toast.id), 5000);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [toast.id, onDismiss]);

  return (
    <div className="coach-toast">
      <CheckCircle size={14} className="coach-toast-icon" />
      <div className="coach-toast-body">
        <span className="coach-toast-title">{toast.label}</span>
        <span className="coach-toast-detail">{toast.detail}</span>
        {toast.kind === "claude_md" && toast.hostPath && (
          <a className="coach-toast-link" href={fileUrl(toast.hostPath)} target="_blank" rel="noreferrer">
            Open CLAUDE.md →
          </a>
        )}
      </div>
      <button className="coach-toast-dismiss" onClick={() => onDismiss(toast.id)} aria-label="Dismiss">
        <X size={12} />
      </button>
    </div>
  );
}

function FindingCard({
  finding,
  onApplied,
}: {
  finding: CoachFinding;
  onApplied: (kind: string, label: string, detail: string, hostPath: string | null) => void;
}) {
  const isWarning = finding.severity === "warning";
  const accentColor = isWarning ? WARNING_COLOR : INFO_COLOR;
  const icon = isWarning ? "⚠" : "→";
  const queryClient = useQueryClient();
  const [pendingKey, setPendingKey] = React.useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (action: CoachAction) =>
      applyCoachAction(
        action.kind,
        action.key,
        action.kind === "claude_md" ? finding.recommendation : undefined,
        finding.title,
      ),
    onMutate: (action) => setPendingKey(`${action.kind}:${action.key}`),
    onSuccess: (result, action) => {
      onApplied(action.kind, action.label, result.detail, result.host_path);
      void queryClient.invalidateQueries({ queryKey: ["coach-patterns"] });
      // Applying an action logs a marker server-side -- refetch the spend
      // chart too so it shows up without a manual page reload.
      void queryClient.invalidateQueries({ queryKey: ["coach-avoidable"] });
    },
    onSettled: () => setPendingKey(null),
  });

  return (
    <div className="pd-card" style={{ borderLeftColor: accentColor }}>
      <div className="pd-card-header">
        <span className="pd-card-icon" style={{ color: accentColor }}>{icon}</span>
        <span className="pd-card-title">{finding.title}</span>
        <span className="pd-card-metric" style={{ color: accentColor }}>{finding.metric}</span>
        <span className="pd-card-cost" style={{ color: accentColor }}>
          {finding.estimated_cost_usd != null ? formatUsd(finding.estimated_cost_usd) : "—"}
        </span>
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
  const [toasts, setToasts] = React.useState<ToastState[]>([]);

  const addToast = React.useCallback((kind: string, label: string, detail: string, hostPath: string | null) => {
    setToasts((prev) => [...prev, { id: toastId++, kind, label, detail, hostPath }]);
  }, []);
  const dismissToast = React.useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toastStack = toasts.length > 0 && (
    <div className="coach-toast-stack">
      {toasts.map((t) => (
        <CoachToast key={t.id} toast={t} onDismiss={dismissToast} />
      ))}
    </div>
  );

  if (data.findings.length === 0) {
    return (
      <>
        {toastStack}
        <div className="empty-state">
          No recurring patterns detected yet — needs more sessions with errors or loops.
        </div>
      </>
    );
  }

  const rows = sortByLastSeen
    ? [...data.findings].sort((a, b) => lastSeenValue(b) - lastSeenValue(a))
    : data.findings;

  return (
    <div className="pd-section">
      {toastStack}
      <p className="pd-subtitle">
        {data.findings.length} pattern{data.findings.length !== 1 ? "s" : ""} detected
        across {data.total_session_count} sessions over the last {data.weeks} weeks.
      </p>
      <div className="pd-columns">
        <span className="pd-col-pattern">Pattern</span>
        <span className="pd-col-metric">Metric</span>
        <span className="pd-col-cost">Est. cost</span>
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
          <FindingCard key={f.title} finding={f} onApplied={addToast} />
        ))}
      </div>
    </div>
  );
}
