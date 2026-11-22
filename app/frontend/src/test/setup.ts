import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { resetWorkspaceState } from "../lib/workspaceState";
import { afterEach } from "vitest";

// Unmount what a test rendered so tests do not see each other's DOM.
afterEach(() => { cleanup(); resetWorkspaceState(); });

// Radix measures and scrolls elements that jsdom does not implement (the Select needs all four).
if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class { observe() { /* no layout in jsdom */ } unobserve() { /* none */ } disconnect() { /* none */ } };
}
const proto = Element.prototype as unknown as Record<string, unknown>;
proto.hasPointerCapture ??= () => false;
proto.setPointerCapture ??= () => undefined;
proto.releasePointerCapture ??= () => undefined;
proto.scrollIntoView ??= () => undefined;
// jsdom has no canvas: say so quietly (MatrixRain then draws nothing) instead of printing "Not implemented" on every empty state. A test that wants a context spies on this.
HTMLCanvasElement.prototype.getContext = (() => null) as unknown as typeof HTMLCanvasElement.prototype.getContext;

// A plain in-memory localStorage for every test (the runtime's own can be a stub without clear()).
const memory = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    getItem: (k: string) => (memory.has(k) ? memory.get(k)! : null),
    setItem: (k: string, v: string) => { memory.set(k, String(v)); },
    removeItem: (k: string) => { memory.delete(k); },
    clear: () => memory.clear(),
    key: (i: number) => [...memory.keys()][i] ?? null,
    get length() { return memory.size; },
  },
});
