import { describe, expect, it } from "vitest";
import type { AgentEvent, AgentMessage, AgentSession } from "../../lib/api";
import { stepsOfSession, argSummary, callText, citedIn, citedNumbers, decisionText, linkCitations, sourcesOf, sourcesOfSteps, editOf, fileViewOf, previewOf, reduceEvent, tasksOf, terminalOf, toolLabel, isMcp, turnUsage, turnsOfSession, type AgentStep } from "./agentSteps";

const m = (id: string, role: AgentMessage["role"], content: string | null, over: Partial<AgentMessage> = {}): AgentMessage => ({ id, role, content, created_at: "2026-10-16T10:00:00.000Z", ...over });
const session = (transcript: AgentMessage[]): AgentSession => ({ schema: "agent_session/1", id: "s1", project_id: "p", mode: "plan", title: "", instructions: "", created_at: "", updated_at: "", status: "idle",
  params: { temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false, auto_approve: false }, transcript, task_list: [], pending_call: null, rounds: 0 });
const call = (id: string, name: string, args = "{}") => ({ id, type: "function" as const, function: { name, arguments: args } });
type Tool = Extract<AgentStep, { kind: "tool" }>;
const tool = (over: Partial<Tool> = {}): Tool => ({ kind: "tool", id: "c1", name: "read_file", args: "{}", state: "ok", summary: "", ...over });

describe("what a turn cost", () => {
  it("adds the tokens and seconds of every round of the turn and gives the speed those make", () => {
    expect(turnUsage([{ prompt_tokens: 900, completion_tokens: 30, seconds: 2 }, { prompt_tokens: 1200, completion_tokens: 50, seconds: 8 }]))
      .toEqual({ prompt_tokens: 1200, completion_tokens: 80, seconds: 10, tokens_per_second: 8 });
    expect(turnUsage([])).toBeNull();
    expect(turnUsage([{ prompt_tokens: 5, completion_tokens: 3 }])).toEqual({ prompt_tokens: 5, completion_tokens: 3 });
  });

  it("puts a turn's usage on the message the thread shows for it", () => {
    const turns = turnsOfSession(session([m("u1", "user", "Look"), m("a1", "assistant", "Listing.", { tool_calls: [call("c1", "list_files")], usage: { prompt_tokens: 100, completion_tokens: 10, seconds: 1 } }),
      m("t1", "tool", "{}", { tool_call_id: "c1", ok: true }), m("a2", "assistant", "Done.", { usage: { prompt_tokens: 150, completion_tokens: 20, seconds: 2 } })]));
    const answer = turns.find((t) => t.role === "assistant")!;
    expect(answer.usage).toEqual({ prompt_tokens: 150, completion_tokens: 30, seconds: 3, tokens_per_second: 10 });
  });
});

