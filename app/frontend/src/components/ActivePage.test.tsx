import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearRequestCache } from "../lib/request";
import { ActivePage, STALE_AFTER_MS, useActive, useOnActivate, useWhileActive } from "./ActivePage";

function Ticker({ tick }: { tick: () => void }) {
  useWhileActive(tick, 1000);
  return <p>ticker</p>;
}
function Refresher({ refresh }: { refresh: () => void }) {
  useOnActivate(refresh);
  return <p>refresher</p>;
}
function Counter() {
  const [n, setN] = useState(0);
  return <button onClick={() => setN(n + 1)}>count {n}</button>;
}
function Flag() {
  return <span>{useActive() ? "on screen" : "hidden"}</span>;
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("useWhileActive", () => {
  it("ticks only while the page is on screen and once right away when it comes back", () => {
    const tick = vi.fn();
    const { rerender } = render(<ActivePage active><Ticker tick={tick} /></ActivePage>);
    act(() => { vi.advanceTimersByTime(3000); });
    expect(tick).toHaveBeenCalledTimes(3);                          // no call at mount: the page does its own first load
    rerender(<ActivePage active={false}><Ticker tick={tick} /></ActivePage>);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(tick).toHaveBeenCalledTimes(3);                          // hidden: silent
    rerender(<ActivePage active><Ticker tick={tick} /></ActivePage>);
    expect(tick).toHaveBeenCalledTimes(4);                          // brought back: refreshed at once
    act(() => { vi.advanceTimersByTime(2000); });
    expect(tick).toHaveBeenCalledTimes(6);
  });

  it("uses the latest function without restarting the timer", () => {
    const first = vi.fn(), second = vi.fn();
    const { rerender } = render(<ActivePage active><Ticker tick={first} /></ActivePage>);
    rerender(<ActivePage active><Ticker tick={second} /></ActivePage>);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});

describe("useOnActivate", () => {
  const away = (refresh: () => void, between: () => void = () => undefined) => {
    const view = render(<ActivePage active><Refresher refresh={refresh} /></ActivePage>);
    const hide = () => view.rerender(<ActivePage active={false}><Refresher refresh={refresh} /></ActivePage>);
    const show = () => view.rerender(<ActivePage active><Refresher refresh={refresh} /></ActivePage>);
    hide(); between(); show();
    return { hide, show, between };
  };

  it("does not run at mount", () => {
    const refresh = vi.fn();
    render(<ActivePage active><Refresher refresh={refresh} /></ActivePage>);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("does not run when the page comes back and nothing happened meanwhile", () => {
    const refresh = vi.fn();
    away(refresh);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("runs once when something was written or a job ended while the page was away", () => {
    const refresh = vi.fn();
    const { hide, show } = away(refresh, () => clearRequestCache());
    expect(refresh).toHaveBeenCalledTimes(1);
    hide(); show();                                                 // away again, nothing happened
    expect(refresh).toHaveBeenCalledTimes(1);
    hide(); clearRequestCache(); show();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("runs after a long absence even if nothing is known to have changed", () => {
    const refresh = vi.fn();
    away(refresh, () => { vi.advanceTimersByTime(STALE_AFTER_MS + 1000); });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("runs on every return when asked to", () => {
    const refresh = vi.fn();
    function Always() { useOnActivate(refresh, { always: true }); return <p>always</p>; }
    const view = render(<ActivePage active><Always /></ActivePage>);
    for (let i = 1; i <= 3; i++) {
      view.rerender(<ActivePage active={false}><Always /></ActivePage>);
      view.rerender(<ActivePage active><Always /></ActivePage>);
      expect(refresh).toHaveBeenCalledTimes(i);
    }
  });
});

describe("a page that stays mounted", () => {
  it("keeps its state while hidden and knows whether it is on screen", () => {
    const { rerender } = render(<section><ActivePage active><Counter /><Flag /></ActivePage></section>);
    fireEvent.click(screen.getByText("count 0"));
    expect(screen.getByText("count 1")).toBeInTheDocument();
    expect(screen.getByText("on screen")).toBeInTheDocument();
    rerender(<section><ActivePage active={false}><Counter /><Flag /></ActivePage></section>);
    expect(screen.getByText("hidden")).toBeInTheDocument();
    rerender(<section><ActivePage active><Counter /><Flag /></ActivePage></section>);
    expect(screen.getByText("count 1")).toBeInTheDocument();
  });
});
