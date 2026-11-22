import { api, errorText, type ChatParams, type Project } from "../../lib/api";
import { num } from "../../lib/format";
import { COMMANDS } from "../../lib/commands";
import type { ViewMessage } from "./MessageView";
import { approxTokens, contextWords } from "./ParamsPanel";

// What a recognized slash command does. Kept out of ChatWorkspace.tsx so it stays small and this stays independently testable: ``ctx`` bundles the state and handlers ChatWorkspace
// already has (or two small new ones, appendResult/updateResult/clearMessages, for pushing a local, never-saved reply into the thread). A command result is a synthetic assistant
// message (see MessageView's ``command`` field): it reads like the rest of the transcript but is never sent to the store, so it does not survive a reload -- acceptable, the same way
// Claude Code's own /status or /context do not persist across sessions either.

export type ChatSummaryLike = { id: string; title: string; preview: string };

export type CommandContext = {
  project: Project;
  chat: { id: string } | null;
  chats: ChatSummaryLike[] | null;
  messages: ViewMessage[];
  params: ChatParams;
  context: number | null;
  ask: (text: string, opts?: { deepResearch?: boolean; planMode?: boolean; forceWebSearch?: boolean }) => Promise<boolean>;
  selectChat: (id: string) => void;
  openConversationList: () => void;
  clearMessages: () => void;
  changeParams: (patch: Partial<ChatParams>) => Promise<void>;
  confirmRemote: () => Promise<boolean>;
  /** Pushes a local, never-saved reply into the thread; returns its id, for ``updateResult`` once an async command has its answer. */
  appendResult: (text: string, name: string) => string;
  updateResult: (id: string, patch: Partial<ViewMessage>) => void;
};

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function runResume(arg: string, ctx: CommandContext) {
  if (!arg) { ctx.openConversationList(); return; }
  const q = arg.toLowerCase();
  const hit = (ctx.chats ?? []).find((c) => c.title.toLowerCase().includes(q) || c.preview.toLowerCase().includes(q));
  if (hit) ctx.selectChat(hit.id);
  else ctx.appendResult(`No conversation matches "${arg}".`, "resume");
}

async function runCompact(ctx: CommandContext) {
  if (!ctx.chat) { ctx.appendResult("There is nothing to compact yet: start a conversation first.", "compact"); return; }
  const allowRemote = await ctx.confirmRemote();
  if (!allowRemote) return;
  const id = ctx.appendResult("Compacting...", "compact");
  try {
    const r = await api.projectChatCompact(ctx.project.id, ctx.chat.id, allowRemote);
    ctx.updateResult(id, { content: r.compacted
      ? `Folded the oldest ${r.messages_before - (r.messages_after - 1)} of ${r.messages_before} messages into a summary; ${r.messages_after - 1} recent ones stay as they are.`
      : "This conversation is short enough that there is nothing to compact yet." });
  } catch (e) {
    ctx.updateResult(id, { content: "", error: errorText(e) });
  }
}

async function runClear(ctx: CommandContext) {
  if (!ctx.chat) { ctx.clearMessages(); return; }
  const id = ctx.appendResult("Clearing...", "clear");
  try {
    await api.projectChatClear(ctx.project.id, ctx.chat.id);
    ctx.clearMessages();
  } catch (e) {
    ctx.updateResult(id, { content: "", error: errorText(e) });
  }
}

async function runAutocompact(arg: string, ctx: CommandContext) {
  const word = arg.trim().toLowerCase();
  const next = word === "on" ? true : word === "off" ? false : !ctx.params.autocompact;
  await ctx.changeParams({ autocompact: next });
  ctx.appendResult(`Automatic compaction is now ${next ? "on" : "off"}.`, "autocompact");
}

function runContext(ctx: CommandContext) {
  const used = approxTokens(ctx.messages.reduce((n, m) => n + m.content.length, 0));
  const everTrimmed = ctx.messages.some((m) => m.trimmed);
  const everCompacted = ctx.messages.some((m) => m.compacted);
  const lines = [contextWords(used, ctx.context), `Automatic compaction is ${ctx.params.autocompact ? "on" : "off"}.`, `Web search is ${ctx.params.web_search ? "on" : "off"}.`,
    everCompacted ? "Earlier messages of this conversation were summarized to save tokens." : "",
    everTrimmed ? "Some earlier messages were left out of what the model is sent (not summarized); /compact or turning automatic compaction on would fold them in instead." : ""]
    .filter(Boolean);
  ctx.appendResult(lines.join("\n"), "context");
}

