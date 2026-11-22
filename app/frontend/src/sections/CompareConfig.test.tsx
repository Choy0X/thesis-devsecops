import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ConfigDiff, DatasetFacts } from "../lib/api";
import { CompareConfig } from "./CompareConfig";
import { buildReport, datasetRows } from "./compareReport";
import report from "./evalReport.fixture.json";
import type { EvalShared } from "../lib/api";

// What differs between two runs: the settings that are not the same, the datasets, and a run whose configuration is missing.

const facts = (over: Partial<DatasetFacts> = {}): DatasetFacts => ({
  tag: "v1", status: "frozen", manifest_sha256: "a".repeat(64), current: true, counts: { records: 100, views: 40, views_by_task: { discovery: 20, repair: 20 } },
  tier_mix: { auto_checked: 30, unreviewed: 70 }, languages: { python: 60, javascript: 40 }, external: null, ...over });
const side = (over: Partial<ConfigDiff["a"]> = {}) => ({ available: true, reason: null, run: "r1", ...over });
const diff = (over: Partial<ConfigDiff> = {}): ConfigDiff => ({
  a: side(), b: side({ run: "r2" }), config: { compared: true, training_compared: true, same: 90, differing: [{ key: "training.lora.rank", a: 16, b: 32 }, { key: "training.optimizer", a: "adamw", b: null }] },
  dataset: { same: true, a: facts(), b: facts() }, ...over });

describe("the dataset rows", () => {
  it("lists only the facts that differ, with both values", () => {
    const rows = datasetRows(facts(), facts({ tag: "v2", status: "unfrozen", counts: { records: 120, views: 40, views_by_task: { discovery: 25, repair: 20 } }, tier_mix: { auto_checked: 30, unreviewed: 90 } }));
    expect(rows).toEqual([
      { field: "dataset", a: "v1", b: "v2" }, { field: "status", a: "frozen", b: "unfrozen" }, { field: "records", a: "100", b: "120" },
      { field: "examples of discovery", a: "20", b: "25" }, { field: "unreviewed rows", a: "70", b: "90" },
    ]);
  });
  it("names an imported file and gives nothing when a dataset is unknown", () => {
    expect(datasetRows(facts(), facts({ external: { original_file: "mine.jsonl" } }))).toEqual([{ field: "imported file", a: "not set", b: "mine.jsonl" }]);
    expect(datasetRows(null, facts())).toEqual([]);
  });
});

describe("the view", () => {
  it("shows the settings that differ and counts the rest, for runs on the same dataset", () => {
    render(<CompareConfig diff={diff()} other={1} />);
    const t = screen.getByRole("table", { name: "Settings that differ" });
    expect(within(t.querySelector("tbody") as HTMLElement).getAllByRole("row")).toHaveLength(2);
    expect(t).toHaveTextContent("training.lora.rank");
    expect(t).toHaveTextContent("not set");
    expect(screen.getByText("2 differ; 90 are the same.")).toBeInTheDocument();
    expect(screen.getByText(/Both runs were trained on the same dataset: dataset_v1 \(frozen\)/)).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Datasets that differ" })).toBeNull();
  });

  it("says when every setting is the same", () => {
    render(<CompareConfig diff={diff({ config: { compared: true, training_compared: true, same: 12, differing: [] } })} other={1} />);
    expect(screen.getByText("The two runs were trained with the same 12 settings.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Settings that differ" })).toBeNull();
  });

  it("warns about different datasets, lists what differs and the languages, and warns about an import", () => {
    const b = facts({ tag: "ext_x", status: "external", languages: { python: 90 }, external: { original_file: "mine.jsonl" } });
    render(<CompareConfig diff={diff({ dataset: { same: false, a: facts(), b } })} other={1} />);
    expect(screen.getByText(/trained on different datasets: dataset_v1 against dataset_ext_x/)).toBeInTheDocument();
    const t = screen.getByRole("table", { name: "Datasets that differ" });
    expect(t).toHaveTextContent("imported file");
    expect(screen.getByText(/Languages \(records\): javascript 40 against 0; python 60 against 90/)).toBeInTheDocument();
    expect(screen.getByText(/imported from outside/)).toBeInTheDocument();
  });

  it("says that a configuration could not be found, instead of calling the runs the same", () => {
    render(<CompareConfig diff={diff({ b: side({ available: false, reason: "The run r2 was not prepared here.", run: "r2" }), config: { compared: false, training_compared: false, same: 0, differing: [] }, dataset: { same: null, a: facts(), b: null } })} other={1} />);
    expect(screen.getByText(/configuration of run B could not be found: The run r2 was not prepared here\./)).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Settings that differ" })).toBeNull();
    expect(screen.queryByText(/same dataset/)).toBeNull();
  });

  it("says when a dataset folder has changed since the run and when the training settings were not compared", () => {
    render(<CompareConfig diff={diff({ b: side({ reason: "the dataset folder dataset_v1 has no readable manifest", run: "r2" }), config: { compared: true, training_compared: false, same: 3, differing: [] },
      dataset: { same: true, a: facts({ current: false }), b: facts() } })} other={1} />);
    expect(screen.getByText(/dataset folder of run A has changed since the run was prepared/)).toBeInTheDocument();
    expect(screen.queryByText(/dataset folder of run B has changed/)).toBeNull();
    expect(screen.getByText(/training settings are compared only when both runs have a training configuration/)).toBeInTheDocument();
    expect(screen.getByText("Run B: the dataset folder dataset_v1 has no readable manifest.")).toBeInTheDocument();
  });

  it("hides a hash in a setting unless Developer mode is on", () => {
    const h = "c".repeat(64);
    render(<CompareConfig diff={diff({ config: { compared: true, training_compared: true, same: 1, differing: [{ key: "dataset.manifest", a: h, b: "x" }] } })} other={1} />);
    expect(screen.getByRole("table", { name: "Settings that differ" })).toHaveTextContent("(hash)");
  });
});

describe("the report", () => {
  const run = report as unknown as EvalShared;
  it("carries the differing settings and datasets under the pair they belong to", () => {
    const b = facts({ tag: "v2" });
    const text = buildReport([run, { ...run, tag: "other" }], [{ a: run.tag, b: "other", guard: { comparable: false, blockers: [], unconfirmed: [] }, warnings: [], items: { a: 1, b: 1, shared: 1 }, stats: null, stats_note: null, disagreements: [], disagreements_total: 0 }],
      false, [diff({ dataset: { same: false, a: facts(), b } })]);
    expect(text).toContain("### What differs between run A and run B");
    expect(text).toContain("| training.lora.rank | 16 | 32 |");
    expect(text).toContain("2 settings differ; 90 are the same.");
    expect(text).toContain("Different datasets: dataset_v1 against dataset_v2.");
    expect(text).toContain("| dataset | v1 | v2 |");
  });
});
