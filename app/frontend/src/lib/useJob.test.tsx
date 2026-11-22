import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, api: { startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: (id: string, after?: number) => `ws://test/${id}${after ? `?after=${after}` : ""}`, clearRequestCache: vi.fn() };
});
import { api } from "./api";
import { resetStreams } from "./jobStreams";
import { useJob } from "./useJob";

class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: any; onmessage: any; onclose: any; onerror: any;
  closed = false;
  constructor(public url: string) { FakeSocket.all.push(this); }
  close() { this.closed = true; }
  open() { this.onopen?.(); }
  send(message: object) { this.onmessage?.({ data: JSON.stringify(message) }); }
  drop() { this.onclose?.(); }
}

let frames: (() => void)[] = [];
const runFrames = () => act(() => { const due = frames; frames = []; due.forEach((f) => f()); });

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.all = [];
  frames = [];
  resetStreams();
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => { frames.push(cb); return frames.length; });
  vi.stubGlobal("cancelAnimationFrame", () => { frames = []; });
  vi.mocked(api.startJob).mockReset();
  vi.mocked(api.getJob).mockReset();
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

function attached() {
  const hook = renderHook(() => useJob());
  act(() => hook.result.current.attach("job1"));
  const socket = FakeSocket.all[0];
  act(() => socket.open());
  return { hook, socket };
}
const texts = (hook: ReturnType<typeof renderHook<ReturnType<typeof useJob>, unknown>>) => hook.result.current.logs.map((l) => l.text);

describe("streaming output", () => {
  it("draws lines that arrive faster than the screen refreshes once per frame, and only the latest", () => {
    const { hook, socket } = attached();
    act(() => socket.send({ type: "logs", lines: [] }));
    act(() => { for (let i = 0; i < 1000; i++) socket.send({ type: "log", line: `line ${i}` }); });
    expect(hook.result.current.logs).toHaveLength(0);               // nothing drawn until the frame
    runFrames();
    expect(hook.result.current.logs).toHaveLength(300);
    expect(texts(hook)[299]).toBe("line 999");
    expect(hook.result.current.earlier).toBe(700);
    expect(frames).toHaveLength(0);
  });

  it("asks for one frame however many messages arrive", () => {
    const { socket } = attached();
    act(() => { for (let i = 0; i < 50; i++) socket.send({ type: "logs", lines: [`a${i}`] }); });
    expect(frames.length).toBeLessThanOrEqual(1);
  });

  it("shows the last lines before it shows that the job is done", () => {
    const { hook, socket } = attached();
    act(() => { socket.send({ type: "logs", lines: ["one"] }); socket.send({ type: "log", line: "two" }); socket.send({ type: "status", status: "done", error: "" }); });
    expect(hook.result.current.status).toBe("done");
    expect(texts(hook)).toEqual(["one", "two"]);
  });

  it("draws older lines on request", () => {
    const { hook, socket } = attached();
    act(() => socket.send({ type: "logs", lines: Array.from({ length: 1000 }, (_, i) => `line ${i}`) }));
    runFrames();
    act(() => hook.result.current.showEarlier());
    expect(hook.result.current.logs).toHaveLength(600);
    expect(hook.result.current.earlier).toBe(400);
  });

  it("keeps the progress as it arrives", () => {
    const { hook, socket } = attached();
    act(() => socket.send({ type: "progress", progress: { label: "Batch 2", processed: 5, total: 10 } }));
    expect(hook.result.current.progress).toMatchObject({ label: "Batch 2" });
  });
});

