import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { projectChats: vi.fn(), projectChatGet: vi.fn(), projectChatCreate: vi.fn(), projectChatUpdate: vi.fn(), projectChatDelete: vi.fn(), projectChatSend: vi.fn(),
    projectChatStop: vi.fn(), projectChatCompact: vi.fn(), projectChatClear: vi.fn(), projectChatEndpoint: vi.fn(), servingStatus: vi.fn(), servingStatusLive: vi.fn(), servingLoad: vi.fn(), updateProject: vi.fn(),
    agentSessions: vi.fn(), agentSessionCreate: vi.fn(), agentSessionGet: vi.fn(), agentSessionUpdate: vi.fn(), agentSessionDelete: vi.fn(), agentSessionRun: vi.fn(), agentSessionApprove: vi.fn(),
    agentSessionCancel: vi.fn(), agentCapabilities: vi.fn(), agentDiff: vi.fn(), agentModels: vi.fn(), agentSessionFork: vi.fn(), chatGpuView: vi.fn().mockResolvedValue({}),
    agentSessionExportUrl: (id: string, sid: string, format: string) => `/export/${id}/${sid}?format=${format}` } };
});
import { api, ApiError, type AgentCapabilities, type AgentEvent, type AgentModel, type AgentMessage, type AgentSession, type AgentSessionParams, type ChatEndpoint, type ChatEvent, type ChatSummary, type Project,
  type StoredChat, type StoredMessage } from "../../lib/api";
import { ConfirmProvider } from "../hud/AlertDialog";
import { ChatWorkspace } from "./ChatWorkspace";

const project = { id: "shop-aaaaaa", name: "Shop" } as Project;
const local: ChatEndpoint = { configured: true, model: "qwen-7b", host: "127.0.0.1", local: true, provider: "managed" };
const msg = (id: string, role: "user" | "assistant", content: string, over: Partial<StoredMessage> = {}): StoredMessage => ({ id, role, content, created_at: "2026-10-13T10:00:00.000Z", ...over });
const stored = (id: string, title: string, messages: StoredMessage[] = [], over: Partial<StoredChat> = {}): StoredChat => ({ schema: "chat/1", id, title, created_at: "2026-10-13T09:00:00.000Z", updated_at: "2026-10-13T10:00:00.000Z",
  params: { system: "", temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false }, messages, ...over });
const lastOf = <T,>(a: T[]): T | undefined => a[a.length - 1];
const summary = (c: StoredChat): ChatSummary => ({ id: c.id, title: c.title, created_at: c.created_at, updated_at: c.updated_at, message_count: c.messages.length, preview: lastOf(c.messages)?.content ?? "" });

let db: Record<string, StoredChat>;
const listed = () => Object.values(db).map(summary).sort((a, b) => b.updated_at.localeCompare(a.updated_at));        // the server lists the newest first
const view = (props: Partial<React.ComponentProps<typeof ChatWorkspace>> = {}) => render(<ConfirmProvider><ChatWorkspace project={project} kind="chat" {...props} /></ConfirmProvider>);

// Agentic mode's own fixtures (kind="agent"): a disjoint store, mirrored the same way the chat store above is.
const agentMsg = (id: string, role: AgentMessage["role"], content: string | null, over: Partial<AgentMessage> = {}): AgentMessage => ({ id, role, content, created_at: "2026-10-15T10:00:00.000Z", ...over });
const DEFAULT_AGENT_PARAMS: AgentSessionParams = { temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false, auto_approve: false };
const agentSession = (id: string, mode: AgentSession["mode"], over: Partial<AgentSession> = {}): AgentSession => ({ schema: "agent_session/1", id, project_id: project.id, mode, title: "", instructions: "",
  params: DEFAULT_AGENT_PARAMS, created_at: "2026-10-15T09:00:00.000Z", updated_at: "2026-10-15T09:00:00.000Z", status: "idle", transcript: [], task_list: [], pending_call: null, rounds: 0, ...over });
const OWN_MODEL: AgentModel = { ref: "", label: "qwen-7b", provider: "default", provider_name: "This computer (llama.cpp)", trust: "local", configured: true, profile: null };
const LAB_MODEL: AgentModel = { ref: "lab/qwen3-8b", label: "qwen3-8b", provider: "lab", provider_name: "Lab server", trust: "lan", configured: true, profile: null };
let agentDb: Record<string, AgentSession>;
const agentSummaryOf = (s: AgentSession) => ({ id: s.id, mode: s.mode, title: s.title, status: s.status, created_at: s.created_at, updated_at: s.updated_at, rounds: s.rounds, message_count: s.transcript.length, pending_call: s.pending_call });

beforeEach(() => {
  db = {};
  agentDb = {};
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.chatGpuView).mockResolvedValue({});
  vi.mocked(api.projectChatEndpoint).mockResolvedValue(local);
  vi.mocked(api.servingStatus).mockResolvedValue({ slots: { llm: { state: "ready", engine: { context: 4096 } } } } as any);
  vi.mocked(api.servingStatusLive).mockResolvedValue({ choices: { llm: { engine: "local", model: "qwen-7b" } }, slots: { llm: { state: "ready" } } } as any);
  vi.mocked(api.projectChats).mockImplementation(async () => ({ chats: listed(), damaged: 0 }));
  vi.mocked(api.projectChatGet).mockImplementation(async (_p, id) => structuredClone(db[id]));
  vi.mocked(api.projectChatCreate).mockImplementation(async (_p, body) => { const c = stored("new000000001", "", [], { params: { system: "", temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false, ...(body?.params ?? {}) } }); db[c.id] = c; return structuredClone(c); });
  vi.mocked(api.projectChatUpdate).mockImplementation(async (_p, id, body) => { const c = db[id]; if (body.title) c.title = body.title; if (body.params) c.params = { ...c.params, ...body.params } as any; return structuredClone(c); });
  vi.mocked(api.projectChatDelete).mockImplementation(async (_p, id) => { delete db[id]; return { ok: true }; });
  vi.mocked(api.projectChatStop).mockResolvedValue({ stopped: true });
  vi.mocked(api.agentModels).mockResolvedValue({ models: [OWN_MODEL] });
  vi.mocked(api.agentSessions).mockImplementation(async () => ({ sessions: Object.values(agentDb).map(agentSummaryOf), damaged: 0 }));
  vi.mocked(api.agentSessionGet).mockImplementation(async (_p, id) => structuredClone(agentDb[id]));
  vi.mocked(api.agentSessionCreate).mockImplementation(async (_p, mode, instructions, params) => {
    const s = agentSession(`new-${Object.keys(agentDb).length}`, mode, { instructions, params: { ...DEFAULT_AGENT_PARAMS, ...params } });
    agentDb[s.id] = s; return structuredClone(s);
  });
  vi.mocked(api.agentSessionUpdate).mockImplementation(async (_p, id, body) => {
    const s = agentDb[id];
    if (body.title !== undefined) s.title = body.title;
    if (body.instructions !== undefined) s.instructions = body.instructions;
    if (body.params) s.params = { ...s.params, ...body.params } as any;
    if (body.mode) s.mode = body.mode;
    if (body.model_ref !== undefined) { if (body.model_ref) s.model_ref = body.model_ref; else delete s.model_ref; }
    return structuredClone(s);
  });
  vi.mocked(api.agentSessionDelete).mockImplementation(async (_p, id) => { delete agentDb[id]; return { deleted: true }; });
  vi.mocked(api.agentSessionCancel).mockResolvedValue({ stopped: true });
  vi.mocked(api.agentCapabilities).mockResolvedValue({ plan: { max_rounds: 20, tools: ["list_files", "propose_plan"] },
    manual: { max_rounds: 40, tools: ["write_file", "run_command"], confirm_tools: ["write_file", "run_command"] }, auto: { max_rounds: 60, max_seconds: 1800, tools: ["write_file", "run_command"] } });
  vi.mocked(api.agentDiff).mockResolvedValue({ files: [] });
});