describe("turnsOfSession", () => {
  it("groups everything the agent did for an instruction into one turn, in order, ending with the answer", () => {
    const turns = turnsOfSession(session([m("u1", "user", "Look"), m("a1", "assistant", "Listing first.", { tool_calls: [call("c1", "list_files")] }),
      m("t1", "tool", "{\"entries\": []}", { tool_call_id: "c1", ok: true, name: "list_files", summary: "Listed the top" }), m("a2", "assistant", "Nothing there.", { thinking: "Short.", thinking_kind: "reasoning" })]));
    expect(turns.map((t) => t.role)).toEqual(["user", "assistant"]);
    const a = turns[1];
    expect(a.content).toBe("Nothing there.");
    expect(a.steps).toEqual([{ kind: "text", text: "Listing first.", narration: true }, { kind: "tool", id: "c1", name: "list_files", args: "{}", state: "ok", summary: "Listed the top", result: "{\"entries\": []}" },
      { kind: "think", text: "Short." }, { kind: "text", text: "Nothing there." }]);            // what it thought before the answer comes right before it, as it showed while it ran
  });

  it("carries the files attached to an instruction with @ onto the owner's message", () => {
    const turns = turnsOfSession(session([m("u1", "user", "What is @app.py?", { attachments: [{ path: "app.py", cut: false }] }), m("a1", "assistant", "A shop.")]));
    expect(turns[0].attachments).toEqual([{ path: "app.py", cut: false }]);
    expect(turnsOfSession(session([m("u1", "user", "Plain"), m("a1", "assistant", "ok")]))[0].attachments).toBeUndefined();
  });

  it("shows what a model thought before a step, and a planning step of the app's own, in order", () => {
    const a = turnsOfSession(session([m("u1", "user", "q"), m("a1", "assistant", "Reading it.", { tool_calls: [call("c1", "read_file")], thinking: "I should read app.py first.", thinking_kind: "deliberation" }),
      m("t1", "tool", "{}", { tool_call_id: "c1", ok: true, name: "read_file", summary: "Read" }), m("a2", "assistant", "Done.", { thinking: "It is enough.", thinking_kind: "reasoning" })]))[1];
    expect(a.steps!.map((s) => (s.kind === "think" ? `think: ${s.text}` : s.kind === "text" ? `text: ${s.text}` : "tool"))).toEqual(["think: I should read app.py first.", "text: Reading it.", "tool", "think: It is enough.", "text: Done."]);
  });

  it("shows the fuller copy of a result that the server kept for the page, and no thinking that was only a thought written beside an answer", () => {
    const a = turnsOfSession(session([m("u1", "user", "q"), m("a1", "assistant", null, { tool_calls: [call("c1", "read_file")] }),
      m("t1", "tool", "{\"text\": \"1: a\"}", { tool_call_id: "c1", ok: true, name: "read_file", summary: "Read", display: "{\"text\": \"1: a\n2: b\"}" }),
      m("a2", "assistant", "Done.", { thinking: "an old thought, saved as thinking", thought: "Enough." })]))[1];
    expect(a.steps!.find((s) => s.kind === "tool")).toMatchObject({ result: "{\"text\": \"1: a\n2: b\"}" });
    expect(a.steps!.some((s) => s.kind === "think")).toBe(false);
  });

  it("marks a failed result and a call that has no result yet", () => {
    const a = turnsOfSession(session([m("u1", "user", "q"), m("a1", "assistant", null, { tool_calls: [call("c1", "read_file"), call("c2", "ask_question")] }),
      m("t1", "tool", "{\"error\": \"No such file\"}", { tool_call_id: "c1", ok: false, name: "read_file", summary: "No such file" })]))[1];
    expect(a.steps!.map((s) => (s.kind === "tool" ? s.state : s.kind))).toEqual(["failed", "waiting"]);
    expect(a.content).toBe("");                                 // no answer yet
  });

  it("pairs a call with the results right after it, since old sessions reused one id for every call", () => {
    const a = turnsOfSession(session([m("u1", "user", "q"), m("a1", "assistant", "First.", { tool_calls: [call("call_0", "read_file")] }), m("t1", "tool", "{}", { tool_call_id: "call_0", ok: true, name: "read_file", summary: "Read it" }),
      m("a2", "assistant", "Then asks.", { tool_calls: [call("call_0", "ask_question")] })]))[1];
    expect(a.steps!.filter((s) => s.kind === "tool").map((s) => (s.kind === "tool" ? s.state : ""))).toEqual(["ok", "waiting"]);       // the second call has no result of its own yet
  });

  it("starts a separate turn for each instruction", () => {
    const turns = turnsOfSession(session([m("u1", "user", "one"), m("a1", "assistant", "first"), m("u2", "user", "two"), m("a2", "assistant", "second")]));
    expect(turns.map((t) => t.content)).toEqual(["one", "first", "two", "second"]);
  });

  it("reads a decision object that an old file holds as the call or the answer it was", () => {
    const answer = JSON.stringify({ thought: "Enough.", action: { kind: "answer", text: "All fine." } });
    const said = JSON.stringify({ thought: "I will look.", action: { kind: "call", name: "list_files", arguments: {} } });
    const turns = turnsOfSession(session([m("u1", "user", "q"), m("a1", "assistant", said), m("a2", "assistant", answer)]));
    expect(turns[1].steps).toEqual([{ kind: "text", text: "I will look." }, { kind: "text", text: "All fine." }]);
    expect(turns[1].thinking).toBeUndefined();
    expect(JSON.stringify(turns)).not.toContain("\"action\"");
  });
});

