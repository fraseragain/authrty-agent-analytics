import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AvoidableSpendChart from "./AvoidableSpendChart";
import type { CoachActionMarker, CoachWeek } from "../api/types";

function week(partial: Partial<CoachWeek>): CoachWeek {
  return {
    week: "2026-06-01",
    week_label: "1 Jun",
    total_usd: 0,
    total_tokens: 0,
    efficient_usd: 0,
    loop_waste_usd: 0,
    error_retry_usd: 0,
    alerted_usd: 0,
    model_overkill_usd: 0,
    avoidable_usd: 0,
    session_count: 0,
    is_partial: false,
    partial_days: 7,
    ...partial,
  };
}

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

describe("AvoidableSpendChart", () => {
  it("shows an empty state with no data", () => {
    render(<AvoidableSpendChart weeks={[]} markers={[]} />);
    expect(screen.getByText("No data yet.")).toBeInTheDocument();
  });

  it("renders the chart and legend for populated weeks", () => {
    const weeks = [
      week({ week: "2026-06-01", week_label: "1 Jun", total_usd: 10, efficient_usd: 10 }),
      week({ week: "2026-06-02", week_label: "2 Jun", total_usd: 20, efficient_usd: 15, loop_waste_usd: 5 }),
    ];
    render(<AvoidableSpendChart weeks={weeks} markers={[]} />);

    expect(screen.getByRole("img", { name: "Avoidable spend over time" })).toBeInTheDocument();
    expect(screen.getByText("Efficient")).toBeInTheDocument();
    expect(screen.getByText("Loop waste")).toBeInTheDocument();
    expect(screen.getByText("1 Jun")).toBeInTheDocument();
    expect(screen.getByText("2 Jun")).toBeInTheDocument();
  });

  it("renders an action marker pill when a marker lands on a visible week", () => {
    const weeks = [week({ week: "2026-06-01", week_label: "1 Jun", total_usd: 10, efficient_usd: 10 })];
    render(
      <AvoidableSpendChart
        weeks={weeks}
        markers={[marker({
          kind: "create_agent", key: "haiku-router", label: "Create haiku-router agent",
          applied_at: "2026-06-01T10:00:00Z", week: "2026-06-01",
        })]}
      />,
    );

    expect(screen.getByText("Create haiku-router agent")).toBeInTheDocument();
    expect(screen.getByText("1 Jun")).toBeInTheDocument();
  });

  it("does not render a marker pill when the marker's week isn't in the visible range", () => {
    const weeks = [week({ week: "2026-06-01", week_label: "1 Jun", total_usd: 10, efficient_usd: 10 })];
    render(
      <AvoidableSpendChart
        weeks={weeks}
        markers={[marker({
          kind: "claude_md", key: "some-finding", label: "Add to CLAUDE.md",
          applied_at: "2026-05-01T10:00:00Z", week: "2026-05-01",
        })]}
      />,
    );

    expect(screen.queryByText("Add to CLAUDE.md")).not.toBeInTheDocument();
  });

  it("enriches the marker pill tooltip with before/after impact when sufficient data exists", () => {
    const weeks = [week({ week: "2026-06-01", week_label: "1 Jun", total_usd: 10, efficient_usd: 10 })];
    render(
      <AvoidableSpendChart
        weeks={weeks}
        markers={[marker({
          kind: "claude_md", key: "some-finding", label: "Add to CLAUDE.md",
          applied_at: "2026-06-01T10:00:00Z", week: "2026-06-01",
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

    expect(screen.getByTitle("Add to CLAUDE.md — avoidable 20.0% → 5.0%")).toBeInTheDocument();
  });
});
