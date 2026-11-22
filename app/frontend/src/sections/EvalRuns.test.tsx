import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { pickOption } from "../test/select";
import report from "./evalReport.fixture.json";
import type { EvalShared, RatioCi } from "../lib/api";
import { EvalRuns, rateText, splitNote, whyNotCounted } from "./EvalRuns";

// The fixture is a report written by the shared evaluator itself (app/backend/pipeline/eval/shared.py) from 12 vulnerable and 10 clean npm cases, 3 python
// cases, and 4 repairs: it has the shape the real runs have, including the tracks that were never attempted.
const run = report as unknown as EvalShared;
const ci = (over: Partial<RatioCi> = {}): RatioCi => ({ estimate: 0.5, numerator: 1, denominator: 2, clusters: 2, ci: [0.2, 0.8], status: "ok", ...over });

describe("the words", () => {
  it("gives a rate with its interval, or the reason there is none", () => {
    expect(rateText(ci())).toBe("50.0% (95% interval 20.0% to 80.0%)");
    expect(rateText(ci({ ci: null, status: "insufficient_clusters" }))).toBe("50.0% (no interval: only one group of cases)");
    expect(rateText(ci({ ci: null, status: "no_denominator", estimate: null }))).toBe("no cases");
    expect(rateText(null, 0.25)).toBe("25.0%");
  });

  it("says what the split means: validation numbers chose the checkpoint, so they are not a held-out result", () => {
    expect(splitNote("val", "selection")).toMatch(/validation split \(selection role\).*not a held-out result/);
    expect(splitNote("train", null)).toBe("Measured on the train split.");
    expect(splitNote(null, null)).toMatch(/not recorded/);
  });

  it("says why a run is only evidence", () => {
    expect(whyNotCounted({ ...run.run_info!, counts_as_evaluation: false, smoke_test: true, partial: true })).toMatch(/a software check on a small model and capped at a few views/);
    expect(whyNotCounted({ ...run.run_info!, counts_as_evaluation: true })).toBeNull();
    expect(whyNotCounted(undefined)).toBeNull();
  });
});

describe("EvalRuns", () => {
  it("shows the run with its split, why it does not count, and the headline numbers with their denominators", () => {
    render(<EvalRuns runs={[run]} />);
    expect(screen.getByText(/Measured on the validation split \(selection role\)/)).toBeInTheDocument();
    expect(screen.getByText(/Kept as evidence only: this run is a software check on a small model and capped at a few views/)).toBeInTheDocument();
    expect(screen.getByText("29 attempts", { exact: false })).toBeInTheDocument();
    const all = screen.getByText("All").closest("tr") as HTMLElement;
    expect(within(all).getByText("25")).toBeInTheDocument();                     // 12 + 10 + 3 views of the discovery track
    expect(all).toHaveTextContent("11 of 15");                                  // vulnerable found
    expect(all).toHaveTextContent("1 of 10");                                   // false alarms
  });

  it("breaks the numbers down by ecosystem and by language and says too few instead of a rate for a thin cell", () => {
    render(<EvalRuns runs={[run]} />);
    const npm = screen.getByText("npm", { selector: "td" }).closest("tr") as HTMLElement;
    expect(npm).toHaveTextContent("8 of 12");
    const pypi = screen.getByText("PyPI", { selector: "td" }).closest("tr") as HTMLElement;
    expect(pypi).toHaveTextContent("too few (3)");
    expect(screen.getByText("python", { selector: "td" }).closest("tr")).toHaveTextContent("too few (3)");
    expect(within(pypi).queryByText(/%/, { selector: "td:nth-child(4)" })).toBeNull();
  });

  it("shows the two Critical errors apart, each with its denominator, and counts what has no usable severity", () => {
    render(<EvalRuns runs={[run]} />);
    expect(screen.getByText("A Critical vulnerability was not detected")).toBeInTheDocument();
    expect(screen.getByText("A Critical vulnerability was found but its severity was set too low")).toBeInTheDocument();
    expect(screen.getByText(/4 Critical cases;/)).toBeInTheDocument();
    expect(screen.getByText(/counted apart, never assumed not Critical/)).toBeInTheDocument();
  });

  it("counts a repair as verified only with a receipt and reports the track that was never attempted as not measured", () => {
    render(<EvalRuns runs={[run]} />);
    const reported = screen.getByText("reported", { selector: "td" }).closest("tr") as HTMLElement;
    expect(reported).toHaveTextContent("2 of 4");
    expect(reported).toHaveTextContent("50.0%");
    expect(screen.getByText(/general utility:/).closest("li")).toHaveTextContent("not measured (no general-utility attempts");
    expect(screen.getByText(/head decision:/).closest("li")).toHaveTextContent("not measured");
  });

  it("offers a choice of run and of model, and a blocked track is a sentence, never a zero", async () => {
    const blockedRun: EvalShared = { ...run, tag: "r2-B0-base", n_attempts: 1, run_info: { ...run.run_info!, counts_as_evaluation: true },
      arms: { base: { generated_decision: { status: "blocked", reason: "no generated_decision attempts" } }, tuned: run.arms.base } };
    render(<EvalRuns runs={[run, blockedRun]} />);
    expect(screen.getByText(/Not measured: no generated_decision attempts\./)).toBeInTheDocument();
    expect(screen.queryByText(/Kept as evidence only/)).toBeNull();
    await pickOption("Model arm", "tuned");
    expect(screen.getByText("All").closest("tr")).toHaveTextContent("11 of 15");
    fireEvent.click(screen.getByRole("button", { name: /r1-B0-base/ }));
    expect(screen.getByText(/Kept as evidence only/)).toBeInTheDocument();
  });
});
