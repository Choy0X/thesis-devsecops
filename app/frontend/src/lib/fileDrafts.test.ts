import { beforeEach, expect, it, vi } from "vitest";
import { fileDraftsFor, resetFileDrafts, saveFileDrafts, unsavedFiles } from "./fileDrafts";

beforeEach(() => resetFileDrafts());

it("keeps drafts per conversation outside any component and counts them", () => {
  fileDraftsFor("one").set("a.py", { file: { path: "a.py", text: "a", version: "v1" }, text: "a2" });
  fileDraftsFor("two").set("b.py", { file: { path: "b.py", text: "b", version: "v1" }, text: "b2" });
  expect(fileDraftsFor("one")).toBe(fileDraftsFor("one"));
  expect(unsavedFiles("one")).toBe(1);
  expect(unsavedFiles()).toBe(2);
  fileDraftsFor("one").delete("a.py");
  expect(unsavedFiles()).toBe(1);
});

it("saves every draft of a conversation with its opened version and stops at the first failure", async () => {
  const drafts = fileDraftsFor("chat");
  drafts.set("a.py", { file: { path: "a.py", text: "a", version: "va" }, text: "a2" });
  drafts.set("b.py", { file: { path: "b.py", text: "b", version: "vb" }, text: "b2" });
  const save = vi.fn(async (path: string, text: string, version: string) => {
    if (path === "b.py") throw new Error("conflict");
    return { path, text, version: version + "-new" };
  });
  await expect(saveFileDrafts("chat", save)).rejects.toThrow("b.py could not be saved: conflict");
  expect(save).toHaveBeenCalledWith("a.py", "a2", "va");
  expect([...drafts.keys()]).toEqual(["b.py"]);
});
