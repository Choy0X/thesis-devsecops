import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { compareRuns: vi.fn(), compareConfig: vi.fn(), compareCwe: vi.fn(), runCurves: vi.fn(), listJobs: vi.fn() } };
});
import report from "./evalReport.fixture.json";
import { api, type CompareStats, type CweData, type DiscoveryReport, type EvalShared, type RunCompare } from "../lib/api";
import { CompareRuns, runLabel, verdictWords } from "./CompareRuns";

// More than two runs, the tables of the runs side by side, the training curves laid over each other and the downloaded report.

const base = report as unknown as EvalShared;
const stats = (over: Partial<CompareStats> = {}): CompareStats => ({
  items: 120, both_right: 70, both_wrong: 20, only_a: 8, only_b: 22, disagreements: 30, accuracy_a: 0.65, accuracy_b: 0.7667, difference: -0.1167, verdict: "b_better",
  reason: "B is right more often.", p_value: 0.0161, interval: [-0.21, -0.03], rule: { alpha: 0.05, min_items: 30, min_disagreements: 10, test: "exact McNemar, two-sided" }, ...over });
const result = (over: Partial<RunCompare> = {}): RunCompare => ({
  a: "x", b: "y", guard: { comparable: true, blockers: [], unconfirmed: [] }, warnings: [], items: { a: 120, b: 120, shared: 120 }, stats: stats(), stats_note: null,
  disagreements: [], disagreements_total: 0, ...over });
const matrix = (confusion: Record<string, Record<string, number>>, over: Partial<EvalShared> = {}): EvalShared => {
  const arm = base.run_info!.arm!;
  const d = base.arms[arm].generated_decision as DiscoveryReport;
  return { ...base, ...over, arms: { [arm]: { generated_decision: { ...d, overall: { ...d.overall, confusion } } } } };
};
const M1 = matrix({ supported: { supported: 6, no_finding: 2 }, no_supported_finding: { no_finding: 9, supported: 1 } }, { tag: "m1-B0-base" });
const M2 = matrix({ supported: { supported: 8, no_finding: 0 }, no_supported_finding: { no_finding: 8, supported: 2 } }, { tag: "m2-B2-tuned", run_info: { ...base.run_info!, run: "m2", baseline: "B2" } });
const M3 = matrix({ supported: { supported: 7, no_finding: 1 }, no_supported_finding: { no_finding: 9, supported: 1 } }, { tag: "m3-B3-field", run_info: { ...base.run_info!, run: "m3", baseline: "B3" } });

beforeEach(() => {
  vi.mocked(api.compareRuns).mockReset();
  vi.mocked(api.compareRuns).mockImplementation(async (a, b) => result({ a, b }));
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
  it("names the other run in the verdict and describes a run in a list", () => {
    expect(verdictWords("b_better", "C")).toBe("Run C is better");
    expect(verdictWords("a_better", "C")).toBe("Run A is better");
    expect(verdictWords("not_enough_data", "C")).toBe("Not enough data to say");
    expect(runLabel(M2)).toBe("m2-B2-tuned (B2, base, val)");
    expect(runLabel({ ...M2, split: null, run_info: undefined })).toBe("m2-B2-tuned");
  });
});

describe("the tables", () => {
  it("puts the runs in columns with the split of each above every table and the matrices with their change from run A", async () => {
    render(<CompareRuns runs={[M1, M2]} />);
    await screen.findByText("Run B is better.");
    expect(screen.getByText("By kind of case")).toBeInTheDocument();
    expect(screen.getByText("By language")).toBeInTheDocument();
    expect(screen.getByText("By ecosystem")).toBeInTheDocument();
    expect(screen.getAllByText(/Measured on: A, the validation split, selection role; used to choose the checkpoint/).length).toBeGreaterThanOrEqual(4);
    const languages = screen.getByRole("region", { name: "By language" });
    expect(within(languages).getByText("python").closest("tr")).toHaveTextContent(/found .*false alarms/);
    const b = screen.getByRole("table", { name: "Confusion matrix of run B" });
    expect(b).toHaveTextContent("Run B, change from run A in brackets");
    const row = within(b).getByText("supported", { selector: "td" }).closest("tr");
    expect(row).toHaveTextContent("0 (-2)");
    expect(row).toHaveTextContent("8 (+2)");
    expect(await screen.findByText("The table by weakness class (CWE) could not be read.")).toBeInTheDocument();
  });
});

