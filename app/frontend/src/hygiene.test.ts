import { describe, expect, it } from "vitest";

// Source-level rules of the interface. Colours live in the design tokens (index.css) and are used through classes or variables, so a theme or a
// palette change is a change of one block. The sources are read as text by Vite, so this needs no Node typings.
const sources = import.meta.glob(["./**/*.ts", "./**/*.tsx", "!./**/*.test.ts", "!./**/*.test.tsx"], {
  query: "?raw", import: "default", eager: true,
}) as Record<string, string>;

const offending = (pattern: RegExp): string[] =>
  Object.entries(sources).flatMap(([file, text]) =>
    text.split(/\r?\n/).flatMap((line: string, i: number) => (pattern.test(line.replace(/&#\d+;/g, "")) ? [`${file}:${i + 1}: ${line.trim().slice(0, 80)}`] : [])));

describe("source hygiene", () => {
  it("finds the sources it is meant to check", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(40);
  });

  it("has no hex colour outside the token file", () => {
    expect(offending(/#[0-9a-fA-F]{3,8}\b/)).toEqual([]);
  });

  it("has no raw rgb, rgba or hsl literal in a style", () => {
    expect(offending(/\b(rgba?|hsla?)\(/)).toEqual([]);
  });

  it("uses no native confirm, prompt or alert (the AlertDialog and Dialog ask instead)", () => {
    expect(offending(/(?<![\w.])(confirm|prompt|alert)\(\s*["'`]/)).toEqual([]);
    expect(offending(/\bwindow\.(confirm|prompt|alert)\(/)).toEqual([]);
  });

  it("has no native select, input or textarea outside the kit (the pages use Select, Input, Textarea, Checkbox, Radio, Switch, FileInput)", () => {
    const native = /<(select|input|textarea)[\s>/]/;
    const outside = Object.entries(sources).filter(([file]) => !file.startsWith("./components/kit/"));
    expect(outside.length).toBeGreaterThan(40);
    const found = outside.flatMap(([file, text]) =>
      text.split(/\r?\n/).flatMap((line: string, i: number) => (native.test(line) ? [`${file}:${i + 1}: ${line.trim().slice(0, 80)}`] : [])));
    expect(found).toEqual([]);
  });

  // A translucent layer over the whole window made the real desktop window flicker on the graphics card while a dialog was scrolled or typed in. A layer behind
  // a dialog is the `scrim` class (clear, the page is dimmed by a filter); a full-window layer that is not meant to show the page is fully opaque.
  it("has no translucent layer over the whole window (a fixed inset-0 box with a background that is not opaque)", () => {
    const translucent = /fixed\s+inset-0[^"'`]*\b(bg|from|to|via)-[\w-]+\/\d+/;
    expect(offending(translucent)).toEqual([]);
    expect(offending(/\b(bg|from|to|via)-[\w-]+\/\d+[^"'`]*\bfixed\s+inset-0/)).toEqual([]);
  });
});
