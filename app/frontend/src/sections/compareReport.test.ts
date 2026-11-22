import { describe, expect, it } from "vitest";
import report from "./evalReport.fixture.json";
import type { CweData, DecisionCell, DiscoveryReport, EvalShared, RunCompare } from "../lib/api";
import { alarmsText, buildReport, cweText, discoveryOf, foundText, gridOf, hideHashes, insufficientText, keysOf, splitShort, withDelta } from "./compareReport";

const base = report as unknown as EvalShared;
const withConfusion = (tag: string, confusion: Record<string, Record<string, number>>, over: Partial<EvalShared> = {}): EvalShared => {
  const arm = base.run_info!.arm!;
  const d = base.arms[arm].generated_decision as DiscoveryReport;
  return { ...base, tag, ...over, arms: { [arm]: { generated_decision: { ...d, overall: { ...d.overall, confusion } } } } };
};
const A = withConfusion("r1-B0-base", { supported: { supported: 6, no_finding: 2 }, no_supported_finding: { no_finding: 9, supported: 1 } });
const B = withConfusion("r1-B2-tuned", { supported: { supported: 8, no_finding: 0, abstained: 1 }, no_supported_finding: { no_finding: 8, supported: 2 } }, { split: "test" });
const out = (over: Partial<RunCompare> = {}): RunCompare => ({ a: A.tag, b: B.tag, guard: { comparable: true, blockers: [], unconfirmed: [] }, warnings: [], items: { a: 20, b: 20, shared: 20 },
  stats: { items: 20, both_right: 12, both_wrong: 2, only_a: 1, only_b: 5, disagreements: 6, accuracy_a: 0.7, accuracy_b: 0.85, difference: -0.15, verdict: "not_enough_data", reason: "Only 6 items disagree.", p_value: null, interval: null,
    rule: { alpha: 0.05, min_items: 30, min_disagreements: 10, test: "exact McNemar, two-sided" } }, stats_note: null, disagreements: [], disagreements_total: 0, ...over });

describe("the words of a cell", () => {
  const cell = (over: Partial<DecisionCell> = {}): DecisionCell => ({ ...(discoveryOf(A)!.overall), ...over });
  it("gives the rate and the count, or says there are too few cases", () => {
    const v = { n: 7, detected: 5, answered: 7, insufficient: false, recall: 5 / 7, conditional_recall: 1, ci: null };
    expect(foundText(cell({ vulnerable: v }))).toBe("71.4% (5 of 7)");
    expect(foundText(cell({ vulnerable: { ...v, insufficient: true } }))).toBe("too few (7)");
    expect(foundText(cell({ vulnerable: { ...v, n: 0, detected: 0 } }))).toBe("no cases");
    expect(foundText(undefined)).toBe("not measured");
    const n = { n: 10, false_positives: 1, answered: 10, insufficient: false, fpr: 0.1, ci: null };
    expect(alarmsText(cell({ negative: n }))).toBe("10.0% (1 of 10)");
    expect(alarmsText(cell({ negative: { ...n, insufficient: true } }))).toBe("too few (10)");
    expect(insufficientText(cell({ insufficient_context: { n: 4, correct: 3, recall: 0.75 } }))).toBe("75.0% (3 of 4)");
    expect(insufficientText(cell({ insufficient_context: { n: 0, correct: 0, recall: null } }))).toBe("no cases");
  });
});

describe("the weakness classes", () => {
  it("gives the rate and the count, 'too few' under the minimum of the run, and 'no cases'", () => {
    expect(cweText({ vulnerable: 30, detected: 24, negatives: 10, false_positives: 1 }, 20)).toEqual({ found: "80.0% (24 of 30)", alarms: "too few (10)" });
    expect(cweText({ vulnerable: 0, detected: 0, negatives: 0, false_positives: 0 }, 20)).toEqual({ found: "no cases", alarms: "no cases" });
    expect(cweText(null, 20)).toEqual({ found: "no data", alarms: "no data" });
    expect(cweText({ vulnerable: 5, detected: 5, negatives: 5, false_positives: 0 }, 5)).toEqual({ found: "100.0% (5 of 5)", alarms: "0.0% (0 of 5)" });
  });
  it("is in the report with the runs that have none and what was left out", () => {
    const cwe: CweData = { runs: [{ tag: A.tag, available: true, reason: null, min_cell: 20, items: 40, unmatched: 2 }, { tag: B.tag, available: false, reason: "no dataset", min_cell: 20, items: 40, unmatched: 0 }],
      cwes: [{ cwe: "CWE-79", cells: [{ vulnerable: 30, detected: 24, negatives: 0, false_positives: 0 }, null] }], note: "Several classes count under each." };
    const text = buildReport([A, B], [out()], false, [], cwe);
    expect(text).toContain("## By weakness class (CWE)");
    expect(text).toContain("| CWE-79 | found 80.0% (24 of 30); false alarms no cases | found no data; false alarms no data |");
    expect(text).toContain("Run B has no weakness classes: no dataset.");
    expect(text).toContain("Run A: 2 of its 40 cases are not in its dataset's views and are left out.");
    expect(text).toContain("Several classes count under each.");
  });
});

