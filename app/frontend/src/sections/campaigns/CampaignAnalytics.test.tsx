import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { campaignAnalytics: vi.fn() } };
});
import { api } from "../../lib/api";
import { BarList } from "../../components/charts/BarList";
import { CampaignAnalyticsTab } from "./CampaignAnalytics";
import { CoverageBars } from "./CoverageBars";
import { StageTimeline } from "./StageTimeline";
import { analytics, campaign } from "./fixtures";

const show = (data = analytics(), row = campaign()) => { vi.mocked(api.campaignAnalytics).mockResolvedValue(data); return render(<CampaignAnalyticsTab campaign={row} />); };
beforeEach(() => vi.mocked(api.campaignAnalytics).mockReset());

describe("BarList", () => {
  it("draws the biggest first, writes the numbers and gives the same as a table", () => {
    render(<BarList title="Language" items={[{ label: "go", value: 3 }, { label: "python", value: 25 }, { label: "none", value: 0 }]} unit="pairs" />);
    const rows = screen.getAllByRole("listitem");
    expect(rows.map((r) => r.getAttribute("aria-label"))).toEqual(["python: 25 pairs, 89 percent", "go: 3 pairs, 11 percent"]);
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "Language" });
    expect(within(table).getAllByRole("row").map((r) => r.textContent)).toEqual(["NameCount (pairs)Share", "python2589%", "go311%"]);
  });

  it("folds the small groups past the limit into one line and says when there is nothing", () => {
    render(<BarList title="CWE" items={Array.from({ length: 5 }, (_, i) => ({ label: `c${i}`, value: 10 - i }))} limit={3} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getByText("13 more in 2 smaller groups")).toBeInTheDocument();
    render(<BarList title="Nothing" items={[]} empty="Every commit gave code." />);
    expect(screen.getByText("Every commit gave code.")).toBeInTheDocument();
  });
});

describe("StageTimeline", () => {
  it("shows each step as a bar with its state in words, and a time left only for the step that reports one", () => {
    const a = analytics();
    render(<StageTimeline steps={a.timeline.steps} now={a.generated_at} />);
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveAttribute("aria-label", "npm: Read the advisories: Done, 10 min 0 s");
    expect(rows[1]).toHaveAttribute("aria-label", expect.stringContaining("Running, 50 min 0 s, about 16 min 40 s left"));
    expect(rows[2]).toHaveAttribute("aria-label", "Whole campaign: Build a draft dataset: Waiting");
    expect(rows[2]).toHaveTextContent("Waiting");
    expect(screen.getByText("2026-10-10 10:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("2026-10-10 11:00 UTC")).toBeInTheDocument();
  });

  it("writes the same as a table, and says when no step has started", () => {
    const a = analytics();
    const { unmount } = render(<StageTimeline steps={a.timeline.steps} now={a.generated_at} />);
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "The steps of the campaign" });
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(within(table).getByText("16 min 40 s")).toBeInTheDocument();
    unmount();
    render(<StageTimeline steps={a.timeline.steps.slice(2)} now={a.generated_at} />);
    expect(screen.getByText("No step has started yet.")).toBeInTheDocument();
  });

  it("marks a failed step by its pattern and its word, not by colour alone", () => {
    const a = analytics();
    render(<StageTimeline steps={[{ ...a.timeline.steps[0], status: "error", error: "x" }]} now={a.generated_at} />);
    expect(screen.getByRole("listitem")).toHaveAttribute("data-status", "error");
    expect(screen.getByRole("listitem")).toHaveTextContent("Failed");
  });
});

