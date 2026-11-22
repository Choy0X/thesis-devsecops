import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { projectChatCompact: vi.fn(), projectChatClear: vi.fn(), servingStatus: vi.fn(), gpuMode: vi.fn(), respondProject: vi.fn(), getJob: vi.fn(), skillGet: vi.fn() } };
});
import { api, type ChatParams, type Project } from "../../lib/api";
import type { ViewMessage } from "./MessageView";
import { runCommand, type CommandContext } from "./commands";

const project = { id: "shop-aaaaaa", name: "Shop" } as Project;
const params = (over: Partial<ChatParams> = {}): ChatParams => ({ system: "", temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false, ...over });

function makeCtx(over: Partial<CommandContext> = {}) {
  const results: { id: string; text: string; error?: string }[] = [];
  let seq = 0;
  const ctx: CommandContext = {
    project, chat: { id: "chat1" }, chats: [], messages: [], params: params(), context: 4096,
    ask: vi.fn().mockResolvedValue(true),
    selectChat: vi.fn(),
    openConversationList: vi.fn(),
    clearMessages: vi.fn(),
    changeParams: vi.fn().mockResolvedValue(undefined),
    confirmRemote: vi.fn().mockResolvedValue(true),
    appendResult: (text: string) => { const id = `r${++seq}`; results.push({ id, text }); return id; },
    updateResult: (id: string, patch: Partial<ViewMessage>) => {
      const r = results.find((r) => r.id === id);
      if (r) { if (patch.content !== undefined) r.text = patch.content; if (patch.error !== undefined) r.error = patch.error; }
    },
    ...over,
  };
  return { ctx, results };
}

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
});

describe("the three commands with no real implementation yet", () => {
  it.each(["batch", "agents", "schedule"])("says /%s is not available yet, whatever the argument", async (name) => {
    const { ctx, results } = makeCtx();
    await runCommand(name, "anything at all", ctx);
    expect(results).toEqual([{ id: "r1", text: `/${name} is not available yet, planned for a later phase.` }]);
  });
});

describe("/skill", () => {
  it("asks for a name when given none", async () => {
    const { ctx, results } = makeCtx();
    await runCommand("skill", "", ctx);
    expect(results).toEqual([{ id: "r1", text: "/skill needs a name: /skill <name> (see Settings > Skills for what is approved)." }]);
    expect(api.skillGet).not.toHaveBeenCalled();
  });

  it("refuses an unapproved skill without touching the conversation's instructions", async () => {
    vi.mocked(api.skillGet).mockResolvedValue({ schema: "skill/1", name: "draft", description: "d", approved: false, created_at: "t", prompt_sha256: "x", prompt: "do X" });
    const { ctx, results } = makeCtx();
    await runCommand("skill", "draft", ctx);
    expect(results[0].text).toBe('"draft" is not approved yet; approve it in Settings > Skills before it can be applied.');
    expect(ctx.changeParams).not.toHaveBeenCalled();
  });

  it("appends an approved skill's prompt to whatever instructions are already there", async () => {
    vi.mocked(api.skillGet).mockResolvedValue({ schema: "skill/1", name: "reviewer", description: "d", approved: true, created_at: "t", prompt_sha256: "x", prompt: "Focus on security." });
    const { ctx, results } = makeCtx({ params: params({ system: "Be terse." }) });
    await runCommand("skill", "reviewer", ctx);
    expect(ctx.changeParams).toHaveBeenCalledWith({ system: "Be terse.\n\nFocus on security." });
    expect(results[0].text).toBe('Applied "reviewer" to this conversation\'s instructions.');
  });

  it("applies whole when there were no instructions yet", async () => {
    vi.mocked(api.skillGet).mockResolvedValue({ schema: "skill/1", name: "reviewer", description: "d", approved: true, created_at: "t", prompt_sha256: "x", prompt: "Focus on security." });
    const { ctx } = makeCtx({ params: params({ system: "" }) });
    await runCommand("skill", "reviewer", ctx);
    expect(ctx.changeParams).toHaveBeenCalledWith({ system: "Focus on security." });
  });

  it("refuses to apply a skill that would push the instructions over the limit, and never calls changeParams", async () => {
    vi.mocked(api.skillGet).mockResolvedValue({ schema: "skill/1", name: "huge", description: "d", approved: true, created_at: "t", prompt_sha256: "x", prompt: "x".repeat(4001) });
    const { ctx, results } = makeCtx();
    await runCommand("skill", "huge", ctx);
    expect(ctx.changeParams).not.toHaveBeenCalled();
    expect(results[0].text).toContain("the limit is 4,000");
  });

  it("reports a skill that does not exist as an error", async () => {
    vi.mocked(api.skillGet).mockRejectedValue(new Error("There is no such skill."));
    const { ctx, results } = makeCtx();
    await runCommand("skill", "nothing", ctx);
    expect(results[0].error).toBe("There is no such skill.");
  });
});