function runUsage(ctx: CommandContext) {
  let prompt = 0, completion = 0, answers = 0;
  for (const m of ctx.messages) {
    if (m.usage) { answers += 1; prompt += m.usage.prompt_tokens ?? 0; completion += m.usage.completion_tokens ?? 0; }
  }
  ctx.appendResult(answers
    ? `This conversation has used about ${num(prompt)} prompt tokens and ${num(completion)} completion tokens over ${answers} ${answers === 1 ? "answer" : "answers"}.`
    : "No answer in this conversation has reported its token usage yet.", "usage");
}

async function runAsk(name: string, arg: string, ctx: CommandContext, opts: { deepResearch?: boolean; planMode?: boolean; forceWebSearch?: boolean }) {
  if (!arg) { ctx.appendResult(`/${name} needs a question: /${name} <question>`, name); return; }
  await ctx.ask(arg, opts);
}

async function runStatus(ctx: CommandContext) {
  const id = ctx.appendResult("Looking up status...", "status");
  try {
    const [serving, gpu] = await Promise.all([api.servingStatus(), api.gpuMode()]);
    const llm = serving.slots.llm;
    const lines = [
      llm ? `Model: ${llm.model ?? serving.choices.llm.model} (${llm.state}${llm.runs_on ? `, ${llm.runs_on}` : ""})` : `Model: ${serving.choices.llm.model} (not loaded)`,
      `GPU mode: ${gpu.mode}.`,
    ].filter(Boolean);
    ctx.updateResult(id, { content: lines.join("\n") });
  } catch (e) {
    ctx.updateResult(id, { content: "", error: errorText(e) });
  }
}

async function runSecurityReview(ctx: CommandContext) {
  const id = ctx.appendResult("Starting a security review...", "security-review");
  try {
    const job = await api.respondProject(ctx.project.id, { scope: "both", mode: "log", depth: "quick", assist: false, allow_remote: false, test: true });
    for (;;) {
      await sleep(1200);
      const j = await api.getJob(job.id);
      if (j.status === "running") { ctx.updateResult(id, { content: `Running${j.progress?.label ? `: ${j.progress.label}` : "..."}` }); continue; }
      if (j.status === "error") { ctx.updateResult(id, { content: "", error: j.error || "The security review failed." }); return; }
      ctx.updateResult(id, { content: "Security review finished. See the Response tab of this project for the full report." });
      return;
    }
  } catch (e) {
    ctx.updateResult(id, { content: "", error: errorText(e) });
  }
}

const MAX_SYSTEM = 4000;                                      // mirrors project_chats.MAX_SYSTEM: a skill is appended whole into the conversation's own system field

async function runSkill(arg: string, ctx: CommandContext) {
  const name = arg.trim();
  if (!name) { ctx.appendResult("/skill needs a name: /skill <name> (see Settings > Skills for what is approved).", "skill"); return; }
  const id = ctx.appendResult("Applying skill...", "skill");
  try {
    const skill = await api.skillGet(name);
    if (!skill.approved) {
      ctx.updateResult(id, { content: `"${skill.name}" is not approved yet; approve it in Settings > Skills before it can be applied.` });
      return;
    }
    const current = ctx.params.system || "";
    const combined = current ? `${current}\n\n${skill.prompt}` : skill.prompt;
    if (combined.length > MAX_SYSTEM) {
      ctx.updateResult(id, { content: `Applying "${skill.name}" would make the instructions ${combined.length.toLocaleString()} characters; the limit is ${MAX_SYSTEM.toLocaleString()}. Shorten the current instructions first (/context, or edit them in the panel).` });
      return;
    }
    await ctx.changeParams({ system: combined });
    ctx.updateResult(id, { content: `Applied "${skill.name}" to this conversation's instructions.` });
  } catch (e) {
    ctx.updateResult(id, { content: "", error: errorText(e) });
  }
}

export async function runCommand(name: string, arg: string, ctx: CommandContext): Promise<void> {
  const spec = COMMANDS.find((c) => c.name === name);
  if (!spec) return;                                          // the composer only calls this for a name parseCommand() already recognized
  if (spec.stub) { ctx.appendResult(`/${name} is not available yet, planned for a later phase.`, name); return; }
  switch (name) {
    case "resume": runResume(arg, ctx); return;
    case "compact": await runCompact(ctx); return;
    case "clear": await runClear(ctx); return;
    case "autocompact": await runAutocompact(arg, ctx); return;
    case "context": runContext(ctx); return;
    case "usage": runUsage(ctx); return;
    case "deep-research": await runAsk(name, arg, ctx, { deepResearch: true, forceWebSearch: true }); return;
    case "plan": await runAsk(name, arg, ctx, { planMode: true }); return;
    case "status": await runStatus(ctx); return;
    case "security-review": await runSecurityReview(ctx); return;
    case "skill": await runSkill(arg, ctx); return;
  }
}
