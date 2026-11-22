import { act, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode, useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isAbort, request, resetRequests } from "../lib/request";
import { PageScope, usePageRequest } from "./PageScope";

/** A fetch that never answers and reports when it is told to stop. */
function hangingFetch() {
  const aborted: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => new Promise<Response>((_resolve, reject) => {
    init.signal?.addEventListener("abort", () => { aborted.push(url); reject(new DOMException("aborted", "AbortError")); }, { once: true });
  })));
  return aborted;
}

function Page({ url, results }: { url: string; results: Promise<unknown>[] }) {
  useEffect(() => { const p = request(url); p.catch(() => undefined); results.push(p); }, [url, results]);       // observed, so a cancel is not reported as unhandled
  return <p>{url}</p>;
}

beforeEach(() => { resetRequests(); vi.useFakeTimers(); });
afterEach(() => { resetRequests(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("PageScope", () => {
  it("keeps the parent request alive when a tab scope is removed", async () => {
    const aborted = hangingFetch();
    const results: Promise<unknown>[] = [];
    function Parent() {
      const owned = usePageRequest();
      const [tab, setTab] = useState(true);
      useEffect(() => { const p = owned(() => request("/api/parent")); p.catch(() => undefined); results.push(p); }, [owned]);
      return <><button onClick={() => setTab(false)}>Leave tab</button>{tab && <PageScope><Page url="/api/tab" results={results} /></PageScope>}</>;
    }
    const view = render(<PageScope><Parent /></PageScope>);
    fireEvent.click(screen.getByText("Leave tab"));
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });
    expect(aborted).toEqual(["/api/tab"]);
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });
    expect(aborted).toEqual(["/api/tab", "/api/parent"]);
  });
  it("aborts the GETs of a page when the page is left", async () => {
    const aborted = hangingFetch();
    const results: Promise<unknown>[] = [];
    const { unmount } = render(<PageScope><Page url="/api/slow" results={results} /></PageScope>);
    expect(aborted).toEqual([]);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });
    expect(aborted).toEqual(["/api/slow"]);
    await expect(results[0]).rejects.toSatisfy(isAbort);
  });

  it("keeps the requests of a page that is still on screen, also under React's development re-mount", async () => {
    const aborted = hangingFetch();
    const results: Promise<unknown>[] = [];
    render(<StrictMode><PageScope><Page url="/api/slow" results={results} /></PageScope></StrictMode>);
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });
    expect(aborted).toEqual([]);
    results.forEach((p) => p.catch(() => undefined));
  });

  it("aborts the previous page and leaves the next page's request alone when the key changes", async () => {
    const aborted = hangingFetch();
    const results: Promise<unknown>[] = [];
    function Shell() {
      const [page, setPage] = useState("a");
      useEffect(() => { if (page === "a") setPage("b"); }, [page]);
      return <PageScope key={page}><Page url={`/api/${page}`} results={results} /></PageScope>;
    }
    render(<Shell />);
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });
    expect(aborted).toEqual(["/api/a"]);
    results.forEach((p) => p.catch(() => undefined));
  });
});