describe("CoverageBars", () => {
  it("shows what had a draft before and what the campaign added, in words", () => {
    render(<CoverageBars before={{ weak: { total: 10, enriched: 2 }, partial: { total: 4, enriched: 0 }, strong: { total: 0, enriched: 0 } }}
      after={{ weak: { total: 10, enriched: 8 }, partial: { total: 4, enriched: 1 }, strong: { total: 0, enriched: 0 } }} />);
    expect(screen.getByLabelText("Weak context: 8 of 10 pairs have a draft, 6 added by this campaign")).toHaveTextContent("8 of 10 (+6)");
    expect(screen.getByLabelText(/Partial context: 1 of 4/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    expect(within(screen.getByRole("table")).getByText("Weak context").closest("tr")).toHaveTextContent("Weak context10286");
  });
});

describe("the analytics tab", () => {
  it("shows the timeline, the yield, the speed, what it produced and what went wrong", async () => {
    show();
    expect(await screen.findByText("Progress and stage timeline")).toBeInTheDocument();
    expect(screen.getByText("What the campaign added")).toBeInTheDocument();
    const byEco = screen.getByRole("table", { name: "What the campaign added to each ecosystem" });
    expect(within(byEco).getByText("npm").closest("tr")).toHaveTextContent("+100 from 10");
    expect(screen.getByText(/made about 120 commits and 80 pairs an hour/)).toBeInTheDocument();
    expect(screen.getByText("Commits and pairs over time")).toBeInTheDocument();
    expect(screen.getByText("Memory of the process")).toBeInTheDocument();
    expect(screen.getByText(/40 usable pairs from 20 commits/)).toBeInTheDocument();
    expect(screen.getByLabelText("python: 25 pairs, 63 percent")).toBeInTheDocument();
    expect(screen.getByLabelText("critical: 8 pairs, 20 percent")).toBeInTheDocument();
    expect(screen.getByLabelText("rate limit: 2 steps, 67 percent")).toBeInTheDocument();
    expect(screen.getByLabelText("no code: no_source_files: 5 commits, 100 percent")).toBeInTheDocument();
    expect(screen.getByText(/interrupted 1 time\(s\)/)).toBeInTheDocument();
    expect(screen.getByText("Waits")).toBeInTheDocument();
    expect(screen.queryByText("Model-written context")).toBeNull();
    expect(api.campaignAnalytics).toHaveBeenCalledWith("cmp-20261010-aaaa");
  });

  it("shows the model-written context when the campaign writes it", async () => {
    show(analytics({ context: { model: "qwen", prompt_version: "enrich-v2", candidates: 30, written: 24, failed: 6, seconds: 120, seconds_per_pair: 5,
      coverage_before: { weak: { total: 30, enriched: 0 }, partial: { total: 0, enriched: 0 }, strong: { total: 0, enriched: 0 } }, coverage_after: { weak: { total: 30, enriched: 24 }, partial: { total: 0, enriched: 0 }, strong: { total: 0, enriched: 0 } }, steps: [] } }));
    expect(await screen.findByText("Model-written context")).toBeInTheDocument();
    expect(screen.getByText(/With qwen \(prompt enrich-v2\): 24 drafts written, 6 failed, of 30 offered; about 5 seconds a pair/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Weak context: 24 of 30 pairs have a draft, 24 added/)).toBeInTheDocument();
  });

  it("says nothing has gone wrong when it has not, and explains a campaign of the earlier kind", async () => {
    show(analytics({ errors: { by_cause: [], latest: [], commits: [], forced_kills: 0, interruptions: 0 } }));
    expect(await screen.findByText("Nothing has gone wrong.")).toBeInTheDocument();
  });

  it("shows only the timeline and the state of the ecosystems for a campaign of the earlier kind, and says why", async () => {
    const a = analytics({ legacy: true, note: "No history was recorded for this campaign: it was made by the earlier kind of campaign, which kept only the state of its steps.", throughput: null, mix: null, context: null,
      funnel: { ...analytics().funnel, has_baseline: false } });
    show(a, campaign({ legacy: true, plan: null, state: "done" }));
    expect(await screen.findByRole("note")).toHaveTextContent("No history was recorded");
    expect(screen.queryByText("Speed")).toBeNull();
    expect(screen.queryByText("What it produced")).toBeNull();
    expect(screen.getByText(/No starting point was recorded/)).toBeInTheDocument();
  });

  it("says why it could not be read, with a way to try again", async () => {
    vi.mocked(api.campaignAnalytics).mockRejectedValueOnce(new Error("down")).mockResolvedValue(analytics());
    render(<CampaignAnalyticsTab campaign={campaign()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the analytics");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Progress and stage timeline")).toBeInTheDocument();
  });
});