/** A turn that answers with these events, then saves the pair the way the server does. */
const answerWith = (events: ChatEvent[], save = true) => vi.mocked(api.projectChatSend).mockImplementation(async (_p, id, body, _s, onEvent) => {
  for (const e of events) onEvent(e);
  if (save) {
    const c = db[id];
    if (body.regenerate) c.messages = c.messages.filter((m, i, all) => !(i === all.length - 1 && m.role === "assistant"));
    if (body.edit_of) c.messages = c.messages.slice(0, c.messages.findIndex((m) => m.id === body.edit_of));
    c.messages.push(msg("u-" + c.messages.length, "user", body.text ?? lastOf(c.messages)?.content ?? ""));
    const of = <K extends ChatEvent["type"]>(type: K) => events.filter((e): e is Extract<ChatEvent, { type: K }> => e.type === type);
    const end = of("done")[0];
    c.messages.push(msg("a-" + c.messages.length, "assistant", of("token").map((e) => e.text).join(""), {
      model: "qwen-7b", thinking: of("thinking").map((e) => e.text).join("") || undefined, tools: of("tool").map((e) => ({ ok: e.ok, summary: e.summary })),
      notices: of("notice").map((e) => e.text), sources: end?.sources, usage: end?.usage, error: of("error")[0]?.message, stopped: end?.stopped || undefined }));        // what the server saves of an answer
    if (!c.title) c.title = (body.text ?? "").slice(0, 60);
  }
});
const done = (over: Partial<Extract<ChatEvent, { type: "done" }>> = {}): ChatEvent => ({ type: "done", sources: [], usage: { prompt_tokens: 800, completion_tokens: 40, seconds: 2, tokens_per_second: 20 }, model: "qwen-7b", model_label: "qwen-7b", ...over });
const ask = async (text: string) => {
  fireEvent.change(screen.getByRole("textbox", { name: "Your question" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
};
const thread = () => within(screen.getByRole("list", { name: "The conversation" }));
const idle = () => act(async () => { await new Promise((r) => setTimeout(r, 20)); });

/** An agent run that answers with these events, then saves the transcript the way the server does (the assistant message with tool_calls, each resolved "tool" message, and the
 * final plain answer or the pending_call, matching agent_loop.py's own persistence). */
const agentAnswerWith = (events: AgentEvent[]) => vi.mocked(api.agentSessionRun).mockImplementation(async (_p, id, body, _s, onEvent) => {
  for (const e of events) onEvent(e);
  const s = agentDb[id];
  if (body.instruction) s.transcript.push(agentMsg(`u-${s.transcript.length}`, "user", body.instruction));
  const of = <K extends AgentEvent["type"]>(type: K) => events.filter((e): e is Extract<AgentEvent, { type: K }> => e.type === type);
  const toolCalls = of("pending_approval");
  const toolRuns = of("tool");
  if (toolCalls.length || toolRuns.length) {
    const calls = [...toolRuns.map((e) => ({ id: `c-${e.name}`, type: "function" as const, function: { name: e.name, arguments: e.arguments } })),
      ...toolCalls.map((e) => ({ id: e.call.id, type: "function" as const, function: { name: e.call.name, arguments: e.call.arguments } }))];
    s.transcript.push(agentMsg(`a-${s.transcript.length}`, "assistant", null, { tool_calls: calls }));
    for (const e of toolRuns) s.transcript.push(agentMsg(`t-${s.transcript.length}`, "tool", JSON.stringify({ summary: e.summary }), { tool_call_id: `c-${e.name}`, ok: e.ok, name: e.name, summary: e.summary }));
    if (toolCalls.length) { s.pending_call = { ...toolCalls[0].call, asked_at: "2026-10-15T10:00:00.000Z" }; s.status = "waiting_approval"; return; }
  }
  const answer = of("token").map((e) => e.text).join("");
  if (answer || !toolCalls.length) { s.transcript.push(agentMsg(`a-${s.transcript.length}`, "assistant", answer)); s.status = "done"; }
});
const agentThread = () => within(screen.getByRole("list", { name: "The session" }));
/** Picks a mode via the composer's own dropdown, types the first message, and sends it -- creating the session (empty instructions) and running that message as its first turn
 * in one action, the same way a person just typing into a chat and hitting Send would. */
const createSession = async (mode: "plan" | "manual" | "auto" = "plan", text = "hello") => {
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Mode" }));
  await user.click(screen.getByRole("option", { name: new RegExp(`^${mode}$`, "i") }));
  fireEvent.change(screen.getByRole("textbox", { name: "Your question" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(api.agentSessionCreate).toHaveBeenCalled());
};

describe("opening the chat", () => {
  it("opens the newest conversation with its messages, lists the others, and says the model", async () => {
    db.old = stored("old", "Older talk", [msg("o1", "user", "old question")], { updated_at: "2026-10-12T10:00:00.000Z" });
    db.cur = stored("cur", "Current talk", [msg("c1", "user", "What is risky?"), msg("c2", "assistant", "The **login** flow.")]);
    view();
    expect(await screen.findByText("What is risky?")).toBeInTheDocument();
    expect(screen.getByText("login")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Current talk/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: /^Older talk/ })).toBeInTheDocument();
    expect(await screen.findByText("qwen-7b", { selector: "p" })).toBeInTheDocument();
  });

  it("offers a ready, empty composer on an empty project, with no conversation until one is asked", async () => {
    view();
    expect(await screen.findByRole("textbox", { name: "Your question" })).toBeEnabled();
    expect(screen.queryByLabelText("Questions to start with")).toBeNull();                       // the starter chips are gone; the composer alone starts a conversation
    expect(api.projectChatCreate).not.toHaveBeenCalled();
    expect(screen.getByText("No conversation yet. Ask a question to start one.")).toBeInTheDocument();
  });

  it("says when no language model is set up and cannot send", async () => {
    vi.mocked(api.projectChatEndpoint).mockResolvedValue({ ...local, configured: false, model: null });
    view();
    expect(await screen.findAllByText(/No language model is set up/)).not.toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "Your question" })).toBeDisabled();
  });

  it("shows why the conversations could not be read", async () => {
    vi.mocked(api.projectChats).mockRejectedValue(new ApiError({ status: 500, title: "The chats folder is unreadable." }));
    view();
    expect(await screen.findByRole("alert")).toHaveTextContent("The chats folder is unreadable.");
  });
});

describe("asking", () => {
  it("creates the conversation on the first question, shows the question at once, writes the answer as it arrives and reads the saved conversation at the end", async () => {
    answerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true }, { type: "token", text: "Look at " }, { type: "token", text: "**auth.py**." }, done({ sources: [{ tool: "read_file", summary: "Read auth.py" }] })]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("Where is the login?");
    expect(await screen.findByText("Where is the login?")).toBeInTheDocument();
    await waitFor(() => expect(api.projectChatCreate).toHaveBeenCalledWith("shop-aaaaaa", { params: { system: "", autocompact: true, web_search: false, temperature: 0.2, top_p: 0.9, max_tokens: 1024 } }));
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "new000000001", { text: "Where is the login?", allow_remote: false }, expect.anything(), expect.any(Function)));
    expect(await screen.findByText("auth.py")).toBeInTheDocument();
    await waitFor(() => expect(api.projectChatGet).toHaveBeenCalledWith("shop-aaaaaa", "new000000001"));
    expect(await screen.findByRole("button", { name: /^Where is the login/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("textbox", { name: "Your question" })).toHaveValue("");
  });

  it("asks in the open conversation without creating another", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "first"), msg("c2", "assistant", "an answer")]);
    answerWith([{ type: "token", text: "more" }, done()]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("an answer");
    await ask("and then?");
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "cur", { text: "and then?", allow_remote: false }, expect.anything(), expect.any(Function)));
    expect(api.projectChatCreate).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getAllByText("more").length).toBeGreaterThan(0));
  });

  it("shows the thinking, what the assistant looked at, a notice and the speed", async () => {
    answerWith([{ type: "thinking", text: "Check the routes. " }, { type: "thinking", text: "Then the tests." }, { type: "tool", name: "read_file", arguments: "{}", ok: true, summary: "Read app.py" },
      { type: "notice", text: "The oldest 4 messages were left out to fit the context." }, { type: "token", text: "Done." },
      done({ sources: [{ tool: "read_file", summary: "Read app.py" }] })]);
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("a");
    await ask("again");
    await waitFor(() => {                                                   // the live answer is replaced by the saved one when the turn is over: look at the end state
      expect(screen.getByText("Check the routes. Then the tests.")).toBeInTheDocument();
      expect(screen.getByText("Looked at: Read app.py")).toBeInTheDocument();
      expect(screen.getByText("The oldest 4 messages were left out to fit the context.")).toHaveAttribute("role", "status");
      expect(screen.getByText(/40 tokens, 20.0 tokens a second, 2.0 s \(prompt 800 tokens\)/)).toBeInTheDocument();
    });
  });

  it("says what went wrong when the answer fails, and keeps the question", async () => {
    answerWith([{ type: "error", message: "The model endpoint did not answer." }]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("hello?");
    expect(await screen.findByRole("alert")).toHaveTextContent("The model endpoint did not answer.");
    expect(thread().getByText("hello?")).toBeInTheDocument();
  });

  it("says why a turn was refused before it started", async () => {
    vi.mocked(api.projectChatSend).mockRejectedValue(new ApiError({ status: 409, title: "Busy", code: "busy", detail: "This conversation is still being answered. Stop it or wait for it to finish." }));
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    view();
    await screen.findByText("a");
    await ask("one more");
    expect(await screen.findByRole("alert")).toHaveTextContent(/still being answered/);
    await idle();                                                           // the saved copy is read meanwhile: it has nothing of the refused turn, and the page keeps what it drew
    expect(screen.getByRole("alert")).toHaveTextContent(/still being answered/);
    expect(thread().getByText("one more")).toBeInTheDocument();
  });
});

