import { endpoint, get, write } from "./client";

export type RuntimePreparation = { chat_id: string; model_id: string; ready: boolean; state: "queued" | "loading" | "ready" | "failed"; phase?: string; message?: string; elapsed_seconds?: number; error?: { message?: string } };
export type PreparationProgress = { phase?: string; message?: string; elapsed_seconds?: number; cancellable?: boolean };

export async function prepareChatRuntime(chatId: string, modelId: string, report?: (status: RuntimePreparation) => void, signal?: AbortSignal) {
  const check = (status: RuntimePreparation) => {
    if (signal?.aborted) throw new Error("Chat preparation cancelled. No message was sent.");
    if (!status || status.model_id !== modelId) throw new Error("The chat model changed during agent preparation. Try again.");
    report?.(status);
    if (status.state === "failed") throw new Error(status.error?.message || status.message || "The coding agent could not start. Retry preparation.");
    return status;
  };
  if (signal?.aborted) throw new Error("Chat preparation cancelled. No message was sent.");
  let status = check(await write<RuntimePreparation>(`${endpoint(chatId)}/runtime/prepare`));
  while (!status.ready) {
    await new Promise<void>((resolve, reject) => {
      const abort = () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); reject(new Error("Chat preparation cancelled. No message was sent.")); };
      const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, 650);
      if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
    });
    status = check(await get<RuntimePreparation>(`${endpoint(chatId)}/runtime-status`, signal));
  }
  return status;
}
