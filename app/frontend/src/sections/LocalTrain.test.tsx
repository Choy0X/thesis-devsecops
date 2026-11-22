import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { localFit: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { api, type LocalFit, type LocalGpuReport } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { LocalTrain, reportLine } from "./LocalTrain";

const BASE = "Qwen/Qwen2.5-Coder-7B";
const SMALL = "Qwen/Qwen2.5-Coder-0.5B";
const gate = (over = {}) => ({ verdict: "cannot_fit", refuse: true, model: BASE, context: 4096, micro_batch: 1, rank: 32,
  message: "Qwen/Qwen2.5-Coder-7B needs at least 5.9 GB of video memory for its weights and adapters, and 4.9 GB is free: it cannot run on this card. Train it on Colab or Kaggle with the notebook, or choose a smaller model.", ...over });
const fitOf = (over: Partial<LocalFit> = {}): LocalFit => ({ available: true, pinned_base: BASE, default_model: SMALL, running: false, model: BASE, is_pinned_base: true,
  gpu: { name: "NVIDIA GeForce GTX 1660", total_mb: 6144, free_mb: 5000 }, gate: gate() as LocalFit["gate"], ...over });
const report = (over: Partial<LocalGpuReport> = {}): LocalGpuReport => ({ kind: "train", model: SMALL, passed: true, gpu: { name: "g" }, folder: "f", state: "finished", complete: true,
  steps_done: 6, steps: 6, seconds: 90, pinned_base: false, ...over });

const show = (last?: LocalGpuReport | null, onDone = () => undefined) => render(<ConfirmProvider><LocalTrain run="r1" last={last} onDone={onDone} /></ConfirmProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.localFit).mockImplementation(async (_run, model) => (model
    ? fitOf({ model, is_pinned_base: false, gate: gate({ verdict: "too_big", refuse: false, model, message: "The estimate at the full context of 4096 tokens is 5.3 GB, more than the 4.9 GB that is free; views shorter than the context need less, and the run stops with an out-of-memory message if one is too long." }) as LocalFit["gate"] })
    : fitOf()));
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "train_local", status: "running", logs: [], progress: null } as any);
  vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "train_local", status: "running", logs: [], progress: null } as any);
  class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
  vi.stubGlobal("WebSocket", Quiet);
});
afterEach(() => vi.unstubAllGlobals());

describe("reportLine", () => {
  it("says in words how the last local training ended and what happens next", () => {
    expect(reportLine(report())).toMatchObject({ tone: "text-ok", text: expect.stringContaining("Finished at step 6 of 6") });
    expect(reportLine(report()).text).toContain("not a result");
    expect(reportLine(report({ pinned_base: true })).text).not.toContain("not a result");
    expect(reportLine(report({ state: "interrupted", complete: false, steps_done: 3, interrupted_by: "session_budget" })).text).toMatch(/Stopped at step 3 of 6 on its session budget\. A checkpoint was written/);
    expect(reportLine(report({ state: "stopped", passed: false, steps_done: 2 })).text).toMatch(/cancelled, or the app was closed.*last checkpoint/);
    expect(reportLine(report({ state: "failed", passed: false })).tone).toBe("text-crit");
    expect(reportLine(report({ state: "running", steps: null })).text).toBe("Running, step 6.");
  });
});

describe("LocalTrain", () => {
  it('offers migration and history without probing or launching desktop training', () => {
    show(report());
    expect(screen.getByText(/Desktop training is retired/i)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(api.localFit).not.toHaveBeenCalled();
    expect(api.startJob).not.toHaveBeenCalled();
  });
});
