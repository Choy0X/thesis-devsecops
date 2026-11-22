import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { computeChoices: vi.fn(), getSettings: vi.fn(), putSettings: vi.fn(), startJob: vi.fn(), listJobs: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { api, type ComputeChoice, type ComputeChoices, type Settings } from "../lib/api";
import { ComputeDialog } from "./ComputeDialog";
import { PerformancePanel } from "./PerformancePanel";
import { RunConsole } from "./RunConsole";
import { ToastProvider } from "./Toast";
import { resetStreams } from "../lib/jobStreams";
import { rememberedOf, rememberedText } from "../lib/compute";

class InertSocket { onopen: any; onmessage: any; onclose: any; onerror: any; constructor(public url: string) {} close() {} send() {} }

const choice = (device: ComputeChoice["device"], over: Partial<ComputeChoice> = {}): ComputeChoice => ({
  device, label: { cpu: "Processor", gpu: "Graphics card", hybrid: "Processor and card" }[device], available: true, reason: null, means: `means ${device}`,
  estimate: { vram_gb: null, ram_gb: null, basis: null, time: null }, ...over });
const data = (over: Partial<ComputeChoices> = {}): ComputeChoices => ({ stage: "gpu_smoke", heavy: true, kind: "smoke", workers: 5, remembered: null, choices: [
  choice("cpu", { estimate: { vram_gb: null, ram_gb: 2.8, basis: "The weights in 32 bits.", time: { runs: 2, median_seconds: 600, last_seconds: 660 } } }),
  choice("gpu", { estimate: { vram_gb: 1.4, ram_gb: null, basis: "From the model's configuration.", time: null } }),
  choice("hybrid", { available: false, reason: "One step cannot be shared.", means: null })], ...over });

describe("ComputeDialog", () => {
  it("shows all three choices, the reason for the one that is not available, and estimates that say what they are", () => {
    render(<ComputeDialog data={data()} initial={null} onStart={vi.fn()} onCancel={vi.fn()} />);
    const group = screen.getByRole("radiogroup", { name: "Device" });
    expect(within(group).getAllByRole("radio")).toHaveLength(3);
    expect(within(group).getByLabelText("Processor and card")).toBeDisabled();
    expect(screen.getByText(/Not available: One step cannot be shared\./)).toBeInTheDocument();
    expect(screen.getByText(/Memory \(an estimate\): 2.8 GB in memory/)).toBeInTheDocument();
    expect(screen.getByText(/Memory \(an estimate\): 1.4 GB on the card/)).toBeInTheDocument();
    expect(screen.getByText(/about 10.0 min on 2 earlier runs here \(the last took 11.0 min\)/)).toBeInTheDocument();
    expect(screen.getByText(/Not a prediction for this run/)).toBeInTheDocument();
    expect(screen.getByText(/not measured yet on this device, so no time is given/)).toBeInTheDocument();
    expect(screen.getByText(/nothing here says that one choice is faster than another/)).toBeInTheDocument();
    expect(screen.getByText(/up to 5 worker processes/)).toBeInTheDocument();
  });

  it("needs a choice before it starts, starts on the chosen device and passes whether to remember it", () => {
    const onStart = vi.fn();
    render(<ComputeDialog data={data()} initial={null} onStart={onStart} onCancel={vi.fn()} />);
    const go = screen.getByRole("button", { name: "Choose a device" });
    expect(go).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Graphics card"));
    fireEvent.click(screen.getByLabelText("Remember for this job type"));
    fireEvent.click(screen.getByRole("button", { name: "Start on the graphics card" }));
    expect(onStart).toHaveBeenCalledWith("gpu", true);
  });

  it("preselects the one choice that exists and can be cancelled", () => {
    const onCancel = vi.fn();
    render(<ComputeDialog data={data()} initial="cpu" onStart={vi.fn()} onCancel={onCancel} />);
    expect(screen.getByLabelText("Processor")).toBeChecked();
    expect(screen.getByRole("button", { name: "Start on the processor" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
  });
});

describe("RunConsole before a job that has a choice of device", () => {
  const settings = (remember = "") => ({ compute_remember: remember } as unknown as Settings);
  beforeEach(() => {
    vi.stubGlobal("WebSocket", InertSocket);
    resetStreams();
    for (const fn of Object.values(api)) (fn as any).mockReset?.();
    vi.mocked(api.listJobs).mockResolvedValue([]);
    vi.mocked(api.computeChoices).mockResolvedValue(data());
    vi.mocked(api.getSettings).mockResolvedValue(settings());
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "gpu_smoke", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "gpu_smoke", status: "running", logs: [], progress: null } as any);
  });
  const run = () => { render(<RunConsole stage="gpu_smoke" label="Run a smoke" params={{ run: "r1" }} />); fireEvent.click(screen.getByRole("button", { name: "Run a smoke" })); };

  it("asks where to run, and starts the job with the device that was chosen", async () => {
    run();
    await screen.findByRole("radiogroup", { name: "Device" });
    expect(api.computeChoices).toHaveBeenCalledWith("gpu_smoke", "r1", undefined);
    expect(api.startJob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Processor"));
    fireEvent.click(screen.getByRole("button", { name: "Start on the processor" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("gpu_smoke", { run: "r1", device: "cpu" }));
    expect(api.putSettings).not.toHaveBeenCalled();
  });

  it("remembers the choice when asked, next to the ones already remembered", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(settings(JSON.stringify({ gpu_eval: "cpu" })));
    vi.mocked(api.putSettings).mockResolvedValue(settings());
    run();
    await screen.findByRole("radiogroup", { name: "Device" });
    fireEvent.click(screen.getByLabelText("Graphics card"));
    fireEvent.click(screen.getByLabelText("Remember for this job type"));
    fireEvent.click(screen.getByRole("button", { name: "Start on the graphics card" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ compute_remember: JSON.stringify({ gpu_eval: "cpu", gpu_smoke: "gpu" }) }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("gpu_smoke", { run: "r1", device: "gpu" }));
  });

  it("does not ask when a remembered choice is available, and asks again when it no longer is", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(settings(JSON.stringify({ gpu_smoke: "gpu" })));
    run();
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("gpu_smoke", { run: "r1", device: "gpu" }));
    expect(screen.queryByRole("radiogroup", { name: "Device" })).toBeNull();
  });

  it("asks again when the remembered device cannot be used now", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(settings(JSON.stringify({ gpu_smoke: "gpu" })));
    vi.mocked(api.computeChoices).mockResolvedValue(data({ choices: [choice("cpu"), choice("gpu", { available: false, reason: "The graphics card is in use by the local model server." }), choice("hybrid", { available: false, reason: "no" })] }));
    run();
    await screen.findByRole("radiogroup", { name: "Device" });
    expect(screen.getByText(/The graphics card is in use by the local model server/)).toBeInTheDocument();
    expect(api.startJob).not.toHaveBeenCalled();
  });

  it("starts nothing when the dialog is cancelled", async () => {
    run();
    await screen.findByRole("radiogroup", { name: "Device" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("radiogroup", { name: "Device" })).toBeNull());
    expect(api.startJob).not.toHaveBeenCalled();
  });

  it("starts the job as before, with no device, when the choices cannot be read", async () => {
    vi.mocked(api.computeChoices).mockRejectedValue(new Error("offline"));
    run();
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("gpu_smoke", { run: "r1" }));
  });

  it("does not ask for a job that has no choice of device", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j2", stage: "campaign", status: "running", logs: [], progress: null } as any);
    render(<RunConsole stage="campaign" label="Start" />);
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalled());
    expect(api.computeChoices).not.toHaveBeenCalled();
  });
});