describe("/resume", () => {
  it("opens the conversation list when given no name", async () => {
    const { ctx } = makeCtx();
    await runCommand("resume", "", ctx);
    expect(ctx.openConversationList).toHaveBeenCalledTimes(1);
    expect(ctx.selectChat).not.toHaveBeenCalled();
  });

  it("switches to the conversation whose title or preview matches, case-insensitively", async () => {
    const { ctx } = makeCtx({ chats: [{ id: "a", title: "Auth bug", preview: "..." }, { id: "b", title: "Other", preview: "eval() in app.py" }] });
    await runCommand("resume", "AUTH", ctx);
    expect(ctx.selectChat).toHaveBeenCalledWith("a");
    await runCommand("resume", "eval", ctx);
    expect(ctx.selectChat).toHaveBeenCalledWith("b");
  });

  it("says so when nothing matches", async () => {
    const { ctx, results } = makeCtx({ chats: [{ id: "a", title: "Auth bug", preview: "" }] });
    await runCommand("resume", "nothing like this", ctx);
    expect(ctx.selectChat).not.toHaveBeenCalled();
    expect(results[0].text).toBe('No conversation matches "nothing like this".');
  });
});

describe("/compact", () => {
  it("says there is nothing to compact yet when no conversation is open", async () => {
    const { ctx, results } = makeCtx({ chat: null });
    await runCommand("compact", "", ctx);
    expect(results[0].text).toContain("nothing to compact yet");
    expect(api.projectChatCompact).not.toHaveBeenCalled();
  });

  it("does nothing when a remote endpoint was not confirmed", async () => {
    const { ctx, results } = makeCtx({ confirmRemote: vi.fn().mockResolvedValue(false) });
    await runCommand("compact", "", ctx);
    expect(api.projectChatCompact).not.toHaveBeenCalled();
    expect(results).toEqual([]);
  });

  it("reports what was folded when it compacted", async () => {
    vi.mocked(api.projectChatCompact).mockResolvedValue({ compacted: true, messages_before: 20, messages_after: 13, summary_chars: 400 });
    const { ctx, results } = makeCtx();
    await runCommand("compact", "", ctx);
    expect(api.projectChatCompact).toHaveBeenCalledWith("shop-aaaaaa", "chat1", true);
    expect(results[0].text).toBe("Folded the oldest 8 of 20 messages into a summary; 12 recent ones stay as they are.");
  });

  it("says there was nothing to compact when the conversation was too short", async () => {
    vi.mocked(api.projectChatCompact).mockResolvedValue({ compacted: false, messages_before: 2, messages_after: 2, summary_chars: 0 });
    const { ctx, results } = makeCtx();
    await runCommand("compact", "", ctx);
    expect(results[0].text).toContain("nothing to compact yet");
  });

  it("shows the server's refusal as an error", async () => {
    vi.mocked(api.projectChatCompact).mockRejectedValue(new Error("busy"));
    const { ctx, results } = makeCtx();
    await runCommand("compact", "", ctx);
    expect(results[0].error).toBeTruthy();
  });
});

describe("/clear", () => {
  it("just clears the local thread when no conversation is open yet", async () => {
    const { ctx } = makeCtx({ chat: null });
    await runCommand("clear", "", ctx);
    expect(ctx.clearMessages).toHaveBeenCalledTimes(1);
    expect(api.projectChatClear).not.toHaveBeenCalled();
  });

  it("empties the stored conversation, then the local thread", async () => {
    vi.mocked(api.projectChatClear).mockResolvedValue({} as any);
    const { ctx } = makeCtx();
    await runCommand("clear", "", ctx);
    expect(api.projectChatClear).toHaveBeenCalledWith("shop-aaaaaa", "chat1");
    expect(ctx.clearMessages).toHaveBeenCalledTimes(1);
  });

  it("keeps the thread and shows the error when clearing fails on the server", async () => {
    vi.mocked(api.projectChatClear).mockRejectedValue(new Error("offline"));
    const { ctx, results } = makeCtx();
    await runCommand("clear", "", ctx);
    expect(ctx.clearMessages).not.toHaveBeenCalled();
    expect(results[0].error).toBeTruthy();
  });
});