describe("decisionText", () => {
  it("is null for words, other JSON and a thought with no action", () => {
    for (const t of ["Plain words.", "{\"a\": 1}", "{\"thought\": \"x\"}", "{broken", "[1]"]) expect(decisionText(t)).toBeNull();
  });
});

describe("reduceEvent", () => {
  const run = (events: AgentEvent[]) => events.reduce<AgentStep[]>(reduceEvent, []);

  it("streams what the agent says before a step as narration, kept apart from the answer", () => {
    const steps = run([{ type: "narration", text: "I will " }, { type: "narration", text: "look." }]);
    expect(steps).toEqual([{ kind: "text", text: "I will look.", narration: true }]);
    expect(run([{ type: "narration", text: "I will look." }, { type: "tool_start", id: "c1", name: "list_files", arguments: "{}" }])).toEqual([
      { kind: "text", text: "I will look.", narration: true }, { kind: "tool", id: "c1", name: "list_files", args: "{}", state: "running", summary: "" }]);
  });

  it("takes narration back when the round turns out to end in the answer, and streams the answer on its own", () => {
    expect(run([{ type: "narration", text: "Enough." }, { type: "narration_drop" }, { type: "token", text: "All " }, { type: "token", text: "fine." }])).toEqual([{ kind: "text", text: "All fine." }]);
    expect(run([{ type: "tool_start", id: "c1", name: "list_files", arguments: "{}" }, { type: "narration_drop" }])).toEqual([{ kind: "tool", id: "c1", name: "list_files", args: "{}", state: "running", summary: "" }]);
  });

  it("keeps reasoning the model did as a thinking step of its own", () => {
    expect(run([{ type: "thinking", text: "Let me " }, { type: "thinking", text: "see." }, { type: "token", text: "Done." }])).toEqual([{ kind: "think", text: "Let me see." }, { kind: "text", text: "Done." }]);
  });

  it("settles a running call with its result, and starts a new text step after it", () => {
    const steps = run([{ type: "tool_start", id: "c1", name: "read_file", arguments: "{\"path\": \"a.py\"}" }, { type: "tool", id: "c1", name: "read_file", arguments: "{\"path\": \"a.py\"}", ok: true, summary: "Read a.py", result: "{}" },
      { type: "token", text: "Done." }]);
    expect(steps).toEqual([{ kind: "tool", id: "c1", name: "read_file", args: "{\"path\": \"a.py\"}", state: "ok", summary: "Read a.py", result: "{}" }, { kind: "text", text: "Done." }]);
  });

  it("adds a result that has no start (a repeated call is answered without one) and ignores events that are not steps", () => {
    expect(run([{ type: "tool", id: "c9", name: "list_files", arguments: "{}", ok: false, summary: "Repeated" }, { type: "notice", text: "x" }, { type: "status", text: "Checking" }, { type: "done" }])).toEqual([
      { kind: "tool", id: "c9", name: "list_files", args: "{}", state: "failed", summary: "Repeated", result: undefined }]);
  });
});

