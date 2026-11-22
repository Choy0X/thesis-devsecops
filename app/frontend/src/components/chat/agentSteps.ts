import type { AgentEvent, AgentMessage, AgentSession, ChatUsage } from "../../lib/api";
import type { ViewMessage } from "./MessageView";

// What an agent session shows, the way Claude Code shows its own work: in order, what the agent said before a step, each tool call with its result (open it to read the result), the task list
// it keeps, and at the end the answer. Two sources make the same list of steps so that the page looks the same while a run is going and after it has been saved and read again: the events
// of a run as they arrive (``reduceEvent``), and the saved transcript (``turnsOfSession``).

export type ToolState = "running" | "ok" | "failed" | "waiting";
export type AgentStep =
  | { kind: "text"; text: string; narration?: boolean }
  | { kind: "think"; text: string }
  | { kind: "tool"; id: string; name: string; args: string; state: ToolState; summary: string; result?: string; live?: string;
      /** A ``task``: the steps its subagent took (while it works they arrive with the run; after that they are read from the subagent's own session), and that session's id. */
      children?: AgentStep[]; child?: string };

export const PREVIEW_CHARS = 20_000;
export const LIVE_CHARS = 20_000;                         // how much of what a running command has printed is kept on the page: its end                      // what the page is given of a result (the server keeps to the same), shown a screenful at a time

/** The text of a model's decision written out as JSON (``{"thought": ..., "action": ...}``), when that is what a message holds. The server turns these into real calls and answers when
 * it reads a session; this is the page's own last guard, so the raw object is never drawn as the answer, whatever an old file or a damaged reply holds. */
export function decisionText(content: string): { text: string; thinking?: string } | null {
  const t = content.trim();
  if (!t.startsWith("{")) return null;
  try {
    const o = JSON.parse(t);
    const a = o?.action;
    if (typeof o?.thought !== "string" || !a || typeof a !== "object") return null;
    if (a.kind === "answer" && typeof a.text === "string") return { text: a.text, thinking: o.thought || undefined };
    if (a.kind === "call") return { text: o.thought };
  } catch { /* ignored: not JSON, so it is words and is shown as they are */ }
  return null;
}

/** The saved transcript as what is drawn: each instruction, then one assistant turn holding every step the agent took for it. A call still waiting for its result (it is paused for the
 * owner) is a step too, marked "waiting". ``content`` is the final answer alone, for copying; the steps include it as their last text. */
/** The id of the session a subagent worked in, from the result of its ``task`` call. */
function childOf(result: string | null | undefined): string | undefined {
  try { const r = JSON.parse(result ?? ""); return typeof r?.child === "string" ? r.child : undefined; } catch { return undefined; }
}

/** The steps of one saved session, in order (a subagent's session: what it did for its one task). */
export function stepsOfSession(session: AgentSession): AgentStep[] {
  return turnsOfSession(session).flatMap((m) => (m.role === "assistant" ? m.steps ?? [] : []));
}

/** What all the rounds of one turn cost together: the tokens written and the seconds added up, the speed those make, and the size of the last request. Null when no round said. */
export function turnUsage(rounds: ChatUsage[]): ChatUsage | null {
  if (rounds.length === 0) return null;
  const sum = (key: "completion_tokens" | "seconds") => rounds.reduce((n, u) => n + (typeof u[key] === "number" ? (u[key] as number) : 0), 0);
  const written = sum("completion_tokens");
  const seconds = sum("seconds");
  const last = rounds[rounds.length - 1];
  return { prompt_tokens: last.prompt_tokens ?? null, completion_tokens: written, ...(seconds > 0 ? { seconds: Math.round(seconds * 100) / 100 } : {}), ...(seconds > 0 && written > 0 ? { tokens_per_second: Math.round((written / seconds) * 10) / 10 } : {}) };
}