afterEach(() => { vi.unstubAllGlobals(); resetStreams(); });

describe("remembered choices", () => {
  it("are read leniently and written without anything that is not a device", () => {
    expect(rememberedOf(JSON.stringify({ a: "cpu", b: "tpu", c: "gpu" }))).toEqual({ a: "cpu", c: "gpu" });
    expect(rememberedOf("")).toEqual({}); expect(rememberedOf("{broken")).toEqual({}); expect(rememberedOf("[1]")).toEqual({}); expect(rememberedOf(undefined)).toEqual({});
    expect(rememberedText({})).toBe(""); expect(rememberedText({ gpu_eval: "cpu" })).toBe('{"gpu_eval":"cpu"}');
  });

  it("are listed in Settings, Performance, and forgotten one at a time", async () => {
    const s = { compute_gpu: "", ui_gpu: "", max_jobs: "", compute_remember: JSON.stringify({ gpu_eval: "cpu", gpu_smoke: "gpu" }) } as unknown as Settings;
    const updated = vi.fn();
    vi.mocked(api.putSettings).mockResolvedValue({ ...s, compute_remember: JSON.stringify({ gpu_smoke: "gpu" }) });
    render(<ToastProvider><PerformancePanel s={s} onUpdated={updated} /></ToastProvider>);
    const list = screen.getByLabelText("Remembered compute choices");
    expect(within(list).getByText(/GPU evaluation:/)).toHaveTextContent("processor");
    expect(within(list).getByText(/GPU check:/)).toHaveTextContent("graphics card");
    fireEvent.click(screen.getByRole("button", { name: "Forget the choice for GPU evaluation" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ compute_remember: JSON.stringify({ gpu_smoke: "gpu" }) }));
    await waitFor(() => expect(updated).toHaveBeenCalled());
  });

  it("say that nothing is remembered when nothing is", () => {
    render(<ToastProvider><PerformancePanel s={{ compute_gpu: "", ui_gpu: "", max_jobs: "", compute_remember: "" } as unknown as Settings} onUpdated={vi.fn()} /></ToastProvider>);
    expect(screen.getByText(/Nothing is remembered: every such job asks\./)).toBeInTheDocument();
  });
});
