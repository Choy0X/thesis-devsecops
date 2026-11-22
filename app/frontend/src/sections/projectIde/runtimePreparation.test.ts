import { expect, it, vi } from "vitest";
import { prepareChatRuntime } from "./runtimePreparation";
import { get, write } from "./client";

vi.mock("./client", async original => ({ ...await original<typeof import("./client")>(), get: vi.fn(), write: vi.fn() }));

it("waits for actual native readiness before continuing and reports real startup phases", async () => {
  vi.mocked(write).mockResolvedValue({ chat_id: "chat", model_id: "model", ready: false, state: "loading", phase: "connecting_tools", message: "Connecting tools" } as never);
  vi.mocked(get).mockResolvedValue({ chat_id: "chat", model_id: "model", ready: true, state: "ready", phase: "ready" } as never);
  const report = vi.fn();
  await prepareChatRuntime("chat", "model", report);
  expect(report).toHaveBeenCalledWith(expect.objectContaining({ phase: "connecting_tools" }));
  expect(write).toHaveBeenCalledWith("/api/chats/chat/runtime/prepare");
  expect(get).toHaveBeenCalledWith("/api/chats/chat/runtime-status", undefined);
});

it("rejects a failed runtime or a different model instead of submitting a message", async () => {
  vi.mocked(write).mockResolvedValueOnce({ chat_id: "chat", model_id: "model", ready: false, state: "failed", error: { message: "Tools unavailable" } } as never);
  await expect(prepareChatRuntime("chat", "model")).rejects.toThrow("Tools unavailable");
  vi.mocked(write).mockResolvedValueOnce({ chat_id: "chat", model_id: "other", ready: true, state: "ready" } as never);
  await expect(prepareChatRuntime("chat", "model")).rejects.toThrow("model changed");
});
