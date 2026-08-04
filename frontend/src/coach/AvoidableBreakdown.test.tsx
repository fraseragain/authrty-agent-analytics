import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AvoidableBreakdown from "./AvoidableBreakdown";
import type { CoachSummary } from "../api/types";

function summary(partial: Partial<CoachSummary> = {}): CoachSummary {
  return {
    total_usd: 100,
    avoidable_usd: 25,
    avoidable_pct: 25,
    loop_waste_usd: 10,
    error_retry_usd: 5,
    alerted_usd: 5,
    model_overkill_usd: 5,
    vs_prev_period_pct: null,
    ...partial,
  };
}

describe("AvoidableBreakdown", () => {
  it("shows an empty state when there's no spend yet", () => {
    render(<AvoidableBreakdown summary={summary({ total_usd: 0 })} />);
    expect(screen.getByText("No spend data yet.")).toBeInTheDocument();
  });

  it("renders every segment label and the total", () => {
    render(<AvoidableBreakdown summary={summary()} />);
    expect(screen.getByText("Efficient spend")).toBeInTheDocument();
    expect(screen.getByText("Loop waste")).toBeInTheDocument();
    expect(screen.getByText("Error retries")).toBeInTheDocument();
    expect(screen.getByText("Alerted sessions")).toBeInTheDocument();
    expect(screen.getByText("Model overkill")).toBeInTheDocument();
    expect(screen.getByText("$100.00")).toBeInTheDocument();
  });

  it("dims zero-value segments in the legend", () => {
    render(<AvoidableBreakdown summary={summary({ loop_waste_usd: 0 })} />);
    const row = screen.getByText("Loop waste").closest(".avoidable-legend-row");
    expect(row).toHaveClass("avoidable-legend-row--zero");
  });

  it("shows the avoidable percentage in the donut centre", () => {
    render(<AvoidableBreakdown summary={summary({ avoidable_pct: 42 })} />);
    expect(screen.getByText("42%")).toBeInTheDocument();
  });
});
