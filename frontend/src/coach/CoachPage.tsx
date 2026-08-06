import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getCoachAvoidable, getCoachPatterns } from "../api/client";
import CoachKpiRow from "./CoachKpiRow";
import AvoidableSpendChart from "./AvoidableSpendChart";
import AvoidableBreakdown from "./AvoidableBreakdown";
import PatternDiscovery from "./PatternDiscovery";
import CoachActionImpactList from "./CoachActionImpactList";
import LoadingBar from "../components/LoadingBar";

const PERIOD_OPTIONS = [
  { weeks: 4, label: "4 weeks" },
  { weeks: 12, label: "12 weeks" },
  { weeks: 26, label: "26 weeks" },
  { weeks: 52, label: "52 weeks" },
];

export default function CoachPage() {
  const [weeks, setWeeks] = useState(12);

  const avoidable = useQuery({
    queryKey: ["coach-avoidable", weeks],
    queryFn: () => getCoachAvoidable(weeks),
  });
  const patterns = useQuery({
    queryKey: ["coach-patterns", weeks],
    queryFn: () => getCoachPatterns(weeks),
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
        <div className="coach-two-col">
          <section className="coach-tile">
            <div className="coach-tile-header">
              <h2>Spend over time</h2>
              <div className="coach-chart-controls">
                <select
                  aria-label="Time period"
                  value={weeks}
                  onChange={(e) => setWeeks(Number(e.target.value))}
                >
                  {PERIOD_OPTIONS.map((p) => (
                    <option key={p.weeks} value={p.weeks}>{p.label}</option>
                  ))}
                </select>
              </div>
            </div>
            <AvoidableSpendChart
              weeks={avoidable.data.weeks}
              markers={avoidable.data.markers}
            />
          </section>

          <section className="coach-tile">
            <h2>Avoidable spend breakdown</h2>
            <AvoidableBreakdown summary={avoidable.data.summary} />
          </section>
        </div>
      )}

      {avoidable.data && (
        <section className="coach-tile">
          <h2>Action impact</h2>
          <CoachActionImpactList markers={avoidable.data.markers} />
        </section>
      )}

      <section className="coach-tile">
        <h2>Pattern discoveries</h2>
        {patterns.isLoading && <div className="empty-state"><LoadingBar caption="Loading pattern discoveries…" /></div>}
        {patterns.isError && <div className="empty-state">Failed to load pattern discoveries.</div>}
        {patterns.data && <PatternDiscovery data={patterns.data} />}
      </section>
    </div>
  );
}