describe("the grid and the deltas", () => {
  it("puts every truth and every outcome that any run has on one grid", () => {
    const g = gridOf([discoveryOf(A), discoveryOf(B), null]);
    expect(g.truths).toEqual(["no_supported_finding", "supported"]);
    expect(g.outcomes).toEqual(["abstained", "no_finding", "supported"]);
    expect(gridOf([null])).toEqual({ truths: [], outcomes: [] });
  });
  it("lists the languages and ecosystems that any run has", () => {
    expect(keysOf([discoveryOf(A), null], "by_language")).toEqual(["javascript", "python"]);
    expect(keysOf([discoveryOf(A)], "by_ecosystem")).toEqual(["PyPI", "npm"]);
  });
  it("shows a count and its change from the first run's", () => {
    expect(withDelta(5, null)).toBe("5");
    expect(withDelta(8, 6)).toBe("8 (+2)");
    expect(withDelta(1, 3)).toBe("1 (-2)");
    expect(withDelta(4, 4)).toBe("4 (same)");
  });
  it("says which split a run was measured on and what that means", () => {
    expect(splitShort(A)).toBe("validation split, selection role; used to choose the checkpoint, so not a held-out result");
    expect(splitShort(B)).toBe("test split");
    expect(splitShort({ ...A, split: null })).toBe("split not recorded");
  });
  it("hides a hash unless Developer mode is on", () => {
    const h = "a".repeat(64);
    expect(hideHashes(`the manifest ${h} changed`, false)).toBe("the manifest (hash) changed");
    expect(hideHashes(`the manifest ${h} changed`, true)).toContain(h);
    expect(hideHashes("a short word and v1", false)).toBe("a short word and v1");
  });
});

describe("the report", () => {
  it("names the runs and the split of each, the verdict, the tables by kind, language and ecosystem, and the matrices with the change from run A", () => {
    const text = buildReport([A, B], [out()], false);
    expect(text).toContain("# Comparison of evaluation runs");
    expect(text).toContain("* Run A: r1-B0-base, baseline B0, arm base, model Qwen/Qwen2.5-Coder-0.5B. Measured on the validation split, selection role; used to choose the checkpoint, so not a held-out result.");
    expect(text).toContain("* Run B: r1-B2-tuned");
    expect(text).toContain("Measured on the test split.");
    expect(text).toContain("## Run A against run B");
    expect(text).toContain("not enough data. Only 6 items disagree.");
    expect(text).toContain("| Measure | Run A | Run B |");
    expect(text).toContain("## By language");
    expect(text).toContain("| python | found ");
    expect(text).toContain("## By ecosystem");
    expect(text).toContain("## Confusion matrix, run B (change from run A in brackets)");
    expect(text).toMatch(/\| supported \| 1 \(\+1\) \| 0 \(-2\) \| 8 \(\+2\) \|/);
    expect(text).toContain("The table by weakness class (CWE) could not be read.");
  });

  it("gives the blockers instead of a verdict for runs that are not comparable, and leaves the hash out unless Developer mode is on", () => {
    const h = "b".repeat(64);
    const bad = out({ guard: { comparable: false, unconfirmed: [], blockers: [{ field: "dataset_version", reason: "The runs used different dataset versions.", a: "v1", b: h }] }, stats: null });
    const plain = buildReport([A, B], [bad], false);
    expect(plain).toContain("These runs are not comparable, so no verdict is given:");
    expect(plain).toContain("The runs used different dataset versions. (dataset_version: A \"v1\", B \"(hash)\")");
    expect(plain).not.toContain(h);
    expect(buildReport([A, B], [bad], true)).toContain(h);
  });

  it("covers every run and every pair when there are more than two, and says when a run has no discovery answers", () => {
    const C = withConfusion("r2-B0-base", {});
    const text = buildReport([A, B, { ...C, arms: {} }], [out(), out({ stats: null, stats_note: "The runs share no item." })], false);
    expect(text).toContain("## Run A against run B");
    expect(text).toContain("## Run A against run C");
    expect(text).toContain("The runs share no item.");
    expect(text).toContain("| Measure | Run A | Run B | Run C |");
    expect(text).toContain("not measured");
  });
});
