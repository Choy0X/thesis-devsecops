import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { storeSummary: vi.fn(), codeSummary: vi.fn(), builds: vi.fn(), mix: vi.fn(), experiments: vi.fn(), listJobs: vi.fn(), device: vi.fn(), lifecycle: vi.fn() } };
});
import { api } from "../lib/api";
import { DevModeProvider } from "../lib/devMode";
import { resetJobsList } from "../lib/jobsList";
import { pipelineSteps, recommended } from "../lib/pipelineSteps";
import { Overview } from "./Overview";

const records = (total: number, mined = 0) => ({ total, by_source: {}, with_cve: total, with_code: mined, with_vector: 0, by_code_status: (mined ? { mined } : {}) as Record<string, number> });
const pairs = (usable: number) => ({ fix_commits: {} as any, pairs: { usable } as any, dataset: null, languages: [] });
const build = { tag: "v1", status: "frozen", path: "", counts: { records: 120, views: 400 }, tier_mix: { auto_checked: 90, human_reviewed: 30 }, versions: {}, blocked: {}, files: {} };
const run = { tag: "run-a", n: 100, accuracy: 0.8, macro_f1: 0.7, per_class: {}, critical_false_negative_rate: 0.25, critical_total: 8, critical_missed: 2, split: "val",
  confusion_matrix: [], confusion_matrix_labels: [] };

describe("pipelineSteps", () => {
  it("recommends the first empty step and marks the ones after it as not yet", () => {
    const steps = pipelineSteps({ records: records(0), pairs: pairs(0), build: null, runs: [] });
    expect(steps.map((s) => s.state)).toEqual(["next", "todo", "todo", "todo", "todo", "todo"]);
    expect(recommended(steps)?.id).toBe("collect");
  });

  it("is done where there is data and recommends the first gap after it", () => {
    const steps = pipelineSteps({ records: records(500, 40), pairs: pairs(0), build: null, runs: [] });
    expect(steps.map((s) => s.state)).toEqual(["done", "done", "next", "todo", "todo", "todo"]);
    expect(steps[0].detail).toBe("500 advisories collected");
    expect(recommended(steps)).toMatchObject({ id: "pairs", to: "/pairs" });
  });

  it("never calls a step done before its numbers are known", () => {
    const steps = pipelineSteps({ records: undefined, pairs: undefined, build: undefined, runs: undefined });
    expect(steps.every((s) => s.state === "unknown" || s.id === "verify")).toBe(true);
    expect(steps.find((s) => s.state === "done")).toBeUndefined();
  });

  it("leaves verification as the last step until receipts exist", () => {
    const steps = pipelineSteps({ records: records(5, 5), pairs: pairs(9), build: build as any, runs: [run as any] });
    expect(steps.map((s) => s.state)).toEqual(["done", "done", "done", "done", "done", "next"]);
  });
});

beforeEach(() => {
  resetJobsList();
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.mix).mockResolvedValue({ scope: "store", total: 6, language: { javascript: 3, python: 2, go: 1 }, ecosystem: { npm: 4, PyPI: 2 }, severity: { critical: 1, high: 2, medium: 2, low: 0, none: 1 } });
  vi.mocked(api.lifecycle).mockResolvedValue({ clean: true, note: "", at: null });
  vi.mocked(api.device).mockResolvedValue({ cpu: { physical_cores: 6, logical_cores: 12, workers: 5 }, ram: { total_gb: 15.9, free_gb: 4.4 },
    disks: [{ drive: "C:", fstype: "NTFS", total_gb: 500, free_gb: 120.5 }], gpu: { available: false, reason: "nvidia-smi was not found", cards: [] } });
});

const show = (dev = false, go = vi.fn()) => render(<MemoryRouter><DevModeProvider initial={dev}><Overview go={go} /></DevModeProvider></MemoryRouter>);

