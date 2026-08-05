import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import PatternDiscovery from "./PatternDiscovery";
import type { CoachFinding, CoachPatternsResponse } from "../api/types";

function finding(partial: Partial<CoachFinding> = {}): CoachFinding {
  return {
    title: "Deep loop sessions",
    body: "2 sessions looped a lot.",
    recommendation: "Add a bash-verifier subagent.",
    metric: "2 sessions",
    severity: "warning",
    session_count: 2,
    occurrence_count: 0,
    skill_key: null,
    source: "heuristic",
    last_seen_at: null,
    ...partial,
  };
}

function response(findings: CoachFinding[], partial: Partial<CoachPatternsResponse> = {}): CoachPatternsResponse {
  return { findings, total_session_count: 10, weeks: 12, ...partial };
}

describe("PatternDiscovery", () => {
  it("shows an empty state when there are no findings", () => {
    render(<PatternDiscovery data={response([])} />);
    expect(screen.getByText(/No recurring patterns detected yet/)).toBeInTheDocument();
  });

  it("renders the subtitle with counts", () => {
    render(<PatternDiscovery data={response([finding()], { total_session_count: 8, weeks: 6 })} />);
    expect(screen.getByText("1 pattern detected across 8 sessions over the last 6 weeks.")).toBeInTheDocument();
  });

  it("pluralizes the subtitle for multiple findings", () => {
    render(<PatternDiscovery data={response([finding(), finding({ title: "Other" })])} />);
    expect(screen.getByText(/^2 patterns detected/)).toBeInTheDocument();
  });

  it("renders each finding's title, metric, body, and recommendation", () => {
    render(<PatternDiscovery data={response([finding()])} />);
    expect(screen.getByText("Deep loop sessions")).toBeInTheDocument();
    expect(screen.getByText("2 sessions")).toBeInTheDocument();
    expect(screen.getByText("2 sessions looped a lot.")).toBeInTheDocument();
    expect(screen.getByText("Add a bash-verifier subagent.")).toBeInTheDocument();
  });

  it("does not render a create-skill button (display-only this phase)", () => {
    render(<PatternDiscovery data={response([finding({ skill_key: "bash-verifier" })])} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows the last-seen date when the backend provides one", () => {
    const lastSeen = "2026-06-03T10:00:00Z";
    render(<PatternDiscovery data={response([finding({ last_seen_at: lastSeen })])} />);
    const timestamp = screen.getByText(
      new Date(lastSeen).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }),
    );
    expect(timestamp).toHaveAttribute("datetime", lastSeen);
  });

  it("omits the last-seen line when the backend doesn't provide one", () => {
    render(<PatternDiscovery data={response([finding({ last_seen_at: null })])} />);
    expect(screen.queryByText(/Last seen/)).not.toBeInTheDocument();
  });
});
