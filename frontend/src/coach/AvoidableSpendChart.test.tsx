import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AvoidableSpendChart from "./AvoidableSpendChart";
import type { CoachWeek } from "../api/types";

function week(partial: Partial<CoachWeek>): CoachWeek {
  return {
    week: "2026-06-01",
    week_label: "1 Jun",
    total_usd: 0,
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

  it("renders a skill marker pill when a marker lands on a visible week", () => {
    const weeks = [week({ week: "2026-06-01", week_label: "1 Jun", total_usd: 10, efficient_usd: 10 })];
    render(
      <AvoidableSpendChart
        weeks={weeks}
        markers={[{ skill_key: "haiku-router", detected_at: "2026-06-01", week: "2026-06-01" }]}
      />,
    );

    expect(screen.getByText(/haiku.*router/)).toBeInTheDocument();
    expect(screen.getByText("2026-06-01")).toBeInTheDocument();
  });
});
