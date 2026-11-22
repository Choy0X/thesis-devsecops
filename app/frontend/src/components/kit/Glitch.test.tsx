import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Glitch } from "./Glitch";

afterEach(() => {
  delete document.documentElement.dataset.motion;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Glitch", () => {
  it("shows the plain word at rest, readable by a screen reader through a hidden copy", () => {
    render(<Glitch text="ztsa" />);
    expect(screen.getByText("ztsa", { selector: ".sr-only" })).toBeInTheDocument();
  });

  it("bursts once on mount and settles back to the real word", () => {
    vi.useFakeTimers();
    render(<Glitch text="ztsa" on="mount" />);
    act(() => { vi.advanceTimersByTime(500); });
    const el = screen.getByText("ztsa", { selector: ".sr-only" }).closest("[data-text]");
    expect(el).toHaveAttribute("data-text", "ztsa");
    expect(el?.querySelector("[aria-hidden]")?.textContent).toBe("ztsa");
  });

  it("auto mode bursts again on its own, on an interval, with nothing to trigger it by hand", () => {
    vi.useFakeTimers();
    render(<Glitch text="ztsa" as="pre" on="auto" />);
    act(() => { vi.advanceTimersByTime(500); });                        // the initial mount-settle finishes
    const el = screen.getByText("ztsa", { selector: ".sr-only" }).closest("[data-text]")!;
    expect(el).not.toHaveAttribute("data-glitch");
    act(() => { vi.advanceTimersByTime(6490); });                       // just short of the interval (6990 total): still quiet
    expect(el).not.toHaveAttribute("data-glitch");
    act(() => { vi.advanceTimersByTime(20); });                         // crosses it (7010 total), well inside the burst's own ~240ms window
    expect(el).toHaveAttribute("data-glitch", "on");                   // the periodic burst fired with no pointer or focus event at all
  });

  it("keeps a multi-line block's line breaks whole while it is still mid-scramble, not just once it settles", () => {
    vi.useFakeTimers();
    const art = "line one\nline two\nline three";
    const { container } = render(<Glitch text={art} as="pre" on="mount" />);
    act(() => { vi.advanceTimersByTime(84); });                         // two settle steps in: still scrambling, not yet the real text
    const shown = container.querySelector("[aria-hidden]")?.textContent ?? "";
    expect(shown).not.toBe(art);                                        // confirms this really caught it mid-scramble, not after it had already settled
    expect(shown.split("\n")).toHaveLength(3);                          // the newlines were never replaced with noise, so the block never collapses onto one line
  });

  it("auto mode never starts its interval, and never scrambles on mount, when motion is not allowed", () => {
    vi.useFakeTimers();
    document.documentElement.dataset.motion = "reduced";
    render(<Glitch text="ztsa" on="auto" />);
    act(() => { vi.advanceTimersByTime(20000); });
    const el = screen.getByText("ztsa", { selector: ".sr-only" }).closest("[data-text]")!;
    expect(el).not.toHaveAttribute("data-glitch");
    expect(el.querySelector("[aria-hidden]")?.textContent).toBe("ztsa");
  });
});
