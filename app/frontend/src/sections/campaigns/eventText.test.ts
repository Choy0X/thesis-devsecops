import { describe, expect, it } from "vitest";
import { detailSummary, eventLook, stamp } from "./eventText";

const ev = (kind: string, data: Record<string, any> = {}) => ({ id: 1, at: "2026-10-10T10:04:30Z", kind, data });

describe("the story of a campaign in sentences", () => {
  it("says what each kind of event means", () => {
    expect(eventLook(ev("created", { ecosystems: ["npm", "PyPI"] })).text).toBe("Campaign created for npm, PyPI.");
    expect(eventLook(ev("started", { steps: 7, resumes: 2 })).text).toBe("A process started to work through 7 steps (resumed 2 times before).");
    expect(eventLook(ev("stage_started", { target: "npm", stage: "mine", index: 3, of: 7 })).text).toBe("Started: Mine the code pairs (npm), step 3 of 7.");
    expect(eventLook(ev("stage_done", { target: "npm", stage: "mine", status: "quota", seconds: 12.5 })).text).toBe("Finished: Mine the code pairs (npm), stopped at the quota in 12.5 s.");
    expect(eventLook(ev("stage_done", { target: "*", stage: "build", status: "skipped" })).text).toBe("Finished: Build a draft dataset, skipped.");
    expect(eventLook(ev("pause_requested")).text).toMatch(/Asked to pause/);
    expect(eventLook(ev("paused", { target: "npm", stage: "mine" })).text).toBe("Paused during Mine the code pairs (npm). What it had done is saved.");
    expect(eventLook(ev("resumed")).text).toBe("Resumed from where it stopped.");
    expect(eventLook(ev("finished", { state: "partial" })).text).toBe("The campaign finished: partial.");
  });

  it("marks what went wrong as a problem and says why", () => {
    const failed = eventLook(ev("stage_error", { target: "npm", stage: "mine", cause: "rate_limit", message: "GitHub API rate limit" }));
    expect(failed).toEqual({ text: "Failed: Mine the code pairs (npm), rate limit: GitHub API rate limit", tone: "problem" });
    expect(eventLook(ev("interrupted", { why: "the process is gone" }))).toEqual({ text: "Interrupted: the process is gone. Resume continues from the last saved step.", tone: "problem" });
    expect(eventLook(ev("forced_kill", { after_seconds: 130 })).text).toMatch(/did not stop in time \(130 s\)/);
    expect(eventLook(ev("finished", { state: "failed" })).tone).toBe("problem");
    expect(eventLook(ev("finished", { state: "done" })).tone).toBe("good");
  });

  it("shows a kind it does not know by its name", () => {
    expect(eventLook(ev("something_new")).text).toBe("something new");
  });

  it("writes a time the same in every time zone", () => {
    expect(stamp("2026-10-10T10:04:30Z")).toBe("2026-10-10 10:04 UTC");
    expect(stamp(null)).toBe("—");
  });

  it("sums a step up in a few words", () => {
    expect(detailSummary({ rows: 12345, advisories: 99, nested: { a: 1 }, ok: true, more: 1, evenmore: 2 })).toBe("rows 12,345, advisories 99, ok true, more 1");
    expect(detailSummary({ skipped: "all 15 mined commits of npm were already checked (mining checks each commit as it mines it)", already_checked: 15 }))
      .toBe("skipped: all 15 mined commits of npm were already checked (mining checks each commit as it mines it) (already checked 15)");
    expect(detailSummary({ skipped: 3, mined: 4 })).toBe("skipped 3, mined 4");                            // a count that is called skipped is a number like any other
    expect(detailSummary(null)).toBe("");
    expect(detailSummary({ a: 1, b: 2, c: 3 }, 2)).toBe("a 1, b 2");
  });
});
