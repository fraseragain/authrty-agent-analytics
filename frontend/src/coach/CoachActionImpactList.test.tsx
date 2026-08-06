import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CoachActionImpactList from "./CoachActionImpactList";
import type { CoachActionMarker } from "../api/types";

function marker(partial: Partial<CoachActionMarker>): CoachActionMarker {
  return {
    kind: "claude_md",
    key: "some-finding",
    label: "Add to CLAUDE.md",
    applied_at: "2026-06-01T10:00:00Z",
    week: "2026-06-01",
    impact: {
      window_days: 7,
      before_days: 0,
      after_days: 0,
      sufficient_data: false,
      before_total_usd: 0,
      after_total_usd: 0,
      before_avoidable_usd: 0,
      after_avoidable_usd: 0,
      before_avoidable_pct: 0,
      after_avoidable_pct: 0,
      confounded: false,
    },
    ...partial,
  };
}

describe("CoachActionImpactList", () => {
  it("shows an empty state with no markers", () => {
    render(<CoachActionImpactList markers={[]} />);
    expect(screen.getByText(/No actions applied yet/)).toBeInTheDocument();
  });

  it("shows an improvement delta when sufficient data exists", () => {
    render(
      <CoachActionImpactList
        markers={[marker({
          label: "Deep loop sessions",
          impact: {
            window_days: 7, before_days: 7, after_days: 7, sufficient_data: true,
            before_total_usd: 100, after_total_usd: 100,
            before_avoidable_usd: 20, after_avoidable_usd: 5,
            before_avoidable_pct: 20, after_avoidable_pct: 5,
            confounded: false,
          },
        })]}
      />,
    );

    expect(screen.getByText("Deep loop sessions")).toBeInTheDocument();
    const delta = screen.getByText("20.0% → 5.0% avoidable");
    expect(delta).toBeInTheDocument();
    expect(delta.className).toContain("positive");
    expect(screen.getByText(/avoidable spend/)).toBeInTheDocument();
  });

  it("shows a worsening delta with the negative class when avoidable % increased", () => {
    render(
      <CoachActionImpactList
        markers={[marker({
          impact: {
            window_days: 7, before_days: 7, after_days: 7, sufficient_data: true,
            before_total_usd: 100, after_total_usd: 100,
            before_avoidable_usd: 5, after_avoidable_usd: 20,
            before_avoidable_pct: 5, after_avoidable_pct: 20,
            confounded: false,
          },
        })]}
      />,
    );

    const delta = screen.getByText("5.0% → 20.0% avoidable");
    expect(delta.className).toContain("negative");
  });

  it("shows a pending message when there isn't enough data yet", () => {
    render(
      <CoachActionImpactList
        markers={[marker({
          impact: {
            window_days: 7, before_days: 7, after_days: 1, sufficient_data: false,
            before_total_usd: 100, after_total_usd: 10,
            before_avoidable_usd: 20, after_avoidable_usd: 1,
            before_avoidable_pct: 20, after_avoidable_pct: 10,
            confounded: false,
          },
        })]}
      />,
    );

    expect(screen.getByText(/1\/7 days of data since applying/)).toBeInTheDocument();
    expect(screen.queryByText(/avoidable$/)).not.toBeInTheDocument();
  });

  it("shows a confounded caveat when another action landed nearby", () => {
    render(
      <CoachActionImpactList
        markers={[marker({
          impact: {
            window_days: 7, before_days: 7, after_days: 7, sufficient_data: true,
            before_total_usd: 100, after_total_usd: 100,
            before_avoidable_usd: 20, after_avoidable_usd: 5,
            before_avoidable_pct: 20, after_avoidable_pct: 5,
            confounded: true,
          },
        })]}
      />,
    );

    expect(screen.getByText(/possibly confounded/)).toBeInTheDocument();
  });
});
