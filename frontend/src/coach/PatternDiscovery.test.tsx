import type React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import PatternDiscovery from "./PatternDiscovery";
import type { CoachFinding, CoachPatternsResponse } from "../api/types";
import * as client from "../api/client";

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
    actions: [],
    ...partial,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

function response(findings: CoachFinding[], partial: Partial<CoachPatternsResponse> = {}): CoachPatternsResponse {
  return { findings, total_session_count: 10, weeks: 12, ...partial };
}

function cardFor(title: string): HTMLElement {
  return screen.getByText(title).closest(".pd-card") as HTMLElement;
}

afterEach(() => vi.restoreAllMocks());

describe("PatternDiscovery", () => {
  it("shows an empty state when there are no findings", () => {
    renderWithClient(<PatternDiscovery data={response([])} />);
    expect(screen.getByText(/No recurring patterns detected yet/)).toBeInTheDocument();
  });

  it("renders the subtitle with counts", () => {
    renderWithClient(<PatternDiscovery data={response([finding()], { total_session_count: 8, weeks: 6 })} />);
    expect(screen.getByText("1 pattern detected across 8 sessions over the last 6 weeks.")).toBeInTheDocument();
  });

  it("pluralizes the subtitle for multiple findings", () => {
    renderWithClient(<PatternDiscovery data={response([finding(), finding({ title: "Other" })])} />);
    expect(screen.getByText(/^2 patterns detected/)).toBeInTheDocument();
  });

  it("renders each finding's title, metric, body, and recommendation", () => {
    renderWithClient(<PatternDiscovery data={response([finding()])} />);
    expect(screen.getByText("Deep loop sessions")).toBeInTheDocument();
    expect(screen.getByText("2 sessions")).toBeInTheDocument();
    expect(screen.getByText("2 sessions looped a lot.")).toBeInTheDocument();
    expect(screen.getByText("Add a bash-verifier subagent.")).toBeInTheDocument();
  });

  it("renders no action buttons when a finding has no actions", () => {
    renderWithClient(<PatternDiscovery data={response([finding({ actions: [] })])} />);
    const card = cardFor("Deep loop sessions");
    expect(within(card).queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders one button per action, using the backend-supplied label", () => {
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            actions: [
              { kind: "claude_md", key: "deep-loop-sessions", label: "Add to CLAUDE.md", applied: false },
              { kind: "create_agent", key: "bash-verifier", label: "Create bash-verifier agent", applied: false },
            ],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    expect(within(card).getByRole("button", { name: "Add to CLAUDE.md" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Create bash-verifier agent" })).toBeInTheDocument();
  });

  it("shows an already-applied action as disabled with an Applied checkmark", () => {
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            actions: [{ kind: "permission_allow", key: "WebSearch", label: "Allow WebSearch without prompting", applied: true }],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    const button = within(card).getByRole("button", { name: "Applied ✓" });
    expect(button).toBeDisabled();
  });

  it("clicking an action button calls the endpoint and flips to Applied on success", async () => {
    const applySpy = vi.spyOn(client, "applyCoachAction").mockResolvedValue({ applied: true, detail: "ok", host_path: null });
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            actions: [{ kind: "create_agent", key: "bash-verifier", label: "Create bash-verifier agent", applied: false }],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    fireEvent.click(within(card).getByRole("button", { name: "Create bash-verifier agent" }));

    await waitFor(() => expect(applySpy).toHaveBeenCalledWith("create_agent", "bash-verifier", undefined));
  });

  it("invalidates the coach-avoidable query on success, so the spend chart's markers refresh", async () => {
    vi.spyOn(client, "applyCoachAction").mockResolvedValue({ applied: true, detail: "ok", host_path: null });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(["coach-avoidable"], { weeks: [], summary: {}, markers: [] });
    render(
      <QueryClientProvider client={queryClient}>
        <PatternDiscovery
          data={response([
            finding({
              actions: [{ kind: "create_agent", key: "bash-verifier", label: "Create bash-verifier agent", applied: false }],
            }),
          ])}
        />
      </QueryClientProvider>,
    );
    const card = cardFor("Deep loop sessions");
    fireEvent.click(within(card).getByRole("button", { name: "Create bash-verifier agent" }));

    await waitFor(() => expect(queryClient.getQueryState(["coach-avoidable"])?.isInvalidated).toBe(true));
  });

  it("shows a toast with the action label and backend detail on success", async () => {
    vi.spyOn(client, "applyCoachAction").mockResolvedValue({
      applied: true,
      detail: "Created ~/.claude/agents/bash-verifier.md",
      host_path: "/Users/fraser/.claude/agents/bash-verifier.md",
    });
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            actions: [{ kind: "create_agent", key: "bash-verifier", label: "Create bash-verifier agent", applied: false }],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    fireEvent.click(within(card).getByRole("button", { name: "Create bash-verifier agent" }));

    expect(await screen.findByText("Create bash-verifier agent")).toBeInTheDocument();
    expect(await screen.findByText("Created ~/.claude/agents/bash-verifier.md")).toBeInTheDocument();
  });

  it("dismisses a toast when its close button is clicked", async () => {
    vi.spyOn(client, "applyCoachAction").mockResolvedValue({ applied: true, detail: "ok", host_path: null });
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            actions: [{ kind: "create_agent", key: "bash-verifier", label: "Create bash-verifier agent", applied: false }],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    fireEvent.click(within(card).getByRole("button", { name: "Create bash-verifier agent" }));

    const dismissBtn = await screen.findByRole("button", { name: "Dismiss" });
    fireEvent.click(dismissBtn);

    await waitFor(() => expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument());
  });

  it("passes the finding's recommendation text when applying a claude_md action", async () => {
    const applySpy = vi.spyOn(client, "applyCoachAction").mockResolvedValue({ applied: true, detail: "ok", host_path: null });
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            recommendation: "Add a bash-verifier subagent.",
            actions: [{ kind: "claude_md", key: "deep-loop-sessions", label: "Add to CLAUDE.md", applied: false }],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    fireEvent.click(within(card).getByRole("button", { name: "Add to CLAUDE.md" }));

    await waitFor(() =>
      expect(applySpy).toHaveBeenCalledWith("claude_md", "deep-loop-sessions", "Add a bash-verifier subagent."),
    );
  });

  it("shows an Open CLAUDE.md link in the toast when a claude_md action succeeds", async () => {
    vi.spyOn(client, "applyCoachAction").mockResolvedValue({
      applied: true,
      detail: "Added to CLAUDE.md",
      host_path: "/Users/fraser/.claude/CLAUDE.md",
    });
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            actions: [{ kind: "claude_md", key: "deep-loop-sessions", label: "Add to CLAUDE.md", applied: false }],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    fireEvent.click(within(card).getByRole("button", { name: "Add to CLAUDE.md" }));

    const link = await screen.findByRole("link", { name: "Open CLAUDE.md →" });
    expect(link).toHaveAttribute("href", "file:///Users/fraser/.claude/CLAUDE.md");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("does not show an Open CLAUDE.md link for non-claude_md actions", async () => {
    vi.spyOn(client, "applyCoachAction").mockResolvedValue({
      applied: true,
      detail: "Created bash-verifier.md",
      host_path: "/Users/fraser/.claude/agents/bash-verifier.md",
    });
    renderWithClient(
      <PatternDiscovery
        data={response([
          finding({
            actions: [{ kind: "create_agent", key: "bash-verifier", label: "Create bash-verifier agent", applied: false }],
          }),
        ])}
      />,
    );
    const card = cardFor("Deep loop sessions");
    fireEvent.click(within(card).getByRole("button", { name: "Create bash-verifier agent" }));

    await screen.findByText("Created bash-verifier.md");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the last-seen date as a column on the card", () => {
    const lastSeen = "2026-06-03T10:00:00Z";
    renderWithClient(<PatternDiscovery data={response([finding({ last_seen_at: lastSeen })])} />);
    const card = cardFor("Deep loop sessions");
    const timestamp = within(card).getByText(
      new Date(lastSeen).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }),
    );
    expect(timestamp).toHaveAttribute("datetime", lastSeen);
  });

  it("shows a dash in the last-seen column when the backend doesn't provide one", () => {
    renderWithClient(<PatternDiscovery data={response([finding({ last_seen_at: null })])} />);
    const card = cardFor("Deep loop sessions");
    expect(within(card).getByText("—")).toBeInTheDocument();
  });

  it("sorts by last seen (most recent first) when the column header is clicked", () => {
    const findings = [
      finding({ title: "older", last_seen_at: "2026-06-01T00:00:00Z" }),
      finding({ title: "undated", last_seen_at: null }),
      finding({ title: "newer", last_seen_at: "2026-06-03T00:00:00Z" }),
    ];
    renderWithClient(<PatternDiscovery data={response(findings)} />);

    fireEvent.click(screen.getByRole("button", { name: /Last seen/i }));

    const titles = screen.getAllByText(/^(older|undated|newer)$/).map((el) => el.textContent);
    expect(titles).toEqual(["newer", "older", "undated"]);
  });

  it("returns to the default order when the column header is clicked again", () => {
    const findings = [
      finding({ title: "older", last_seen_at: "2026-06-01T00:00:00Z" }),
      finding({ title: "newer", last_seen_at: "2026-06-03T00:00:00Z" }),
    ];
    renderWithClient(<PatternDiscovery data={response(findings)} />);

    const sortButton = screen.getByRole("button", { name: /Last seen/i });
    fireEvent.click(sortButton);
    fireEvent.click(sortButton);

    const titles = screen.getAllByText(/^(older|newer)$/).map((el) => el.textContent);
    expect(titles).toEqual(["older", "newer"]);
  });
});