describe("the weakness classes", () => {
  const data = (over: Partial<CweData> = {}): CweData => ({
    runs: [{ tag: "m1-B0-base", available: true, reason: null, min_cell: 20, items: 60, unmatched: 0 }, { tag: "m2-B2-tuned", available: true, reason: null, min_cell: 20, items: 60, unmatched: 3 }],
    cwes: [{ cwe: "CWE-79", cells: [{ vulnerable: 30, detected: 24, negatives: 40, false_positives: 4 }, { vulnerable: 30, detected: 27, negatives: 10, false_positives: 0 }] },
      { cwe: "CWE-89", cells: [{ vulnerable: 4, detected: 3, negatives: 0, false_positives: 0 }, { vulnerable: 4, detected: 4, negatives: 0, false_positives: 0 }] }],
    note: "A case with several weakness classes counts under each of them.", ...over });

  it("puts each class in a row with a column per run, says too few under the run's minimum, and tells what was left out", async () => {
    vi.mocked(api.compareCwe).mockResolvedValue(data());
    render(<CompareRuns runs={[M1, M2]} />);
    const region = await screen.findByRole("region", { name: "By weakness class (CWE)" });
    expect(api.compareCwe).toHaveBeenCalledWith(["m1-B0-base", "m2-B2-tuned"]);
    const row79 = within(region).getByText("CWE-79").closest("tr") as HTMLElement;
    expect(row79).toHaveTextContent("found 80.0% (24 of 30)");
    expect(row79).toHaveTextContent("found 90.0% (27 of 30)");
    expect(row79).toHaveTextContent("false alarms 10.0% (4 of 40)");
    expect(row79).toHaveTextContent("false alarms too few (10)");
    const row89 = within(region).getByText("CWE-89").closest("tr") as HTMLElement;
    expect(row89).toHaveTextContent("found too few (4)");
    expect(row89).toHaveTextContent("false alarms no cases");
    expect(within(region).getByText("Run B: 3 of its 60 cases are not in its dataset's views and are left out.")).toBeInTheDocument();
    expect(within(region).getByText("A case with several weakness classes counts under each of them.")).toBeInTheDocument();
  });

  it("says why a run has no classes and shows the others", async () => {
    vi.mocked(api.compareCwe).mockResolvedValue(data({ runs: [{ tag: "m1-B0-base", available: false, reason: "the run does not say which training run it came from", min_cell: 20, items: 60, unmatched: 0 },
      { tag: "m2-B2-tuned", available: true, reason: null, min_cell: 20, items: 60, unmatched: 0 }], cwes: [{ cwe: "CWE-79", cells: [null, { vulnerable: 30, detected: 27, negatives: 0, false_positives: 0 }] }] }));
    render(<CompareRuns runs={[M1, M2]} />);
    const region = await screen.findByRole("region", { name: "By weakness class (CWE)" });
    expect(within(region).getByText("Run A has no weakness classes: the run does not say which training run it came from.")).toBeInTheDocument();
    expect(within(region).getByText("CWE-79").closest("tr")).toHaveTextContent("found no data");
  });
});

describe("more than two runs", () => {
  it("adds a third run, sets each against run A, names the other run in its verdict and removes it again", async () => {
    vi.mocked(api.compareRuns).mockImplementation(async (a, b) => result({ a, b, stats: stats({ verdict: b === "m3-B3-field" ? "no_difference_shown" : "b_better", reason: `reason for ${b}` }) }));
    render(<CompareRuns runs={[M1, M2, M3]} />);
    await screen.findByText(/reason for m3-B3-field/);
    await pickOption("Run A", /^m1-B0-base/);
    await pickOption("Run B", /^m2-B2-tuned/);
    await vi.waitFor(() => expect(api.compareRuns).toHaveBeenCalledWith("m1-B0-base", "m2-B2-tuned"));
    fireEvent.click(await screen.findByRole("button", { name: "Add a run" }));
    expect(screen.getByRole("combobox", { name: "Run C" })).toHaveTextContent(/^m3-B3-field/);
    await vi.waitFor(() => expect(api.compareRuns).toHaveBeenCalledWith("m1-B0-base", "m3-B3-field"));
    const toC = await screen.findByRole("group", { name: "Run A against run C" });
    expect(within(toC).getByText("No difference shown.")).toBeInTheDocument();
    const toB = screen.getByRole("group", { name: "Run A against run B" });
    expect(within(toB).getByText("Run B is better.")).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader", { name: "Run C" }).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Remove Run C" }));
    await vi.waitFor(() => expect(screen.queryByLabelText("Run C")).toBeNull());
    expect(screen.queryByRole("group", { name: "Run A against run C" })).toBeNull();
  });

  it("asks for different runs when two of the picks are the same, and compares nothing", async () => {
    render(<CompareRuns runs={[M1, M2, M3]} />);
    await screen.findByText("Run B is better.");
    fireEvent.click(screen.getByRole("button", { name: "Add a run" }));
    await screen.findByLabelText("Run C");
    vi.mocked(api.compareRuns).mockClear();
    await pickOption("Run C", /^m3-B3-field/);
    await pickOption("Run B", /^m3-B3-field/);
    expect(await screen.findByText("Choose different runs.")).toBeInTheDocument();
    expect(api.compareRuns).not.toHaveBeenCalled();
  });
});

