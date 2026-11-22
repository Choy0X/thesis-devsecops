import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { runCurves: vi.fn() } };
});
import { api, type CurveSource } from "../lib/api";
import { LIVE_MS, RunCurvesView, points, seriesFor, validationSeries } from "./RunCurvesView";

const src = (over: Partial<CurveSource> = {}): CurveSource => ({ label: "run", rows: 3, skipped: 0, steps: [1, 2, 3], loss: [1.5, null, 1.0], grad_norm: [3, 2, 1],
  fields: { decision: [2, 1.8, 1.6], structure_eos: [0.6, 0.5, 0.4] }, ...over });

beforeEach(() => vi.mocked(api.runCurves).mockReset());

describe("points", () => {
  it("leaves a gap out instead of drawing a zero", () => {
    expect(points([1, 2, 3], [1.5, null, 1.0])).toEqual([{ x: 1, y: 1.5 }, { x: 3, y: 1.0 }]);
    expect(points([1], undefined)).toEqual([]);
  });
  it("names the series after the part of the answer and prefixes a local check", () => {
    const s = seriesFor(src({ label: "smoke-1" }));
    expect(s.loss[0].name).toBe("smoke-1: loss");
    expect(s.fields.map((f) => f.name)).toEqual(["smoke-1: decision", "smoke-1: structure eos"]);
    expect(seriesFor(src({ loss: undefined })).loss).toEqual([]);
  });
});

describe("validationSeries", () => {
  it("is the validation loss by step and leaves out a point without one", () => {
    const ev = (step: number, val_loss: number | null) => ({ step, pass: 1, family_pass: 1, metric: -(val_loss ?? 0), metric_name: "negative_validation_loss", val_loss, n_examples: 8 });
    expect(validationSeries(src({ evaluations: [ev(4, 0.9), ev(8, null), ev(12, 0.7)] }))).toEqual([{ name: "validation loss", points: [{ x: 4, y: 0.9 }, { x: 12, y: 0.7 }] }]);
    expect(validationSeries(src())).toEqual([]);
    expect(validationSeries(src({ evaluations: [] }))).toEqual([]);
  });
});

describe("RunCurvesView", () => {
  it("shows imported analytics and provides the complete JSON download", async () => {
    vi.mocked(api.runCurves).mockResolvedValue({ sources: [src({ label: "Import abc", analytics: {
      schema: "training-analytics/1", import_id: "abcdef0123456789", run: { state: "finished" },
      summary: { committed_steps: 3, committed_tokens: 128, completeness: "recorded_history" },
    } })] });
    render(<RunCurvesView name="r1" />);
    const link = await screen.findByRole("link", { name: "Download complete analytics JSON" });
    expect(link).toHaveAttribute("href", "/api/finetune/runs/r1/imports/abcdef0123456789/analytics");
    expect(screen.getByText(/128 supervised tokens/)).toBeInTheDocument();
  });
  it("draws the validation loss and lists the evaluations with their metric and the examples scored", async () => {
    vi.mocked(api.runCurves).mockResolvedValue({ sources: [src({ evaluations: [{ step: 4, pass: 0.5, family_pass: 0.598, metric: -0.7466, metric_name: "negative_validation_loss", val_loss: 0.7466, n_examples: 8 },
      { step: 8, pass: 1, family_pass: 1.2, metric: -0.61, metric_name: "negative_validation_loss", val_loss: 0.61, n_examples: 8 }] })] });
    render(<RunCurvesView name="r1" />);
    expect(await screen.findByText("Validation loss")).toBeInTheDocument();
    const rows = screen.getByRole("table", { name: "Evaluations of run" }).querySelectorAll("tbody tr");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toBe("40.60negative validation loss-0.74668");
  });

  it("draws the loss, the loss by part and the size of the updates, saying how many steps were logged", async () => {
    vi.mocked(api.runCurves).mockResolvedValue({ sources: [src({ skipped: 2 })] });
    render(<RunCurvesView name="r1" />);
    expect(await screen.findByText("The run: 3 steps logged, 2 lines could not be read.")).toBeInTheDocument();
    expect(screen.getByText("Loss")).toBeInTheDocument();
    expect(screen.getByText("Loss by part of the answer")).toBeInTheDocument();
    expect(screen.getByText("Size of the updates")).toBeInTheDocument();
    expect(api.runCurves).toHaveBeenCalledWith("r1");
  });

  it("says a run without a log has no curves yet", async () => {
    vi.mocked(api.runCurves).mockResolvedValue({ sources: [] });
    render(<RunCurvesView name="r1" />);
    expect(await screen.findByText(/no training log yet/)).toBeInTheDocument();
  });

  it("names a local check separately from the run", async () => {
    vi.mocked(api.runCurves).mockResolvedValue({ sources: [src(), src({ label: "smoke-1" })] });
    render(<RunCurvesView name="r1" />);
    expect(await screen.findByText("Local check smoke-1: 3 steps logged.")).toBeInTheDocument();
  });

  it("reads the curves again while a local training runs, keeps them when a refresh fails, and stops when it ends", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.runCurves).mockResolvedValue({ sources: [src()] });
      const view = render(<RunCurvesView name="r1" live />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(api.runCurves).toHaveBeenCalledTimes(1);
      vi.mocked(api.runCurves).mockImplementation(() => Promise.reject(new Error("busy")));
      await act(async () => { await vi.advanceTimersByTimeAsync(LIVE_MS); });
      expect(api.runCurves).toHaveBeenCalledTimes(2);
      expect(screen.getByText("The run: 3 steps logged.")).toBeInTheDocument();                       // the last curves stay
      vi.mocked(api.runCurves).mockResolvedValue({ sources: [src({ rows: 9 })] });
      await act(async () => { await vi.advanceTimersByTimeAsync(LIVE_MS); });
      expect(screen.getByText("The run: 9 steps logged.")).toBeInTheDocument();
      view.rerender(<RunCurvesView name="r1" live={false} />);                                         // the training ended: one last read, then no more
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const calls = vi.mocked(api.runCurves).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(LIVE_MS * 3); });
      expect(vi.mocked(api.runCurves).mock.calls.length).toBe(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it("names a local training as a training, not as a check", async () => {
    vi.mocked(api.runCurves).mockResolvedValue({ sources: [src({ label: "train-Qwen-Qwen2.5-Coder-0.5B-c357a5d0" })] });
    render(<RunCurvesView name="r1" />);
    expect(await screen.findByText("Local training train-Qwen-Qwen2.5-Coder-0.5B-c357a5d0: 3 steps logged.")).toBeInTheDocument();
  });
});
