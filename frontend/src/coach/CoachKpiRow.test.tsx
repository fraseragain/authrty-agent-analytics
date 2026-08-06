import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CoachKpiRow from "./CoachKpiRow";
import type { CoachSummary } from "../api/types";

function summary(partial: Partial<CoachSummary> = {}): CoachSummary {
  return {
    total_usd: 100,
    total_tokens: 2_500_000,
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

describe("CoachKpiRow", () => {
  it("renders total tokens, total spend, avoidable spend, and avoidable percentage", () => {
    render(<CoachKpiRow summary={summary()} />);
    expect(screen.getByText("Total tokens")).toBeInTheDocument();
    expect(screen.getByText("2.5M")).toBeInTheDocument();
    expect(screen.getByText("$100.00")).toBeInTheDocument();
    expect(screen.getByText("$25.00")).toBeInTheDocument();
    expect(screen.getByText("25.0%")).toBeInTheDocument();
  });

  it("shows an em dash when there is no previous period to compare", () => {
    render(<CoachKpiRow summary={summary({ vs_prev_period_pct: null })} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("marks a waste-rate increase as negative (more waste is worse)", () => {
    render(<CoachKpiRow summary={summary({ vs_prev_period_pct: 12.3 })} />);
    expect(screen.getByText("Waste % vs last period")).toBeInTheDocument();
    const delta = screen.getByText(/12\.3%/);
    expect(delta).toHaveClass("negative");
    expect(delta.textContent).toContain("▲");
  });

  it("marks a waste-rate decrease as positive (less waste is better)", () => {
    render(<CoachKpiRow summary={summary({ vs_prev_period_pct: -8 })} />);
    const delta = screen.getByText(/8\.0%/);
    expect(delta).toHaveClass("positive");
    expect(delta.textContent).toContain("▼");
  });
});
