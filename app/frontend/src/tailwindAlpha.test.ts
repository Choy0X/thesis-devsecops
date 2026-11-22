import postcss from "postcss";
import tailwindcss from "tailwindcss";
import { describe, expect, it } from "vitest";
// @ts-ignore: the configuration is a plain JavaScript file
import config from "../tailwind.config.js";

// The theme colours are CSS variables, which Tailwind cannot give a transparency by itself: `bg-bg/80` once produced no rule at all, so the dimming behind a dialog and the soft borders were
// missing. These tests build the CSS of a few classes with the real configuration.

async function css(classes: string): Promise<string> {
  const out = await postcss([tailwindcss({ ...(config as object), content: [{ raw: classes }] })]).process("@tailwind utilities;", { from: undefined });
  return out.css;
}

describe("colours with a transparency modifier", () => {
  it("make a rule for the dimming behind a dialog and for the soft borders", async () => {
    const out = await css("bg-bg/80 border-line/60 divide-line/60 bg-surface-2/50 bg-brand/10 border-brand/40 bg-warn/10");
    expect(out).toContain("color-mix(in srgb, var(--bg) 80%, transparent)");
    expect(out).toContain("color-mix(in srgb, var(--border) 60%, transparent)");
    expect(out).toContain("color-mix(in srgb, var(--surface-2) 50%, transparent)");
    expect(out).toContain("color-mix(in srgb, var(--brand) 10%, transparent)");
    expect(out).toContain("color-mix(in srgb, var(--warn) 10%, transparent)");
    expect((out.match(/\{/g) ?? []).length).toBeGreaterThanOrEqual(7);                          // a rule for each class, none silently dropped
  });

  it("work for the groups of colours too (severity, chart, diff)", async () => {
    const out = await css("text-sev-high/50 fill-chart-3/20 bg-diff-add-bg/30");
    expect(out).toContain("color-mix(in srgb, var(--sev-high) 50%, transparent)");
    expect(out).toContain("color-mix(in srgb, var(--chart-3) 20%, transparent)");
    expect(out).toContain("color-mix(in srgb, var(--diff-add-bg) 30%, transparent)");
  });

  it("leave a colour used plainly as the variable itself", async () => {
    const out = await css("bg-brand text-fg border-line bg-diff-add-bg text-sev-critical");
    expect(out).not.toContain("color-mix");
    for (const v of ["--brand", "--fg", "--border", "--diff-add-bg", "--sev-critical"]) expect(out).toContain(`var(${v})`);
  });
});