describe("loading the local model before a turn", () => {
  it("disables the composer and shows the progress dialog instead of sending, then sends once the model turns ready", async () => {
    vi.mocked(api.servingStatusLive).mockResolvedValueOnce({ choices: { llm: { engine: "local", model: "qwen-7b" } }, slots: { llm: { state: "absent" } } } as any);
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: { slots: {} } } as any);
    answerWith([{ type: "token", text: "ok" }, done()]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("go");
    expect(await screen.findByRole("dialog", { name: "Loading the language model" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Your question", hidden: true })).toBeDisabled();
    expect(api.projectChatSend).not.toHaveBeenCalled();
    vi.mocked(api.servingStatusLive).mockResolvedValue({ choices: { llm: { engine: "local", model: "qwen-7b" } }, slots: { llm: { state: "ready" } } } as any);
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalled(), { timeout: 4000 });
  });

  it("sends at once when the model is already ready", async () => {
    answerWith([{ type: "token", text: "ok" }, done()]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("go");
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalled());
    expect(screen.queryByRole("dialog", { name: "Loading the language model" })).toBeNull();
  });
});

describe("a model that is not on this computer", () => {
  const remote = { ...local, local: false, host: "api.example.org" };

  it("asks before anything is sent, sends nothing when the answer is no, and remembers a yes", async () => {
    vi.mocked(api.projectChatEndpoint).mockResolvedValue(remote);
    answerWith([{ type: "token", text: "ok" }, done()]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("first");
    const ask1 = await screen.findByRole("alertdialog");
    expect(ask1).toHaveTextContent("Send excerpts of Shop to api.example.org?");
    fireEvent.click(within(ask1).getByRole("button", { name: "Not now" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.projectChatSend).not.toHaveBeenCalled();
    expect(api.projectChatCreate).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Your question" })).toHaveValue("first");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Send excerpts" }));
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "new000000001", { text: "first", allow_remote: true }, expect.anything(), expect.any(Function)));
    await idle();
    await ask("second");
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});

describe("while an answer is being written", () => {
  /** A turn that has said these things and is still going: the page shows what has arrived. */
  const midway = (events: ChatEvent[]) => vi.mocked(api.projectChatSend).mockImplementation(async (_p, _id, _b, _s, onEvent) => { for (const e of events) onEvent(e); await new Promise<void>(() => undefined); });

  it("shows the thinking, the files it looked at, the notices and the words as they arrive, and the model that writes", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    midway([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true }, { type: "thinking", text: "Let me look " }, { type: "thinking", text: "at the routes." },
      { type: "retrieved", items: [{ file: "src/app.py", line: 3, score: 0.9 }] }, { type: "retrieved_web", items: [{ url: "https://example.com/x", title: "X", domain: "example.com", score: 0.8 }] },
      { type: "tool", name: "read_file", arguments: "{}", ok: false, summary: "A file that is kept from the assistant" },
      { type: "notice", text: "The oldest 2 messages were left out of what the model is sent." }, { type: "token", text: "The key is " }, { type: "token", text: "hard-coded." }]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("a");
    await ask("what now?");
    expect(await thread().findByText("The key is hard-coded.")).toBeInTheDocument();
    expect(thread().getByText("Let me look at the routes.")).toBeInTheDocument();
    expect(thread().getByText("Looked at: Searched the project index: src/app.py:3")).toBeInTheDocument();
    expect(thread().getByText("Looked at: Searched the web: example.com")).toBeInTheDocument();
    expect(thread().getByText("Could not: A file that is kept from the assistant")).toBeInTheDocument();
    expect(thread().getByText("The oldest 2 messages were left out of what the model is sent.")).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(thread().getAllByRole("button", { name: "Copy the answer" }), "only the earlier answer has actions: the one being written has none yet").toHaveLength(1);
  });

  it("shows the sources, the speed and that it was stopped from the done event, before the saved copy is read", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    let release: () => void = () => undefined;
    vi.mocked(api.projectChatGet).mockImplementationOnce(async () => structuredClone(db.cur)).mockImplementation(() => new Promise((resolve) => { release = () => resolve(structuredClone(db.cur)); }));
    vi.mocked(api.projectChatSend).mockImplementation(async (_p, _id, _b, _s, onEvent) => {
      onEvent({ type: "token", text: "Half an answer" });
      onEvent({ type: "done", sources: [{ tool: "read_file", summary: "Read auth.py" }], usage: { prompt_tokens: 500, completion_tokens: 12, seconds: 2, tokens_per_second: 6 }, model: "qwen-7b", model_label: "qwen-7b", stopped: true });
    });
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await ask("go");
    expect(await thread().findByText("Stopped. What was written so far is kept.")).toBeInTheDocument();
    expect(thread().getByText("qwen-7b")).toBeInTheDocument();
    expect(thread().getByText("12 tokens, 6.0 tokens a second, 2.0 s (prompt 500 tokens)")).toBeInTheDocument();
    expect(thread().getByText(/can still misread what it reads/)).toBeInTheDocument();
    release();
  });

  it("keeps what it had drawn and shows the saved conversation only when it can be read", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    vi.mocked(api.projectChatSend).mockImplementation(async (_p, _id, _b, _s, onEvent) => { onEvent({ type: "token", text: "A drawn answer" }); onEvent(done()); });
    vi.mocked(api.projectChatGet).mockImplementationOnce(async () => structuredClone(db.cur)).mockRejectedValue(new Error("offline"));
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await ask("go on");
    expect(await thread().findByText("A drawn answer")).toBeInTheDocument();
    await idle();
    expect(thread().getByText("A drawn answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  });
});

describe("stopping, regenerating and editing", () => {
  it("stops the answer with a request of its own, and shows what the server kept", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    let finish: () => void = () => undefined;
    vi.mocked(api.projectChatSend).mockImplementation((_p, _id, _b, _s, onEvent) => new Promise<void>((resolve) => {
      onEvent({ type: "token", text: "Half an " });
      finish = () => {
        db.cur.messages.push(msg("u9", "user", "stop me"), msg("a9", "assistant", "Half an answer", { stopped: true }));
        onEvent({ type: "done", sources: [], usage: null, model: "qwen-7b", model_label: "qwen-7b", stopped: true });
        resolve();
      };
    }));
    vi.mocked(api.projectChatStop).mockImplementation(async () => { finish(); return { stopped: true }; });
    view();
    await screen.findByText("a");
    await ask("stop me");
    expect(await screen.findByText("Half an")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(api.projectChatStop).toHaveBeenCalledWith("shop-aaaaaa", "cur"));
    expect(await screen.findByText("Stopped. What was written so far is kept.")).toBeInTheDocument();
    expect(thread().getByText("Half an answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  });

  it("stops listening when the server cannot be asked to stop", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    let signal: AbortSignal | null = null;
    vi.mocked(api.projectChatSend).mockImplementation((_p, _id, _b, s) => new Promise<void>((resolve) => { signal = s; s.addEventListener("abort", () => resolve()); }));
    vi.mocked(api.projectChatStop).mockRejectedValue(new Error("offline"));
    view();
    await screen.findByText("a");
    await ask("hang");
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    await waitFor(() => expect(signal!.aborted).toBe(true));
  });

  it("asks the last answer again, taking the old one off the page at once", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "the question"), msg("c2", "assistant", "the first answer")]);
    answerWith([{ type: "token", text: "the second answer" }, done()]);
    view();
    await screen.findByText("the first answer");
    fireEvent.click(screen.getByRole("button", { name: "Regenerate the answer" }));
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "cur", { regenerate: true, allow_remote: false }, expect.anything(), expect.any(Function)));
    expect(await thread().findByText("the second answer")).toBeInTheDocument();
    expect(thread().queryByText("the first answer")).toBeNull();
  });

  it("sends an edited question from where it was, and the answers after it go", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "old words"), msg("c2", "assistant", "old answer"), msg("c3", "user", "later"), msg("c4", "assistant", "later answer")]);
    answerWith([{ type: "token", text: "new answer" }, done()]);
    view();
    await screen.findByText("old words");
    fireEvent.click(screen.getAllByRole("button", { name: "Edit and send again" })[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "Your question, edited" }), { target: { value: "new words" } });
    fireEvent.click(screen.getByRole("button", { name: "Send again" }));
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "cur", { edit_of: "c1", text: "new words", allow_remote: false }, expect.anything(), expect.any(Function)));
    expect(await thread().findByText("new answer")).toBeInTheDocument();
    expect(thread().queryByText("later answer")).toBeNull();
    expect(thread().queryByText("old answer")).toBeNull();
  });

  it("does not offer Regenerate or Edit before the conversation is saved", async () => {
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    expect(screen.queryByRole("button", { name: "Regenerate the answer" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit and send again" })).toBeNull();
  });

  it("keeps the person in the conversation while it is answered: the settings, the list and the box are held", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    db.other = stored("other", "Other", [msg("o1", "user", "x")], { updated_at: "2026-10-01T10:00:00.000Z" });
    vi.mocked(api.projectChatSend).mockImplementation(() => new Promise<void>(() => undefined));
    view();
    await screen.findByText("a");
    await ask("never ends");
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New chat" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Other/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit instructions" })).toBeDisabled();
  });
});

describe("what the page keeps and drops while a turn is under way", () => {
  const stall = () => vi.mocked(api.projectChatSend).mockImplementation(() => new Promise<void>(() => undefined));

  it("takes the old answer off the page at once when it is asked again", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "the question"), msg("c2", "assistant", "the first answer")]);
    stall();
    view();
    await screen.findByRole("list", { name: "The conversation" });
    fireEvent.click(await screen.findByRole("button", { name: "Regenerate the answer" }));
    expect(await thread().findByText("Writing...")).toBeInTheDocument();
    expect(thread().queryByText("the first answer")).toBeNull();
    expect(thread().getByText("the question")).toBeInTheDocument();
  });

  it("takes the answers after an edited question off the page at once, and shows the edited words", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "old words"), msg("c2", "assistant", "old answer"), msg("c3", "user", "later"), msg("c4", "assistant", "later answer")]);
    stall();
    view();
    await screen.findByRole("list", { name: "The conversation" });
    fireEvent.click(screen.getAllByRole("button", { name: "Edit and send again" })[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "Your question, edited" }), { target: { value: "new words" } });
    fireEvent.click(screen.getByRole("button", { name: "Send again" }));
    expect(await thread().findByText("Writing...")).toBeInTheDocument();
    expect(thread().getByText("new words")).toBeInTheDocument();
    for (const gone of ["old words", "old answer", "later", "later answer"]) expect(thread().queryByText(gone)).toBeNull();
  });

  it("shows the model of an answer that ended with an error, from the start event, when the saved copy cannot be read", async () => {
    db.cur = stored("cur", "Cur", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    vi.mocked(api.projectChatGet).mockImplementationOnce(async () => structuredClone(db.cur)).mockRejectedValue(new Error("offline"));
    vi.mocked(api.projectChatSend).mockImplementation(async (_p, _id, _b, _s, onEvent) => { onEvent({ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true }); onEvent({ type: "error", message: "The model endpoint did not answer." }); });
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await ask("go");
    expect(await screen.findByRole("alert")).toHaveTextContent("The model endpoint did not answer.");
    await idle();
    expect(thread().getByText("qwen-7b")).toBeInTheDocument();
  });

  it("asks again for the confirmation when the server says it was not given", async () => {
    vi.mocked(api.projectChatEndpoint).mockResolvedValue({ ...local, local: false, host: "api.example.org" });
    vi.mocked(api.projectChatSend).mockRejectedValueOnce(new ApiError({ status: 409, title: "Not confirmed", code: "remote_not_confirmed", detail: "The model runs on api.example.org. Confirm that first." }));
    answerWith([{ type: "token", text: "ok" }, done()]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("first");
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Send excerpts" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Confirm that first.");
    await idle();
    await ask("second");
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Send excerpts of Shop to api.example.org?");
  });

  it("starts New chat with the model's own settings, not with what was set for the last blank one", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "q")]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit instructions" }));
    const dialog = await screen.findByRole("dialog", { name: "Instructions" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Instructions" }), { target: { value: "Answer in French." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("Answer in French.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Current/ }));
    await screen.findByRole("list", { name: "The conversation" });
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(screen.queryByText("Answer in French.")).toBeNull());
    await ask("hello");
    await waitFor(() => expect(api.projectChatCreate).toHaveBeenCalledWith("shop-aaaaaa", { params: { system: "", autocompact: true, web_search: false, temperature: 0.2, top_p: 0.9, max_tokens: 1024 } }));
  });
});

