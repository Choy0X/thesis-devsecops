import { describe, expect, it } from "vitest";
import { commitUrl, webUrl } from "./links";

describe("webUrl", () => {
  it("keeps http and https addresses", () => {
    expect(webUrl("https://example.org/a/b")).toBe("https://example.org/a/b");
    expect(webUrl("http://example.org/")).toBe("http://example.org");
  });
  it("refuses every other kind of address and anything that is not an address", () => {
    for (const bad of ["javascript:alert(1)", "JaVaScRiPt:alert(1)//github.com", "data:text/html,<b>x</b>", "file:///etc/passwd", "ftp://x/y", "github.com/o/r", "", null, undefined])
      expect(webUrl(bad as string | null | undefined)).toBeNull();
  });
});

describe("commitUrl", () => {
  it("builds the commit page on GitHub and on GitLab", () => {
    expect(commitUrl("https://github.com/o/r", "abcdef1234")).toBe("https://github.com/o/r/commit/abcdef1234");
    expect(commitUrl("https://gitlab.com/g/p", "ABCDEF1234")).toBe("https://gitlab.com/g/p/-/commit/ABCDEF1234");
  });
  it("makes no link for another host, a look-alike host or an unsafe address", () => {
    expect(commitUrl("https://example.org/o/r", "abcdef1234")).toBeNull();
    expect(commitUrl("https://github.com.evil.example/o/r", "abcdef1234")).toBeNull();
    expect(commitUrl("javascript:alert(1)//github.com", "abcdef1234")).toBeNull();
  });
  it("makes no link when the commit id is not a commit id", () => {
    expect(commitUrl("https://github.com/o/r", "main")).toBeNull();
    expect(commitUrl("https://github.com/o/r", "abc\"onmouseover=\"x")).toBeNull();
    expect(commitUrl("https://github.com/o/r", null)).toBeNull();
  });
});
