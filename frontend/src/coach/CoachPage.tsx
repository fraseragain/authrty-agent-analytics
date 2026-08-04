import { useQuery } from "@tanstack/react-query";
import { getCoachAvoidable } from "../api/client";
import CoachKpiRow from "./CoachKpiRow";
import AvoidableSpendChart from "./AvoidableSpendChart";
import AvoidableBreakdown from "./AvoidableBreakdown";
import LoadingBar from "../components/LoadingBar";

export default function CoachPage() {
  const avoidable = useQuery({
    queryKey: ["coach-avoidable"],
    queryFn: () => getCoachAvoidable(12),
  });

  return (
    <div className="coach-page">
      <div className="coach-page-header">
        <h1>Coach</h1>
        <p>Avoidable spend based on your usage patterns.</p>
      </div>

      {avoidable.isLoading && <div className="empty-state"><LoadingBar caption="Loading Coach data…" /></div>}
      {avoidable.isError && (
        <div className="empty-state">Failed to load Coach data.</div>
      )}

      {avoidable.data && (
        <CoachKpiRow summary={avoidable.data.summary} />
      )}

      {avoidable.data && (
        <section className="coach-tile">
          <h2>Spend over time</h2>
          <AvoidableSpendChart
            weeks={avoidable.data.weeks}
            markers={[]}
          />
        </section>
      )}

      {avoidable.data && (
        <section className="coach-tile">
          <h2>Avoidable spend breakdown</h2>
          <AvoidableBreakdown summary={avoidable.data.summary} />
        </section>
      )}
    </div>
  );
}