describe("the conversations", () => {
  it("starts a blank conversation with New chat, and the next question creates it", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    answerWith([{ type: "token", text: "fresh" }, done()]);
    view();
    await screen.findByText("a");
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(screen.queryByRole("list", { name: "The conversation" })).toBeNull());    // back to a blank draft, not "cur" any more
    await ask("a fresh start");
    await waitFor(() => expect(api.projectChatCreate).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "new000000001", { text: "a fresh start", allow_remote: false }, expect.anything(), expect.any(Function)));
  });

  it("opens another conversation when it is chosen", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "current words")]);
    db.other = stored("other", "Other", [msg("o1", "user", "other words")], { updated_at: "2026-10-01T10:00:00.000Z" });
    view();
    await screen.findByText("current words");
    fireEvent.click(screen.getByRole("button", { name: /^Other/ }));
    expect(await thread().findByText("other words")).toBeInTheDocument();
    expect(thread().queryByText("current words")).toBeNull();
    expect(screen.getByRole("button", { name: /^Other/ })).toHaveAttribute("aria-current", "true");
  });

  it("renames a conversation, in the list", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "q")]);
    view();
    await screen.findByRole("button", { name: /^Current/ });
    fireEvent.click(screen.getByRole("button", { name: "Rename Current" }));
    const box = screen.getByRole("textbox", { name: "New name of Current" });
    fireEvent.change(box, { target: { value: "Login review" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(api.projectChatUpdate).toHaveBeenCalledWith("shop-aaaaaa", "cur", { title: "Login review" }));
    expect(await screen.findByRole("button", { name: /^Login review/ })).toBeInTheDocument();
  });

  it("deletes the open conversation after asking, and opens the next one", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "current words")]);
    db.other = stored("other", "Other", [msg("o1", "user", "other words")], { updated_at: "2026-10-01T10:00:00.000Z" });
    view();
    await screen.findByText("current words");
    fireEvent.click(screen.getByRole("button", { name: "Delete Current" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete it" }));
    await waitFor(() => expect(api.projectChatDelete).toHaveBeenCalledWith("shop-aaaaaa", "cur"));
    expect(await thread().findByText("other words")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Current/ })).toBeNull();
  });

  it("goes back to a blank conversation when the last one is deleted", async () => {
    db.cur = stored("cur", "Only", [msg("c1", "user", "only words")]);
    view();
    await screen.findByText("only words");
    fireEvent.click(screen.getByRole("button", { name: "Delete Only" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete it" }));
    expect(await screen.findByText("No conversation yet. Ask a question to start one.")).toBeInTheDocument();     // "Only" was the one conversation there was
    expect(screen.queryByText("only words")).toBeNull();
  });

  it("shows the sentence of a failed rename", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "q")]);
    vi.mocked(api.projectChatUpdate).mockRejectedValue(new ApiError({ status: 400, title: "A conversation needs a name." }));
    view();
    await screen.findByRole("button", { name: /^Current/ });
    fireEvent.click(screen.getByRole("button", { name: "Rename Current" }));
    fireEvent.change(screen.getByRole("textbox", { name: "New name of Current" }), { target: { value: "x" } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "New name of Current" }), { key: "Enter" });
    expect(await screen.findByRole("alert")).toHaveTextContent("A conversation needs a name.");
  });
});

describe("the settings of a conversation", () => {
  it("saves a value of the open conversation and shows what the server says is wrong with one", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "q")]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("q");
    const temperature = await screen.findByRole("spinbutton", { name: "Temperature" });
    fireEvent.change(temperature, { target: { value: "0.4" } });
    fireEvent.blur(temperature);
    await waitFor(() => expect(api.projectChatUpdate).toHaveBeenCalledWith("shop-aaaaaa", "cur", { params: { temperature: 0.4 } }));
    vi.mocked(api.projectChatUpdate).mockRejectedValueOnce(new ApiError({ status: 400, title: "Bad", detail: "The temperature must be between 0 and 2." }));
    fireEvent.change(temperature, { target: { value: "9" } });
    fireEvent.blur(temperature);
    expect(await screen.findByRole("alert")).toHaveTextContent("The temperature must be between 0 and 2.");
  });

  it("keeps the settings of a conversation that is not saved yet, and gives them to it when it is created", async () => {
    answerWith([{ type: "token", text: "ok" }, done()]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    fireEvent.click(screen.getByRole("button", { name: "Edit instructions" }));
    const dialog = await screen.findByRole("dialog", { name: "Instructions" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Instructions" }), { target: { value: "Answer in French." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.projectChatUpdate).not.toHaveBeenCalled();
    await ask("bonjour");
    await waitFor(() => expect(api.projectChatCreate).toHaveBeenCalledWith("shop-aaaaaa", { params: { system: "Answer in French.", autocompact: true, web_search: false, temperature: 0.2, top_p: 0.9, max_tokens: 1024 } }));
  });

  it("shows the context used and the model in the settings", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "x".repeat(400))]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText(/x{400}/);
    expect(await screen.findByText("about 800 of 4,096 tokens of context")).toBeInTheDocument();
    expect(screen.getByText("Runs on this computer.")).toBeInTheDocument();
  });
});

describe("the layout", () => {
  it("shows the list and the settings of a conversation beside the thread, and the Settings button folds the settings away", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "words")]);
    view();
    await screen.findByText("words");
    expect(screen.getByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "This conversation's settings" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.queryByRole("region", { name: "This conversation's settings" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("region", { name: "This conversation's settings" })).toBeInTheDocument();
  });

  it("stops listening when it is left", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "q"), msg("c2", "assistant", "a")]);
    let signal: AbortSignal | null = null;
    vi.mocked(api.projectChatSend).mockImplementation((_p, _id, _b, s) => new Promise<void>(() => { signal = s; }));
    const { unmount } = view();
    await screen.findByText("a");
    await ask("long one");
    await waitFor(() => expect(signal).not.toBeNull());
    unmount();
    expect(signal!.aborted).toBe(true);
  });

  it("offers the conversation list and the settings as drawers for narrow windows, closing the conversations one on a pick", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "words")]);
    db.other = stored("other", "Other", [msg("o1", "user", "x")], { updated_at: "2026-10-01T10:00:00.000Z" });
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("words");
    fireEvent.click(screen.getByRole("button", { name: "Conversations (narrow window)" }));
    const convoDialog = await screen.findByRole("dialog", { name: "Conversations" });
    fireEvent.click(within(convoDialog).getByRole("button", { name: /^Other/ }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Conversations" })).toBeNull());
    await thread().findByText("x");
    fireEvent.click(screen.getByRole("button", { name: "Settings (narrow window)" }));
    expect(await screen.findByRole("dialog", { name: "This conversation's settings" })).toBeInTheDocument();
  });
});

describe("which conversation stays open across a visit", () => {
  it("reopens the given chat id when it still exists, instead of the most recently updated one", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "newest")], { updated_at: "2026-10-13T12:00:00.000Z" });
    db.older = stored("older", "Older", [msg("o1", "user", "older words")], { updated_at: "2026-10-01T10:00:00.000Z" });
    view({ initialId: "older" });
    await screen.findByRole("list", { name: "The conversation" });
    expect(await thread().findByText("older words")).toBeInTheDocument();
    expect(thread().queryByText("newest")).toBeNull();
  });

  it("falls back to the most recently updated conversation when the given id is gone", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "newest")], { updated_at: "2026-10-13T12:00:00.000Z" });
    view({ initialId: "long-gone" });
    await screen.findByRole("list", { name: "The conversation" });
    expect(await thread().findByText("newest")).toBeInTheDocument();
  });

  it("starts on an empty draft instead of the most recently updated conversation when startEmpty is given", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "newest")], { updated_at: "2026-10-13T12:00:00.000Z" });
    view({ startEmpty: true });
    await screen.findByRole("textbox", { name: "Your question" });
    expect(screen.queryByRole("list", { name: "The conversation" })).toBeNull();      // "cur" still shows in the sidebar's own list; it is just not the open one
  });

  it("still opens the given chat id when startEmpty is also given", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "newest")], { updated_at: "2026-10-13T12:00:00.000Z" });
    view({ initialId: "cur", startEmpty: true });
    await screen.findByRole("list", { name: "The conversation" });
    expect(await thread().findByText("newest")).toBeInTheDocument();
  });

  it("reports the open chat id back as it changes: picking one, starting a new one, and deleting the open one", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "words")]);
    db.other = stored("other", "Other", [msg("o1", "user", "x")], { updated_at: "2026-10-01T10:00:00.000Z" });
    const onIdChange = vi.fn();
    view({ onIdChange });
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("words");
    await waitFor(() => expect(onIdChange).toHaveBeenCalledWith("cur"));
    fireEvent.click(screen.getByRole("button", { name: /^Other/ }));
    await thread().findByText("x");
    expect(onIdChange).toHaveBeenLastCalledWith("other");
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(onIdChange).toHaveBeenLastCalledWith(null);
    fireEvent.click(screen.getByRole("button", { name: /^Current/ }));
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("words");
    fireEvent.click(screen.getByRole("button", { name: "Delete Current" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete it" }));
    await waitFor(() => expect(onIdChange).toHaveBeenLastCalledWith("other"));
  });
});

