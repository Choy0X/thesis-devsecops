import { beforeEach, describe, expect, it, vi } from "vitest";
import { isChunkError, reloadOnce } from "./staleBuild";

describe("isChunkError", () => {
  it("recognises a page file that could not be fetched, in the words each browser uses", () => {
    expect(isChunkError(new TypeError("Failed to fetch dynamically imported module: http://127.0.0.1:58334/assets/RecordsPage-BwU4vUME.js"))).toBe(true);
    expect(isChunkError(new TypeError("error loading dynamically imported module: http://x/assets/a.js"))).toBe(true);
    expect(isChunkError(new TypeError("Importing a module script failed."))).toBe(true);
    const named = new Error("Loading chunk 7 failed."); named.name = "ChunkLoadError";
    expect(isChunkError(named)).toBe(true);
  });
  it("leaves every other error alone", () => {
    expect(isChunkError(new Error("the answer had no list"))).toBe(false);
    expect(isChunkError(new TypeError("Cannot read properties of undefined"))).toBe(false);
    expect(isChunkError(null)).toBe(false);
    expect(isChunkError(undefined)).toBe(false);
  });
});

describe("reloadOnce", () => {
  beforeEach(() => { sessionStorage.clear(); });
  it("reloads the window once, and not again within half a minute, so a missing file cannot make it loop", () => {
    const reload = vi.fn();
    expect(reloadOnce(1_000_000, reload)).toBe(true);
    expect(reloadOnce(1_000_000 + 5_000, reload)).toBe(false);
    expect(reloadOnce(1_000_000 + 29_999, reload)).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(reloadOnce(1_000_000 + 30_001, reload)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });
  it("does not reload when it cannot remember that it did (no storage), because it could not stop a loop", () => {
    const reload = vi.fn();
    vi.stubGlobal("sessionStorage", { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } });
    expect(reloadOnce(5_000_000, reload)).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