describe("what a call is shown as", () => {
  it("names the one thing a call is about", () => {
    expect(argSummary("read_file", "{\"path\": \"src/a.py\", \"start_line\": 10, \"end_line\": 40}")).toBe("src/a.py:10-40");
    expect(argSummary("search_code", "{\"pattern\": \"eval\", \"path\": \"src\"}")).toBe("eval in src");
    expect(argSummary("run_command", "{\"command\": \"pytest -q\"}")).toBe("pytest -q");
    expect(argSummary("update_task_list", "{\"tasks\": [{\"text\": \"a\"}, {\"text\": \"b\"}]}")).toBe("2 items");
    expect(argSummary("git_status", "{}")).toBe("");
    expect(argSummary("read_file", "not json")).toBe("");
  });

  it("previews a result as the text a person would read", () => {
    expect(previewOf(tool({ result: JSON.stringify({ text: "1: x = 1", summary: "s" }) }))).toBe("1: x = 1");
    expect(previewOf(tool({ name: "list_files", result: JSON.stringify({ entries: [{ name: "src", type: "folder" }, { name: "a.py", type: "file" }] }) }))).toBe("src/\na.py");
    expect(previewOf(tool({ name: "search_code", result: JSON.stringify({ matches: [{ file: "a.py", line: 3, text: "eval(x)" }] }) }))).toBe("a.py:3: eval(x)");
    expect(previewOf(tool({ name: "run_command", result: JSON.stringify({ exit_code: 1, output: "FAILED" }) }))).toBe("exit code 1\nFAILED");
    expect(previewOf(tool({ name: "find_files", result: JSON.stringify({ files: ["a.py", "b.py"] }) }))).toBe("a.py\nb.py");
    expect(previewOf(tool({ result: JSON.stringify({ text: "1: x", truncated: "text: kept 1 of 9 characters (cut to fit)", note: "You have now made this exact call 3 times." }) })))
      .toBe("1: x\n\ntext: kept 1 of 9 characters (cut to fit)\nYou have now made this exact call 3 times.");
    expect(previewOf(tool({ result: JSON.stringify({ error: "No such file" }) }))).toBe("No such file");
    expect(previewOf(tool({ result: "not json at all" }))).toBe("not json at all");
    expect(previewOf(tool({ result: undefined }))).toBe("");
  });

  it("writes a call the way the agent made it: a command as itself, anything else as its name and arguments", () => {
    expect(callText("run_command", "{\"command\": \"pytest -q\", \"path\": \"api\"}")).toBe("$ pytest -q   (in api)");
    expect(callText("run_command", "{\"command\": \"ls\"}")).toBe("$ ls");
    expect(callText("search_code", "{\"pattern\": \"eval\", \"path\": \"app/**/*\"}")).toBe("search_code(pattern: \"eval\", path: \"app/**/*\")");
    const long = callText("write_file", JSON.stringify({ path: "a.py", content: "x".repeat(500) }));
    expect(long.startsWith("write_file(path: \"a.py\", content: \"xxxx")).toBe(true);
    expect(long.endsWith("...)")).toBe(true);
    expect(long.length).toBeLessThan(200);                                     // a long text is shortened
    expect(callText("read_file", "not json")).toBe("not json");
  });

  it("gives the viewer the lines of a file without the numbers the tool put in front of them, and where they begin", () => {
    const nl = String.fromCharCode(10);
    const read = tool({ result: JSON.stringify({ path: "src/a.py", start_line: 40, end_line: 42, text: ["40: import os", "41: ", "42: 7: not a number prefix of the file"].join(nl) }) });
    expect(fileViewOf(read)).toEqual({ path: "src/a.py", startLine: 40, code: ["import os", "", "7: not a number prefix of the file"].join(nl) });
    expect(fileViewOf(tool({ name: "write_file", args: JSON.stringify({ path: "new.py", content: `x = 1${nl}` }), result: "{}" }))).toEqual({ path: "new.py", startLine: 1, code: `x = 1${nl}` });
    expect(fileViewOf(tool({ state: "failed", result: JSON.stringify({ error: "No such file" }) }))).toBeNull();
    expect(fileViewOf(tool({ name: "search_code", result: JSON.stringify({ matches: [] }) }))).toBeNull();
    expect(fileViewOf(tool({ result: JSON.stringify({ path: "a.py", text: "" }) }))).toBeNull();
  });

  it("reads the tasks and the old and new text out of a call's arguments", () => {
    expect(tasksOf("{\"tasks\": [{\"text\": \"a\", \"done\": true}, {\"text\": \"\"}, {\"text\": \"b\"}]}")).toEqual([{ text: "a", done: true }, { text: "b", done: false }]);
    expect(tasksOf("{}")).toEqual([]);
    expect(editOf("{\"path\": \"a.py\", \"old\": \"x\", \"new\": \"y\"}")).toEqual({ path: "a.py", old: "x", new: "y" });
    expect(editOf("{\"path\": \"a.py\"}")).toBeNull();
  });
});

