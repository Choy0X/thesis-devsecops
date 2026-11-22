import { describe, expect, it } from "vitest";
import { acceptSave, commandUsage, fitChatPercent, isDirty, parseCommand, reconcileBuffer, type FileBuffer } from "./state";

it("reserves the explorer, editor and divider when restoring a wide agent panel", () => {
  const percent = fitChatPercent(65, 945, 180);
  expect(percent * 945 / 100 + 180 + 260 + 4).toBeCloseTo(945);
  expect(percent).toBeLessThan(65);
  expect(fitChatPercent(25, 945, 180) * 945 / 100).toBeGreaterThanOrEqual(300);
});

it("adapts the split to the measured work area and collapsed explorer", () => {
  const narrower = fitChatPercent(65, 915, 180);
  expect(narrower * 915 / 100 + 180 + 260 + 4).toBeCloseTo(915);
  expect(fitChatPercent(65, 945, 0)).toBe(65);
  expect(fitChatPercent(40, 0, 0)).toBe(40);
});

describe("project terminal commands", () => {
  it("offers commands when typing the slash alone", () => {
    expect(parseCommand("/")).toMatchObject({ kind: "menu", choices: [{ command: "/plan" }, { command: "/resume" }, { command: "/new" }, { command: "/scan" }, { command: "/diff" }, { command: "/help" }] });
  });
  it("parses resume and preserves the title query", () => {
    expect(parseCommand("/resume")).toEqual({ kind: "resume", query: "" });
    expect(parseCommand("/RESUME  Login Bug ")).toEqual({ kind: "resume", query: "Login Bug" });
  });
  it("parses new and help and rejects arguments they do not accept", () => {
    expect(parseCommand("/new")).toEqual({ kind: "new" });
    expect(parseCommand("/help")).toEqual({ kind: "help" });
    expect(parseCommand("/new later")).toEqual({ kind: "invalid", usage: commandUsage });
    expect(parseCommand("/frobnicate")).toEqual({ kind: "invalid", usage: commandUsage });
  });
  it("keeps incomplete commands in local menus", () => {
    expect(parseCommand("/scan")).toMatchObject({ kind: "menu", choices: [{ command: "/scan code" }, { command: "/scan model" }, { command: "/scan dependencies" }] });
    expect(parseCommand(" /scan dependencies ")).toMatchObject({ kind: "menu", choices: [{ command: "/scan dependencies versions" }, { command: "/scan dependencies source" }] });
  });
  it("distinguishes code, advisories, and selected source review", () => {
    expect(parseCommand("/scan code")).toEqual({ kind: "scan", scope: "project_code" });
    expect(parseCommand("/scan model")).toEqual({ kind: "scan", scope: "model_review" });
    expect(parseCommand("/scan dependencies versions")).toEqual({ kind: "scan", scope: "dependencies", dependency_mode: "versions" });
    expect(parseCommand("/scan dependencies source")).toEqual({ kind: "scan", scope: "dependencies", dependency_mode: "source" });
  });
  it.each(["/scan nonsense", "/scan code dependencies", "/scan dependencies source pkg", "/scan dependencies versions all", "/delete", "/scanner"])("rejects invalid command %s", command => {
    expect(parseCommand(command)).toMatchObject({ kind: "invalid", usage: expect.stringContaining("/scan dependencies source") });
  });
  it("keeps ordinary text ordinary", () => {
    expect(parseCommand("Explain /scan code")).toEqual({ kind: "text" });
    expect(parseCommand("hello /resume")).toEqual({ kind: "text" });
  });
});

describe("editor buffer protection", () => {
  const saved: FileBuffer = { path: "src/app.ts", text: "saved", saved: "saved", version: "v1" };
  it("reloads clean buffers when the agent changes the saved workspace", () => {
    expect(reconcileBuffer(saved, { text: "agent edit", version: "v2" })).toEqual({ path: saved.path, text: "agent edit", saved: "agent edit", version: "v2" });
  });
  it("preserves dirty contents and original save version when the agent changes the file", () => {
    const result = reconcileBuffer({ ...saved, text: "my unsaved work" }, { text: "agent edit", version: "v2" });
    expect(result).toEqual({ ...saved, text: "my unsaved work", conflict: true, remoteVersion: "v2" });
    expect(isDirty(result)).toBe(true);
  });
  it("does not erase typing that happens during an asynchronous save", () => {
    const result = acceptSave({ ...saved, text: "continued typing" }, "submitted snapshot", "v3");
    expect(result).toMatchObject({ text: "continued typing", saved: "submitted snapshot", version: "v3", conflict: false });
    expect(isDirty(result)).toBe(true);
  });
  it("does not create a conflict for unchanged saved files", () => {
    const dirty = { ...saved, text: "my edits" };
    expect(reconcileBuffer(dirty, { text: "saved", version: "v1" })).toBe(dirty);
  });
});
it("recognizes planning with and without a goal and preserves its wording", () => {
  expect(parseCommand("/pl")).toMatchObject({ kind: "menu", choices: [{ command: "/plan" }] });
  expect(parseCommand("/plan")).toEqual({ kind: "plan", goal: "" });
  expect(parseCommand("/PLAN Keep the Public API")).toEqual({ kind: "plan", goal: "Keep the Public API" });
});
