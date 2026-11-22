import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/apiError";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: () => "ws://test" };
});
import { api } from "../lib/api";
import { RunConsole } from "./RunConsole";

describe("RunConsole when a job cannot start", () => {
  beforeEach(() => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
  });

  it("shows why instead of waiting for output forever", async () => {
    vi.mocked(api.startJob).mockRejectedValue(new ApiError({ status: 409, title: "a campaign is already running", hint: "Pause it first." }));
    render(<RunConsole stage="campaign" label="Start" />);
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("a campaign is already running"));
    expect(screen.getByText("Pause it first.")).toBeInTheDocument();
    expect(screen.queryByText(/waiting for output/)).toBeNull();
  });

  it("shows a failed run's reason too", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "campaign", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "campaign", status: "error", error: "SystemExit: 1", logs: ["it broke"], progress: null } as any);
    class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }     // a socket that never opens
    vi.stubGlobal("WebSocket", FakeSocket);
    render(<RunConsole stage="campaign" label="Start" />);
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("SystemExit: 1"), { timeout: 4000 });
    vi.unstubAllGlobals();
  });

  it("starts through the launch route when one is given, so that route's refusal is shown", async () => {
    const launch = vi.fn().mockRejectedValue(new ApiError({ status: 400, title: "unknown ecosystem: Fortran" }));
    vi.mocked(api.startJob).mockClear();
    render(<RunConsole stage="campaign" label="Start" params={{ ecosystems: ["Fortran"] }} launch={launch} />);
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("unknown ecosystem: Fortran"));
    expect(launch).toHaveBeenCalledWith({ ecosystems: ["Fortran"] });
    expect(api.startJob).not.toHaveBeenCalled();
  });
});

describe("RunConsole progress", () => {
  it("shows a download in sizes with its speed, the time left and which step of the job it is", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    const progress = { state: "downloading", phase: "download", step: 1, steps: 2, unit: "bytes", label: "Downloading the npm dump", processed: 5 * 1024 ** 2, total: 20 * 1024 ** 2, rate: 1024 ** 2, eta: 15 };
    vi.mocked(api.startJob).mockResolvedValue({ id: "j2", stage: "osv_install", status: "running", logs: [], progress } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j2", stage: "osv_install", status: "running", logs: [], progress } as any);
    class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }     // a socket that never opens: the console polls
    vi.stubGlobal("WebSocket", FakeSocket);
    render(<RunConsole stage="osv_install" label="Download" />);
    fireEvent.click(screen.getByRole("button", { name: /Download/ }));
    expect(await screen.findByText("Step 1 of 2", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText("Downloading the npm dump")).toBeInTheDocument();
    expect(screen.getByText(/5\.0 MB \/ 20\.0 MB/)).toBeInTheDocument();
    expect(screen.getByText(/25\.0%/)).toBeInTheDocument();
    expect(screen.getByText("1.0 MB/s · 0:15 left")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("shows files read as a count with the word, and only what is done so far when the total is not known", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    const progress = { state: "reading", phase: "read", step: 2, steps: 2, unit: "files", label: "Reading the npm dump", processed: 1200 };
    vi.mocked(api.startJob).mockResolvedValue({ id: "j3", stage: "osv_install", status: "running", logs: [], progress } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j3", stage: "osv_install", status: "running", logs: [], progress } as any);
    class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }     // a socket that never opens: the console polls
    vi.stubGlobal("WebSocket", FakeSocket);
    render(<RunConsole stage="osv_install" label="Read" />);
    fireEvent.click(screen.getByRole("button", { name: /Read/ }));
    expect(await screen.findByText("1,200 files so far", {}, { timeout: 4000 })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});

describe("RunConsole oneShot", () => {
  it("replaces the start button with Done once a oneShot run finishes, instead of inviting it to be repeated", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    vi.mocked(api.startJob).mockClear().mockResolvedValue({ id: "j6", stage: "osv_install", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j6", stage: "osv_install", status: "done", error: "", logs: ["finished"], log_start: 0, progress: null } as any);
    class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }
    vi.stubGlobal("WebSocket", FakeSocket);
    render(<RunConsole stage="osv_install" label="Update" oneShot />);
    fireEvent.click(screen.getByRole("button", { name: /Update/ }));
    expect(await screen.findByText("Done", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Update/ })).toBeNull();
    expect(api.startJob).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("stays repeatable, as before, when oneShot is not given", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    vi.mocked(api.startJob).mockClear().mockResolvedValue({ id: "j7", stage: "campaign", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j7", stage: "campaign", status: "done", error: "", logs: ["finished"], log_start: 0, progress: null } as any);
    class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }
    vi.stubGlobal("WebSocket", FakeSocket);
    render(<RunConsole stage="campaign" label="Start" />);
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start" })).toBeEnabled(), { timeout: 4000 });
    expect(screen.queryByText("Done")).toBeNull();
    expect(api.startJob).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});

describe("RunConsole output", () => {
  it("draws what the job printed the way a terminal shows it: a redraw is one row, colours are kept and the header says what runs", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    const logs = ["Model     org/m", "bar  10%\r", "bar 100%", "\x1b[32mFinished\x1b[0m  6.2 MB"];
    vi.mocked(api.startJob).mockResolvedValue({ id: "j4", stage: "hf_download", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j4", stage: "hf_download", status: "done", error: "", logs, log_start: 0, progress: null } as any);
    class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }     // a socket that never opens: the console polls
    vi.stubGlobal("WebSocket", FakeSocket);
    render(<RunConsole stage="hf_download" label="Download" />);
    fireEvent.click(screen.getByRole("button", { name: /Download/ }));
    const log = await screen.findByRole("log", {}, { timeout: 4000 });
    await waitFor(() => expect(log.children).toHaveLength(3), { timeout: 4000 });          // the poll comes once a second
    expect(Array.from(log.children).map((c) => c.textContent)).toEqual(["Model     org/m", "bar 100%", "Finished  6.2 MB"]);
    expect(screen.getByText("Finished")).toHaveClass("text-ok");
    expect(screen.getByText("Download a model")).toBeInTheDocument();
    expect(screen.getByText("finished")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Copy the output of Download a model/ })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("copies every row that is kept, also the ones above what is on screen", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    const logs = Array.from({ length: 420 }, (_, i) => `line ${i}`);
    vi.mocked(api.startJob).mockResolvedValue({ id: "j5", stage: "hf_download", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j5", stage: "hf_download", status: "done", error: "", logs, log_start: 0, progress: null } as any);
    class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }     // a socket that never opens: the console polls
    vi.stubGlobal("WebSocket", FakeSocket);
    const write = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText: write }, configurable: true });
    render(<RunConsole stage="hf_download" label="Download" />);
    fireEvent.click(screen.getByRole("button", { name: /Download/ }));
    await screen.findByRole("button", { name: /Load earlier lines \(120 more\)/ }, { timeout: 4000 });          // 420 rows kept, 300 on screen
    fireEvent.click(screen.getByRole("button", { name: /Copy the output/ }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
    expect(write.mock.calls[0][0].split("\n")).toEqual(logs);
    vi.unstubAllGlobals();
  });
});