describe("subagents", () => {
  const start = (id: string): AgentEvent => ({ type: "tool_start", id, name: "task", arguments: JSON.stringify({ description: "Look at app.py", prompt: "What does it do?", agent: "explore" }) });
  const sub = (event: AgentEvent): AgentEvent => ({ type: "sub", call: "t1", child: "child1", agent: "explore", event });

  it("folds what a subagent does into the task that started it, by the rules the thread's own steps follow", () => {
    let steps = reduceEvent([], start("t1"));
    steps = reduceEvent(steps, sub({ type: "tool_start", id: "r1", name: "read_file", arguments: "{\"path\": \"app.py\"}" }));
    steps = reduceEvent(steps, sub({ type: "tool", id: "r1", name: "read_file", arguments: "{\"path\": \"app.py\"}", ok: true, summary: "Read app.py", result: "{\"text\": \"1: x = 1\"}" }));
    steps = reduceEvent(steps, sub({ type: "token", text: "It sets x." }));
    expect(steps).toHaveLength(1);
    const task = steps[0] as Extract<AgentStep, { kind: "tool" }>;
    expect(task.child).toBe("child1");
    expect(task.children!.map((s) => (s.kind === "tool" ? `${s.name}:${s.state}` : s.kind === "text" ? `text:${s.text}` : s.kind))).toEqual(["read_file:ok", "text:It sets x."]);
  });

  it("keeps the subagent's steps when the task's own result arrives", () => {
    let steps = reduceEvent([], start("t1"));
    steps = reduceEvent(steps, sub({ type: "token", text: "Looking." }));
    steps = reduceEvent(steps, { type: "tool", id: "t1", name: "task", arguments: "{}", ok: true, summary: "explore: found it", result: JSON.stringify({ agent: "explore", child: "child1", answer: "found it" }) });
    const task = steps[0] as Extract<AgentStep, { kind: "tool" }>;
    expect(task.state).toBe("ok") ;
    expect(task.children).toHaveLength(1);
    expect(task.child).toBe("child1");
  });

  it("ignores a step of a subagent whose task is not on the page", () => {
    expect(reduceEvent([], sub({ type: "token", text: "stray" }))).toEqual([]);
  });

  it("knows the session a subagent worked in from the saved result, and reads that session's steps in order", () => {
    const turns = turnsOfSession(session([m("u1", "user", "Ask"), m("a1", "assistant", null, { tool_calls: [call("t1", "task", "{\"description\": \"x\", \"prompt\": \"y\", \"agent\": \"explore\"}")] }),
      m("r1", "tool", JSON.stringify({ agent: "explore", child: "child1", answer: "found it", summary: "explore: found it" }), { tool_call_id: "t1", ok: true, name: "task", summary: "explore: found it" }), m("a2", "assistant", "Done.")]));
    const task = turns[1].steps!.find((s) => s.kind === "tool") as Extract<AgentStep, { kind: "tool" }>;
    expect(task.child).toBe("child1");
    expect(task.children).toBeUndefined();                                                              // read when the card is opened, not carried in the parent's file
    const child = session([m("c1", "user", "What does it do?"), m("c2", "assistant", null, { tool_calls: [call("r", "read_file")] }), m("c3", "tool", "{}", { tool_call_id: "r", ok: true, name: "read_file", summary: "Read" }), m("c4", "assistant", "It sets x.")]);
    expect(stepsOfSession(child).map((s) => (s.kind === "tool" ? s.name : s.kind))).toEqual(["read_file", "text"]);
  });

  it("labels a task, says who it is for, and shows the answer as its result", () => {
    expect(toolLabel("task")).toBe("Subagent");
    expect(argSummary("task", JSON.stringify({ agent: "explore", description: "Find the login code" }))).toBe("explore: Find the login code");
    expect(argSummary("task", JSON.stringify({ description: "No agent named" }))).toBe("explore: No agent named");
    expect(previewOf(tool({ name: "task", result: JSON.stringify({ agent: "explore", child: "c", answer: "It is in auth.py.", summary: "x" }) }))).toBe("It is in auth.py.");
  });
});