describe("a slash command typed into the composer", () => {
  it("answers locally and never sends a real turn", async () => {
    db.cur = stored("cur", "Current", [msg("c1", "user", "words"), msg("c2", "assistant", "an answer")]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("an answer");
    await ask("/context");
    expect(await thread().findByText(/tokens of context/)).toBeInTheDocument();
    expect(api.projectChatSend).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Your question" })).toHaveValue("");
  });

  it("/clear empties the conversation on the server and in the thread", async () => {
    vi.mocked(api.projectChatClear).mockResolvedValue({} as any);
    db.cur = stored("cur", "Current", [msg("c1", "user", "words"), msg("c2", "assistant", "an answer")]);
    view();
    await screen.findByRole("list", { name: "The conversation" });
    await thread().findByText("an answer");
    await ask("/clear");
    await waitFor(() => expect(api.projectChatClear).toHaveBeenCalledWith("shop-aaaaaa", "cur"));
    await waitFor(() => expect(screen.queryByRole("list", { name: "The conversation" })).toBeNull());
  });

  it("/deep-research sends the question alone, with the mode as a flag the command word never becomes part of", async () => {
    answerWith([{ type: "token", text: "A longer answer." }, done()]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("/deep-research why does this leak memory?");
    await screen.findByRole("list", { name: "The conversation" });
    expect(await thread().findByText("why does this leak memory?")).toBeInTheDocument();
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "new000000001",
      { text: "why does this leak memory?", deep_research: true, plan_mode: undefined, force_web_search: true, allow_remote: false }, expect.anything(), expect.any(Function)));
  });

  it("an unrecognized leading slash is sent as an ordinary question", async () => {
    answerWith([{ type: "token", text: "ok" }, done()]);
    view();
    await screen.findByRole("textbox", { name: "Your question" });
    await ask("/etc/passwd readable?");
    await waitFor(() => expect(api.projectChatSend).toHaveBeenCalledWith("shop-aaaaaa", "new000000001", { text: "/etc/passwd readable?", allow_remote: false }, expect.anything(), expect.any(Function)));
  });
});

// Agentic mode: the same ChatWorkspace component, kind="agent" -- a session instead of a chat, a mode dropdown beside Send instead of the slash menu, tool-call chips and a task
// list, and (Manual) an approval card in place of the composer. Everything above this point (the sidebar, the message thread, stop/regenerate/edit, the layout) is exercised with
// kind="chat" and applies to an agent instance unchanged, since it is the same render tree; only what genuinely differs between the two kinds is tested again here.
describe("agentic mode: starting and asking", () => {
  it("offers a mode picker and a composer with no session yet, and starting sends the first message as the first turn", async () => {
    view({ kind: "agent" });
    expect(await screen.findByRole("combobox", { name: "Mode" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Your question" })).toBeInTheDocument();
    await createSession("manual", "Look around");
    expect(api.agentSessionCreate).toHaveBeenCalledWith("shop-aaaaaa", "manual", "", { autocompact: true, web_search: false, auto_approve: false, engine: "v2", temperature: 0.2, top_p: 0.9, max_tokens: null });
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { instruction: "Look around", allow_remote: false }, expect.anything(), expect.anything()));
  });

  it("plan mode answers a question with a plain answer and no approval card", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" }, { type: "token", text: "Looks fine." }, { type: "done" }]);
    view({ kind: "agent" });
    await createSession("plan", "Is it safe?");
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    expect(await agentThread().findByText("Looks fine.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog", { name: "Approval needed" })).toBeNull();
  });

  it("shows the task list once the session has one", async () => {
    agentDb.s1 = agentSession("s1", "auto", { task_list: [{ text: "Update the dependency", done: true }, { text: "Re-run the scan", done: false }] });
    view({ kind: "agent", initialId: "s1" });
    const list = await screen.findByRole("list", { name: "Task list" });
    expect(within(list).getByText("Done: Update the dependency")).toBeInTheDocument();
    expect(within(list).getByText("Active: Re-run the scan")).toBeInTheDocument();
  });
});

describe("agentic mode: the mode control", () => {
  it("is as tall as the composer's own text box, not the kit's shorter default button height", async () => {
    view({ kind: "agent" });
    expect((await screen.findByRole("combobox", { name: "Mode" })).className).toContain("h-[2.75rem]");
  });

  it("stays live once a session exists: switching it PATCHes the session, disabled only while a run is in progress", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "manual" }, { type: "token", text: "ok" }, { type: "done" }]);
    view({ kind: "agent" });
    await createSession("manual", "hello");
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    const combobox = await screen.findByRole("combobox", { name: "Mode" });
    expect(combobox).toHaveTextContent("Manual");
    expect(combobox).not.toBeDisabled();                               // idle once the run is done -- not locked by a session existing, only by being busy
    const user = userEvent.setup();
    await user.click(combobox);
    await user.click(await screen.findByRole("option", { name: "Auto" }));
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { mode: "auto" }));
    expect(await screen.findByRole("combobox", { name: "Mode" })).toHaveTextContent("Auto");
  });

  it("offers no model choice while the settings' own model is the only one, and no session leaves the choice unsaid", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" }, { type: "token", text: "ok" }, { type: "done" }]);
    view({ kind: "agent" });
    await screen.findByRole("combobox", { name: "Mode" });
    expect(screen.queryByRole("combobox", { name: "Model" })).toBeNull();
    await createSession("plan", "hello");
    await waitFor(() => expect(api.agentSessionCreate).toHaveBeenCalledWith("shop-aaaaaa", "plan", "", expect.objectContaining({ max_tokens: null })));   // the reply length is the model's own
  });

  it("lets the owner pick a provider's model for a session that is not saved yet, and sends it when the session is created", async () => {
    vi.mocked(api.agentModels).mockResolvedValue({ models: [OWN_MODEL, LAB_MODEL] });
    agentAnswerWith([{ type: "start", model: "qwen3-8b", model_label: "qwen3-8b", host: "192.168.1.20", local: false, mode: "plan" }, { type: "token", text: "ok" }, { type: "done" }]);
    view({ kind: "agent" });
    const user = userEvent.setup();
    const model = await screen.findByRole("combobox", { name: "Model" });
    expect(model).toHaveTextContent("qwen-7b");
    await user.click(model);
    expect(await screen.findByText("Lab server")).toBeInTheDocument();                          // grouped by the provider that serves it
    await user.click(await screen.findByRole("option", { name: "qwen3-8b" }));
    await createSession("plan", "hello");
    await waitFor(() => expect(api.agentSessionCreate).toHaveBeenCalledWith("shop-aaaaaa", "plan", "", expect.anything(), "lab/qwen3-8b"));
  });

  it("gives a session that exists another model through the PATCH route, and back to the settings' own with an empty name", async () => {
    vi.mocked(api.agentModels).mockResolvedValue({ models: [OWN_MODEL, LAB_MODEL] });
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" }, { type: "token", text: "ok" }, { type: "done" }]);
    view({ kind: "agent" });
    await createSession("plan", "hello");
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Model" }));
    await user.click(await screen.findByRole("option", { name: "qwen3-8b" }));
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { model_ref: "lab/qwen3-8b" }));
  });

  it("lets the owner choose how much the model thinks, for a session that exists (PATCH) and offers only the levels the model has", async () => {
    vi.mocked(api.agentModels).mockResolvedValue({ models: [OWN_MODEL, { ...LAB_MODEL, profile: { protocol: "native", source: "probed", family: "gpt-oss", reasoning: "effort", tool_format: "harmony", native_tools: true, max_output: 2048,
      context: 8192, notes: [], efforts: ["off", "low", "medium", "high"], thinks_itself: true } }] });
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" }, { type: "token", text: "ok" }, { type: "done" }]);
    view({ kind: "agent" });
    await createSession("plan", "hello");
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Thinking effort" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["Thinking: off", "Thinking: low", "Thinking: medium", "Thinking: high", "Thinking: max"]);          // the model of the settings: no profile yet, every level
    await user.click(screen.getByRole("option", { name: "Thinking: medium" }));
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { params: { effort: "medium" } }));
  });

  it("offers one Export button that opens a menu of the formats for the open session, and a Fork on an answer, which opens a new session from the conversation so far", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" }, { type: "token", text: "Looks fine." }, { type: "done" }]);
    vi.mocked(api.agentSessionFork).mockImplementation(async (_p, id) => { const child = structuredClone(agentDb[id]); child.id = "forked"; child.title = "Fork of a session"; agentDb.forked = child; return structuredClone(child); });
    view({ kind: "agent" });
    await createSession("plan", "Is it safe?");
    expect(await agentThread().findByText("Looks fine.")).toBeInTheDocument();
    const sessionId = Object.keys(agentDb).find((k) => k !== "forked")!;
    const user = userEvent.setup();
    expect(screen.queryByRole("button", { name: "Export JSON" })).toBeNull();
    expect(screen.queryByRole("link", { name: /Export/ })).toBeNull();                          // nothing is a link until the menu is open
    await user.click(screen.getByRole("button", { name: "Export" }));
    const entries = screen.getAllByRole("menuitem");
    expect(entries.map((e) => e.getAttribute("href"))).toEqual([`/export/shop-aaaaaa/${sessionId}?format=md`, `/export/shop-aaaaaa/${sessionId}?format=json`]);
    expect(entries.map((e) => e.textContent?.slice(0, 14))).toEqual(["Markdown (.md)", "JSON (.json)Th"]);
    expect(entries.every((e) => e.hasAttribute("download"))).toBe(true);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Fork a new session from here" }));
    await waitFor(() => expect(api.agentSessionFork).toHaveBeenCalledWith("shop-aaaaaa", sessionId, expect.stringMatching(/^a-/)));
    await waitFor(() => expect(api.agentSessionGet).toHaveBeenCalledWith("shop-aaaaaa", "forked"));
  });

  it("disables the mode control while a run is in progress", async () => {
    let resolveRun!: () => void;
    vi.mocked(api.agentSessionRun).mockImplementation(() => new Promise((resolve) => { resolveRun = () => resolve(); }));
    view({ kind: "agent" });
    await createSession("manual", "hello");
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    expect(await screen.findByRole("combobox", { name: "Mode" })).toBeDisabled();
    resolveRun();
  });

  it("shows the one-time check of how the model takes tools while it runs, and takes it away when it is over or a step arrives", async () => {
    let emit: (e: AgentEvent) => void = () => undefined;
    vi.mocked(api.agentSessionRun).mockImplementation((_p, _id, _b, _s, onEvent) => new Promise<void>(() => { emit = onEvent; }));
    view({ kind: "agent" });
    await createSession("plan", "look around");
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    const text = "Checking how this model takes tools (once, about half a minute)...";
    act(() => { emit({ type: "status", text }); });
    expect(await agentThread().findByText(text)).toBeInTheDocument();
    expect(agentThread().getByText(text)).toHaveAttribute("role", "status");
    act(() => { emit({ type: "status", text: "" }); });
    expect(agentThread().queryByText(text)).toBeNull();
    act(() => { emit({ type: "status", text }); });                                            // a clearing event that was lost: the first step takes it away
    expect(agentThread().getByText(text)).toBeInTheDocument();
    act(() => { emit({ type: "tool_start", id: "c1", name: "list_files", arguments: "{}" }); });
    expect(agentThread().queryByText(text)).toBeNull();
  });

  it("does not keep the check on the page when the run ends in an error", async () => {
    let emit: (e: AgentEvent) => void = () => undefined;
    vi.mocked(api.agentSessionRun).mockImplementation((_p, _id, _b, _s, onEvent) => new Promise<void>(() => { emit = onEvent; }));
    view({ kind: "agent" });
    await createSession("plan", "look around");
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    act(() => { emit({ type: "status", text: "Checking how this model takes tools (once, about half a minute)..." }); });
    act(() => { emit({ type: "error", message: "The model could not be reached." }); });
    expect(agentThread().queryByText(/Checking how this model/)).toBeNull();
    expect(await agentThread().findByText("The model could not be reached.")).toBeInTheDocument();
  });

  it("never shows a chat command menu for a leading slash", async () => {
    view({ kind: "agent" });
    const box = await screen.findByRole("textbox", { name: "Your question" });
    fireEvent.change(box, { target: { value: "/plan" } });
    expect(screen.queryByRole("listbox", { name: "Commands" })).toBeNull();
  });

  it("picking a mode and typing an instruction creates the session and sends it as the first message", async () => {
    agentAnswerWith([{ type: "token", text: "ok" }, { type: "done" }]);
    view({ kind: "agent" });
    await createSession("plan", "Plan how to fix the worst finding of the last scan.");
    await waitFor(() => expect(api.agentSessionCreate).toHaveBeenCalledWith("shop-aaaaaa", "plan", "",
      { autocompact: true, web_search: false, auto_approve: false, engine: "v2", temperature: 0.2, top_p: 0.9, max_tokens: null }));
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String),
      { instruction: "Plan how to fix the worst finding of the last scan.", allow_remote: false }, expect.anything(), expect.anything()));
  });

  it("the local-model loading dialog gates an agent send the same way it gates a chat ask", async () => {
    vi.mocked(api.servingStatusLive).mockResolvedValueOnce({ choices: { llm: { engine: "local", model: "qwen-7b" } }, slots: { llm: { state: "absent" } } } as any);
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: { slots: {} } } as any);
    agentAnswerWith([{ type: "token", text: "ok" }, { type: "done" }]);
    view({ kind: "agent" });
    await createSession("plan", "go");
    expect(await screen.findByRole("dialog", { name: "Loading the language model" })).toBeInTheDocument();
    expect(api.agentSessionRun).not.toHaveBeenCalled();
    vi.mocked(api.servingStatusLive).mockResolvedValue({ choices: { llm: { engine: "local", model: "qwen-7b" } }, slots: { llm: { state: "ready" } } } as any);
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled(), { timeout: 4000 });
  });
});

