import { fireEvent, render, screen, within } from "@testing-library/react";
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

function cardFor(title: string): HTMLElement {
  return screen.getByText(title).closest(".pd-card") as HTMLElement;
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
    expect(screen.queryByRole("button", { name: /create/i })).not.toBeInTheDocument();
  });

  it("shows the last-seen date as a column on the card", () => {
    const lastSeen = "2026-06-03T10:00:00Z";
    render(<PatternDiscovery data={response([finding({ last_seen_at: lastSeen })])} />);
    const card = cardFor("Deep loop sessions");
    const timestamp = within(card).getByText(
      new Date(lastSeen).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }),
    );
    expect(timestamp).toHaveAttribute("datetime", lastSeen);
  });

  it("shows a dash in the last-seen column when the backend doesn't provide one", () => {
    render(<PatternDiscovery data={response([finding({ last_seen_at: null })])} />);
    const card = cardFor("Deep loop sessions");
    expect(within(card).getByText("—")).toBeInTheDocument();
  });

  it("sorts by last seen (most recent first) when the column header is clicked", () => {
    const findings = [
      finding({ title: "older", last_seen_at: "2026-06-01T00:00:00Z" }),
      finding({ title: "undated", last_seen_at: null }),
      finding({ title: "newer", last_seen_at: "2026-06-03T00:00:00Z" }),
    ];
    render(<PatternDiscovery data={response(findings)} />);

    fireEvent.click(screen.getByRole("button", { name: /Last seen/i }));

    const titles = screen.getAllByText(/^(older|undated|newer)$/).map((el) => el.textContent);
    expect(titles).toEqual(["newer", "older", "undated"]);
  });

  it("returns to the default order when the column header is clicked again", () => {
    const findings = [
      finding({ title: "older", last_seen_at: "2026-06-01T00:00:00Z" }),
      finding({ title: "newer", last_seen_at: "2026-06-03T00:00:00Z" }),
    ];
    render(<PatternDiscovery data={response(findings)} />);

    const sortButton = screen.getByRole("button", { name: /Last seen/i });
    fireEvent.click(sortButton);
    fireEvent.click(sortButton);

    const titles = screen.getAllByText(/^(older|newer)$/).map((el) => el.textContent);
    expect(titles).toEqual(["older", "newer"]);
  });
});