export function turnsOfSession(session: AgentSession): ViewMessage[] {
  const out: ViewMessage[] = [];
  const t = session.transcript;
  let turn: { id: string; created: string; steps: AgentStep[]; answered: boolean; thinking?: string; usage: ChatUsage[] } | null = null;
  const close = () => {
    if (!turn || turn.steps.length === 0) { turn = null; return; }
    const last = turn.steps[turn.steps.length - 1];
    const usage = turnUsage(turn.usage);
    out.push({ id: turn.id, role: "assistant", content: turn.answered && last.kind === "text" ? last.text : "", created_at: turn.created, steps: turn.steps, thinking: turn.thinking, ...(usage ? { usage } : {}) });
    turn = null;
  };
  for (let i = 0; i < t.length; i++) {
    const m = t[i];
    if (m.role === "user") { close(); out.push({ id: m.id, role: "user", content: m.content ?? "", created_at: m.created_at, ...(m.attachments?.length ? { attachments: m.attachments.map((a) => ({ path: a.path, cut: !!a.cut })) } : {}) }); continue; }
    if (m.role === "tool") continue;
    turn ??= { id: m.id, created: m.created_at, steps: [], answered: false, usage: [] };
    if (m.usage) turn.usage.push(m.usage);
    if (m.tool_calls?.length) {
      const results = new Map<string, AgentMessage>();                       // the results right after this message, by id: old sessions reused one id for every call
      for (let j = i + 1; j < t.length && t[j].role === "tool"; j++) if (t[j].tool_call_id) results.set(t[j].tool_call_id!, t[j]);
      const said = decisionText(m.content ?? "")?.text ?? (m.content ?? "");
      if (m.thinking?.trim() && (m.thinking_kind === "reasoning" || m.thinking_kind === "deliberation")) turn.steps.push({ kind: "think", text: m.thinking });       // what it thought before this step, as it showed while it ran
      if (said.trim()) turn.steps.push({ kind: "text", text: said, narration: true });
      for (const c of m.tool_calls) {
        const r = results.get(c.id);
        turn.steps.push({ kind: "tool", id: c.id, name: c.function.name, args: c.function.arguments, state: r ? (r.ok ? "ok" : "failed") : "waiting", summary: r?.summary ?? "", result: r?.display ?? r?.content ?? undefined,
          ...(c.function.name === "task" && childOf(r?.content) ? { child: childOf(r?.content) } : {}) });
      }
    } else {
      const d = decisionText(m.content ?? "");
      const text = d ? d.text : (m.content ?? "");
      if (m.thinking?.trim() && (m.thinking_kind === "reasoning" || m.thinking_kind === "deliberation")) turn.steps.push({ kind: "think", text: m.thinking });       // only thinking the model really did (its own, or the planning step); what it wrote beside its answer (``thought``) is not shown
      if (text.trim()) turn.steps.push({ kind: "text", text });
      turn.answered = true;
    }
  }
  close();
  return out;
}

/** One event of a running session folded into the steps drawn so far. Words go on the last text step, and a new one starts after a tool (so what the agent says before and after a call
 * are separate paragraphs). What the agent says before a step (``narration``) is its own kind of text, kept apart from the answer: when the round turns out to end in the answer, not in a
 * step, it is taken back (``narration_drop``). Reasoning the model did (``thinking``) is a step of its own. */
export function reduceEvent(steps: AgentStep[], e: AgentEvent): AgentStep[] {
  const last = steps[steps.length - 1];
  if (e.type === "thinking") return last?.kind === "think" ? [...steps.slice(0, -1), { kind: "think", text: last.text + e.text }] : [...steps, { kind: "think", text: e.text }];
  if (e.type === "narration") return last?.kind === "text" && last.narration ? [...steps.slice(0, -1), { ...last, text: last.text + e.text }] : [...steps, { kind: "text", text: e.text, narration: true }];
  if (e.type === "narration_drop") return last?.kind === "text" && last.narration ? steps.slice(0, -1) : steps;
  if (e.type === "token") return last?.kind === "text" && !last.narration ? [...steps.slice(0, -1), { kind: "text", text: last.text + e.text }] : [...steps, { kind: "text", text: e.text }];
  if (e.type === "tool_start") return [...steps, { kind: "tool", id: e.id, name: e.name, args: e.arguments, state: "running", summary: "" }];
  if (e.type === "sub") {                                          // a step of the subagent, folded into the task that started it by the same rules as the thread's own
    const at = steps.findIndex((s) => s.kind === "tool" && s.id === e.call);
    if (at < 0) return steps;
    return steps.map((s, i) => (i === at && s.kind === "tool" ? { ...s, child: e.child, children: reduceEvent(s.children ?? [], e.event) } : s));
  }
  if (e.type === "tool_output") {                              // what a running command printed so far, shown in its card until the final result replaces it
    const at = steps.findIndex((s) => s.kind === "tool" && s.id === e.id && s.state === "running");
    if (at < 0) return steps;
    return steps.map((s, i) => (i === at && s.kind === "tool" ? { ...s, live: ((s.live ?? "") + e.text).slice(-LIVE_CHARS) } : s));
  }
  if (e.type === "tool") {
    const at = steps.findIndex((s) => s.kind === "tool" && s.id === e.id);
    const before = at >= 0 && steps[at].kind === "tool" ? (steps[at] as Extract<AgentStep, { kind: "tool" }>) : null;
    const done: AgentStep = { kind: "tool", id: e.id, name: e.name, args: e.arguments, state: e.ok ? "ok" : "failed", summary: e.summary, result: e.result, ...(before?.children ? { children: before.children, child: before.child } : {}) };
    return at >= 0 ? steps.map((s, i) => (i === at ? done : s)) : [...steps, done];
  }
  return steps;
}