describe("a dropped connection", () => {
  it("is reopened after a pause and the whole log replaces what was drawn, without repeats", () => {
    const { hook, socket } = attached();
    act(() => socket.send({ type: "logs", lines: ["a", "b"] }));
    runFrames();
    act(() => socket.drop());
    expect(FakeSocket.all).toHaveLength(1);
    act(() => { vi.advanceTimersByTime(500); });
    expect(FakeSocket.all).toHaveLength(2);
    const again = FakeSocket.all[1];
    expect(again.url).toBe("ws://test/job1?after=2");               // it asks only for the lines it has not seen
    act(() => again.open());
    act(() => again.send({ type: "logs", lines: ["c"], start: 2 }));
    runFrames();
    expect(texts(hook)).toEqual(["a", "b", "c"]);
  });

  it("waits longer each time and gives up on the socket for the poll", async () => {
    vi.mocked(api.getJob).mockResolvedValue({ id: "job1", stage: "x", status: "running", logs: ["polled"], error: "", progress: null } as any);
    const { hook, socket } = attached();
    let current = socket;
    for (const wait of [500, 1000, 2000, 4000, 8000, 15000]) {
      act(() => current.drop());
      const before = FakeSocket.all.length;
      act(() => { vi.advanceTimersByTime(wait - 1); });
      expect(FakeSocket.all.length).toBe(before);                   // not yet
      act(() => { vi.advanceTimersByTime(1); });
      expect(FakeSocket.all.length).toBe(before + 1);
      current = FakeSocket.all[before];
      act(() => current.open());
    }
    act(() => current.drop());
    await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
    expect(api.getJob).toHaveBeenCalled();
    expect(texts(hook)).toEqual(["polled"]);
  });

  it("does not reopen after the job finished", () => {
    const { socket } = attached();
    act(() => socket.send({ type: "status", status: "done", error: "" }));
    act(() => socket.drop());
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("polls at once when the socket never opened", async () => {
    vi.mocked(api.getJob).mockResolvedValue({ id: "job1", stage: "x", status: "done", logs: ["x"], error: "", progress: null } as any);
    const hook = renderHook(() => useJob());
    act(() => hook.result.current.attach("job1"));
    act(() => FakeSocket.all[0].drop());                              // closed without ever opening
    await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
    expect(hook.result.current.status).toBe("done");
    expect(FakeSocket.all).toHaveLength(1);
  });
});

describe("leaving", () => {
  it("closes the socket and never reopens it after unmount", () => {
    const { hook, socket } = attached();
    hook.unmount();
    act(() => { vi.advanceTimersByTime(1); });
    expect(socket.closed).toBe(true);
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("does not stop a pending reconnect from being cancelled by unmount", () => {
    const { hook, socket } = attached();
    act(() => socket.drop());
    hook.unmount();
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("ignores an attach that arrives after unmount", () => {
    const hook = renderHook(() => useJob());
    const attach = hook.result.current.attach;
    hook.unmount();
    attach("late");
    expect(FakeSocket.all).toHaveLength(0);
  });
});

describe("several consoles following one job", () => {
  it("share a single socket, each with its own window of lines", () => {
    const a = renderHook(() => useJob());
    const b = renderHook(() => useJob());
    act(() => { a.result.current.attach("shared"); b.result.current.attach("shared"); });
    expect(FakeSocket.all).toHaveLength(1);
    const socket = FakeSocket.all[0];
    act(() => socket.open());
    act(() => socket.send({ type: "logs", lines: Array.from({ length: 700 }, (_, i) => `line ${i}`) }));
    runFrames();
    expect(a.result.current.logs).toHaveLength(300);
    expect(b.result.current.logs).toHaveLength(300);
    act(() => a.result.current.showEarlier());
    expect(a.result.current.logs).toHaveLength(600);
    expect(b.result.current.logs).toHaveLength(300);                // the other console is unaffected
  });

  it("keep the connection until the last one leaves", () => {
    const a = renderHook(() => useJob());
    const b = renderHook(() => useJob());
    act(() => { a.result.current.attach("shared"); b.result.current.attach("shared"); });
    const socket = FakeSocket.all[0];
    a.unmount();
    act(() => { vi.advanceTimersByTime(1); });
    expect(socket.closed).toBe(false);
    b.unmount();
    act(() => { vi.advanceTimersByTime(1); });
    expect(socket.closed).toBe(true);
  });

  it("open a separate socket for a different job", () => {
    const a = renderHook(() => useJob());
    const b = renderHook(() => useJob());
    act(() => { a.result.current.attach("one"); b.result.current.attach("two"); });
    expect(FakeSocket.all.map((s) => s.url)).toEqual(["ws://test/one", "ws://test/two"]);
  });

  it("all see the end of the job", () => {
    const a = renderHook(() => useJob());
    const b = renderHook(() => useJob());
    act(() => { a.result.current.attach("shared"); b.result.current.attach("shared"); });
    act(() => FakeSocket.all[0].send({ type: "status", status: "error", error: "boom" }));
    expect(a.result.current.status).toBe("error");
    expect(b.result.current).toMatchObject({ status: "error", error: "boom" });
  });
});
