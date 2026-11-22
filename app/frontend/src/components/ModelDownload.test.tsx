import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/apiError";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: () => "ws://test" };
});
import { api, type Job } from "../lib/api";
import { useOnChanged } from "../lib/changes";
import { resetJobsList } from "../lib/jobsList";
import { resetStreams } from "../lib/jobStreams";
import { ConfirmProvider } from "./hud/AlertDialog";
import { ModelDownload } from "./ModelDownload";

const MB = 1024 ** 2;
class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }       // a socket that never opens: the console polls

const job = (over: Partial<Job> = {}): Job => ({ id: "d1", stage: "hf_download", status: "running", params: { repo_id: "org/m", quant: "" }, started: new Date().toISOString(), finished: "", error: "", log_count: 0, logs: [], progress: null, ...over } as Job);
const view = (props: Partial<React.ComponentProps<typeof ModelDownload>> = {}) => render(
  <ConfirmProvider><ModelDownload repoId="org/m" quant="" label="Download" hint="Stop keeps what is already downloaded." {...props} /></ConfirmProvider>);

beforeEach(() => {
  resetJobsList();
  resetStreams();
  vi.mocked(api.listJobs).mockReset().mockResolvedValue([]);
  vi.mocked(api.startJob).mockReset();
  vi.mocked(api.getJob).mockReset();
  vi.mocked(api.cancelJob).mockReset().mockResolvedValue({ ok: true } as any);
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => vi.unstubAllGlobals());

describe("ModelDownload", () => {
  it("shows only the button and its hint before anything has run, and says that nothing is active", () => {
    const active = vi.fn();
    view({ onActive: active });
    expect(screen.getByRole("button", { name: "Download" })).toBeEnabled();
    expect(screen.getByText("Stop keeps what is already downloaded.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Download of/)).toBeNull();
    expect(screen.queryByRole("log")).toBeNull();
    expect(active).toHaveBeenLastCalledWith(false);
  });

  it("does not start while disabled", () => {
    view({ disabled: true });
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("asks first and starts nothing when the person says no", async () => {
    view({ confirmText: "This downloads 4 files, 14 GB." });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("This downloads 4 files, 14 GB.");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.startJob).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/Download of/)).toBeNull();
  });

  it("starts the download of this model with the chosen quantisation, shows the panel and the terminal, and says a download is active", async () => {
    vi.mocked(api.startJob).mockResolvedValue(job() as any);
    vi.mocked(api.getJob).mockResolvedValue(job({ progress: { state: "downloading", unit: "bytes", processed: 2 * MB, total: 8 * MB, rate: MB, eta: 6, files: 1, files_total: 4 }, logs: ["Model     org/m", "Plan      4 files, 8.0 MB"], log_start: 0 } as any) as any);
    const active = vi.fn();
    view({ quant: "Q4_K_M", onActive: active });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(api.startJob).toHaveBeenCalledWith("hf_download", { repo_id: "org/m", quant: "Q4_K_M" });
    expect(await screen.findByLabelText("Download of org/m")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(screen.queryByText("Stop keeps what is already downloaded.")).toBeNull();                 // the hint is for before anything has run
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    expect(await screen.findByText("1 of 4 files", {}, { timeout: 4000 })).toBeInTheDocument();
    const log = await screen.findByRole("log", { name: "Output of Download a model" });
    await waitFor(() => expect(log.textContent).toContain("Plan      4 files, 8.0 MB"), { timeout: 4000 });
  });

  it("stops the download that runs", async () => {
    vi.mocked(api.startJob).mockResolvedValue(job() as any);
    vi.mocked(api.getJob).mockResolvedValue(job() as any);
    view();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith("d1"));
  });

  it("offers to continue after a stop, in the one button, and says what was kept", async () => {
    vi.mocked(api.startJob).mockResolvedValue(job() as any);
    vi.mocked(api.getJob).mockResolvedValue(job({ status: "cancelled", progress: { state: "downloading", processed: MB, total: 8 * MB } } as any) as any);
    view();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByText("Stopped", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue the download" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.getAllByRole("button", { name: /Continue the download/ })).toHaveLength(1);
    expect(screen.getByText(/What was fetched is kept/)).toBeInTheDocument();
  });

  it("says it is finished, with what arrived, and keeps the terminal", async () => {
    vi.mocked(api.startJob).mockResolvedValue(job() as any);
    vi.mocked(api.getJob).mockResolvedValue(job({ status: "done", logs: ["Finished  8.0 MB in 4.0 s"], log_start: 0, progress: { state: "done", unit: "bytes", processed: 8 * MB, total: 8 * MB, files: 4, files_total: 4, seconds: 4 } } as any) as any);
    view();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByText("Finished", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText("8.0 MB in 4 files, took 4.0 s (2.0 MB/s).")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("log").textContent).toContain("Finished  8.0 MB in 4.0 s"));
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download" })).toBeNull();
  });

  it("stops offering to start again once the job is done: pressing it again would repeat work already finished", async () => {
    vi.mocked(api.startJob).mockResolvedValue(job() as any);
    vi.mocked(api.getJob).mockResolvedValue(job({ status: "done", logs: ["Finished  8.0 MB in 4.0 s"], log_start: 0, progress: { state: "done", unit: "bytes", processed: 8 * MB, total: 8 * MB, files: 4, files_total: 4, seconds: 4 } } as any) as any);
    view();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Done", {}, { timeout: 4000 });
    expect(screen.queryByRole("button", { name: "Download" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Continue the download" })).toBeNull();
    expect(api.startJob).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Done").closest("button")).toBeNull();          // a plain indicator, not a button that can be clicked
  });

  it("says why a start was refused, in the panel", async () => {
    vi.mocked(api.startJob).mockRejectedValue(new ApiError({ status: 409, title: "too many jobs are running", hint: "Wait for one to finish." }));
    const active = vi.fn();
    view({ onActive: active });
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("too many jobs are running");
    expect(screen.getByText("Wait for one to finish.")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
  });

  it("says why a download failed, in words", async () => {
    vi.mocked(api.startJob).mockResolvedValue(job() as any);
    vi.mocked(api.getJob).mockResolvedValue(job({ status: "error", error: "not enough disk space: 3.0 GB are needed", logs: ["Model     org/m"], log_start: 0 } as any) as any);
    view();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByRole("alert", {}, { timeout: 4000 })).toHaveTextContent("not enough disk space: 3.0 GB are needed");
  });

  it("follows a download of this model that was already running, and says so to the dialog", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job({ id: "run1", params: { repo_id: "org/m", quant: "" } })]);
    vi.mocked(api.getJob).mockResolvedValue(job({ id: "run1", progress: { state: "downloading", unit: "bytes", processed: MB, total: 4 * MB, files: 2, files_total: 3 } } as any) as any);
    const active = vi.fn();
    view({ onActive: active });
    expect(await screen.findByLabelText("Download of org/m", {}, { timeout: 4000 })).toBeInTheDocument();
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(api.startJob).not.toHaveBeenCalled();
  });

  it("does not take a download of another model for its own", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job({ id: "other", params: { repo_id: "org/other", quant: "" } })]);
    const active = vi.fn();
    view({ onActive: active });
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    expect(screen.queryByLabelText(/Download of/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(active).not.toHaveBeenCalledWith(true);
    expect(api.getJob).not.toHaveBeenCalled();
  });
});

describe("ModelDownload as an install", () => {
  const installJob = (over: Partial<Job> = {}): Job => job({ id: "i1", stage: "install_model", params: { repo_id: "org/m", quant: "Q4_K_M", role: "chat" }, ...over });
  const installView = (props: Partial<React.ComponentProps<typeof ModelDownload>> = {}) => view({ stage: "install_model", quant: "Q4_K_M", label: "Install", role: "chat", extra: { role: "chat" }, ...props });

  it("starts the install job with the quantisation and what is added, and shows the panel of an install", async () => {
    vi.mocked(api.startJob).mockResolvedValue(installJob() as any);
    vi.mocked(api.getJob).mockResolvedValue(installJob({ progress: { state: "converting", label: "Converting org/m to Q4_K_M", unit: "percent", processed: 20, total: 100, step: 3, steps: 3, phase: "convert" } } as any) as any);
    installView({ extra: { role: "chat", source: "hf", skip_download: true } });
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(api.startJob).toHaveBeenCalledWith("install_model", { repo_id: "org/m", quant: "Q4_K_M", role: "chat", source: "hf", skip_download: true });
    expect(await screen.findByLabelText("Install of org/m")).toBeInTheDocument();
    expect(await screen.findByText("Converting", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole("log", { name: "Output of Install a model" })).toBeInTheDocument();
  });

  it("says ready to run, as what, when the install is over", async () => {
    vi.mocked(api.startJob).mockResolvedValue(installJob() as any);
    vi.mocked(api.getJob).mockResolvedValue(installJob({ status: "done", logs: ["Finished"], log_start: 0, progress: { state: "done", unit: "percent", processed: 100, total: 100, phase: "convert" } } as any) as any);
    installView();
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(await screen.findByText("Ready to run", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText(/ready to run as a language model/)).toBeInTheDocument();
    // A second Install would fail outright: the converted file is already there and is never overwritten.
    expect(screen.queryByRole("button", { name: "Install" })).toBeNull();
  });

  it("offers to continue the install, not the download, after a stop", async () => {
    vi.mocked(api.startJob).mockResolvedValue(installJob() as any);
    vi.mocked(api.getJob).mockResolvedValue(installJob({ status: "cancelled", progress: { state: "downloading", processed: MB, total: 8 * MB, phase: "download", step: 2, steps: 3 } } as any) as any);
    installView();
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(await screen.findByRole("button", { name: "Continue the install" }, { timeout: 4000 })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Continue the download" })).toBeNull();
  });

  it("follows an install of this model that was already running, and a plain download of it too", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([installJob({ id: "run2" })]);
    vi.mocked(api.getJob).mockResolvedValue(installJob({ id: "run2", progress: { state: "downloading", unit: "bytes", processed: MB, total: 4 * MB, phase: "download", step: 2, steps: 3 } } as any) as any);
    const active = vi.fn();
    installView({ onActive: active });
    expect(await screen.findByLabelText("Install of org/m", {}, { timeout: 4000 })).toBeInTheDocument();
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    expect(api.startJob).not.toHaveBeenCalled();
  });

  it("treats a job that reports steps as an install even when the dialog started a plain download", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([installJob({ id: "run3" })]);
    vi.mocked(api.getJob).mockResolvedValue(installJob({ id: "run3", progress: { state: "converting", unit: "percent", processed: 5, total: 100, phase: "convert", step: 3, steps: 3 } } as any) as any);
    view();
    expect(await screen.findByLabelText("Install of org/m", {}, { timeout: 4000 })).toBeInTheDocument();
  });
});

describe("ModelDownload tells the app when its job ends", () => {
  it("asks the jobs list at once, so that a list behind the dialog changes with the panel and does not wait for the next poll", async () => {
    const finished = job({ id: "i9", stage: "install_model", status: "done", params: { repo_id: "org/m", quant: "Q4_K_M" }, logs: ["Ready"], log_start: 0,
      progress: { state: "done", unit: "percent", processed: 100, total: 100, phase: "convert", step: 3, steps: 3 } } as any);
    let over = false;
    vi.mocked(api.startJob).mockResolvedValue(job({ id: "i9", stage: "install_model", params: { repo_id: "org/m", quant: "Q4_K_M" } }) as any);
    vi.mocked(api.getJob).mockImplementation(async () => { over = true; return finished as any; });
    vi.mocked(api.listJobs).mockImplementation(async () => (over ? [finished] : []));
    const heard = vi.fn();
    function Spy() { useOnChanged(["models"], heard); return null; }
    render(<ConfirmProvider><Spy /><ModelDownload repoId="org/m" quant="Q4_K_M" label="Install" stage="install_model" /></ConfirmProvider>);
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(heard).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(await screen.findByText("Ready to run", {}, { timeout: 4000 })).toBeInTheDocument();
    await waitFor(() => expect(heard).toHaveBeenCalled(), { timeout: 1500 });                          // shorter than the 3 s of a poll
  });

  it("does the same when the job it follows fails or is stopped, and says nothing when no job ever ran", async () => {
    vi.mocked(api.startJob).mockResolvedValue(job({ id: "i8" }) as any);
    vi.mocked(api.getJob).mockResolvedValue(job({ id: "i8", status: "cancelled" }) as any);
    const callsBefore = () => vi.mocked(api.listJobs).mock.calls.length;
    view();
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    const idleCalls = callsBefore();
    await act(async () => { await new Promise((r) => setTimeout(r, 200)); });
    expect(callsBefore()).toBe(idleCalls);                                                              // an idle dialog asks nothing more
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByText("Stopped", {}, { timeout: 4000 })).toBeInTheDocument();
    const afterStop = callsBefore();
    await waitFor(() => expect(callsBefore()).toBeGreaterThan(afterStop - 1));
    expect(afterStop).toBeGreaterThanOrEqual(idleCalls + 2);                                            // one when the job started, one when it ended
  });
});