describe("agentic mode: settings", () => {
  it("shows the same settings a chat has, plus the mode's real caps and, in Manual, an auto-approve switch", async () => {
    agentDb.s1 = agentSession("s1", "manual", { instructions: "Be terse." });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByText("Instructions: Be terse.");                        // the status panel's own "Working on" section, not a toggled aside any more
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const panel = await screen.findByRole("dialog", { name: "This session's settings" });
    expect(within(panel).getByRole("spinbutton", { name: "Temperature" })).toBeInTheDocument();
    expect(within(panel).getByRole("spinbutton", { name: "Top p" })).toBeInTheDocument();
    expect(within(panel).getByRole("spinbutton", { name: "Longest answer" })).toBeInTheDocument();
    expect(within(panel).getByRole("switch", { name: "Automatic compaction" })).toBeInTheDocument();
    expect(within(panel).getByRole("switch", { name: "Search the web" })).toBeInTheDocument();
    expect(within(panel).getByText("Be terse.")).toBeInTheDocument();
    expect(await within(panel).findByText(/Manual: works in a sandboxed copy, up to 40 rounds/)).toBeInTheDocument();
    expect(within(panel).getByRole("switch", { name: "Auto-approve" })).toBeInTheDocument();
  });

  it("does not offer auto-approve in Plan or Auto mode", async () => {
    agentDb.s1 = agentSession("s1", "plan");
    view({ kind: "agent", initialId: "s1" });
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    const panel = await screen.findByRole("dialog", { name: "This session's settings" });
    expect(await within(panel).findByText(/Plan: read-only, up to 20 rounds/)).toBeInTheDocument();
    expect(within(panel).queryByRole("switch", { name: "Auto-approve" })).toBeNull();
  });

  it("saves a setting of the open session through the PATCH route", async () => {
    agentDb.s1 = agentSession("s1", "manual", { instructions: "Be terse." });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByText("Instructions: Be terse.");
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const panel = await screen.findByRole("dialog", { name: "This session's settings" });
    const temperature = within(panel).getByRole("spinbutton", { name: "Temperature" });
    fireEvent.change(temperature, { target: { value: "0.4" } });
    fireEvent.blur(temperature);
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", "s1", { params: { temperature: 0.4 } }));
  });

  it("turning on auto-approve saves it through the same PATCH route", async () => {
    agentDb.s1 = agentSession("s1", "manual", { instructions: "Be terse." });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByText("Instructions: Be terse.");
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const panel = await screen.findByRole("dialog", { name: "This session's settings" });
    const toggle = within(panel).getByRole("switch", { name: "Auto-approve" });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", "s1", { params: { auto_approve: true } }));
  });

  it("offers the engine beside Mode: a session with no saved choice reads as the old one, and a change is saved through the same PATCH route", async () => {
    agentDb.s1 = agentSession("s1", "auto", { instructions: "Be terse." });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByText("Instructions: Be terse.");
    const user = userEvent.setup();
    const engine = screen.getByRole("combobox", { name: "Engine" });
    expect(engine).toHaveTextContent("Engine: standard");
    await user.click(engine);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["Engine: durable run", "Engine: standard"]);
    await user.click(screen.getByRole("option", { name: "Engine: durable run" }));
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", "s1", { params: { engine: "v2" } }));
  });

  it("starts a new session on the durable run, says so in the empty state, and lets the owner pick the old engine before the session exists", async () => {
    view({ kind: "agent" });
    const engine = await screen.findByRole("combobox", { name: "Engine" });
    expect(engine).toHaveTextContent("Engine: durable run");
    expect(screen.getByText(/Durable run is on: each run keeps a record/)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(engine);
    await user.click(screen.getByRole("option", { name: "Engine: standard" }));
    expect(screen.getByRole("combobox", { name: "Engine" })).toHaveTextContent("Engine: standard");
    expect(screen.getByText(/Standard engine \(the one used so far\)/)).toBeInTheDocument();
    await createSession("manual", "go");
    await waitFor(() => expect(api.agentSessionCreate).toHaveBeenCalledWith("shop-aaaaaa", "manual", "", expect.objectContaining({ engine: "legacy" })));
  });

  it("keeps the settings of a session that is not saved yet, and gives them to it when it is created", async () => {
    view({ kind: "agent" });
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    const panel = await screen.findByRole("dialog", { name: "This session's settings" });
    const temperature = within(panel).getByRole("spinbutton", { name: "Temperature" });
    fireEvent.change(temperature, { target: { value: "0.6" } });
    fireEvent.blur(temperature);
    expect(api.agentSessionUpdate).not.toHaveBeenCalled();
    fireEvent.click(within(panel).getByRole("button", { name: "Close" }));
    await createSession("manual", "go");
    await waitFor(() => expect(api.agentSessionCreate).toHaveBeenCalledWith("shop-aaaaaa", "manual", "",
      { autocompact: true, web_search: false, auto_approve: false, engine: "v2", temperature: 0.6, top_p: 0.9, max_tokens: null }));
  });
});

describe("agentic mode: the dependency scan question", () => {
  const ask = async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" },
      { type: "pending_approval", call: { id: "c1", name: "rescan_dependencies", arguments: "{}" } }]);
    view({ kind: "agent" });
    await createSession("plan", "Which dependencies are outdated?");
    return await screen.findByRole("dialog", { name: "Scan the dependencies?" });
  };
  const resolveWith = () => vi.mocked(api.agentSessionApprove).mockImplementation(async (_p, id, body) => {
    const s = agentDb[id];
    s.transcript.push(agentMsg("t-dep", "tool", JSON.stringify({ summary: "Ran a scan" }), { tool_call_id: body.call_id, ok: body.decision === "approve", name: "rescan_dependencies", summary: "Ran a scan" }));
    s.pending_call = null; s.status = "idle";
    return { ok: true, summary: "x", session: structuredClone(s) } as any;
  });

  it("asks in a dialog of its own, not on the generic approval card", async () => {
    const dialog = await ask();
    expect(within(dialog).getByRole("button", { name: "Confirm" })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog", { name: "Approval needed" })).toBeNull();
  });

  it("confirms with a plain approval, and the run goes on", async () => {
    const dialog = await ask();
    resolveWith();
    vi.mocked(api.agentSessionRun).mockImplementation(async () => undefined);
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { call_id: "c1", decision: "approve" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Scan the dependencies?" })).toBeNull());
  });

  it("goes away as soon as it is answered, not when the run that follows has ended", async () => {
    // seen in the real window: the dialog stayed on the screen, its buttons off, over the steps of the run that went on after Confirm or Cancel
    const dialog = await ask();
    resolveWith();
    let finish: () => void = () => undefined;
    vi.mocked(api.agentSessionRun).mockImplementation(() => new Promise<undefined>((resolve) => { finish = () => resolve(undefined); }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Scan the dependencies?" })).toBeNull());
    expect(screen.queryByText("Waiting for your answer about the dependency scan.")).toBeNull();
    finish();
  });

  it("sends do not ask me again as remember always", async () => {
    const dialog = await ask();
    resolveWith();
    vi.mocked(api.agentSessionRun).mockImplementation(async () => undefined);
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Do not ask me again" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { call_id: "c1", decision: "approve", remember: "always" }));
  });

  it("denies on Cancel with no note", async () => {
    const dialog = await ask();
    resolveWith();
    vi.mocked(api.agentSessionRun).mockImplementation(async () => undefined);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { call_id: "c1", decision: "deny" }));
  });
});

