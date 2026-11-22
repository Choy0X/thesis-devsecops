import { describe, expect, it } from "vitest";
import { parseDiff } from "../components/hud/DiffViewer";
import { lineDiff } from "./RecordPairs";

describe("lineDiff (the pair editor's live preview)", () => {
  it("is one unified hunk that the viewer reads as added, removed and unchanged lines", () => {
    const diff = lineDiff("a\nb\nc", "a\nx\nc")!;
    expect(diff.split("\n")[0]).toBe("@@ -1,3 +1,3 @@");
    expect(parseDiff(diff).map((r) => [r.kind, r.text])).toEqual([["hunk", "@@ -1,3 +1,3 @@"], ["ctx", "a"], ["del", "b"], ["add", "x"], ["ctx", "c"]]);
  });

  it("shows a new text as additions only, with no phantom empty removed line", () => {
    const rows = parseDiff(lineDiff("", "one\ntwo")!);
    expect(rows.map((r) => r.kind)).toEqual(["hunk", "add", "add"]);
  });

  it("gives up with null when the texts are too large to compare in the browser", () => {
    const big = Array.from({ length: 2100 }, (_, i) => `line ${i}`).join("\n");
    expect(lineDiff(big, big + "\nextra")).toBeNull();
  });
});
