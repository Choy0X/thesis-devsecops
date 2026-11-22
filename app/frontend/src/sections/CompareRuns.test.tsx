import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { compareRuns: vi.fn(), compareConfig: vi.fn(), compareCwe: vi.fn(), runCurves: vi.fn(), listJobs: vi.fn() } };
});
import report from "./evalReport.fixture.json";
import { api, type CompareStats, type EvalShared, type RunCompare } from "../lib/api";
import { CompareRuns, VERDICT_WORDS, discoveryOf, fieldWords } from "./CompareRuns";

const base = report as unknown as EvalShared;
const A: EvalShared = { ...base, tag: "r1-B0-base" };
const B: EvalShared = { ...base, tag: "r1-B2-tuned", run_info: { ...base.run_info!, baseline: "B2", arm: "base" } };
const stats = (over: Partial<CompareStats> = {}): CompareStats => ({
  items: 120, both_right: 70, both_wrong: 20, only_a: 8, only_b: 22, disagreements: 30, accuracy_a: 0.65, accuracy_b: 0.7667, difference: -0.1167, verdict: "b_better",
  reason: "B is right more often on the items where the runs disagree (p = 0.0161).", p_value: 0.0161, interval: [-0.21, -0.03],
  rule: { alpha: 0.05, min_items: 30, min_disagreements: 10, test: "exact McNemar, two-sided" }, ...over });
const result = (over: Partial<RunCompare> = {}): RunCompare => ({
  a: A.tag, b: B.tag, guard: { comparable: true, blockers: [], unconfirmed: [] }, warnings: [], items: { a: 120, b: 120, shared: 120 }, stats: stats(), stats_note: null,
  disagreements: [{ view_id: "v1", truth: "supported", a: "no_finding", b: "supported" }], disagreements_total: 30, ...over });

beforeEach(() => {
  vi.mocked(api.compareRuns).mockReset();
  vi.mocked(api.compareRuns).mockResolvedValue(result());
  vi.mocked(api.compareConfig).mockReset();
  vi.mocked(api.compareConfig).mockRejectedValue(new Error("no configuration"));
  vi.mocked(api.compareCwe).mockReset();
  vi.mocked(api.compareCwe).mockRejectedValue(new Error("no classes"));
  vi.mocked(api.runCurves).mockReset();
  vi.mocked(api.runCurves).mockResolvedValue({ sources: [] });
  vi.mocked(api.listJobs).mockReset();
  vi.mocked(api.listJobs).mockResolvedValue([]);
});

describe("the words", () => {
  it("names a checked field in plain words and a verdict in a sentence", () => {
    expect(fieldWords("input_policy_hash")).toBe("what the model was shown");
    expect(fieldWords("retrieval.kb_version")).toBe("the knowledge base");
    expect(fieldWords("some_new.field")).toBe("some new field");
    expect(VERDICT_WORDS.not_enough_data).toBe("Not enough data to say");
  });

  it("reads the discovery numbers of the arm a run is about", () => {
    expect(discoveryOf(A)?.overall.n).toBe(25);
    expect(discoveryOf({ ...A, arms: { base: { generated_decision: { status: "blocked", reason: "none" } } } })).toBeNull();
    expect(discoveryOf({ ...A, arms: {} })).toBeNull();
  });
});

