import { describe, expect, it } from "vitest";
import { canDelete, canPause, canResume, elapsed, percentOf, resumeWord, stageWord, whereItIs } from "./campaignFormat";
import { campaign } from "./fixtures";

describe("what a campaign says about itself", () => {
  it("explains missing sources when every collection step completed", () => {
    expect(whereItIs(campaign({ state: "partial", summary: {
      unresolved_items: 40, steps: { total: 7, done: 7, error: 0, pending: 0, paused: 0, quota: 0, skipped: 0 },
    } }))).toBe("Collection finished; 40 source commits unresolved");
  });
  it("says where it is in words, with the ecosystem and the step", () => {
    expect(whereItIs(campaign())).toBe("Mine the code pairs (npm), step 3 of 4");
    expect(whereItIs(campaign({ progress: { target: "*", stage: "build", step: 4, steps: 4 } }))).toBe("Build a draft dataset, step 4 of 4");
    expect(whereItIs(campaign({ progress: {} }))).toBe("Starting");
    expect(whereItIs(campaign({ state: "paused", progress: {} }))).toBe("Not started");
    expect(whereItIs(campaign({ state: "done" }))).toBe("Finished");
    expect(whereItIs(campaign({ state: "partial", summary: { steps: { total: 4, done: 2, quota: 0, skipped: 0, error: 2, paused: 0, pending: 0 } } }))).toBe("2 step(s) failed");
    expect(whereItIs(campaign({ state: "failed" }))).toBe("No step finished");
    expect(stageWord("dep_versions")).toBe("Check dependency versions");
    expect(stageWord("unknown_stage")).toBe("unknown stage");
  });

  it("gives a percentage between 0 and 100, 100 when it is done", () => {
    expect(percentOf(campaign())).toBe(62.5);
    expect(percentOf(campaign({ progress: { percent: 140 } }))).toBe(100);
    expect(percentOf(campaign({ progress: {} }))).toBe(0);
    expect(percentOf(campaign({ state: "done", progress: { percent: 80 } }))).toBe(100);
  });

  it("offers pause only to a running campaign, resume to one that stopped, and delete to one that is not working", () => {
    const can = (state: any, legacy = false) => [canPause(campaign({ state, legacy })), canResume(campaign({ state, legacy })), canDelete({ state } as any)];
    expect(can("running")).toEqual([true, false, false]);
    expect(can("pausing")).toEqual([false, false, false]);
    expect(can("paused")).toEqual([false, true, true]);
    expect(can("interrupted")).toEqual([false, true, true]);
    expect(can("partial")).toEqual([false, true, true]);
    expect(can("failed")).toEqual([false, true, true]);
    expect(can("done")).toEqual([false, false, true]);
    expect(can("paused", true)).toEqual([false, false, true]);
    expect(resumeWord({ state: "partial" })).toBe("Retry the failed steps");
    expect(resumeWord({ state: "paused" })).toBe("Resume");
  });

  it("measures the time from its start to its end, or to now while it works", () => {
    expect(elapsed(campaign({ state: "done", finished_at: "2026-10-10T11:30:15Z" }))).toBe("1:30:15");
    expect(elapsed(campaign({ started_at: "2026-10-10T10:00:00Z" }), Date.parse("2026-10-10T10:02:05Z"))).toBe("2:05");
    expect(elapsed(campaign({ started_at: null }))).toBeNull();
  });
});
