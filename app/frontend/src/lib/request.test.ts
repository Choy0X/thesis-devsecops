import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./apiError";
import { isAbort, request, resetRequests, setRequestScope, GET_TIMEOUT_MS, CACHE_TTL_MS } from "./request";

type Answer = { status?: number; body?: unknown; headers?: Record<string, string>; hang?: boolean };

/** A fetch that answers from a script and honours the abort signal like the real one. */
function fakeFetch(script: Answer[] | ((url: string, n: number) => Answer)) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fn = vi.fn((url: string, init: RequestInit = {}) => {
    const n = calls.length;
    calls.push({ url, init });
    const a = typeof script === "function" ? script(url, n) : script[Math.min(n, script.length - 1)];
    return new Promise<Response>((resolve, reject) => {
      const abort = () => reject(new DOMException("aborted", "AbortError"));
      if (init.signal?.aborted) return abort();
      init.signal?.addEventListener("abort", abort, { once: true });
      if (a.hang) return;
      Promise.resolve().then(() => resolve(new Response(JSON.stringify(a.body ?? {}), { status: a.status ?? 200, headers: a.headers })));
    });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

beforeEach(() => { vi.useFakeTimers(); resetRequests(); });
afterEach(() => { resetRequests(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("request headers", () => {
  it.each([
    { "Content-Type": "application/problem+json", "X-Test": "kept" },
    new Headers({ "Content-Type": "application/problem+json", "X-Test": "kept" }),
    [["Content-Type", "application/problem+json"], ["X-Test", "kept"]],
  ] as HeadersInit[])("overrides defaults case-insensitively for every HeadersInit format", async headers => {
    const { calls } = fakeFetch([{ body: { ok: true } }]);
    await request("/api/x", { method: "POST", headers, body: "{}" });
    const sent = new Headers(calls[0].init.headers);
    expect(sent.get("content-type")).toBe("application/problem+json");
    expect(sent.get("x-test")).toBe("kept");
    expect(sent.get("x-slow")).toBe("poll");
  });
});

describe("shared and kept GETs", () => {
  it("invalidates affected data after a write without discarding unrelated training lists", async () => {
    const { fn } = fakeFetch([{ body: { n: 1 } }]);
    await request('/api/finetune/runs');
    await request('/api/data/review/queue');
    await request('/api/data/review/decision', {method: 'POST', body: '{}'});
    await request('/api/finetune/runs');
    await request('/api/data/review/queue');
    expect(fn.mock.calls.filter(([url]) => url === '/api/finetune/runs')).toHaveLength(1);
    expect(fn.mock.calls.filter(([url]) => url === '/api/data/review/queue')).toHaveLength(2);
  });

  it("does not cache an older GET that finishes after a write invalidates its resource", async () => {
    let finish: (response: Response) => void = () => undefined;
    const fn = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }))
      .mockResolvedValueOnce(new Response('{}', {status: 200}))
      .mockResolvedValueOnce(new Response('{"n":2}', {status: 200}));
    vi.stubGlobal('fetch', fn);
    const prior = request('/api/data/review/queue');
    await request('/api/data/review/decision', {method: 'POST'});
    finish(new Response('{"n":1}', {status: 200}));
    await prior;
    expect(await request('/api/data/review/queue')).toEqual({n: 2});
  });
  it("sends one request for two identical GETs in flight", async () => {
    const { fn } = fakeFetch([{ body: { n: 1 } }]);
    const [a, b] = await Promise.all([request("/api/x"), request("/api/x")]);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(a).toEqual({ n: 1 });
    expect(b).toBe(a);
  });

  it("does not share different urls", async () => {
    const { fn } = fakeFetch([{ body: {} }]);
    await Promise.all([request("/api/x"), request("/api/y")]);
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("answers a revisit inside the ttl from memory and asks again after it", async () => {
    const { fn } = fakeFetch([{ body: { n: 1 } }, { body: { n: 2 } }]);
    expect(await request("/api/x")).toEqual({ n: 1 });
    expect(await request("/api/x")).toEqual({ n: 1 });
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(CACHE_TTL_MS + 1);
    expect(await request("/api/x")).toEqual({ n: 2 });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("always asks the server when the ttl is zero (polling)", async () => {
    const { fn } = fakeFetch([{ body: { n: 1 } }, { body: { n: 2 } }]);
    await request("/api/job", { ttl: 0 });
    expect(await request("/api/job", { ttl: 0 })).toEqual({ n: 2 });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("forgets what it kept after any write, and never shares or keeps a write", async () => {
    const { fn } = fakeFetch([{ body: { n: 1 } }, { body: { ok: true } }, { body: { n: 2 } }]);
    await request("/api/x");
    await request("/api/x", { method: "POST", body: "{}" });
    expect(await request("/api/x")).toEqual({ n: 2 });
    await Promise.all([request("/api/w", { method: "POST" }), request("/api/w", { method: "POST" })]);
    expect(fn.mock.calls.filter(([u]) => u === "/api/w")).toHaveLength(2);
  });

  it("does not keep a failed answer", async () => {
    const { fn } = fakeFetch([{ status: 500, body: { error: "boom" } }, { body: { n: 1 } }]);
    await expect(request("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(await request("/api/x")).toEqual({ n: 1 });
    expect(fn).toHaveBeenCalledTimes(2);
  });
});

describe("aborting", () => {
  it("rejects with an abort error and stops the request when the only caller aborts", async () => {
    const { calls } = fakeFetch([{ hang: true }]);
    const c = new AbortController();
    const p = request("/api/x", { signal: c.signal });
    c.abort();
    await expect(p).rejects.toSatisfy(isAbort);
    expect((calls[0].init.signal as AbortSignal).aborted).toBe(true);
  });

  it("keeps a shared request alive while another caller still waits", async () => {
    const { calls } = fakeFetch([{ hang: true }]);
    const c = new AbortController();
    const first = request("/api/x", { signal: c.signal });
    const second = request("/api/x");
    c.abort();
    await expect(first).rejects.toSatisfy(isAbort);
    expect((calls[0].init.signal as AbortSignal).aborted).toBe(false);
    second.catch(() => undefined);
  });

  it("starts a new request when the one in flight was abandoned", async () => {
    const { fn } = fakeFetch([{ hang: true }, { body: { n: 2 } }]);
    const c = new AbortController();
    const first = request("/api/x", { signal: c.signal });
    c.abort();
    await expect(first).rejects.toSatisfy(isAbort);
    expect(await request("/api/x")).toEqual({ n: 2 });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("aborts a GET when the page that asked is left, but not a write", async () => {
    const { calls } = fakeFetch((url) => (url === "/api/save" ? { body: { ok: true } } : { hang: true }));
    const page = new AbortController();
    setRequestScope(page);
    const read = request("/api/slow");
    const write = request("/api/save", { method: "POST", body: "{}" });
    page.abort();
    await expect(read).rejects.toSatisfy(isAbort);
    await expect(write).resolves.toEqual({ ok: true });
    expect((calls.find((c) => c.url === "/api/save")!.init.signal as AbortSignal).aborted).toBe(false);
  });

  it("does not abort a global request when the page is left", async () => {
    fakeFetch([{ body: { stamp: "1" } }]);
    const page = new AbortController();
    setRequestScope(page);
    const p = request("/api/data/stamp", { global: true, ttl: 0 });
    page.abort();
    await expect(p).resolves.toEqual({ stamp: "1" });
  });

  it("rejects at once for a signal that is already aborted", async () => {
    const { fn } = fakeFetch([{ body: {} }]);
    const c = new AbortController();
    c.abort();
    await expect(request("/api/x", { signal: c.signal })).rejects.toSatisfy(isAbort);
    expect(fn).not.toHaveBeenCalledWith("/api/x", expect.objectContaining({ signal: expect.objectContaining({ aborted: false }) }));
  });
});

describe("the backend is still computing (202)", () => {
  const warming = { status: 202, headers: { "x-slow-cache": "warming" }, body: { warming: true } };

  it("asks again until the answer is ready", async () => {
    const { fn } = fakeFetch([warming, warming, { body: { rows: 3 } }]);
    const p = request("/api/summary");
    await vi.advanceTimersByTimeAsync(10_000);
    await expect(p).resolves.toEqual({ rows: 3 });
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it("stops asking the moment the caller aborts", async () => {
    const { fn } = fakeFetch([warming]);
    const c = new AbortController();
    const p = request("/api/summary", { signal: c.signal });
    await vi.advanceTimersByTimeAsync(1500);
    const before = fn.mock.calls.length;
    c.abort();
    await expect(p).rejects.toSatisfy(isAbort);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fn.mock.calls.length).toBe(before);
  });

  it("does not treat a plain 202 as computing", async () => {
    fakeFetch([{ status: 202, body: { queued: true } }]);
    await expect(request("/api/x")).resolves.toEqual({ queued: true });
  });
});

describe("timeouts and failures", () => {
  it("gives up on a GET that never answers", async () => {
    fakeFetch([{ hang: true }]);
    const p = request("/api/x");
    p.catch(() => undefined);
    await vi.advanceTimersByTimeAsync(GET_TIMEOUT_MS + 1);
    await expect(p).rejects.toMatchObject({ code: "timeout" });
  });

  it("does not time out a write unless asked", async () => {
    fakeFetch([{ body: { ok: true } }]);
    const p = request("/api/long", { method: "POST" });
    await vi.advanceTimersByTimeAsync(GET_TIMEOUT_MS * 3);
    await expect(p).resolves.toEqual({ ok: true });
  });

  it("maps a refused connection and an error body to ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(request("/api/x")).rejects.toMatchObject({ status: 0, code: "unreachable" });
    fakeFetch([{ status: 409, body: { ok: false, error: "busy", detail: "busy", hint: "wait", code: "conflict" } }]);
    await expect(request("/api/y")).rejects.toMatchObject({ status: 409, title: "busy", hint: "wait" });
  });
});

describe("read-only POSTs", () => {
  it("do not make the layer forget what it kept, unlike a write", async () => {
    const { fn } = fakeFetch([{ body: { n: 1 } }, { body: { preview: true } }, { body: { n: 2 } }]);
    await request("/api/data/view");
    await request("/api/code/dataset/preview", { method: "POST", body: "{}", readOnly: true });
    expect(await request("/api/data/view")).toEqual({ n: 1 });              // still kept
    expect(fn).toHaveBeenCalledTimes(2);
    await request("/api/code/reset", { method: "POST", body: "{}" });
    await request("/api/data/view");
    expect(fn.mock.calls.filter(([u]) => u === "/api/data/view")).toHaveLength(2);   // a related write cleared it
  });

  it("can be cancelled by the caller", async () => {
    const { calls } = fakeFetch([{ hang: true }]);
    const c = new AbortController();
    const p = request("/api/code/dataset/preview", { method: "POST", body: "{}", readOnly: true, signal: c.signal });
    c.abort();
    await expect(p).rejects.toSatisfy(isAbort);
    expect((calls[0].init.signal as AbortSignal).aborted).toBe(true);
  });
});
