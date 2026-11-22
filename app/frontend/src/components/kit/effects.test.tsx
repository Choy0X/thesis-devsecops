import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Glitch, MatrixRain } from ".";

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete document.documentElement.dataset.motion;
});

const burst = (container: HTMLElement) => container.querySelector(".glitch")!.getAttribute("data-glitch");

describe("Glitch", () => {
  it("gives a screen reader the plain word and hides the animated copy", () => {
    const { container } = render(<Glitch text="Vuln Framework" />);
    expect(container.querySelector(".sr-only")).toHaveTextContent("Vuln Framework");
    expect(container.querySelector("[aria-hidden]")).toHaveTextContent("Vuln Framework");
    expect(container.querySelector(".glitch")).toHaveAttribute("data-text", "Vuln Framework");
  });

  it("bursts once when the pointer arrives and stops by itself", () => {
    const { container } = render(<Glitch text="Settings" />);
    expect(burst(container)).toBeNull();
    fireEvent.mouseEnter(container.querySelector(".glitch")!);
    expect(burst(container)).toBe("on");
    act(() => { vi.advanceTimersByTime(300); });
    expect(burst(container)).toBeNull();
  });

  it("allows one burst a second at most, however fast the pointer moves", () => {
    const { container } = render(<Glitch text="Settings" />);
    const el = container.querySelector(".glitch")!;
    fireEvent.mouseEnter(el);
    act(() => { vi.advanceTimersByTime(300); });
    fireEvent.mouseEnter(el);                         // 300 ms after the first: too soon
    expect(burst(container)).toBeNull();
    act(() => { vi.advanceTimersByTime(800); });
    fireEvent.mouseEnter(el);                         // more than a second after the first
    expect(burst(container)).toBe("on");
  });

  it("bursts on keyboard focus too", () => {
    const { container } = render(<Glitch text="Train" />);
    fireEvent.focus(container.querySelector(".glitch")!);
    expect(burst(container)).toBe("on");
  });

  it("settles out of noise when it appears, and ends on the real word", () => {
    const { container } = render(<Glitch text="Overview" on="mount" />);
    const visual = () => container.querySelector("[aria-hidden]")!.textContent;
    expect(visual()).not.toBe("Overview");              // noise first (the chance of the noise equalling the word is nil)
    act(() => { vi.advanceTimersByTime(600); });
    expect(visual()).toBe("Overview");
    expect(container.querySelector(".sr-only")).toHaveTextContent("Overview");
  });

  it("does nothing at all when motion is not allowed", () => {
    document.documentElement.dataset.motion = "reduced";
    const { container } = render(<Glitch text="Overview" on="both" />);
    expect(container.querySelector("[aria-hidden]")).toHaveTextContent("Overview");
    fireEvent.mouseEnter(container.querySelector(".glitch")!);
    expect(burst(container)).toBeNull();
  });
});

describe("MatrixRain", () => {
  it("is a hidden canvas that takes no pointer events, and starts and stops its animation", () => {
    const raf = vi.spyOn(window, "requestAnimationFrame").mockReturnValue(7);
    const cancel = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    const draw = { font: "", globalAlpha: 1, globalCompositeOperation: "", fillStyle: "", fillRect: vi.fn(), fillText: vi.fn() };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(draw as unknown as CanvasRenderingContext2D);
    const { container, unmount } = render(<div><MatrixRain /></div>);
    const canvas = container.querySelector("canvas")!;
    expect(canvas).toHaveAttribute("aria-hidden", "true");
    expect(canvas.className).toContain("pointer-events-none");
    expect(raf).toHaveBeenCalled();
    unmount();
    expect(cancel).toHaveBeenCalledWith(7);
  });

  it("draws nothing when motion is not allowed", () => {
    document.documentElement.dataset.motion = "reduced";
    const { container } = render(<div><MatrixRain /></div>);
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });
});