describe("CompareRuns", () => {
  it('shows grouped independent-security uncertainty without an item-level better verdict', async () => {
    vi.mocked(api.compareRuns).mockResolvedValue(result({stats: null,
      stats_note: 'Independent security benchmark: grouped intervals only.',
      grouped_paired: {independent_groups: 1, views: 50, direction: 'tuned_minus_base', scope: 'Descriptive grouped uncertainty.',
        recall: {estimate: .2, interval_95: null, state: 'insufficient_independent_groups'}}} as any));
    render(<CompareRuns runs={[A, B]} />);
    const table = await screen.findByRole('table', {name: 'Grouped paired metrics'});
    expect(within(table).getByText('Automatic detection recall')).toBeVisible();
    expect(within(table).getByText('insufficient independent groups')).toBeVisible();
    expect(screen.getByText(/1 independent repository\/issue group/)).toBeVisible();
    expect(screen.queryByText('Run B is better.')).not.toBeInTheDocument();
    expect(screen.queryByText(/Rule: at least/)).not.toBeInTheDocument();
  });
  it("compares the last two runs, says which is better on the items both answered, and shows the rule that was applied", async () => {
    render(<CompareRuns runs={[A, B]} />);
    expect(await screen.findByText("Run B is better.")).toBeInTheDocument();
    expect(api.compareRuns).toHaveBeenCalledWith("r1-B0-base", "r1-B2-tuned");
    expect(screen.getByText("120 items answered by both")).toBeInTheDocument();
    expect(screen.getByText("only A right 8")).toBeInTheDocument();
    expect(screen.getByText("only B right 22")).toBeInTheDocument();
    expect(screen.getByText(/A minus B -11.7% \(95% interval -21.0% to -3.0%\)/)).toBeInTheDocument();
    expect(screen.getByText(/Rule: at least 30 items and 10 where the runs disagree; exact McNemar, two-sided, significance level 0.05/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("puts the two runs side by side with the vulnerable found, the false alarms and the Critical misses", async () => {
    render(<CompareRuns runs={[A, B]} />);
    await screen.findByText("Run B is better.");
    const found = screen.getByText("Vulnerable found", { selector: "td" }).closest("tr") as HTMLElement;
    expect(within(found).getAllByText(/73.3%/)).toHaveLength(2);
    expect(screen.getByText("Critical not detected", { selector: "td" }).closest("tr")).toHaveTextContent("0 of 4");
    expect(screen.getByText(/validation split \(selection role\)/)).toBeInTheDocument();
  });

  it("names what makes two runs not comparable and gives no verdict, though the numbers stay visible", async () => {
    vi.mocked(api.compareRuns).mockResolvedValue(result({
      guard: { comparable: false, unconfirmed: [], blockers: [{ field: "dataset_version", reason: "The runs used different dataset versions.", a: "v1", b: "a".repeat(64) }] }, stats: null }));
    render(<CompareRuns runs={[A, B]} />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("These runs are not comparable, so no verdict is given.");
    expect(alert).toHaveTextContent("The runs used different dataset versions.");
    expect(alert).toHaveTextContent("A v1, B aaaaaaaaaaaa...");
    expect(screen.queryByText(/is better\./)).toBeNull();
    expect(screen.queryByText(/items answered by both/)).toBeNull();
    expect(screen.getByText("Vulnerable found", { selector: "td" })).toBeInTheDocument();
  });

  it("says not enough data, never no difference, and lists what neither run recorded", async () => {
    vi.mocked(api.compareRuns).mockResolvedValue(result({
      guard: { comparable: true, blockers: [], unconfirmed: [{ field: "prompt_version", reason: "x" }, { field: "retrieval.embedder", reason: "x" }] },
      stats: stats({ verdict: "not_enough_data", reason: "Not enough data to say: only 12 items (the rule needs at least 30 items and 10 where the runs disagree).", p_value: null, interval: null, items: 12 }) }));
    render(<CompareRuns runs={[A, B]} />);
    expect(await screen.findByText("Not enough data to say.")).toBeInTheDocument();
    expect(screen.queryByText(/No difference shown/)).toBeNull();
    expect(screen.getByText(/neither run recorded it: the prompt, the retrieval embedder\./)).toBeInTheDocument();
    expect(screen.getByText(/Accuracy A/)).not.toHaveTextContent(/95% interval/);
  });

  it("warns about a run that is only evidence, next to the comparison", async () => {
    vi.mocked(api.compareRuns).mockResolvedValue(result({ warnings: ["Run A is a software check on a small model, so it is not an evaluation of a control."] }));
    render(<CompareRuns runs={[A, B]} />);
    expect(await screen.findByText(/Run A is a software check on a small model/)).toBeInTheDocument();
    expect(screen.getByText("Run B is better.")).toBeInTheDocument();
  });

  it("says when the runs share no item and lists the disagreements on request", async () => {
    vi.mocked(api.compareRuns).mockResolvedValue(result({ stats: null, stats_note: "The runs share no item, so they cannot be compared item by item." }));
    const { unmount } = render(<CompareRuns runs={[A, B]} />);
    expect(await screen.findByText(/share no item/)).toBeInTheDocument();
    unmount();
    vi.mocked(api.compareRuns).mockResolvedValue(result());
    render(<CompareRuns runs={[A, B]} />);
    const summary = await screen.findByText(/The 30 items where run A and run B disagree \(the first 1\)/);
    fireEvent.click(summary);
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("no_finding")).toBeInTheDocument();
  });

  it("asks for two different runs and compares again when the choice changes", async () => {
    const C: EvalShared = { ...base, tag: "r2-B0-base" };
    render(<CompareRuns runs={[A, B, C]} />);
    await screen.findByText("Run B is better.");
    expect(api.compareRuns).toHaveBeenCalledWith("r1-B2-tuned", "r2-B0-base");                 // the last two by default
    await pickOption("Run A", /^r2-B0-base/);
    expect(await screen.findByText("Choose two different runs.")).toBeInTheDocument();
    expect(api.compareRuns).toHaveBeenCalledTimes(1);
    await pickOption("Run B", /^r1-B0-base/);
    await vi.waitFor(() => expect(api.compareRuns).toHaveBeenLastCalledWith("r2-B0-base", "r1-B0-base"));
  });

  it("reports a comparison that failed", async () => {
    vi.mocked(api.compareRuns).mockImplementation(() => Promise.reject(new Error("no attempts")));
    render(<CompareRuns runs={[A, B]} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The runs could not be compared: no attempts");
  });
});