describe("agentic mode: manual mode's pending approval", () => {
  it("shows the full arguments of the call, never a shortened preview, and lets the owner approve", async () => {
    const longArgs = JSON.stringify({ path: "new.py", content: "x = 1\n" + "# padding\n".repeat(50) });
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "manual" },
      { type: "pending_approval", call: { id: "c1", name: "write_file", arguments: longArgs } }]);
    view({ kind: "agent" });
    await createSession("manual", "Add new.py");
    const card = await screen.findByRole("alertdialog", { name: "Approval needed" });
    expect(within(card).getByText("Approval needed: write_file")).toBeInTheDocument();
    expect(within(card).getByText(longArgs)).toBeInTheDocument();                 // the whole thing, not cut to a preview length

    vi.mocked(api.agentSessionApprove).mockImplementation(async (_p, id, body) => {
      const s = agentDb[id];
      s.transcript.push(agentMsg("t-done", "tool", JSON.stringify({ ok: true }), { tool_call_id: body.call_id, ok: true, name: "write_file", summary: "Wrote new.py" }));
      s.pending_call = null; s.status = "idle";
      return { ok: true, summary: "Wrote new.py", session: structuredClone(s) };
    });
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "manual" }, { type: "token", text: "Added it." }, { type: "done" }]);
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { call_id: "c1", decision: "approve" }));
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalled());
    expect(await agentThread().findByText("Added it.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog", { name: "Approval needed" })).toBeNull();
  });

  it("a denied call is told to the model as a refusal and the session keeps going", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "manual" },
      { type: "pending_approval", call: { id: "c1", name: "run_command", arguments: "{\"command\":\"rm -rf /\"}" } }]);
    view({ kind: "agent" });
    await createSession("manual", "Clean up");
    const card = await screen.findByRole("alertdialog", { name: "Approval needed" });

    vi.mocked(api.agentSessionApprove).mockImplementation(async (_p, id, body) => {
      const s = agentDb[id];
      s.transcript.push(agentMsg("t-done", "tool", JSON.stringify({ error: "The owner denied this action." }), { tool_call_id: body.call_id, ok: false, name: "run_command", summary: "The owner denied this." }));
      s.pending_call = null; s.status = "idle";
      return { ok: false, summary: "The owner denied this.", session: structuredClone(s) };
    });
    agentAnswerWith([{ type: "done" }]);
    fireEvent.click(within(card).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", expect.any(String), { call_id: "c1", decision: "deny" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog", { name: "Approval needed" })).toBeNull());
  });
});

describe("agentic mode: plan review", () => {
  it("shows the plan-review card, never the write/run approval card, when propose_plan pauses the session", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" },
      { type: "pending_approval", call: { id: "c1", name: "propose_plan", arguments: JSON.stringify({ plan: "1. Read auth.py\n2. Add input validation" }) } }]);
    view({ kind: "agent" });
    await createSession("plan", "Is the login safe?");
    const card = await screen.findByRole("alertdialog", { name: "Implement this plan?" });
    expect(within(card).getByText(/1\. Read auth\.py/)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Accept - Manual" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Accept - Auto" })).toBeInTheDocument();
    expect(within(card).getByRole("textbox", { name: "Your question" })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog", { name: "Approval needed" })).toBeNull();
  });

  it("accepting switches this same session's mode in place and continues it, no new session", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" },
      { type: "pending_approval", call: { id: "c1", name: "propose_plan", arguments: JSON.stringify({ plan: "Add rate limiting to /login." }) } }]);
    view({ kind: "agent" });
    await createSession("plan", "Is the login safe?");
    const card = await screen.findByRole("alertdialog", { name: "Implement this plan?" });
    const planSessionId = Object.keys(agentDb).find((id) => agentDb[id].mode === "plan")!;

    vi.mocked(api.agentSessionApprove).mockImplementation(async (_p, id, body) => {
      const s = agentDb[id];
      s.transcript.push(agentMsg("t-done", "tool", JSON.stringify({ ok: true }), { tool_call_id: body.call_id, ok: true, name: "propose_plan", summary: "Proposed a plan for the owner's review." }));
      s.pending_call = null; s.status = "idle";
      return { ok: true, summary: "Proposed a plan for the owner's review.", session: structuredClone(s) };
    });
    agentAnswerWith([{ type: "token", text: "Starting." }, { type: "done" }]);
    fireEvent.click(within(card).getByRole("button", { name: "Accept - Manual" }));
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", planSessionId, { call_id: "c1", decision: "approve" }));
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", planSessionId, { mode: "manual" }));
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalledWith("shop-aaaaaa", planSessionId, { instruction: undefined, edit_of: undefined, allow_remote: false }, expect.anything(), expect.anything()));
    expect(api.agentSessionCreate).toHaveBeenCalledTimes(1);           // only the one session this test itself created through createSession -- accepting never forks a new one
    expect(await agentThread().findByText("Starting.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog", { name: "Implement this plan?" })).toBeNull();
    expect(await screen.findByRole("combobox", { name: "Mode" })).toHaveTextContent("Manual");
  });

  it("asking for changes denies with the owner's feedback and the same session continues, no new session", async () => {
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" },
      { type: "pending_approval", call: { id: "c1", name: "propose_plan", arguments: JSON.stringify({ plan: "Rewrite everything." }) } }]);
    view({ kind: "agent" });
    await createSession("plan", "Plan it");
    const card = await screen.findByRole("alertdialog", { name: "Implement this plan?" });
    const planSessionId = Object.keys(agentDb)[0];

    vi.mocked(api.agentSessionApprove).mockImplementation(async (_p, id, body) => {
      const s = agentDb[id];
      s.transcript.push(agentMsg("t-done", "tool", JSON.stringify({ error: "changes wanted" }), { tool_call_id: body.call_id, ok: false, name: "propose_plan", summary: "The owner asked for changes." }));
      s.pending_call = null; s.status = "idle";
      return { ok: false, summary: "The owner asked for changes.", session: structuredClone(s) };
    });
    agentAnswerWith([{ type: "token", text: "A narrower plan." }, { type: "pending_approval", call: { id: "c2", name: "propose_plan", arguments: JSON.stringify({ plan: "Narrower." }) } }]);
    fireEvent.change(within(card).getByRole("textbox", { name: "Your question" }), { target: { value: "Too broad, scope it to login only." } });
    fireEvent.click(within(card).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", planSessionId, { call_id: "c1", decision: "deny", feedback: "Too broad, scope it to login only." }));
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalledWith("shop-aaaaaa", planSessionId, expect.anything(), expect.anything(), expect.anything()));
    expect(api.agentSessionCreate).toHaveBeenCalledTimes(1);          // only the one session this test itself created -- no second session from asking for changes
  });
});

describe("agentic mode: ask_question", () => {
  it("shows a suggested-options card for each question and submits the owner's answers as edited_arguments", async () => {
    const questions = [{ question: "Which root looks riskiest?", options: ["backend", "frontend"] }];
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" },
      { type: "pending_approval", call: { id: "c1", name: "ask_question", arguments: JSON.stringify({ questions }) } }]);
    view({ kind: "agent" });
    await createSession("plan", "Where should I focus?");
    const card = await screen.findByRole("alertdialog", { name: "A question for your review" });
    expect(within(card).getByText("Which root looks riskiest?")).toBeInTheDocument();
    const submit = within(card).getByRole("button", { name: "Submit answer" });
    expect(submit).toBeDisabled();                                   // nothing picked yet

    vi.mocked(api.agentSessionApprove).mockImplementation(async (_p, id, body) => {
      const s = agentDb[id];
      s.transcript.push(agentMsg("t-done", "tool", JSON.stringify({ answers: ["backend"] }), { tool_call_id: body.call_id, ok: true, name: "ask_question", summary: "Answered 1 of 1 question(s)." }));
      s.pending_call = null; s.status = "idle";
      return { ok: true, summary: "Answered 1 of 1 question(s).", session: structuredClone(s) };
    });
    agentAnswerWith([{ type: "token", text: "Focusing on backend." }, { type: "done" }]);
    fireEvent.click(within(card).getByRole("button", { name: "backend" }));
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);
    const planSessionId = Object.keys(agentDb)[0];
    await waitFor(() => expect(api.agentSessionApprove).toHaveBeenCalledWith("shop-aaaaaa", planSessionId,
      { call_id: "c1", decision: "approve", edited_arguments: { questions, answers: ["backend"] } }));
    expect(await agentThread().findByText("Focusing on backend.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog", { name: "A question for your review" })).toBeNull();
  });

  it("lets a free-text answer stand in for any suggested option", async () => {
    const questions = [{ question: "Anything else to check?" }];
    agentAnswerWith([{ type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" },
      { type: "pending_approval", call: { id: "c1", name: "ask_question", arguments: JSON.stringify({ questions }) } }]);
    view({ kind: "agent" });
    await createSession("plan", "Go");
    const card = await screen.findByRole("alertdialog", { name: "A question for your review" });
    fireEvent.click(within(card).getByRole("button", { name: "Type my own answer" }));
    fireEvent.change(within(card).getByRole("textbox", { name: "Your answer to: Anything else to check?" }), { target: { value: "Also check the admin routes." } });
    expect(within(card).getByRole("button", { name: "Submit answer" })).not.toBeDisabled();
  });
});