describe("sources", () => {
  const search = (no: number, over: Record<string, unknown> = {}) => tool({ id: `s${no}`, name: "websearch", args: JSON.stringify({ query: "q" }), result: JSON.stringify({ query: "q", results: [
    { no, title: "The guide", url: "https://docs.example.com/guide", domain: "docs.example.com", snippet: "How it works" }, { no: no + 1, title: "Advisory", url: "https://example.org/a", domain: "example.org", snippet: "" }] }), ...over });
  const fetched = (no: number) => tool({ id: `f${no}`, name: "webfetch", args: "{}", result: JSON.stringify({ no, url: "https://docs.example.com/guide", title: "The guide, read", text: "..." }) });
  const message = (steps: AgentStep[]) => ({ id: "m", role: "assistant" as const, content: "", created_at: "", steps });

  it("lists the pages of web searches and fetches by number, and nothing else (a file read or a command is not a source)", () => {
    const messages = [message([tool({ name: "read_file", result: JSON.stringify({ text: "x" }) }), tool({ id: "b", name: "bash", result: JSON.stringify({ output: "x" }) }), search(1)])];
    expect(sourcesOf(messages).map((s) => [s.no, s.kind, s.domain, s.snippet])).toEqual([[1, "result", "docs.example.com", "How it works"], [2, "result", "example.org", ""]]);
  });

  it("makes a page that was searched for and then fetched one source, marked as read, with the title the page gave", () => {
    const got = sourcesOfSteps([search(1), fetched(1)]);
    expect(got).toHaveLength(2);
    expect(got[0]).toMatchObject({ no: 1, kind: "page", title: "The guide, read", snippet: "How it works" });
  });

  it("leaves out a call that failed or is still running, an address that is not http, and a result that is not JSON", () => {
    const bad = tool({ id: "x", name: "websearch", result: JSON.stringify({ results: [{ no: 1, title: "T", url: "javascript:alert(1)" }, { no: 2, title: "T", url: "file:///etc/passwd" }] }) });
    expect(sourcesOfSteps([search(1, { state: "failed" }), search(3, { state: "running" }), bad, tool({ name: "websearch", result: "not json" })])).toEqual([]);
  });

  it("reads the numbers an answer cites, and only in its final text", () => {
    expect([...citedNumbers("See [1] and [3]. Not a[2] or [4](http://x) or [5]: no.")].sort()).toEqual([1, 3]);
    const steps: AgentStep[] = [{ kind: "text", text: "I will look [9]", narration: true }, search(1), { kind: "text", text: "It works like this [1], as [2] says." }];
    expect([...citedIn([message(steps)])].sort()).toEqual([1, 2]);
    expect([...citedIn([{ id: "m", role: "assistant", content: "Plain answer [7]", created_at: "" }])]).toEqual([7]);
  });

  it("turns each [n] that names a source into a link to the page, and leaves code and other brackets alone", () => {
    const sources = sourcesOfSteps([search(1)]);
    const text = "It works [1] and [2].\n\nNot a source [3], an index a[1], a link [1](http://x.org), `x[1]` or\n\n```\ny[2]\n```";
    const out = linkCitations(text, sources);
    expect(out).toContain("[[1]](https://docs.example.com/guide)") ;
    expect(out).toContain("[[2]](https://example.org/a).");
    expect(out).toContain("[3], an index a[1], a link [1](http://x.org), `x[1]`");
    expect(out).toContain("```\ny[2]\n```");
    expect(linkCitations("No sources [1]", [])).toBe("No sources [1]");
  });

  it("keeps an address from ending the link early or starting another one", () => {
    const sources = sourcesOfSteps([tool({ name: "websearch", result: JSON.stringify({ query: "q", results: [{ no: 1, title: "T", url: "https://x.org/a)[evil](javascript:alert(1))" }] }) })]);
    const out = linkCitations("See [1].", sources);
    expect(out).toBe("See [[1]](https://x.org/a%29%5Bevil%5D%28javascript:alert%281%29%29).");
  });

  it("labels the web tools and says what they are about", () => {
    expect([toolLabel("websearch"), toolLabel("webfetch")]).toEqual(["Web search", "Web page"]);
    expect(argSummary("websearch", JSON.stringify({ query: "flask csrf" }))).toBe("flask csrf");
    expect(argSummary("webfetch", JSON.stringify({ url: "https://x.org/a" }))).toBe("https://x.org/a");
  });

  it("labels a tool of an MCP server as the server and the tool, and nothing else that starts that way", () => {
    expect(toolLabel("mcp__files__read_file")).toBe("files: read_file");
    expect(toolLabel("mcp__git__log__short")).toBe("git: log__short");
    expect([isMcp("mcp__files__read"), isMcp("mcp__files"), isMcp("mcp____x"), isMcp("read_file")]).toEqual([true, false, false, false]);
  });
});

