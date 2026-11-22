import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, api: { listJobs: vi.fn(), getJob: vi.fn(), startJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: (id: string, after?: number) => `ws://test/${id}${after ? `?after=${after}` : ""}`, clearRequestCache: vi.fn() };
});
import { api } from "./api";
import { RunConsole } from "../components/RunConsole";
import { resetStreams } from "./jobStreams";
import { refreshJobs, resetJobsList, runningJobId, useRunningJobId } from "./jobsList";
import { resetChanges, subscribeChanged } from "./changes";

class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: any; onmessage: any; onclose: any; onerror: any;
  constructor(public url: string) { FakeSocket.all.push(this); }
  close() { /* nothing to close */ }
}

const job = (id: string, stage: string, status = "running") => ({ id, stage, status, params: {}, started: "", finished: "", error: "", log_count: 0 });
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.all = [];
  resetJobsList();
  resetStreams();
  vi.mocked(api.listJobs).mockReset();
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => { resetJobsList(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("the shared job list", () => {
  it("is asked for once however many consoles ask", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    const hooks = Array.from({ length: 19 }, () => renderHook(() => useRunningJobId("dep_versions")));
    await tick(10);
    expect(api.listJobs).toHaveBeenCalledTimes(1);
    hooks.forEach((h) => h.unmount());
  });

  it("hands each console the running job of its own stage", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("j1", "mine_code"), job("j2", "ingest", "done")] as any);
    const mine = renderHook(() => useRunningJobId("mine_code"));
    const ingest = renderHook(() => useRunningJobId("ingest"));
    await tick(10);
    expect(mine.result.current).toBe("j1");
    expect(ingest.result.current).toBeNull();
    expect(runningJobId("mine_code")).toBe("j1");
  });

  it("polls quickly while a job runs, slowly otherwise, and stops when nobody watches", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("j1", "x")] as any);
    const h = renderHook(() => useRunningJobId("x"));
    await tick(10);
    expect(api.listJobs).toHaveBeenCalledTimes(1);
    await tick(3100);
    expect(api.listJobs).toHaveBeenCalledTimes(2);
    vi.mocked(api.listJobs).mockResolvedValue([]);
    await tick(3100);
    expect(h.result.current).toBeNull();
    const calls = vi.mocked(api.listJobs).mock.calls.length;
    await tick(10_000);
    expect(vi.mocked(api.listJobs).mock.calls.length).toBe(calls);        // idle: every 15 s
    await tick(6000);
    expect(vi.mocked(api.listJobs).mock.calls.length).toBe(calls + 1);
    h.unmount();
    await tick(60_000);
    expect(vi.mocked(api.listJobs).mock.calls.length).toBe(calls + 1);    // nobody watching: no more requests
  });

  it("re-renders its consoles only when a job starts or ends, not on every poll", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("j1", "x")] as any);
    let renders = 0;
    renderHook(() => { renders++; return useRunningJobId("x"); });
    await tick(10);
    const before = renders;
    await tick(10_000);                                                    // three polls, same running set
    expect(renders).toBe(before);
  });

  it("asks at once when told a job was started", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);
    renderHook(() => useRunningJobId("x"));
    await tick(10);
    vi.mocked(api.listJobs).mockResolvedValue([job("new", "x")] as any);
    refreshJobs();
    await tick(10);
    expect(runningJobId("x")).toBe("new");
  });
});

describe("many consoles on one page", () => {
  it("make one list request and open one socket per running job", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("m1", "dep_versions")] as any);
    const { unmount } = render(
      <div>
        {Array.from({ length: 19 }, (_, i) => <RunConsole key={i} stage="dep_versions" label={`Step ${i}`} />)}
        <RunConsole stage="phase_audit" label="Check" />
      </div>,
    );
    await tick(50);
    expect(api.listJobs).toHaveBeenCalledTimes(1);
    expect(FakeSocket.all.map((s) => s.url)).toEqual(["ws://test/m1"]);
    unmount();
  });
});

describe("a run started elsewhere", () => {
  it("is followed by every console of its stage but drawn by none until the reader asks", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("m1", "dep_versions")] as any);
    const { unmount } = render(
      <div>
        <RunConsole stage="dep_versions" label="Step A" />
        <RunConsole stage="dep_versions" label="Step B" />
      </div>,
    );
    await tick(50);
    const socket = FakeSocket.all[0];
    await act(async () => {
      socket.onopen?.();
      socket.onmessage?.({ data: JSON.stringify({ type: "logs", lines: ["alpha line", "beta line"] }) });
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.queryByText("alpha line")).toBeNull();
    expect(screen.getAllByText(/started elsewhere/)).toHaveLength(2);
    fireEvent.click(screen.getAllByRole("button", { name: "Show output" })[0]);
    expect(screen.getAllByText("alpha line")).toHaveLength(1);               // only the console the reader opened
    unmount();
  });
});

describe("telling the app what a finished job changed", () => {
  const heard = () => {
    const calls: string[] = [];
    subscribeChanged(["models"], () => calls.push("models"));
    subscribeChanged(["program"], () => calls.push("program"));
    return calls;
  };
  beforeEach(() => resetChanges());

  it("says nothing for downloads that were already over when the page opened", async () => {
    const calls = heard();
    vi.mocked(api.listJobs).mockResolvedValue([job("old", "hf_download", "done"), job("older", "install_llama_cpp", "done")] as any);
    const h = renderHook(() => useRunningJobId("hf_download"));
    await tick(10);
    expect(calls).toEqual([]);
    h.unmount();
  });

  it("says models changed once when a download that was running is over, and nothing while it runs", async () => {
    const calls = heard();
    vi.mocked(api.listJobs).mockResolvedValue([job("d1", "hf_download")] as any);
    const h = renderHook(() => useRunningJobId("hf_download"));
    await tick(10);
    await tick(3100);
    expect(calls).toEqual([]);
    vi.mocked(api.listJobs).mockResolvedValue([job("d1", "hf_download", "done")] as any);
    await tick(3100);
    expect(calls).toEqual(["models"]);
    await tick(20_000);
    expect(calls).toEqual(["models"]);
    h.unmount();
  });

  it("ignores completed historical installer jobs for the retired runtime", async () => {
    const calls = heard();
    vi.mocked(api.listJobs).mockResolvedValue([] as any);
    const h = renderHook(() => useRunningJobId("install_llama_cpp"));
    await tick(10);
    vi.mocked(api.listJobs).mockResolvedValue([job("i1", "install_llama_cpp", "done")] as any);
    await tick(15_100);
    expect(calls).toEqual([]);
    h.unmount();
  });

  it("also says it for a job that started and ended between two looks", async () => {
    const calls = heard();
    vi.mocked(api.listJobs).mockResolvedValue([] as any);
    const h = renderHook(() => useRunningJobId("hf_download"));
    await tick(10);
    vi.mocked(api.listJobs).mockResolvedValue([job("quick", "hf_download", "done")] as any);
    await tick(15_100);
    expect(calls).toEqual(["models"]);
    h.unmount();
  });
});
