import { whenWords } from "./format";
import { describe, expect, it } from "vitest";
import { amountOf, bytes, clockSpan, fixed, num, pct } from "./format";

describe("format", () => {
  it("shows a dash for a missing value", () => {
    expect(pct(null)).toBe("—");
    expect(num(undefined)).toBe("—");
    expect(fixed(null)).toBe("—");
  });

  it("formats rates, counts and fixed decimals", () => {
    expect(pct(0.1234)).toBe("12.3%");
    expect(pct(0.5, 0)).toBe("50%");
    expect(num(1234567)).toBe("1,234,567");
    expect(fixed(2, 2)).toBe("2.00");
  });

  it("writes sizes, spans and amounts in the unit a job reports", () => {
    expect(bytes(512)).toBe("512 B");
    expect(bytes(740 * 1024)).toBe("740 kB");
    expect(bytes(12.34 * 1024 ** 2)).toBe("12.3 MB");
    expect(bytes(1.5 * 1024 ** 3)).toBe("1.5 GB");
    expect(bytes(null)).toBe("—");
    expect(clockSpan(75)).toBe("1:15");
    expect(clockSpan(3725)).toBe("1:02:05");
    expect(clockSpan(-3)).toBe("0:00");
    expect(amountOf(2048, "bytes")).toBe("2 kB");
    expect(amountOf(1200, "files")).toBe("1,200 files");
    expect([amountOf(1, "files"), amountOf(0, "files"), amountOf(2, "files")]).toEqual(["1 file", "0 files", "2 files"]);
    expect(amountOf(1200)).toBe("1,200");
  });
});

it("says when something was last used", () => {
    const now = 1_800_000_000;
    expect(whenWords(now - 5, now)).toBe("just now");
    expect(whenWords(now - 89, now)).toBe("just now");
    expect(whenWords(now - 600, now)).toBe("10 min ago");
    expect(whenWords(now - 3 * 3600, now)).toBe("3 h ago");
    expect(whenWords(now - 3 * 86400, now)).toBe("3 days ago");
    expect(whenWords(now + 50, now)).toBe("just now");
  });
