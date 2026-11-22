import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { getSettings: vi.fn(), experiments: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { api } from "../lib/api";
import { resetStreams } from "../lib/jobStreams";
import { Evaluate } from "./Evaluate";

class InertSocket { onopen: any; onmessage: any; onclose: any; onerror: any; constructor(public url: string) {} close() {} send() {} }

const classifierRun = (tag: string, over: Record<string, unknown> = {}) => ({ tag, n: 50, split: "val", accuracy: 0.62, macro_f1: 0.55, coverage: 0.98, critical_missed: 3, critical_total: 10, shared: { n_attempts: 50 }, ...over });
const detectorRun = (tag: string) => ({ tag, task: "detection", n: 300, split: "val", recall: 0.7, false_positive_rate_safe: 0.2, coverage: 1, shared: { n_attempts: 300 } });

const show = () => render(<MemoryRouter><Evaluate /></MemoryRouter>);

beforeEach(() => {
  vi.stubGlobal("WebSocket", InertSocket);
  resetStreams();
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.getSettings).mockResolvedValue({ llm_model: "qwen2.5-coder-7b" } as never);
  vi.mocked(api.experiments).mockResolvedValue([classifierRun("old-clf"), classifierRun("older-clf-no-attempts", { shared: undefined }), detectorRun("det-1"), { tag: "other", macro_f1: 0.1 }] as never);
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "classify", status: "running", logs: [], progress: null } as never);
  vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "classify", status: "running", logs: [], progress: null } as never);
});
afterEach(() => { vi.unstubAllGlobals(); resetStreams(); });

describe("Evaluate", () => {
  it("names the model under test and offers the two tasks, the severity one first", async () => {
    show();
    expect(await screen.findByText("qwen2.5-coder-7b")).toBeInTheDocument();
    const group = screen.getByRole("radiogroup", { name: "What to evaluate" });
    expect(within(group).getByRole("radio", { name: "Severity of a described vulnerability" })).toHaveAttribute("aria-checked", "true");
    expect(within(group).getByRole("radio", { name: "Is this code vulnerable?" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/Change it in Settings, or serve a model on the Model Hub/)).toBeInTheDocument();
  });

  it("says when no model is configured", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ llm_model: "" } as never);
    show();
    expect(await screen.findByText(/not configured \(Settings, language model\)/)).toBeInTheDocument();
  });

  it("starts the classifier with the selected split and sample size", async () => {
    show();
    await screen.findByText("qwen2.5-coder-7b");
    await pickOption("Split", "train");
    fireEvent.change(screen.getByLabelText("Samples"), { target: { value: "120" } });
    expect(screen.queryByRole("switch", { name: "Retrieval augmentation" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Run classification" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("classify", { split: "train", n: 120 }));
  });

  it("validates the detector threshold and starts with the selected choices", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j2", stage: "detect", status: "running", logs: [], progress: null } as never);
    show();
    await screen.findByText("qwen2.5-coder-7b");
    fireEvent.click(screen.getByRole("radio", { name: "Is this code vulnerable?" }));
    expect(screen.queryByRole("switch", { name: "Retrieval augmentation" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Threshold"), { target: { value: "1.5" } });
    expect(screen.getByRole("alert")).toHaveTextContent("a probability from 0 to 1");
    expect(screen.getByRole("button", { name: "Run detection" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Threshold"), { target: { value: "0.35" } });
    fireEvent.click(screen.getByRole("button", { name: "Run detection" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("detect", { split: "val", n: 50, threshold: 0.35 }));
  });

  it("lists only the earlier runs of the chosen kind that the shared evaluator also scored, with their numbers", async () => {
    show();
    const list = await screen.findByRole("list", { name: "Earlier evaluations" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveTextContent("old-clf");
    expect(items[0]).toHaveTextContent("accuracy 62.0%, macro F1 0.55, Critical missed 3 of 10, answered 98.0%");
    fireEvent.click(screen.getByRole("radio", { name: "Is this code vulnerable?" }));
    const detector = within(screen.getByRole("list", { name: "Earlier evaluations" })).getAllByRole("listitem");
    expect(detector).toHaveLength(1);
    expect(detector[0]).toHaveTextContent("det-1");
    expect(detector[0]).toHaveTextContent("recall 70.0%, false alarms on safe code 20.0%, answered 100.0%");
    expect(screen.getByRole("link", { name: "Open the Results tab" })).toHaveAttribute("href", "/train?tab=results");
  });

  it("says that none exists yet for a kind without runs", async () => {
    vi.mocked(api.experiments).mockResolvedValue([]);
    show();
    expect(await screen.findByText(/No run of this kind yet/)).toBeInTheDocument();
  });
});
