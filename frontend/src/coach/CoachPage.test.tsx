import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CoachPage from "./CoachPage";
import * as client from "../api/client";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CoachPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(client, "getCoachAvoidable").mockResolvedValue({
    weeks: [
      {
        week: "2026-06-01", week_label: "1 Jun", total_usd: 10, total_tokens: 1000, efficient_usd: 10,
        loop_waste_usd: 0, error_retry_usd: 0, alerted_usd: 0, model_overkill_usd: 0,
        avoidable_usd: 0, session_count: 1, is_partial: false, partial_days: 7,
      },
    ],
    summary: {
      total_usd: 10, total_tokens: 1000, avoidable_usd: 0, avoidable_pct: 0, loop_waste_usd: 0,
      error_retry_usd: 0, alerted_usd: 0, model_overkill_usd: 0, vs_prev_period_pct: null,
    },
    markers: [],
  });
  vi.spyOn(client, "getCoachPatterns").mockResolvedValue({
    findings: [
      {
        title: "Deep loop sessions", body: "2 sessions looped.", recommendation: "Add a verifier.",
        metric: "2 sessions", severity: "warning", session_count: 2, occurrence_count: 0,
        skill_key: null, source: "heuristic", last_seen_at: null, actions: [],
      },
    ],
    total_session_count: 5,
    weeks: 12,
  });
});
afterEach(() => vi.restoreAllMocks());

describe("CoachPage", () => {
  it("renders the header, KPI row, chart, and donut once data loads", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Coach" })).toBeInTheDocument();
    const totalLabel = await screen.findByText("Total spend");
    const kpiRow = totalLabel.closest(".coach-kpi-row") as HTMLElement;
    expect(within(kpiRow).getByText("$10.00")).toBeInTheDocument();
    expect(screen.getByText("Spend over time")).toBeInTheDocument();
    expect(screen.getByText("Avoidable spend breakdown")).toBeInTheDocument();
  });

  it("shows an error state when the request fails", async () => {
    vi.mocked(client.getCoachAvoidable).mockRejectedValue(new Error("boom"));

    renderPage();

    expect(await screen.findByText("Failed to load Coach data.")).toBeInTheDocument();
  });

  it("renders pattern discoveries once loaded", async () => {
    renderPage();

    expect(await screen.findByText("Pattern discoveries")).toBeInTheDocument();
    expect(await screen.findByText("Deep loop sessions")).toBeInTheDocument();
  });

  it("shows an error state when pattern discovery fails", async () => {
    vi.mocked(client.getCoachPatterns).mockRejectedValue(new Error("boom"));

    renderPage();

    expect(await screen.findByText("Failed to load pattern discoveries.")).toBeInTheDocument();
  });

  it("shows the action impact tile with an empty state when no actions have been applied", async () => {
    renderPage();

    expect(await screen.findByText("Action impact")).toBeInTheDocument();
    expect(await screen.findByText(/No actions applied yet/)).toBeInTheDocument();
  });

  it("renders action impact content once a marker is present", async () => {
    vi.mocked(client.getCoachAvoidable).mockResolvedValue({
      weeks: [
        {
          week: "2026-06-01", week_label: "1 Jun", total_usd: 10, efficient_usd: 10,
          loop_waste_usd: 0, error_retry_usd: 0, alerted_usd: 0, model_overkill_usd: 0,
          avoidable_usd: 0, session_count: 1, is_partial: false, partial_days: 7,
        },
      ],
      summary: {
        total_usd: 10, avoidable_usd: 0, avoidable_pct: 0, loop_waste_usd: 0,
        error_retry_usd: 0, alerted_usd: 0, model_overkill_usd: 0, vs_prev_period_pct: null,
      },
      markers: [{
        kind: "claude_md", key: "deep-loop-sessions", label: "Deep loop sessions",
        applied_at: "2026-06-01T10:00:00Z", week: "2026-06-01",
        impact: {
          window_days: 7, before_days: 7, after_days: 7, sufficient_data: true,
          before_total_usd: 100, after_total_usd: 100,
          before_avoidable_usd: 20, after_avoidable_usd: 5,
          before_avoidable_pct: 20, after_avoidable_pct: 5,
          confounded: false,
        },
      }],
    });

    renderPage();

    expect(await screen.findByText("20.0% → 5.0% avoidable")).toBeInTheDocument();
  });

  it("re-fetches with the new period when the time-period selector changes", async () => {
    renderPage();
    const select = await screen.findByLabelText("Time period");

    fireEvent.change(select, { target: { value: "4" } });

    await waitFor(() => {
      expect(client.getCoachAvoidable).toHaveBeenLastCalledWith(4);
      expect(client.getCoachPatterns).toHaveBeenLastCalledWith(4);
    });
  });
});