describe("what differs", () => {
  const side = (run: string) => ({ available: true, reason: null, run });
  it("shows the settings that differ for each pair, and still shows the verdict when the settings cannot be read", async () => {
    vi.mocked(api.compareConfig).mockImplementation(async (a, b) => ({ a: side(a), b: side(b), config: { compared: true, training_compared: true, same: 20, differing: [{ key: `training.epochs for ${b}`, a: 1, b: 3 }] }, dataset: { same: null, a: null, b: null } }));
    const { unmount } = render(<CompareRuns runs={[M1, M2]} />);
    expect(await screen.findByText("What differs between run A and run B")).toBeInTheDocument();
    expect(screen.getByText("training.epochs for m2-B2-tuned")).toBeInTheDocument();
    expect(api.compareConfig).toHaveBeenCalledWith("m1-B0-base", "m2-B2-tuned");
    unmount();
    vi.mocked(api.compareConfig).mockRejectedValue(new Error("unreadable"));
    render(<CompareRuns runs={[M1, M2]} />);
    expect(await screen.findByText("Run B is better.")).toBeInTheDocument();
    expect(screen.queryByText("What differs between run A and run B")).toBeNull();
  });
});

describe("the training curves", () => {
  const curves = (steps: number[], loss: number[], validation: { step: number; val_loss: number }[] = []) => ({ sources: [{ label: "run", rows: steps.length, skipped: 0, steps, loss, fields: {},
    evaluations: validation.map((v) => ({ ...v, pass: 1, family_pass: 1, metric: -v.val_loss, metric_name: "negative_validation_loss", n_examples: 8 })) }] });

  it("lays the training loss of the runs over each other, says where they match, and draws a training run once however many evaluations came from it", async () => {
    vi.mocked(api.runCurves).mockImplementation(async (name) => (name === "m2" ? curves([1, 2, 3, 4], [1.4, 1.2, 1.0, 0.9], [{ step: 2, val_loss: 1.1 }, { step: 4, val_loss: 0.95 }])
      : name === "m3" ? curves([1, 2, 3], [1.5, 1.3, 1.1]) : { sources: [] }));
    const M2b = { ...M2, tag: "m2-B2-other-arm" };
    render(<CompareRuns runs={[M2, M3, M2b]} />);
    await pickOption("Run A", /^m2-B2-tuned/);
    await pickOption("Run B", /^m3-B3-field/);
    expect(await screen.findByText(/At step 3, the last step every run here has reached: A 1.0000, B 1.1000\./)).toBeInTheDocument();
    expect(api.runCurves).toHaveBeenCalledWith("m2");
    expect(api.runCurves).toHaveBeenCalledWith("m3");
    expect(screen.getByText("Training loss of the runs, by step")).toBeInTheDocument();
    expect(screen.getByText("Validation loss of the runs, by step")).toBeInTheDocument();
  });

  it("says when none of the runs has a training log (a base model that was only evaluated)", async () => {
    render(<CompareRuns runs={[M1, M2]} />);
    expect(await screen.findByText(/None of these runs has a training log/)).toBeInTheDocument();
  });

  it("says when the runs do not name the training run they came from", async () => {
    const lost = (r: EvalShared): EvalShared => ({ ...r, run_info: { ...r.run_info!, run: null } });
    render(<CompareRuns runs={[lost(M1), lost(M2)]} />);
    expect(await screen.findByText(/do not say which training run they came from/)).toBeInTheDocument();
  });
});

describe("the download", () => {
  it("offers the comparison as a Markdown file once the runs are compared, with the split of each run in it", async () => {
    const blobs: Blob[] = [];
    const real = { c: URL.createObjectURL, r: URL.revokeObjectURL };
    URL.createObjectURL = (b: Blob | MediaSource) => { blobs.push(b as Blob); return "blob:x"; };
    URL.revokeObjectURL = () => undefined;
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    try {
      render(<CompareRuns runs={[M1, M2]} />);
      fireEvent.click(await screen.findByRole("button", { name: "Download the comparison" }));
      expect(click).toHaveBeenCalled();
      expect(blobs).toHaveLength(1);
      expect(blobs[0].type).toBe("text/markdown");
      const text = await new Promise<string>((resolve) => { const fr = new FileReader(); fr.onload = () => resolve(String(fr.result)); fr.readAsText(blobs[0]); });        // jsdom's Blob has no text()
      expect(text).toContain("# Comparison of evaluation runs");
      expect(text).toContain("Measured on the validation split, selection role");
    } finally {
      click.mockRestore();
      URL.createObjectURL = real.c;
      URL.revokeObjectURL = real.r;
    }
  });

  it("is not offered while there is nothing to download", async () => {
    vi.mocked(api.compareRuns).mockImplementation(() => Promise.reject(new Error("no attempts")));
    render(<CompareRuns runs={[M1, M2]} />);
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "Download the comparison" })).toBeNull();
  });
});
