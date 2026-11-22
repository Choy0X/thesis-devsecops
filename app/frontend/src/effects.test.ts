/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";

// Every script source of the app as text (the tests themselves are left out). The stylesheet is checked in tests/test_frontend_effects.py,
// because the test runner does not hand out CSS text.
const files = import.meta.glob(["./**/*.{ts,tsx}", "!./**/*.test.*"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

describe("effects that are costly without GPU compositing", () => {
  it("has no backdrop blur in any component", () => {
    expect(Object.keys(files).length).toBeGreaterThan(20);
    const offenders = Object.entries(files).filter(([, body]) => /backdrop-blur|backdrop-filter/.test(body)).map(([name]) => name);
    expect(offenders).toEqual([]);
  });

  it("does not fade the whole page in on every visit", () => {
    expect(files["./App.tsx"]).toBeTruthy();
    expect(files["./App.tsx"]).not.toMatch(/className="[^"]*\brise\b/);
  });
});
