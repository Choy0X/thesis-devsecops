import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WhenVisible } from "./WhenVisible";

class FakeObserver {
  static all: FakeObserver[] = [];
  disconnected = false;
  constructor(public callback: (entries: { isIntersecting: boolean }[]) => void, public options?: IntersectionObserverInit) { FakeObserver.all.push(this); }
  observe() { /* the test decides when it intersects */ }
  disconnect() { this.disconnected = true; }
  show() { this.callback([{ isIntersecting: true }]); }
}

beforeEach(() => { FakeObserver.all = []; vi.stubGlobal("IntersectionObserver", FakeObserver); });
afterEach(() => vi.unstubAllGlobals());

describe("WhenVisible", () => {
  it("keeps its children out of the page, so they send no request, until they are about to be seen", () => {
    const mounted = vi.fn();
    function Heavy() { mounted(); return <p>heavy panel</p>; }
    render(<WhenVisible><Heavy /></WhenVisible>);
    expect(mounted).not.toHaveBeenCalled();
    expect(screen.getByText(/Loads when you scroll here/)).toBeInTheDocument();
    act(() => FakeObserver.all[0].show());
    expect(screen.getByText("heavy panel")).toBeInTheDocument();
    expect(mounted).toHaveBeenCalled();
  });

  it("watches a little ahead of the screen and stops watching once shown", () => {
    render(<WhenVisible margin={250}><p>x</p></WhenVisible>);
    expect(FakeObserver.all[0].options?.rootMargin).toBe("250px");
    act(() => FakeObserver.all[0].show());
    expect(FakeObserver.all[0].disconnected).toBe(true);
  });

  it("shows the children at once where the browser cannot tell", () => {
    vi.unstubAllGlobals();
    vi.stubGlobal("IntersectionObserver", undefined);
    render(<WhenVisible><p>plain</p></WhenVisible>);
    expect(screen.getByText("plain")).toBeInTheDocument();
  });

  it("is not a loading skeleton, so a page that is waiting for the reader never looks unfinished", () => {
    const { container } = render(<WhenVisible><p>x</p></WhenVisible>);
    expect(container.querySelector(".skeleton")).toBeNull();
  });
});