/** A page the agent searched for or fetched, as the status panel lists it. The number is the one the answer cites as [n]; it is the same for the whole session. */
export type WebSource = { no: number; kind: "result" | "page"; url: string; title: string; domain: string; snippet: string; query: string };

const HTTP = /^https?:\/\//i;
const domainOf = (url: string): string => { try { return new URL(url).hostname.replace(/^www\./i, ""); } catch { return ""; } };

/** The sources a result of ``websearch`` or ``webfetch`` names: one for each hit of a search, or the page of a fetch. */
function webItems(r: Record<string, unknown>): WebSource[] {
  const out: WebSource[] = [];
  if (Array.isArray(r.results)) {
    for (const x of r.results as Record<string, unknown>[]) {
      if (x && typeof x.no === "number" && typeof x.url === "string" && HTTP.test(x.url)) {
        out.push({ no: x.no, kind: "result", url: x.url, title: typeof x.title === "string" && x.title ? x.title : x.url, domain: typeof x.domain === "string" && x.domain ? x.domain : domainOf(x.url),
          snippet: typeof x.snippet === "string" ? x.snippet : "", query: typeof r.query === "string" ? r.query : "" });
      }
    }
  } else if (typeof r.no === "number" && typeof r.url === "string" && HTTP.test(r.url)) {
    out.push({ no: r.no, kind: "page", url: r.url, title: typeof r.title === "string" && r.title ? r.title : r.url, domain: domainOf(r.url), snippet: "", query: "" });
  }
  return out;
}

/** The sources of a list of steps, by number. A page that was searched for and then fetched is one source, marked as read. Read from the results of the web tools alone (the same results the
 * server keeps), so the list is the same while a run goes and after the session is opened again; files read and commands run are not sources. */
export function sourcesOfSteps(steps: AgentStep[]): WebSource[] {
  const byNo = new Map<number, WebSource>();
  for (const s of steps) {
    if (s.kind !== "tool" || s.state !== "ok" || (s.name !== "websearch" && s.name !== "webfetch")) continue;
    const r = parsed(s.result);
    if (!r) continue;
    for (const item of webItems(r)) {
      const old = byNo.get(item.no);
      if (!old) byNo.set(item.no, item);
      else if (item.kind === "page") byNo.set(item.no, { ...old, kind: "page", title: item.title || old.title });
    }
  }
  return [...byNo.values()].sort((a, b) => a.no - b.no);
}

export const sourcesOf = (messages: ViewMessage[]): WebSource[] => sourcesOfSteps(messages.flatMap((m) => m.steps ?? []));

/** The numbers an answer cites as [n]. */
export function citedNumbers(text: string): Set<number> {
  return new Set([...text.matchAll(/(?<![\w\])])\[(\d{1,3})\](?![([:])/g)].map((m) => Number(m[1])));
}

/** The numbers the answers of a thread cite: its final texts (not what the agent said before a step) and the single answers of messages that have no steps. */
export function citedIn(messages: ViewMessage[]): Set<number> {
  const out = new Set<number>();
  messages.filter((m) => m.role === "assistant").forEach((m) => {
    const texts = m.steps ? m.steps.flatMap((s, i) => (s.kind === "text" && !(s.narration || m.steps![i + 1]?.kind === "tool") ? [s.text] : [])) : [m.content];
    texts.forEach((t) => citedNumbers(t).forEach((n) => out.add(n)));
  });
  return out;
}