describe("agentic mode: editing the last instruction", () => {
  it("offers Edit only on the last instruction, never an earlier one", async () => {
    agentDb.s1 = agentSession("s1", "manual", { transcript: [
      agentMsg("u1", "user", "first instruction"), agentMsg("a1", "assistant", "first answer"),
      agentMsg("u2", "user", "second instruction"), agentMsg("a2", "assistant", "second answer"),
    ] });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByRole("list", { name: "The session" });
    await agentThread().findByText("second instruction");
    const edits = agentThread().getAllByRole("button", { name: "Edit and send again" });
    expect(edits).toHaveLength(1);                                   // only the last one offers it, not "first instruction" too
    fireEvent.click(edits[0]);
    expect(screen.getByRole("textbox", { name: "Your question, edited" })).toHaveValue("second instruction");
  });

  it("sends the edited instruction with edit_of, and the discarded round disappears at once", async () => {
    agentDb.s1 = agentSession("s1", "manual", { transcript: [agentMsg("u1", "user", "write new.py"), agentMsg("a1", "assistant", "wrote it")] });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByRole("list", { name: "The session" });
    await agentThread().findByText("write new.py");
    vi.mocked(api.agentSessionRun).mockImplementation(() => new Promise<void>(() => undefined));          // stall, inspect the optimistic view before it resolves
    fireEvent.click(agentThread().getByRole("button", { name: "Edit and send again" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Your question, edited" }), { target: { value: "write new.py but log instead" } });
    fireEvent.click(screen.getByRole("button", { name: "Send again" }));
    await waitFor(() => expect(api.agentSessionRun).toHaveBeenCalledWith("shop-aaaaaa", "s1",
      { instruction: "write new.py but log instead", edit_of: "u1", allow_remote: false }, expect.anything(), expect.anything()));
    expect(agentThread().getByText("write new.py but log instead")).toBeInTheDocument();
    expect(agentThread().queryByText("write new.py")).toBeNull();
    expect(agentThread().queryByText("wrote it")).toBeNull();
  });
});

describe("agentic mode: the session list", () => {
  it("shows a session with a pending call as waiting for approval, in the shared sidebar", async () => {
    agentDb.s1 = agentSession("s1", "manual", { status: "waiting_approval", pending_call: { id: "c1", name: "write_file", arguments: "{}", asked_at: "" } });
    view({ kind: "agent" });
    expect(await screen.findByText("Waiting for approval")).toBeInTheDocument();
  });

  it("can be renamed and deleted through the shared sidebar, like a chat", async () => {
    agentDb.s1 = agentSession("s1", "plan");
    view({ kind: "agent" });
    const nav = within(await screen.findByRole("navigation", { name: "Agent sessions" }));
    await nav.findByRole("button", { name: /^Plan/ });
    fireEvent.click(nav.getByRole("button", { name: /^Rename/ }));
    const box = nav.getByRole("textbox", { name: /^New name of/ });
    fireEvent.change(box, { target: { value: "Login review" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(api.agentSessionUpdate).toHaveBeenCalledWith("shop-aaaaaa", "s1", { title: "Login review" }));
    expect(await nav.findByRole("button", { name: /^Login review/ })).toBeInTheDocument();
    fireEvent.click(nav.getByRole("button", { name: /^Delete/ }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete it" }));
    await waitFor(() => expect(api.agentSessionDelete).toHaveBeenCalledWith("shop-aaaaaa", "s1"));
  });

  it("falls back to a blank draft, not the next session in the list, when the open one is deleted", async () => {
    agentDb.s1 = agentSession("s1", "plan", { transcript: [agentMsg("m1", "user", "hi")] });
    agentDb.s2 = agentSession("s2", "manual", { updated_at: "2026-10-01T09:00:00.000Z" });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByRole("list", { name: "The session" });
    expect(agentThread().getByText("hi")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete Plan" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete it" }));
    expect(await screen.findByText("No target locked")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "The session" })).toBeNull();
  });
});

describe("agentic mode: what the agent did, step by step", () => {
  const start: AgentEvent = { type: "start", model: "qwen-7b", model_label: "qwen-7b", host: "127.0.0.1", local: true, mode: "plan" };
  const stepsOf = async () => { await screen.findByRole("list", { name: "The session" }); return agentThread().findByRole("list", { name: "What the agent did" }); };
  const call = (id: string, name: string, args = "{}") => ({ id, type: "function" as const, function: { name, arguments: args } });
  /** A run that streams these events and saves the same work to the transcript, as the server does: the words before a call with the call, each result, the answer. */
  const runWith = (events: AgentEvent[], saved: (s: AgentSession) => void) => vi.mocked(api.agentSessionRun).mockImplementation(async (_p, id, body, _s, onEvent) => {
    for (const e of events) onEvent(e);
    const s = agentDb[id];
    if (body.instruction) s.transcript.push(agentMsg(`u-${s.transcript.length}`, "user", body.instruction));
    saved(s);
    s.status = "done";
  });

  it("shows the words said before a call, the call with its result to open, and the answer last, while it runs and after it is saved", async () => {
    const listing = JSON.stringify({ entries: [{ name: "app.py", type: "file" }] });
    runWith([start, { type: "narration", text: "I will list the project." }, { type: "tool_start", id: "c1", name: "list_files", arguments: "{}" },
      { type: "tool", id: "c1", name: "list_files", arguments: "{}", ok: true, summary: "Listed the top of the project", result: listing }, { type: "token", text: "It has one file." }, { type: "done" }],
    (s) => s.transcript.push(agentMsg("a1", "assistant", "I will list the project.", { tool_calls: [call("c1", "list_files")] }),
      agentMsg("t1", "tool", listing, { tool_call_id: "c1", ok: true, name: "list_files", summary: "Listed the top of the project" }), agentMsg("a2", "assistant", "It has one file.")));
    view({ kind: "agent" });
    await createSession("plan", "What is here?");
    const steps = await stepsOf();
    expect(within(steps).getByText("I will list the project.")).toBeInTheDocument();
    expect(within(steps).getByText("List files")).toBeInTheDocument();
    expect(within(steps).getByText("It has one file.")).toBeInTheDocument();
    expect(within(steps).getByText("app.py")).toBeInTheDocument();                    // the result, inside the call's own closed details
    const text = steps.textContent ?? "";
    expect(text.indexOf("I will list the project.")).toBeLessThan(text.indexOf("List files"));
    expect(text.indexOf("List files")).toBeLessThan(text.indexOf("It has one file."));
  });

  it("draws a call that is waiting for the owner as a step marked so, under the words that came before it", async () => {
    agentDb.s1 = agentSession("s1", "manual", { status: "waiting_approval", transcript: [agentMsg("u1", "user", "Fix it"),
      agentMsg("a1", "assistant", "I will fix the typo.", { tool_calls: [call("c1", "edit_file", JSON.stringify({ path: "app.py", old: "x = 1", new: "x = 2" }))] })],
    pending_call: { id: "c1", name: "edit_file", arguments: JSON.stringify({ path: "app.py", old: "x = 1", new: "x = 2" }), asked_at: "" } });
    view({ kind: "agent", initialId: "s1" });
    const steps = await stepsOf();
    expect(within(steps).getByText("I will fix the typo.")).toBeInTheDocument();
    expect(within(steps).getByText("Edit")).toBeInTheDocument();
    expect(within(steps).getByText("Waiting for you")).toBeInTheDocument();
  });

  it("shows the task list the agent keeps as a checklist in the stream", async () => {
    const tasks = JSON.stringify({ tasks: [{ text: "Read app.py", done: true }, { text: "Write the plan", done: false }] });
    agentDb.s1 = agentSession("s1", "plan", { transcript: [agentMsg("u1", "user", "Plan"), agentMsg("a1", "assistant", "", { tool_calls: [call("c1", "update_task_list", tasks)] }),
      agentMsg("t1", "tool", "{}", { tool_call_id: "c1", ok: true, name: "update_task_list", summary: "Updated the task list" }), agentMsg("a2", "assistant", "Done.")] });
    view({ kind: "agent", initialId: "s1" });
    const steps = await stepsOf();
    const checklist = within(steps).getByRole("list", { name: "Task list" });
    expect(within(checklist).getByText("Read app.py")).toBeInTheDocument();
    expect(within(checklist).getByText("Done:", { exact: false })).toBeInTheDocument();
    expect(within(checklist).getByText("Write the plan")).toBeInTheDocument();
  });

  it("never draws a decision object as the answer, whatever an old file holds", async () => {
    agentDb.s1 = agentSession("s1", "plan", { transcript: [agentMsg("u1", "user", "q"), agentMsg("a1", "assistant", JSON.stringify({ thought: "Enough.", action: { kind: "answer", text: "All fine." } }))] });
    view({ kind: "agent", initialId: "s1" });
    await screen.findByRole("list", { name: "The session" });
    expect(await agentThread().findByText("All fine.")).toBeInTheDocument();
    expect(screen.queryByText(/"thought"/)).toBeNull();
    expect(screen.queryByText(/"action"/)).toBeNull();
  });

  it("starts a new session in the mode that was used last, and still lets another be picked", async () => {
    agentDb.recent = agentSession("recent", "auto", { updated_at: "2026-10-15T12:00:00.000Z" });
    agentDb.older = agentSession("older", "plan", { updated_at: "2026-10-14T12:00:00.000Z" });
    view({ kind: "agent", startEmpty: true });
    const combobox = await screen.findByRole("combobox", { name: "Mode" });
    await waitFor(() => expect(combobox).toHaveTextContent("Auto"));
    const user = userEvent.setup();
    await user.click(combobox);
    await user.click(await screen.findByRole("option", { name: "Manual" }));
    expect(await screen.findByRole("combobox", { name: "Mode" })).toHaveTextContent("Manual");
  });
});