describe("/autocompact", () => {
  it("toggles when given no word", async () => {
    const { ctx } = makeCtx({ params: params({ autocompact: true }) });
    await runCommand("autocompact", "", ctx);
    expect(ctx.changeParams).toHaveBeenCalledWith({ autocompact: false });
  });

  it("sets it explicitly with on or off", async () => {
    const { ctx: onCtx } = makeCtx({ params: params({ autocompact: false }) });
    await runCommand("autocompact", "on", onCtx);
    expect(onCtx.changeParams).toHaveBeenCalledWith({ autocompact: true });
    const { ctx: offCtx } = makeCtx({ params: params({ autocompact: true }) });
    await runCommand("autocompact", "off", offCtx);
    expect(offCtx.changeParams).toHaveBeenCalledWith({ autocompact: false });
  });
});

describe("/context and /usage", () => {
  it("/context reports the token estimate and the automatic-compaction status, purely locally", async () => {
    const { ctx, results } = makeCtx({ params: params({ autocompact: false }), context: 4096 });
    await runCommand("context", "", ctx);
    expect(results[0].text).toContain("tokens of context");
    expect(results[0].text).toContain("Automatic compaction is off.");
  });

  it("/usage sums the tokens already reported by this conversation's answers", async () => {
    const messages: ViewMessage[] = [
      { id: "1", role: "user", content: "q", created_at: "" },
      { id: "2", role: "assistant", content: "a", created_at: "", usage: { prompt_tokens: 100, completion_tokens: 40 } },
      { id: "3", role: "assistant", content: "b", created_at: "", usage: { prompt_tokens: 150, completion_tokens: 20 } },
    ];
    const { ctx, results } = makeCtx({ messages });
    await runCommand("usage", "", ctx);
    expect(results[0].text).toBe("This conversation has used about 250 prompt tokens and 60 completion tokens over 2 answers.");
  });

  it("/usage says so when nothing has reported its usage yet", async () => {
    const { ctx, results } = makeCtx({ messages: [{ id: "1", role: "user", content: "q", created_at: "" }] });
    await runCommand("usage", "", ctx);
    expect(results[0].text).toBe("No answer in this conversation has reported its token usage yet.");
  });
});

describe("/deep-research and /plan", () => {
  it("need a question, and say so without asking anything when none is given", async () => {
    const { ctx, results } = makeCtx();
    await runCommand("deep-research", "", ctx);
    expect(ctx.ask).not.toHaveBeenCalled();
    expect(results[0].text).toContain("needs a question");
  });

  it("ask with the extra mode, the command word itself never part of the saved question", async () => {
    const { ctx } = makeCtx();
    await runCommand("deep-research", "why does this leak memory?", ctx);
    expect(ctx.ask).toHaveBeenCalledWith("why does this leak memory?", { deepResearch: true, forceWebSearch: true });
    await runCommand("plan", "how should I fix the auth bug?", ctx);
    expect(ctx.ask).toHaveBeenCalledWith("how should I fix the auth bug?", { planMode: true });
  });
});

describe("/status", () => {
  it("reports the model and GPU status", async () => {
    vi.mocked(api.servingStatus).mockResolvedValue({
      slots: { llm: { state: "ready", model: "qwen-7b.gguf", runs_on: "gpu", in_flight: 0, served: 4 } },
      choices: { llm: { engine: "local", model: "qwen-7b.gguf" } }, queue: [],
    } as any);
    vi.mocked(api.gpuMode).mockResolvedValue({ mode: "serve", holders: [] } as any);
    const { ctx, results } = makeCtx();
    await runCommand("status", "", ctx);
    expect(results[0].text).toContain("qwen-7b.gguf");
    expect(results[0].text).toContain("GPU mode: serve.");
  });
});

describe("/security-review", () => {
  it("starts a review and polls the job until it finishes", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.respondProject).mockResolvedValue({ id: "job1", stage: "respond_project" });
      vi.mocked(api.getJob)
        .mockResolvedValueOnce({ status: "running", progress: { label: "scanning" } } as any)
        .mockResolvedValueOnce({ status: "done" } as any);
      const { ctx, results } = makeCtx();
      const done = runCommand("security-review", "", ctx);
      await vi.advanceTimersByTimeAsync(1300);
      await vi.advanceTimersByTimeAsync(1300);
      await done;
      expect(results[0].text).toBe("Security review finished. See the Response tab of this project for the full report.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the job's own error when the review fails", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.respondProject).mockResolvedValue({ id: "job1", stage: "respond_project" });
      vi.mocked(api.getJob).mockResolvedValueOnce({ status: "error", error: "the sandbox could not start" } as any);
      const { ctx, results } = makeCtx();
      const done = runCommand("security-review", "", ctx);
      await vi.advanceTimersByTimeAsync(1300);
      await done;
      expect(results[0].error).toBe("the sandbox could not start");
    } finally {
      vi.useRealTimers();
    }
  });
});
