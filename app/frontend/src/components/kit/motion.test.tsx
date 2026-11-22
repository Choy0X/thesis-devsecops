import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { motionAllowed, useMotionAllowed } from "./motion";

function Probe() {
  return <p>{useMotionAllowed() ? "moving" : "still"}</p>;
}

afterEach(() => {
  delete document.documentElement.dataset.motion;
  vi.unstubAllGlobals();
});

describe("motion gate", () => {
  it("allows motion when nothing asks for less of it", () => {
    expect(motionAllowed()).toBe(true);
    render(<Probe />);
    expect(screen.getByText("moving")).toBeInTheDocument();
  });

  it("stops when the window is drawn without the graphics card, and follows the attribute live", async () => {
    render(<Probe />);
    expect(screen.getByText("moving")).toBeInTheDocument();
    await act(async () => { document.documentElement.dataset.motion = "reduced"; await Promise.resolve(); });
    expect(screen.getByText("still")).toBeInTheDocument();
    expect(motionAllowed()).toBe(false);
  });

  it("stops when the system asks for reduced motion", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: () => undefined, removeEventListener: () => undefined }));
    expect(motionAllowed()).toBe(false);
    render(<Probe />);
    expect(screen.getByText("still")).toBeInTheDocument();
  });
});