describe("the terminal tools", () => {
  const step = (over: Record<string, unknown>) => ({ kind: "tool" as const, id: "c", name: "bash", args: "{}", state: "ok" as const, summary: "", ...over });

  it("labels the terminal and the search tools", () => {
    expect([toolLabel("bash"), toolLabel("grep"), toolLabel("glob"), toolLabel("recall")]).toEqual(["Terminal", "Search", "Find files", "Recall"]);
  });

  it("says what a search or a recall is about", () => {
    expect(argSummary("grep", JSON.stringify({ pattern: "TODO", path: "src" }))).toBe("TODO in src");
    expect(argSummary("bash", JSON.stringify({ command: "git status" }))).toBe("git status");
    expect(argSummary("recall", JSON.stringify({ handle: "out_0123456789ab" }))).toBe("out_0123456789ab");
  });

  it("writes a terminal call as the command, and says when it was run raw", () => {
    expect(callText("bash", JSON.stringify({ command: "rg -n x | head" }))).toBe("$ rg -n x | head");
    expect(callText("bash", JSON.stringify({ command: "git diff", raw: true }))).toBe("$ git diff   (raw: nothing left out)");
  });

  it("reads how the terminal ran a command from its result", () => {
    const r = { command: "git log", ran_as: ["rtk git log"], exit_code: 0, output: "x", lines: 4, recovery: "out_0123456789ab", condensed: { by: "rtk", hidden_lines: 9, recall: "abcdef012345" } };
    expect(terminalOf(step({ result: JSON.stringify(r) }))).toEqual({ ranAs: ["rtk git log"], hidden: 9, hiddenHandle: "abcdef012345", condensed: true, recovery: "out_0123456789ab", lines: 4, exitCode: 0 });
    expect(terminalOf(step({ result: JSON.stringify({ output: "x" }) }))).toMatchObject({ ranAs: [], condensed: false, recovery: null });
    expect(terminalOf(step({ result: JSON.stringify({ error: "refused" }) }))).toBeNull();
    expect(terminalOf(step({ name: "run_command", result: JSON.stringify({ output: "x" }) }))).toBeNull();
  });

  it("previews a terminal result as its exit code and output", () => {
    expect(previewOf(step({ result: JSON.stringify({ exit_code: 1, output: "none" }) }))).toBe("exit code 1\nnone");
  });
});

describe("live output of a running command", () => {
  const running = (id = "c1"): AgentStep[] => reduceEvent([], { type: "tool_start", id, name: "bash", arguments: "{\"command\":\"pytest\"}" });
  const NL = String.fromCharCode(10);

  it("collects what a running command prints in its own step, in order", () => {
    let steps = running();
    steps = reduceEvent(steps, { type: "tool_output", id: "c1", stream: "stdout", text: "collecting" + NL });
    steps = reduceEvent(steps, { type: "tool_output", id: "c1", stream: "stderr", text: "warning" + NL });
    expect(steps[0]).toMatchObject({ kind: "tool", state: "running", live: "collecting" + NL + "warning" + NL });
  });

  it("keeps the end of a very long output, which is the part that is new", () => {
    let steps = running();
    steps = reduceEvent(steps, { type: "tool_output", id: "c1", stream: "stdout", text: "a".repeat(15_000) });
    steps = reduceEvent(steps, { type: "tool_output", id: "c1", stream: "stdout", text: "b".repeat(15_000) });
    const live = (steps[0] as { live: string }).live;
    expect(live.length).toBe(20_000);
    expect(live.endsWith("b".repeat(15_000))).toBe(true);
  });

  it("ignores output for a step that is not running, and the final result replaces what was shown live", () => {
    const steps = reduceEvent(running("c1"), { type: "tool_output", id: "other", stream: "stdout", text: "x" });
    expect(steps).toEqual(running("c1"));
    let done = reduceEvent(running(), { type: "tool_output", id: "c1", stream: "stdout", text: "partial" });
    done = reduceEvent(done, { type: "tool", id: "c1", name: "bash", arguments: "{}", ok: true, summary: "Ran pytest", result: JSON.stringify({ exit_code: 0, output: "all of it" }) });
    expect(done[0]).toMatchObject({ state: "ok" });
    expect((done[0] as { live?: string }).live).toBeUndefined();
    expect(reduceEvent(done, { type: "tool_output", id: "c1", stream: "stdout", text: "late" })).toEqual(done);
  });
});