/** An answer with each [n] that names a source of the session made a link to that page. Code (a fenced block, an inline span) and things like ``a[1]`` are left alone. */
export function linkCitations(text: string, sources: WebSource[]): string {
  if (sources.length === 0 || !/\[\d/.test(text)) return text;
  const byNo = new Map(sources.map((s) => [s.no, s.url.replace(/[()[\]<>\s]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase().padStart(2, "0")}`)]));         // nothing in an address may end the link early
  return text.split(/(```[\s\S]*?```|`[^`\n]*`)/).map((part, i) => (i % 2 === 1 ? part
    : part.replace(/(?<![\w\])])\[(\d{1,3})\](?![([:])/g, (whole, n: string) => (byNo.has(Number(n)) ? `[[${n}]](${byNo.get(Number(n))})` : whole)))).join("");
}

const LABELS: Record<string, string> = {
  list_files: "List files", find_files: "Find files", read_file: "Read", search_code: "Search", project_graph: "Project graph", scan_results: "Read scan", rescan_code: "Code scan",
  rescan_dependencies: "Dependency scan", write_file: "Write", edit_file: "Edit", run_command: "Run", diff_so_far: "Changes so far", checkpoint: "Checkpoint", git_status: "Git status",
  git_log: "Git log", update_task_list: "Task list", propose_plan: "Proposed plan", ask_question: "Question", bash: "Terminal", grep: "Search", glob: "Find files", recall: "Recall",
  websearch: "Web search", webfetch: "Web page", task: "Subagent",
};
/** A tool of an MCP server is named ``mcp__<server>__<tool>``; the page shows it as ``<server>: <tool>``. */
export const isMcp = (name: string): boolean => name.startsWith("mcp__") && name.indexOf("__", 5) > 5;
const mcpLabel = (name: string): string => { const rest = name.slice(5); const at = rest.indexOf("__"); return `${rest.slice(0, at)}: ${rest.slice(at + 2)}`; };
export const toolLabel = (name: string): string => LABELS[name] ?? (isMcp(name) ? mcpLabel(name) : name);

function parsed(text: string | undefined): Record<string, unknown> | null {
  if (!text) return null;
  try { const v = JSON.parse(text); return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null; } catch { return null; }
}

/** The one short thing a call is about, from its arguments: the path, the pattern, the command. Empty when it takes none. */
export function argSummary(name: string, args: string): string {
  const a = parsed(args) ?? {};
  const pick = (...keys: string[]) => keys.map((k) => a[k]).find((v) => typeof v === "string" && v) as string | undefined;
  if (name === "read_file") {
    const from = typeof a.start_line === "number" ? `:${a.start_line}${typeof a.end_line === "number" ? `-${a.end_line}` : ""}` : "";
    return (pick("path") ?? "") + from;
  }
  if (name === "search_code" || name === "grep") return `${pick("pattern") ?? ""}${pick("path") ? ` in ${pick("path")}` : ""}`;
  if (name === "recall") return pick("handle") ?? "";
  if (name === "websearch") return pick("query") ?? "";
  if (name === "task") return `${pick("agent") ?? "explore"}: ${pick("description") ?? ""}`;
  if (name === "webfetch") return pick("url") ?? "";
  if (name === "update_task_list") return Array.isArray(a.tasks) ? `${a.tasks.length} item${a.tasks.length === 1 ? "" : "s"}` : "";
  return pick("path", "pattern", "command", "message", "kind") ?? "";
}

/** The call as the agent wrote it, for the line above its output: a command as `$ command`, anything else as its name with its arguments (a long text shortened). */
export function callText(name: string, args: string): string {
  const a = parsed(args);
  if (!a) return args.trim().slice(0, 300);
  if (name === "run_command" && typeof a.command === "string") return `$ ${a.command}${typeof a.path === "string" && a.path ? `   (in ${a.path})` : ""}`;
  if (name === "bash" && typeof a.command === "string") return `$ ${a.command}${a.raw === true ? "   (raw: nothing left out)" : ""}`;
  const show = (v: unknown): string => {
    const t = typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v) ?? String(v);
    return t.length > 120 ? `${t.slice(0, 117)}...` : t;
  };
  const parts = Object.entries(a).map(([k, v]) => `${k}: ${show(v)}`);
  return `${name}(${parts.join(", ")})`;
}

/** A tool's result as text to read: the lines of a file, the entries of a folder, the matches of a search, a command's output. Falls back to the result as it is, clipped. */
export function previewOf(step: Extract<AgentStep, { kind: "tool" }>): string {
  const r = parsed(step.result);
  if (!r) return (step.result ?? "").slice(0, PREVIEW_CHARS);
  if (typeof r.error === "string") return r.error;
  const lines = (xs: unknown, f: (x: Record<string, unknown>) => string) => (Array.isArray(xs) ? xs.map((x) => f(x as Record<string, unknown>)).join("\n") : "");
  let text = "";
  if (typeof r.text === "string") text = r.text;
  else if (typeof r.output === "string") text = `${typeof r.exit_code === "number" ? `exit code ${r.exit_code}\n` : ""}${r.output}`;
  else if (Array.isArray(r.entries)) text = lines(r.entries, (e) => `${e.name}${e.type === "folder" ? "/" : ""}`);
  else if (Array.isArray(r.matches)) text = lines(r.matches, (m) => `${m.file}:${m.line}: ${m.text}`);
  else if (Array.isArray(r.files)) text = (r.files as unknown[]).join("\n");
  else if (Array.isArray(r.findings)) text = lines(r.findings, (f) => `${f.severity ?? ""} ${f.rule ?? ""} ${f.file}:${f.line} ${f.summary ?? ""}`);
  else if (typeof r.message === "string") text = r.message;
  else if (typeof r.answer === "string" && typeof r.child === "string") text = r.answer;                      // what a subagent answered
  else { const { summary: _summary, ...rest } = r; return JSON.stringify(rest, null, 1).slice(0, PREVIEW_CHARS); }
  const said = [r.truncated, r.note].filter((x): x is string => typeof x === "string" && x !== "");           // what was left out, and the note to the model about a loop, are part of what the call returned
  return (said.length ? `${text}\n\n${said.join("\n")}` : text).slice(0, PREVIEW_CHARS);
}

/** What the terminal says of a command it ran: how it was run (``rtk git log ...``), what rtk left out, and the handle of the whole output when this was cut. Null for a call that is not a terminal's. */
export type TerminalInfo = { ranAs: string[]; hidden: number | null; hiddenHandle: string | null; condensed: boolean; recovery: string | null; lines: number | null; exitCode: number | null };
export function terminalOf(step: Extract<AgentStep, { kind: "tool" }>): TerminalInfo | null {
  if (step.name !== "bash") return null;
  const r = parsed(step.result);
  if (!r || typeof r.output !== "string") return null;
  const c = r.condensed && typeof r.condensed === "object" ? (r.condensed as Record<string, unknown>) : null;
  return { ranAs: Array.isArray(r.ran_as) ? (r.ran_as as unknown[]).filter((x): x is string => typeof x === "string") : [], hidden: c && typeof c.hidden_lines === "number" ? c.hidden_lines : null,
    hiddenHandle: c && typeof c.recall === "string" ? c.recall : null, condensed: !!c, recovery: typeof r.recovery === "string" ? r.recovery : null, lines: typeof r.lines === "number" ? r.lines : null,
    exitCode: typeof r.exit_code === "number" ? r.exit_code : null };
}

/** The file a call showed, as code for the viewer: the lines of a ``read_file`` result without the numbers the tool put in front of them (the viewer numbers them itself, from the line the read
 * began at), or the content of a ``write_file`` call. Null when the step did not show a file. */
export function fileViewOf(step: Extract<AgentStep, { kind: "tool" }>): { path: string; code: string; startLine: number } | null {
  if (step.state !== "ok") return null;
  if (step.name === "read_file") {
    const r = parsed(step.result);
    if (!r || typeof r.text !== "string" || typeof r.path !== "string" || r.text === "") return null;
    return { path: r.path, code: r.text.split("\n").map((l) => l.replace(/^\d+: /, "")).join("\n"), startLine: typeof r.start_line === "number" ? r.start_line : 1 };
  }
  if (step.name === "write_file") {
    const a = parsed(step.args);
    return a && typeof a.path === "string" && typeof a.content === "string" ? { path: a.path, code: a.content, startLine: 1 } : null;
  }
  return null;
}

/** The tasks of an ``update_task_list`` call, from its arguments; empty when they are not a list of tasks. */
export function tasksOf(args: string): { text: string; done: boolean }[] {
  const a = parsed(args);
  return Array.isArray(a?.tasks) ? (a!.tasks as unknown[]).map((t) => ({ text: String((t as Record<string, unknown>)?.text ?? ""), done: !!(t as Record<string, unknown>)?.done })).filter((t) => t.text) : [];
}

/** The old and the new text of an ``edit_file`` call, from its arguments, or null when it is not one that has both. */
export function editOf(args: string): { path: string; old: string; new: string } | null {
  const a = parsed(args);
  return a && typeof a.old === "string" && typeof a.new === "string" ? { path: String(a.path ?? ""), old: a.old, new: a.new } : null;
}