describe("Overview", () => {
  it("on an empty database shows the stepper, one recommended action and honest empty states", async () => {
    vi.mocked(api.storeSummary).mockResolvedValue(records(0) as any);
    vi.mocked(api.codeSummary).mockResolvedValue(pairs(0) as any);
    vi.mocked(api.builds).mockResolvedValue({ builds: [], next_version: "v1", next_versions: {} });
    vi.mocked(api.experiments).mockResolvedValue([]);
    const go = vi.fn();
    show(false, go);
    expect(await screen.findByRole("button", { name: "Collect advisories" })).toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: "Pipeline" })).getAllByRole("listitem");
    expect(steps).toHaveLength(6);
    expect(steps[0]).toHaveAttribute("data-state", "next");
    expect(screen.getAllByText("No evaluated run yet").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/No dataset has been built yet/)).toBeInTheDocument();
    screen.getByRole("button", { name: /Collect advisories/ }).click();
    expect(go).toHaveBeenCalledWith("collection");
  });

  it("states the headline with its denominator and uses plain names for the review tiers", async () => {
    vi.mocked(api.storeSummary).mockResolvedValue(records(500, 40) as any);
    vi.mocked(api.codeSummary).mockResolvedValue(pairs(300) as any);
    vi.mocked(api.builds).mockResolvedValue({ builds: [build as any], next_version: "v2", next_versions: {} });
    vi.mocked(api.experiments).mockResolvedValue([run as any]);
    show();
    expect(await screen.findByText("25.0%")).toBeInTheDocument();
    expect(screen.getByText(/2 of 8 Critical missed/)).toBeInTheDocument();
    expect(screen.getByText("Checked by rules")).toBeInTheDocument();
    expect(screen.getByText("Human reviewed")).toBeInTheDocument();
    expect(screen.queryByText(/auto_checked/)).toBeNull();
    expect(screen.queryByText(/dataset_v1/)).toBeNull();
    expect(screen.queryByText(/run run-a/)).toBeNull();
  });

  it("shows internal names only in Developer mode", async () => {
    vi.mocked(api.storeSummary).mockResolvedValue(records(500, 40) as any);
    vi.mocked(api.codeSummary).mockResolvedValue(pairs(300) as any);
    vi.mocked(api.builds).mockResolvedValue({ builds: [build as any], next_version: "v2", next_versions: {} });
    vi.mocked(api.experiments).mockResolvedValue([run as any]);
    show(true);
    expect(await screen.findByText("dataset_v1")).toBeInTheDocument();
    expect(screen.getByText("run run-a")).toBeInTheDocument();
  });

  it("tells the person when the app did not close properly last time", async () => {
    vi.mocked(api.storeSummary).mockResolvedValue(records(0) as any);
    vi.mocked(api.codeSummary).mockResolvedValue(pairs(0) as any);
    vi.mocked(api.builds).mockResolvedValue({ builds: [], next_version: "v1", next_versions: {} });
    vi.mocked(api.experiments).mockResolvedValue([]);
    vi.mocked(api.lifecycle).mockResolvedValue({ clean: false, note: "The app did not close properly last time.", at: "x" });
    show();
    expect(await screen.findByText("The app did not close properly last time.")).toBeInTheDocument();
  });

  it("flags a job that stopped and shows the computer's figures", async () => {
    vi.mocked(api.storeSummary).mockResolvedValue(records(0) as any);
    vi.mocked(api.codeSummary).mockResolvedValue(pairs(0) as any);
    vi.mocked(api.builds).mockResolvedValue({ builds: [], next_version: "v1", next_versions: {} });
    vi.mocked(api.experiments).mockResolvedValue([]);
    vi.mocked(api.listJobs).mockResolvedValue([{ id: "a1", stage: "campaign", status: "interrupted", params: {}, started: "", finished: "", error: "", log_count: 0 }]);
    show();
    await waitFor(() => expect(screen.getByLabelText("Alerts")).toHaveTextContent("Collection campaign was cut off when the app closed."));
    expect(await screen.findByText("C: 120.5 of 500 GB free")).toBeInTheDocument();
    expect(screen.getByText("nvidia-smi was not found")).toBeInTheDocument();
  });
});
