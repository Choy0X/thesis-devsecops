import { describe, expect, it } from "vitest";
import { KEEP, LogBuffer, VIEW, toneOf } from "./logBuffer";

const lines = (n: number, from = 0) => Array.from({ length: n }, (_, i) => `line ${from + i}`);

describe("toneOf", () => {
  it("colours a line by its words, once", () => {
    expect(toneOf("Traceback (most recent call last)")).toBe("error");
    expect(toneOf("rate limit reached")).toBe("error");
    expect(toneOf("skipping 3 files")).toBe("warn");
    expect(toneOf("written to data/processed")).toBe("ok");
    expect(toneOf("processing batch 4")).toBe("plain");
    expect(toneOf("failed but done")).toBe("error");                // the first matching rule wins, as before
    expect(toneOf("Warning: You are sending unauthenticated requests. Set a token for higher rate limits.")).toBe("warn");          // it says it is a warning
    expect(toneOf("  WARN deprecated flag, error handling is unchanged")).toBe("warn");
    expect(toneOf("the warning light failed")).toBe("error");        // only a line that begins with the word is a warning line
  });
});

describe("LogBuffer", () => {
  it("shows the last lines and counts the rest as earlier", () => {
    const b = new LogBuffer();
    b.push(lines(1000));
    const shown = b.window();
    expect(shown).toHaveLength(VIEW);
    expect(shown[VIEW - 1].text).toBe("line 999");
    expect(b.earlier()).toBe(1000 - VIEW);
  });

  it("gives every line a number that never changes, so rows keep their identity", () => {
    const b = new LogBuffer();
    b.push(lines(10));
    const before = b.window().map((l) => l.id);
    b.push(lines(5, 10));
    const after = b.window().map((l) => l.id);
    expect(after.slice(0, 10)).toEqual(before);
    expect(after).toEqual([...Array(15).keys()]);
  });

  it("draws a redraw over its row: one row for a bar, and the entries are still counted", () => {
    const b = new LogBuffer();
    b.push(["start", "bar  10%\r", "bar  60%\r", "bar 100%", "end"]);
    expect(b.window().map((l) => l.text)).toEqual(["start", "bar 100%", "end"]);
    expect(b.received).toBe(5);
    expect(b.earlier()).toBe(0);
  });

  it("keeps colours as runs, and no escape code in the text", () => {
    const b = new LogBuffer();
    b.push(["\x1b[31mred\x1b[0m and plain"]);
    const [row] = b.window();
    expect(row.text).toBe("red and plain");
    expect(row.runs.map((r) => [r.text, r.fg])).toEqual([["red", "red"], [" and plain", null]]);
  });

  it("gives a row that did not change the same object, and a changed row a new revision", () => {
    const b = new LogBuffer();
    b.push(["fixed", "bar 1%\r"]);
    const [fixed, bar] = b.window();
    b.push(["bar 2%\r"]);
    const [fixed2, bar2] = b.window();
    expect(fixed2).toBe(fixed);
    expect(bar2).not.toBe(bar);
    expect([bar2.id, bar2.text]).toEqual([bar.id, "bar 2%"]);
    expect(bar2.rev).toBeGreaterThan(bar.rev);
  });

  it("gives every kept row as plain text for Copy", () => {
    const b = new LogBuffer();
    b.push(["a", "x\r", "y", "\x1b[1mb\x1b[0m"]);
    expect(b.text()).toBe("a\ny\nb");
    const many = new LogBuffer();
    many.push(lines(VIEW + 40));
    expect(many.text().split("\n")).toEqual(lines(VIEW + 40));              // all the kept rows, not only the ones a window shows
  });

  it("counts earlier rows, not entries", () => {
    const b = new LogBuffer();
    b.push([...lines(VIEW + 5), ...Array.from({ length: 50 }, (_, i) => `bar ${i}%\r`)]);        // the fifty redraws are one row
    expect(b.received).toBe(VIEW + 55);
    expect(b.earlier()).toBe(6);
  });

  it("keeps the numbers going when the old lines scroll out of the window", () => {
    const b = new LogBuffer();
    b.push(lines(VIEW + 50));
    expect(b.window()[0].id).toBe(50);
  });

  it("lets each reader ask for its own window", () => {
    const b = new LogBuffer();
    b.push(lines(1000));
    expect(b.window(VIEW * 2)).toHaveLength(VIEW * 2);
    expect(b.earlier(VIEW * 2)).toBe(1000 - VIEW * 2);
    expect(b.window(VIEW)).toHaveLength(VIEW);                      // another reader's window is unchanged
    expect(b.window(10_000)).toHaveLength(1000);
    expect(b.earlier(10_000)).toBe(0);
  });

  it("remembers a bounded number of lines", () => {
    const b = new LogBuffer();
    b.push(lines(KEEP + 500));
    expect(b.window(KEEP * 2)).toHaveLength(KEEP);
    expect(b.window(KEEP * 2)[0].text).toBe("line 500");
    expect(b.received).toBe(KEEP + 500);
  });

  it("starts over on reset", () => {
    const b = new LogBuffer();
    b.push(lines(500));
    b.reset(["a", "b"]);
    expect(b.window().map((l) => l.text)).toEqual(["a", "b"]);
    expect(b.window()[0].id).toBe(0);
    expect(b.earlier()).toBe(0);
  });

  it("places lines by the server's numbering, appending only what is new", () => {
    const b = new LogBuffer();
    b.place(0, lines(5));
    b.place(3, lines(5, 3));                                        // an overlap: the first two are already here
    expect(b.window().map((l) => l.text)).toEqual(lines(8));
    b.place(8, lines(2, 8));
    expect(b.received).toBe(10);
    b.place(0, lines(10));                                          // everything again: nothing is added
    expect(b.received).toBe(10);
  });

  it("skips ahead when the server sends only the tail of a long log", () => {
    const b = new LogBuffer();
    b.place(3000, lines(3, 3000));
    expect(b.window().map((l) => l.id)).toEqual([0, 1, 2]);        // a row has a number of its own (a redraw makes entries and rows differ); the entries keep the server's count (received)
    expect(b.received).toBe(3003);
    b.place(3003, lines(2, 3003));
    expect(b.window().map((l) => l.text)).toEqual(lines(5, 3000));
  });
});
