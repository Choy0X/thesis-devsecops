import { ChevronsDownUp, ChevronsUpDown, MessagesSquare, SlidersHorizontal } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, errorText, type Effort, type AgentCapabilities, type AgentModel, type AgentDiffFile, type AgentEvent, type AgentMode, type AgentSession, type AgentSessionParams, type AgentSessionSummary,
  type ChatEndpoint, type ChatEvent, type ChatParams, type ChatSummary, type ChatTurnBody, type Project, type StoredChat } from "../../lib/api";
import { parseCommand } from "../../lib/commands";
import { useConfirm } from "../hud/AlertDialog";
import { Dialog, Sheet } from "../hud/Dialog";
import { Menu, Select, Switch } from "../kit";
import { LoadModelDialog } from "../LoadModelDialog";
import { Button, Skeleton } from "../ui";
import { AgentEmptyState } from "./AgentEmptyState";
import { AgentStatusPanel } from "./AgentStatusPanel";
import { ApprovalCard, type Remember } from "./ApprovalCard";
import { ChildSource, CollapseOutputs, OutputSource, SourcesContext, type LoadOutput } from "./AgentActivity";
import { DependencyScanDialog } from "./DependencyScanDialog";
import { reduceEvent, sourcesOf, stepsOfSession, turnsOfSession } from "./agentSteps";
import { AskQuestionCard } from "./AskQuestionCard";
import { Composer } from "./Composer";
import { runCommand, type CommandContext } from "./commands";
import { ConversationList } from "./ConversationList";
import { MessageView, type ViewMessage } from "./MessageView";
import { approxTokens, DEFAULT_NUMBER_PARAMS, ParamsPanel } from "./ParamsPanel";
import { useActive } from '../ActivePage';
import { PlanReviewCard } from "./PlanReviewCard";

/** The files a session can be saved as. Another format is one more entry here, and one more case in agentic/export.py. */
const EXPORT_FORMATS = [
  { format: "md", label: "Markdown (.md)", description: "Readable, masked for credentials" },
  { format: "json", label: "JSON (.json)", description: "The whole session as data, masked for credentials" },
] as const;

// A project's conversation, of either kind: a normal chat (kind="chat", read-only tools) or an agent session (kind="agent", a sandboxed copy it can write to and run commands
// in, a mode chosen once and locked). One component, one page, the same sidebar, the same settings panel, the same message-bubble rendering, the same composer, for both:
// switching between them (ProjectConversationFocus.tsx mounts this component twice, once per kind) only changes which extra capabilities are unlocked -- a mode dropdown beside
// Send, tool-call chips, a task list, agent-only settings, a pending-approval card in place of the composer -- never the shape of the page itself.
// Conversations are kept on this computer (the server saves the question before it asks and the answer when it ends, fails or is stopped; an agent session the same way, one round
// at a time), so the page shows what the server says is saved: after every turn it reads the conversation again. The assistant reads the project with read-only tools in Plan mode
// and cannot change anything in either Plan or Normal Chat; Manual and Auto write only to a sandboxed copy, never the real project (see pipeline/agent_apply.py on the backend).

const DRAFT_PARAMS: ChatParams = { system: "", autocompact: true, web_search: false, ...DEFAULT_NUMBER_PARAMS };
const DRAFT_AGENT_PARAMS: AgentSessionParams = { autocompact: true, web_search: false, auto_approve: false, engine: "v2", ...DEFAULT_NUMBER_PARAMS, max_tokens: null };      // how much a reply may be is the model's own unless the owner says; a new session starts on the newer engine (the server does the same when nothing is sent)
const ENGINE_OPTIONS: { value: "v2" | "legacy"; label: string }[] = [{ value: "v2", label: "Engine: durable run" }, { value: "legacy", label: "Engine: standard" }];
const PENDING_QUESTION = "pending-question";
const PENDING_ANSWER = "pending-answer";

const MODES = [
  { value: "plan", label: "Plan", description: "Reads the real project, read-only. Proposes a plan for your review, never a direct change." },
  { value: "manual", label: "Manual", description: "Works in a sandboxed copy of the project. Pauses here for your approval before writing a file or running a command." },
  { value: "auto", label: "Auto", description: "Works in a sandboxed copy without pausing for approval, capped at 60 rounds or 30 minutes. Nothing it does reaches the real project." },
] as const;
const MODE_OPTIONS = MODES.map((m) => ({ value: m.value, label: m.label, hint: m.description }));
const MODE_LABEL: Record<AgentMode, string> = { plan: "Plan", manual: "Manual", auto: "Auto" };

/** An agent session has no title or preview the way a chat does -- only a mode, a status, how many rounds, and whether it is waiting on a person -- synthesized here into the same
 * shape so the one ConversationList component needs no changes of its own to show both kinds side by side. */
function toChatSummary(s: AgentSessionSummary): ChatSummary {
  return { id: s.id, created_at: s.created_at, updated_at: s.updated_at, message_count: s.message_count, title: s.title || MODE_LABEL[s.mode],
    preview: s.pending_call ? "Waiting for approval" : `${s.status}, ${s.rounds} round${s.rounds === 1 ? "" : "s"}` };
}

/** The plan text out of a propose_plan pending call's own JSON arguments ({"plan": "..."}); an empty string, never an error, when the call somehow carries something else. */
function planTextOf(call: { arguments: string }): string {
  try { return String(JSON.parse(call.arguments)?.plan ?? ""); } catch { return ""; }
}

/** The questions out of an ask_question pending call's own JSON arguments ({"questions": [{"question", "options"?}, ...]}); empty, never an error, when malformed. */
function questionsOf(call: { arguments: string }): { question: string; options?: string[] }[] {
  try {
    const qs = JSON.parse(call.arguments)?.questions;
    return Array.isArray(qs) ? qs.map((q) => ({ question: String(q?.question ?? ""), options: Array.isArray(q?.options) ? q.options.map(String) : undefined })) : [];
  } catch { return []; }
}

export function ChatWorkspace({ project, kind, initialId, startEmpty, onIdChange }: {
  project: Project;
  /** Fixed for the component's whole life -- a conversation's mode (which kind it even is) is locked the moment it is created, same as the backend. */
  kind: "chat" | "agent";
  initialId?: string | null;
  /** Skip the usual fallback to the most recently updated conversation when no id is named: used the first time a tab that is not the page's own entry point is shown (always, for
   *  kind="agent" -- an agent session is deliberately never auto-opened), so it opens on an empty draft rather than someone's unrelated conversation. */
  startEmpty?: boolean;
  onIdChange?: (id: string | null) => void;
}) {
  const confirm = useConfirm();
  const [endpoint, setEndpoint] = useState<ChatEndpoint | null>(null);
  const [context, setContext] = useState<number | null>(null);
  const [chats, setChats] = useState<ChatSummary[] | null>(null);           // for kind="agent", agent sessions synthesized into this same shape (toChatSummary)
  const [damaged, setDamaged] = useState(0);
  const [chat, setChat] = useState<StoredChat | null>(null);                  // kind="chat" only: the open conversation as the server has it; null is a new one that is not saved yet
  const [session, setSession] = useState<AgentSession | null>(null);        // kind="agent" only, the same way
  const [mode, setMode] = useState<AgentMode>("plan");                      // kind="agent" only: the mode chosen for a session that does not exist yet; frozen once it does
  const [messages, setMessages] = useState<ViewMessage[]>([]);                // what is drawn, for either kind: the saved messages/transcript and the one being written
  const [draftParams, setDraftParams] = useState<ChatParams>(DRAFT_PARAMS);       // kind="chat" only: the settings of a conversation that is not saved yet
  const [draftInstructions, setDraftInstructions] = useState("");               // kind="agent" only: the instructions of a session that does not exist yet
  const [draftAgentParams, setDraftAgentParams] = useState<AgentSessionParams>(DRAFT_AGENT_PARAMS);   // kind="agent" only, the same way as draftParams
  const [agentCaps, setAgentCaps] = useState<AgentCapabilities | null>(null);    // kind="agent" only: each mode's real round/time caps and tools, for the settings panel
  const [diff, setDiff] = useState<AgentDiffFile[] | null>(null);              // kind="agent" only: the project's agent sandbox diff, for the status panel's "Changes" section
  const webSources = useMemo(() => (kind === "agent" ? sourcesOf(messages) : null), [kind, messages]);        // the pages of the session, for the [n] of an answer
  /** The steps a subagent took, read from its own session when the thread is shown again (while it works they come with the run). */
  const loadChild = useCallback(async (id: string) => stepsOfSession(await api.agentSessionGet(project.id, id)), [project.id]);
  const [collapseOutputs, setCollapseOutputs] = useState(false);               // kind="agent" only: every call folded to its header line
  const sessionId = session?.id;
  const loadOutput = useMemo<LoadOutput | null>(() => (kind === "agent" && sessionId ? (handle, start, lines) => api.agentSessionOutput(project.id, sessionId, handle, start, lines) : null), [kind, sessionId, project.id]);
  const [settingsOpen, setSettingsOpen] = useState(false);                     // kind="agent" only: Settings is a dialog here, never an inline panel
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [paramsError, setParamsError] = useState("");
  const [showParams, setShowParams] = useState(true);
  const [convoSheetOpen, setConvoSheetOpen] = useState(false);              // the conversation list and (kind="chat" only) the settings panel, as drawers below the `rail` breakpoint
  const [paramsSheetOpen, setParamsSheetOpen] = useState(false);
  const [awaitingModel, setAwaitingModel] = useState(false);                  // kind="chat" only: the local model is not ready yet: the composer is disabled and a progress dialog is shown until it is
  const allowed = useRef(false);                                             // the person confirmed sending excerpts to a model that is not on this computer, while this panel is open
  const abort = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const thread = useRef<HTMLUListElement>(null);
  const stick = useRef(true);                                                // the thread follows the new words only while the person has not scrolled up
  const pendingSend = useRef<(() => void) | null>(null);                     // the turn that is waiting for the local model to finish loading
  const [models, setModels] = useState<AgentModel[]>([]);                  // kind="agent" only: every model a session can be given (the settings' own first)
  const [draftModel, setDraftModel] = useState("");                          // the model chosen for the session that is not saved yet ("" is the settings' own)
  const modeChosen = useRef(false);                                          // kind="agent" only: the mode of the draft session was picked on this page, so the last session's no longer sets it
  const cmdSeq = useRef(0);                                                  // tells apart the local, never-saved replies a slash command pushes into the thread
  const pageActive = useActive();
  const gpuView = useRef(`chat-view:${crypto.randomUUID()}`);
  useEffect(() => {
    if (!endpoint?.local) return;
    const owner = gpuView.current;
    const update = (active: boolean) => api.chatGpuView(owner, active).catch(() => undefined); // ignored: the next heartbeat retries registration, and the server expires a missed release after its TTL
    update(pageActive);
    const timer = pageActive ? setInterval(() => update(true), 15000) : undefined;
    return () => {if (timer) clearInterval(timer); update(false);};
  }, [pageActive, endpoint?.local]);
  useEffect(() => () => { alive.current = false; abort.current?.abort(); }, []);
  useEffect(() => { if (kind === "agent") api.agentModels().then((r) => { if (alive.current) setModels(r.models); }).catch(() => { if (alive.current) setModels([]); }); }, [kind]);      // ignored: without the list there is only the settings' own model, and no choice to show

  /** Runs ``fn`` now, unless the chosen model is local and not ready yet: then the composer disables, a progress dialog loads it, and ``fn`` runs once it turns ready. kind="chat"
   *  only -- an agent run that hits an unready local model simply reports the resulting error, the same gap that already existed before this component had one kind. */
  const runWhenReady = async (fn: () => void): Promise<void> => {
    try {
      const live = await api.servingStatusLive();
      if (live.choices.llm.engine === "local" && live.slots.llm?.state !== "ready") {
        pendingSend.current = fn;
        setAwaitingModel(true);
        return;
      }
    } catch {
      // ignored: the status could not be read; the turn itself reports the problem if the model truly is not reachable
    }
    fn();
  };

  const loadList = useCallback(async (): Promise<ChatSummary[]> => {
    try {
      if (kind === "chat") {
        const r = await api.projectChats(project.id);
        if (alive.current) { setChats(r.chats); setDamaged(r.damaged); }
        return r.chats;
      }
      const r = await api.agentSessions(project.id);
      const mapped = r.sessions.map(toChatSummary);
      if (alive.current) {
        setChats(mapped); setDamaged(r.damaged);
        if (!modeChosen.current && r.sessions[0]) setMode(r.sessions[0].mode);            // a new session starts in the mode used last (the newest session's), as Claude Code does, until one is picked here
      }
      return mapped;
    } catch (e) {
      if (alive.current) { setError(errorText(e)); setChats((c) => c ?? []); }
      return [];
    }
  }, [project.id, kind]);
  const open = useCallback(async (id: string | null) => {
    setError(""); setParamsError("");
    if (id === null) { if (kind === "chat") setChat(null); else setSession(null); setMessages([]); return; }
    try {
      if (kind === "chat") {
        const c = await api.projectChatGet(project.id, id);
        if (!alive.current) return;
        setChat(c); setMessages(c.messages);
      } else {
        const s = await api.agentSessionGet(project.id, id);
        if (!alive.current) return;
        setSession(s); setMessages(turnsOfSession(s));
      }
    } catch (e) { if (alive.current) setError(errorText(e)); }
  }, [project.id, kind]);

  useEffect(() => {
    api.projectChatEndpoint(project.id).then((e) => alive.current && setEndpoint(e)).catch((e) => alive.current && setError(errorText(e)));
    api.servingStatus().then((s) => alive.current && setContext(s.slots.llm?.state === "ready" && s.slots.llm.engine?.context ? s.slots.llm.engine.context : null)).catch(() => undefined);       // ignored: the meter then says the size is not known
    if (kind === "agent") api.agentCapabilities(project.id).then((c) => alive.current && setAgentCaps(c)).catch(() => undefined);       // ignored: the panel then says it is still looking
    void loadList().then((all) => {
      const preferred = initialId && all.some((c) => c.id === initialId) ? initialId : (startEmpty ? undefined : all[0]?.id);
      if (preferred) { void open(preferred); onIdChange?.(preferred); }
    });
  }, [project.id]);                                                          // eslint-disable-line react-hooks/exhaustive-deps; ignored: the first look at this project's conversations
  useEffect(() => { const el = thread.current; if (el && stick.current) el.scrollTop = el.scrollHeight; }, [messages]);
  /** The status panel's "Changes" section: refetched once a run ends, so it reflects what the latest instruction actually did. kind="agent" only; Plan mode never writes anything,
   *  but it is still fetched there too (cheap, and the panel itself decides whether to show the section) rather than special-cased here. */
  const loadDiff = () => { api.agentDiff(project.id).then((d) => alive.current && setDiff(d.files)).catch(() => undefined); };       // ignored: the panel just omits the section if this fails
  useEffect(() => {
    if (kind !== "agent" || !session || busy) return;
    loadDiff();
  }, [kind, project.id, session?.id, busy]);                                 // eslint-disable-line react-hooks/exhaustive-deps; ignored: loadDiff only reads project.id

  const patchLive = (fn: (m: ViewMessage) => ViewMessage) => { if (alive.current) setMessages((all) => all.map((m) => (m.id === PENDING_ANSWER ? fn(m) : m))); };
  /** A slash command's own reply (kind="chat" only): a local message that reads like the rest of the transcript but is never sent to the store (see MessageView's ``command`` field). */
  const appendResult = (text: string, name: string): string => {
    const id = `cmd-${name}-${++cmdSeq.current}`;
    setMessages((all) => [...all, { id, role: "assistant", content: text, created_at: "", command: { name, ok: true } }]);
    return id;
  };
  const updateResult = (id: string, patch: Partial<ViewMessage>) => setMessages((all) => all.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  const clearMessages = () => setMessages([]);
  const openConversationList = () => setConvoSheetOpen(true);
  /** Events of either kind, patched onto the one live, streaming ``PENDING_ANSWER`` placeholder both kinds use. Shared-shape events (token/thinking/tool/notice) need one branch
   *  each; chat-only (retrieved/retrieved_web/saved) and agent-only (pending_approval) variants narrow by their own tag automatically and need no ``kind`` check. ``pending_approval``
   *  needs no patch at all: it is always the stream's last event, and the session read again once the run ends carries ``pending_call``, the persisted source of truth the approval
   *  card reads from. */
  const onEvent = (e: ChatEvent | AgentEvent) => {
    if (kind === "agent" && (e.type === "token" || e.type === "thinking" || e.type === "narration" || e.type === "narration_drop" || e.type === "tool_start" || e.type === "tool_output" || e.type === "tool" || e.type === "sub")) patchLive((m) => ({ ...m, status: undefined, steps: reduceEvent(m.steps ?? [], e as AgentEvent) }));       // a step has arrived: whatever the run was busy with is over
    else if (e.type === "narration" || e.type === "narration_drop" || e.type === "tool_start" || e.type === "tool_output") { /* agent-only events: a chat never gets them */ }
    else if (e.type === "token") patchLive((m) => ({ ...m, content: m.content + e.text }));
    else if (e.type === "thinking") patchLive((m) => ({ ...m, thinking: (m.thinking ?? "") + e.text }));
    else if (e.type === "tool") patchLive((m) => ({ ...m, tools: [...(m.tools ?? []), { ok: e.ok, summary: e.summary }] }));
    else if (e.type === "notice") patchLive((m) => ({ ...m, notices: [...(m.notices ?? []), e.text] }));
    else if (e.type === "status") patchLive((m) => ({ ...m, status: e.text || undefined }));
    else if (e.type === "attached") setMessages((all) => { const at = all.map((m) => m.role).lastIndexOf("user"); return at < 0 ? all : all.map((m, i) => (i === at ? { ...m, attachments: e.files } : m)); });
    else if (e.type === "retrieved") patchLive((m) => ({ ...m, tools: [...(m.tools ?? []), { ok: true, summary: `Searched the project index: ${e.items.map((i) => `${i.file}:${i.line}`).join(", ")}` }] }));
    else if (e.type === "retrieved_web") patchLive((m) => ({ ...m, tools: [...(m.tools ?? []), { ok: true, summary: `Searched the web: ${e.items.map((i) => i.domain).join(", ")}` }] }));
    else if (e.type === "start") patchLive((m) => ({ ...m, model: e.model, model_label: e.model_label }));
    else if (e.type === "pending_approval") { /* no-op, see docstring above */ }
    else if (e.type === "error") patchLive((m) => ({ ...m, error: e.message, streaming: false, status: undefined }));
    else if (e.type === "done") patchLive((m) => ({
      ...m, stopped: e.stopped || undefined, streaming: false, status: undefined,
      ...("sources" in e ? { sources: e.sources, usage: e.usage, model: e.model || m.model, model_label: e.model_label || m.model_label, trimmed: e.trimmed || undefined, compacted: e.compacted || undefined } : {}),
    }));
  };

  /** One chat turn: the question is added at once, the answer is written as it arrives, and when it is over the conversation is read again from the server. */
  const turn = async (chatId: string, body: Omit<ChatTurnBody, "allow_remote">, shown: ViewMessage[]) => {
    setMessages([...shown, { id: PENDING_ANSWER, role: "assistant", content: "", created_at: "", streaming: true }]);
    stick.current = true;
    setBusy(true);
    const controller = new AbortController();
    abort.current = controller;
    let failure = "";
    try {
      await api.projectChatSend(project.id, chatId, { ...body, allow_remote: allowed.current }, controller.signal, onEvent);
    } catch (e) {
      if (e instanceof ApiError && e.code === "remote_not_confirmed") allowed.current = false;
      failure = e instanceof ApiError ? e.detail : errorText(e);                 // a refusal's detail says what to do; its title only names it
    } finally { abort.current = null; }
    if (!alive.current) return;
    if (failure) patchLive((m) => ({ ...m, error: failure, streaming: false }));
    else patchLive((m) => (m.streaming ? { ...m, streaming: false } : m));
    const [saved] = await Promise.all([api.projectChatGet(project.id, chatId).catch(() => null), loadList()]);       // ignored: the page keeps what it drew when the saved copy cannot be read
    if (!alive.current) return;
    setBusy(false);
    if (saved && !failure) { setChat(saved); setMessages(saved.messages); }
    else if (saved) setChat(saved);
  };

  /** One run of the current agent session: a fresh instruction (shown at once, the same way a chat turn's question is), or none to continue after an approve/deny. ``editOf``
   *  undoes the transcript's own last instruction (and, in Manual/Auto, anything it wrote or ran in the sandbox) before ``instruction`` replaces it -- the messages shown locally
   *  drop everything from that point on too, so the page does not flash the discarded round before the server confirms it is gone. The session is read again from the server
   *  once it ends, so the page shows exactly what was saved -- the same refetch-and-replace shape ``turn`` above already uses. Takes the session id explicitly rather than
   *  reading ``session`` from state, since a session just created in the same action (``send`` below) would otherwise still see the pre-update, null ``session``. */
  const run = async (sessionId: string, instruction?: string, editOf?: string) => {
    if (busy) return;
    if (!(await confirmRemote())) return;
    const editAt = editOf ? messages.findIndex((m) => m.id === editOf) : -1;
    const base = editAt >= 0 ? messages.slice(0, editAt) : messages;
    const shown = instruction ? [...base, { id: PENDING_QUESTION, role: "user" as const, content: instruction, created_at: "" }] : base;
    setMessages([...shown, { id: PENDING_ANSWER, role: "assistant", content: "", created_at: "", streaming: true, steps: [] }]);
    stick.current = true;
    setBusy(true); setError("");
    const controller = new AbortController();
    abort.current = controller;
    let failure = "";
    try {
      await api.agentSessionRun(project.id, sessionId, { instruction, edit_of: editOf, allow_remote: allowed.current }, controller.signal, onEvent);
    } catch (e) {
      if (e instanceof ApiError && e.code === "remote_not_confirmed") allowed.current = false;
      failure = e instanceof ApiError ? e.detail : errorText(e);
    } finally { abort.current = null; }
    if (!alive.current) return;
    if (failure) patchLive((m) => ({ ...m, error: failure, streaming: false }));
    else patchLive((m) => (m.streaming ? { ...m, streaming: false } : m));
    const fresh = await api.agentSessionGet(project.id, sessionId).catch(() => null);
    if (!alive.current) return;
    setBusy(false);
    if (fresh) { setSession(fresh); if (!failure) setMessages(turnsOfSession(fresh)); }
    void loadList();
  };

  const confirmRemote = async (): Promise<boolean> => {
    if (!endpoint || endpoint.local || allowed.current) return true;
    const ok = await confirm({ title: `Send excerpts of ${project.name} to ${endpoint.host || "another computer"}?`,
      description: `The model runs on ${endpoint.host || "another computer"}, not on this computer. When the assistant reads a file, the lines it reads are sent there with your question. Credentials the app can recognise are masked first, and files that look like they hold credentials are never read.`,
      confirmLabel: "Send excerpts", cancelLabel: "Not now" });
    if (ok) allowed.current = true;
    return ok;
  };
  const ensureChat = async (): Promise<string | null> => {
    if (chat) return chat.id;
    try {
      const made = await api.projectChatCreate(project.id, { params: draftParams });
      if (!alive.current) return null;
      setChat(made);
      onIdChange?.(made.id);
      return made.id;
    } catch (e) { if (alive.current) setError(errorText(e)); return null; }
  };

  const ask = async (text: string, opts?: { deepResearch?: boolean; planMode?: boolean; forceWebSearch?: boolean }): Promise<boolean> => {
    if (!opts) {
      const parsed = parseCommand(text);
      if (parsed) { void runCommand(parsed.name, parsed.arg, commandContext); return true; }
    }
    const question = text.trim();
    if (!question || busy || !endpoint?.configured) return false;
    if (!(await confirmRemote())) return false;
    const id = await ensureChat();
    if (!id) return false;
    setError("");
    void runWhenReady(() => void turn(id, { text: question, deep_research: opts?.deepResearch, plan_mode: opts?.planMode, force_web_search: opts?.forceWebSearch },
      [...messages, { id: PENDING_QUESTION, role: "user", content: question, created_at: "" }]));
    return true;
  };
  const regenerate = () => {
    if (!chat || busy) return;
    const last = messages[messages.length - 1];
    const shown = last?.role === "assistant" ? messages.slice(0, -1) : messages;
    void runWhenReady(() => void turn(chat.id, { regenerate: true }, shown));
  };
  const edit = async (messageId: string, text: string) => {
    if (!chat || busy || !text.trim()) return;
    if (!(await confirmRemote())) return;
    const at = messages.findIndex((m) => m.id === messageId);
    if (at < 0) return;
    const shown = [...messages.slice(0, at), { ...messages[at], id: PENDING_QUESTION, content: text.trim() }];
    void runWhenReady(() => void turn(chat.id, { edit_of: messageId, text: text.trim() }, shown));
  };

  /** The composer's own onSend for kind="agent": creates the session on the first message (empty instructions -- the message itself is the first real turn), then runs it, exactly
   *  the way ensureChat()+turn() create an empty chat before its own first message. Waits for a local model to finish loading first, the same way ask() does. */
  const send = async (text: string): Promise<boolean> => {
    if (busy) return false;
    try {
      let sid = session?.id;
      if (!sid) {
        const s = draftModel ? await api.agentSessionCreate(project.id, mode, draftInstructions, draftAgentParams, draftModel) : await api.agentSessionCreate(project.id, mode, draftInstructions, draftAgentParams);
        await loadList();
        if (!alive.current) return false;
        setSession(s); onIdChange?.(s.id); sid = s.id;
      }
      void runWhenReady(() => void run(sid!, text));
      return true;
    } catch (e) { if (alive.current) setError(errorText(e)); return false; }
  };
  /** Answers the session's one pending call. ``editedArguments`` carries the owner's own answers in place of the model's (``ask_question``'s suggested-options card); ``feedback``
   *  carries free text onto a "deny" (the plan-review card's "what should change" composer). ``skipContinue`` skips the usual fresh run right after -- only ``acceptPlan`` needs
   *  this, since it changes the session's mode first and continues under that, not under whatever mode the call was asked in. */
  const decide = async (decision: "approve" | "deny", opts?: { feedback?: string; editedArguments?: Record<string, unknown>; skipContinue?: boolean; remember?: Remember }): Promise<boolean> => {
    const pending = session?.pending_call;
    if (!session || !pending) return false;
    setBusy(true);
    try {
      await api.agentSessionApprove(project.id, session.id,
        { call_id: pending.id, decision, ...(opts?.feedback ? { feedback: opts.feedback } : {}), ...(opts?.editedArguments ? { edited_arguments: opts.editedArguments } : {}), ...(opts?.remember ? { remember: opts.remember } : {}) });
    } catch (e) { setError(e instanceof ApiError ? e.detail : errorText(e)); setBusy(false); return false; }
    setBusy(false);
    if (alive.current) setSession((s) => (s && s.id === session.id ? { ...s, pending_call: null } : s));       // answered: its card or dialog goes now, not when the run that follows ends and the session is read again
    if (!opts?.skipContinue) void run(session.id);                     // a fresh run with no new instruction: the model sees the result and continues
    return true;
  };
  /** Accepting a proposed plan: resolves it (approved -- a trivial, non-destructive tool, nothing to review there), switches this same session's mode in place, then continues
   *  it -- the model keeps everything it already investigated, rather than starting a disconnected session with only the plan's own text as a first message. */
  const acceptPlan = async (targetMode: "manual" | "auto") => {
    const pending = session?.pending_call;
    if (!session || !pending || pending.name !== "propose_plan") return;
    const approved = await decide("approve", { skipContinue: true });
    if (!approved || !alive.current) return;
    if (!(await changeMode(targetMode))) return;
    void runWhenReady(() => void run(session.id));
  };

  /** Switches the live mode of the open agent session (a draft, pre-session choice just sets local state, as it always has). Takes effect on the next ``run()`` --
   *  ``_workspace_for`` on the backend picks the real project or the project's one shared sandbox fresh from ``session.mode`` every time, so nothing has to migrate. */
  const changeMode = async (v: AgentMode): Promise<boolean> => {
    if (!session) { modeChosen.current = true; setMode(v); return true; }
    try { const next = await api.agentSessionUpdate(project.id, session.id, { mode: v }); if (alive.current) setSession(next); return true; }
    catch (e) { if (alive.current) setError(errorText(e)); return false; }
  };
  /** A new session from the conversation up to the end of the turn the answer belongs to, opened at once. The original, and the sandbox, stay as they are. */
  const forkSession = async (messageId: string): Promise<void> => {
    if (!session || busy) return;
    try {
      const child = await api.agentSessionFork(project.id, session.id, messageId);
      if (!alive.current) return;
      await loadList();
      await open(child.id);
      onIdChange?.(child.id);
    } catch (e) { if (alive.current) setError(errorText(e)); }
  };
  /** Gives the open session another model (a draft just remembers the choice). "" goes back to the model the settings name. Takes effect on the next run. */
  const changeModel = async (ref: string): Promise<void> => {
    if (!session) { setDraftModel(ref); return; }
    try { const next = await api.agentSessionUpdate(project.id, session.id, { model_ref: ref }); if (alive.current) setSession(next); }
    catch (e) { if (alive.current) setError(errorText(e)); }
  };
  /** Editing the session's own last instruction and sending it again: undoes it completely (agent_loop.run's ``edit_of``), then runs the edited text as its replacement. Only
   *  wired to the one message id ``lastUserId`` names below -- every earlier instruction renders with no Edit control at all. */
  const editInstruction = (messageId: string, text: string) => {
    if (!session || busy || !text.trim()) return;
    void runWhenReady(() => void run(session.id, text.trim(), messageId));
  };
  const rename = async (id: string, title: string) => {
    try {
      if (kind === "chat") { await api.projectChatUpdate(project.id, id, { title }); if (chat?.id === id) setChat((c) => (c ? { ...c, title } : c)); }
      else { const next = await api.agentSessionUpdate(project.id, id, { title }); if (session?.id === id) setSession(next); }
      await loadList();
    } catch (e) { setError(errorText(e)); }
  };
  const remove = async (id: string) => {
    try {
      if (kind === "chat") await api.projectChatDelete(project.id, id);
      else await api.agentSessionDelete(project.id, id);
      await loadList();
      const activeId = kind === "chat" ? chat?.id : session?.id;
      if (activeId === id) {
        // a chat falls back to whatever is now first in the list (today's own behaviour); an agent session never auto-opens "the most recent" anything (see startEmpty below),
        // so it falls back to a blank draft instead, for the same reason.
        if (kind === "chat") { const rest = (await loadList()).filter((c) => c.id !== id); await open(rest[0]?.id ?? null); onIdChange?.(rest[0]?.id ?? null); }
        else { await open(null); onIdChange?.(null); }
      }
    } catch (e) { setError(errorText(e)); }
  };
  const changeParams = async (patch: Partial<ChatParams>) => {
    setParamsError("");
    if (!chat) { setDraftParams((p) => ({ ...p, ...patch })); return; }
    try { const next = await api.projectChatUpdate(project.id, chat.id, { params: patch }); if (alive.current) setChat((c) => (c ? { ...c, params: next.params } : c)); }
    catch (e) { setParamsError(e instanceof ApiError ? e.detail : errorText(e)); }
  };
  /** The settings panel's ``params`` prop, for kind="agent": the same ChatParams shape (``system`` standing in for the session's own ``instructions``), from the session once
   *  there is one, else the drafts kept for a session that does not exist yet. */
  const toAgentChatParams = (): ChatParams => {
    const p = session?.params ?? draftAgentParams;
    return { system: session?.instructions ?? draftInstructions, temperature: p.temperature, top_p: p.top_p, max_tokens: p.max_tokens, autocompact: p.autocompact, web_search: p.web_search };
  };
  /** The settings panel's ``onChange`` for kind="agent": ``system`` (the panel's name for it) becomes the session's own ``instructions``; everything else is a session setting.
   *  ``auto_approve`` is not part of ChatParams at all -- the panel never sends it itself, only this component's own Auto-approve switch does, through the same function. */
  const changeAgentParams = async (patch: Partial<ChatParams> & { auto_approve?: boolean; engine?: "legacy" | "v2" }) => {
    setParamsError("");
    const { system, ...rest } = patch;
    if (!session) {
      if (system !== undefined) setDraftInstructions(system);
      if (Object.keys(rest).length) setDraftAgentParams((p) => ({ ...p, ...rest }));
      return;
    }
    try {
      const body: { instructions?: string; params?: Partial<AgentSessionParams> } = {};
      if (system !== undefined) body.instructions = system;
      if (Object.keys(rest).length) body.params = rest;
      const next = await api.agentSessionUpdate(project.id, session.id, body);
      if (alive.current) setSession(next);
    } catch (e) { setParamsError(e instanceof ApiError ? e.detail : errorText(e)); }
  };
  const changeInstructions = async (text: string) => {
    setParamsError("");
    try { await api.updateProject(project.id, { instructions: text }); const ep = await api.projectChatEndpoint(project.id); if (alive.current) setEndpoint(ep); }
    catch (e) { setParamsError(e instanceof ApiError ? e.detail : errorText(e)); }
  };
  const stop = () => {
    if (kind === "chat") { if (!chat) return; api.projectChatStop(project.id, chat.id).catch(() => abort.current?.abort()); }
    else { if (!session) return; api.agentSessionCancel(project.id, session.id).catch(() => abort.current?.abort()); }
    // the server ends the answer and keeps what was written; if it cannot be asked, the page stops listening
  };
  const newConversation = () => { if (kind === "chat") setDraftParams(DRAFT_PARAMS); else { setMode(session?.mode ?? mode); modeChosen.current = true; } void open(null); onIdChange?.(null); };
  const select = (id: string) => { void open(id); onIdChange?.(id); };

  const used = approxTokens(messages.reduce((n, m) => n + m.content.length, 0));
  const params = chat?.params ?? draftParams;
  const last = messages[messages.length - 1];
  const lastUserId = [...messages].reverse().find((m) => m.role === "user")?.id;      // kind="agent" only: the one instruction Edit is offered on
  const everTrimmed = messages.some((m) => m.trimmed);
  const everCompacted = messages.some((m) => m.compacted);
  const commandContext: CommandContext = { project, chat: chat ? { id: chat.id } : null, chats, messages, params, context,
    ask, selectChat: select, openConversationList, clearMessages, changeParams, confirmRemote, appendResult, updateResult };
  const effectiveMode = session?.mode ?? mode;
  const modelOptions = (() => {                                              // grouped by the provider that serves them; the choice is only there when there is something to choose
    const groups = new Map<string, { value: string; label: string }[]>();
    for (const m of models) groups.set(m.provider_name, [...(groups.get(m.provider_name) ?? []), { value: m.ref, label: m.label }]);
    return [...groups.entries()].map(([label, options]) => ({ label, options }));
  })();
  const modelControl = kind === "agent" && models.length > 1
    ? <Select aria-label="Model" value={session ? (session.model_ref ?? "") : draftModel} onValueChange={(v) => void changeModel(v)} disabled={busy || !!session?.pending_call} className="h-[2.75rem] w-44 shrink-0" options={modelOptions} />
    : undefined;
  const EFFORT_WORDS: Record<Effort, string> = { off: "Thinking: off", low: "Thinking: low", medium: "Thinking: medium", high: "Thinking: high", max: "Thinking: max" };
  const chosenRef = session ? (session.model_ref ?? "") : draftModel;
  const efforts: Effort[] = models.find((m) => m.ref === chosenRef)?.profile?.efforts ?? ["off", "low", "medium", "high", "max"];
  const effort: Effort = (session?.params ?? draftAgentParams).effort ?? "off";
  /** The level of thinking of the open session (a draft just remembers it). Takes effect on the next round. */
  const changeEffort = async (v: Effort): Promise<void> => {
    if (!session) { setDraftAgentParams((p) => ({ ...p, effort: v })); return; }
    try { const next = await api.agentSessionUpdate(project.id, session.id, { params: { effort: v } }); if (alive.current) setSession(next); }
    catch (e) { if (alive.current) setError(errorText(e)); }
  };
  const effortControl = kind === "agent"
    ? <Select aria-label="Thinking effort" value={efforts.includes(effort) ? effort : efforts[0]} onValueChange={(v) => void changeEffort(v as Effort)} disabled={busy || !!session?.pending_call}
        className="h-[2.75rem] w-40 shrink-0" options={efforts.map((e) => ({ value: e, label: EFFORT_WORDS[e] }))} />
    : undefined;
  const engine: "v2" | "legacy" = (session?.params ?? draftAgentParams).engine === "v2" ? "v2" : "legacy";       // a session with no saved choice runs the old loop, and says so
  /** The engine of the open session (a draft just remembers it). Takes effect on the next run. */
  const engineControl = kind === "agent"
    ? <Select aria-label="Engine" value={engine} onValueChange={(v) => void changeAgentParams({ engine: v as "v2" | "legacy" })} disabled={busy || !!session?.pending_call}
        className="h-[2.75rem] w-48 shrink-0" options={ENGINE_OPTIONS} />
    : undefined;
  const modeControl = kind === "agent"
    ? <Select aria-label="Mode" value={effectiveMode} onValueChange={(v) => void changeMode(v as AgentMode)} disabled={busy} className="h-[2.75rem] w-28 shrink-0" options={MODE_OPTIONS} />
    : undefined;

  const conversationList = (onSelect: (id: string) => void, onNew: () => void) => chats === null ? <Skeleton className="h-24 w-full" /> : (
    <ConversationList chats={chats} activeId={(kind === "chat" ? chat?.id : session?.id) ?? null} damaged={damaged} busy={busy} onSelect={onSelect} onNew={onNew}
      onRename={rename} onDelete={remove}
      navLabel={kind === "chat" ? "Conversations" : "Agent sessions"} newLabel={kind === "chat" ? "New chat" : "New session"} />);
  const agentExtra = kind === "agent" ? (
    <div className="space-y-2 border-t border-line pt-3">
      <h2 className="text-[0.7rem] font-semibold uppercase tracking-wider text-faint">This mode</h2>
      {agentCaps ? (
        <p className="text-muted">
          {effectiveMode === "plan" ? `Plan: read-only, up to ${agentCaps.plan.max_rounds} rounds. Proposes a plan for your review; it never writes or runs anything.`
            : effectiveMode === "manual" ? `Manual: works in a sandboxed copy, up to ${agentCaps.manual.max_rounds} rounds. Pauses before ${agentCaps.manual.confirm_tools.join(" or ")} for your approval, unless auto-approve is on.`
            : `Auto: works in a sandboxed copy without pausing, up to ${agentCaps.auto.max_rounds} rounds or ${Math.round(agentCaps.auto.max_seconds / 60)} minutes. Nothing it does reaches the real project.`}
        </p>
      ) : <p className="text-muted">Looking for the mode's limits...</p>}
      <p className="text-muted">{engine === "v2"
        ? "Engine: the durable run (new). Each run keeps a record of what it may spend, what it changed and which checks it ran; claims of success are checked against that record, and the Task tab appears. Change it beside Mode."
        : "Engine: standard (old). The loop used so far, with no record of a run. Change it beside Mode."}</p>
      {effectiveMode === "manual" && (
        <Switch label="Auto-approve" description="Skip the write/run confirmation card; the agent proceeds without pausing." checked={(session?.params ?? draftAgentParams).auto_approve}
          disabled={busy} onCheckedChange={(v) => void changeAgentParams({ auto_approve: v })} />
      )}
    </div>
  ) : undefined;
  const paramsPanel = kind === "chat" ? (
    <ParamsPanel key={chat?.id ?? "new"} params={params} endpoint={endpoint} context={context} used={used} disabled={busy} error={paramsError} onChange={(p) => void changeParams(p)}
      onChangeInstructions={changeInstructions} everTrimmed={everTrimmed} everCompacted={everCompacted} />
  ) : (
    <ParamsPanel key={session?.id ?? "new"} params={toAgentChatParams()} endpoint={endpoint} context={context} used={used} disabled={busy} error={paramsError}
      onChange={(p) => void changeAgentParams(p)} onChangeInstructions={changeInstructions} extra={agentExtra} layout="grid" agent />
  );

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button variant="ghost" icon={<MessagesSquare size={15} aria-hidden />} aria-label={`${kind === "chat" ? "Conversations" : "Agent sessions"} (narrow window)`}
          className="rail:hidden" onClick={() => setConvoSheetOpen(true)}>{kind === "chat" ? "Conversations" : "Agent sessions"}</Button>
        <span className="hidden rail:block" />
        {kind === "chat" ? (
          <>
            <Button variant="ghost" icon={<SlidersHorizontal size={15} aria-hidden />} aria-pressed={showParams} className="hidden rail:inline-flex" onClick={() => setShowParams((v) => !v)}>Settings</Button>
            <Button variant="ghost" icon={<SlidersHorizontal size={15} aria-hidden />} aria-label="Settings (narrow window)" className="rail:hidden" onClick={() => setParamsSheetOpen(true)}>Settings</Button>
          </>
        ) : (
          <div className="flex items-center gap-1">
            <Button variant="ghost" icon={collapseOutputs ? <ChevronsUpDown size={15} aria-hidden /> : <ChevronsDownUp size={15} aria-hidden />} aria-pressed={collapseOutputs}
              title="Fold every call to its header line, or show each with its output" onClick={() => setCollapseOutputs((v) => !v)}>{collapseOutputs ? "Expand outputs" : "Collapse outputs"}</Button>
            {session && messages.length > 0 && (
              <Menu label="Export" menuLabel="Export this session as" title="Save this session as a file"
                items={EXPORT_FORMATS.map((f) => ({ key: f.format, label: f.label, description: f.description, href: api.agentSessionExportUrl(project.id, session.id, f.format), download: true }))} />)}
            <Button variant="ghost" icon={<SlidersHorizontal size={15} aria-hidden />} onClick={() => setSettingsOpen(true)}>Settings</Button>
          </div>
        )}
      </div>
      {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
      {endpoint && !endpoint.configured && (
        <p role="status" className="text-[0.82rem] text-muted">No language model is set up. Start a local one under Model Hub, Serving, or give the address of a hosted one in Settings. Until then the chat has nothing to answer with.</p>)}
      <div className="flex min-h-0 flex-1 gap-4">
        <aside className="hidden min-h-0 w-64 shrink-0 flex-col rail:flex">{conversationList(select, newConversation)}</aside>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
          <div className="min-h-0 flex-1">
            {messages.length > 0 ? (
              <CollapseOutputs.Provider value={collapseOutputs}>
              <OutputSource.Provider value={loadOutput}>
              <SourcesContext.Provider value={webSources}>
              <ChildSource.Provider value={loadChild}>
              <ul ref={thread} aria-label={kind === "chat" ? "The conversation" : "The session"} aria-live="polite" onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}
                className="h-full space-y-4 overflow-y-auto pr-1">
                {messages.map((m) => <MessageView key={m.id} message={m} last={m === last} busy={busy || awaitingModel} onRegenerate={chat ? regenerate : undefined}
                  onEdit={kind === "chat" ? (chat ? (id, text) => void edit(id, text) : undefined) : (m.id === lastUserId ? editInstruction : undefined)}
                  editNote={kind === "agent" ? "Sending it again undoes this instruction completely -- its answer, and anything it wrote or ran in the sandbox." : undefined}
                  onFork={kind === "agent" && session && !m.streaming && m.steps ? (id) => void forkSession(id) : undefined} />)}
              </ul>
              </ChildSource.Provider>
              </SourcesContext.Provider>
              </OutputSource.Provider>
              </CollapseOutputs.Provider>
            ) : kind === "agent" && !busy ? (
              <AgentEmptyState mode={effectiveMode} engine={engine} />
            ) : null}
          </div>
          {kind === "agent" && session?.pending_call?.name === "ask_question" ? (
            <AskQuestionCard questions={questionsOf(session.pending_call)} busy={busy}
              onAnswer={(answers) => void decide("approve", { editedArguments: { questions: questionsOf(session.pending_call!), answers } })} onStop={stop} />
          ) : kind === "agent" && session?.pending_call?.name === "propose_plan" ? (
            <PlanReviewCard plan={planTextOf(session.pending_call)} busy={busy} onAccept={(m) => void acceptPlan(m)} onRequestChanges={(fb) => decide("deny", { feedback: fb })} onStop={stop} />
          ) : kind === "agent" && session?.pending_call?.name === "rescan_dependencies" ? (
            <DependencyScanDialog busy={busy} onConfirm={(dontAsk) => void decide("approve", { ...(dontAsk ? { remember: "always" as const } : {}) })} onCancel={() => void decide("deny")} />
          ) : kind === "agent" && session?.pending_call ? (
            <ApprovalCard call={session.pending_call} busy={busy} onApprove={(remember) => void decide("approve", { remember })} onDeny={(feedback) => void decide("deny", { feedback })} />
          ) : (
            <Composer busy={busy} disabled={!endpoint?.configured || awaitingModel}
              placeholder={kind === "agent" ? (effectiveMode === "plan" ? "A question for the plan" : "An instruction for the agent") : undefined}
              hint={awaitingModel ? "The language model is loading; the question is sent once it is ready."
                : (kind === "agent" && !session ? "Starts a new session in the mode chosen beside Send." : undefined)}
              onSend={kind === "chat" ? ask : send} onStop={stop} beforeSend={kind === "agent" ? <>{modelControl}{effortControl}{engineControl}{modeControl}</> : undefined} commands={kind === "chat"} />
          )}
        </div>
        {kind === "chat" ? (
          showParams && <aside className="hidden w-64 shrink-0 overflow-y-auto rail:block">{paramsPanel}</aside>
        ) : (
          <aside className="hidden w-64 shrink-0 overflow-y-auto rail:block"><AgentStatusPanel session={session} mode={effectiveMode} messages={messages} diff={diff} busy={busy} onChanged={loadDiff} /></aside>
        )}
      </div>
      <Sheet open={convoSheetOpen} onOpenChange={setConvoSheetOpen} title={kind === "chat" ? "Conversations" : "Agent sessions"}>
        {conversationList((id) => { select(id); setConvoSheetOpen(false); }, () => { newConversation(); setConvoSheetOpen(false); })}
      </Sheet>
      {kind === "chat" ? (
        <Sheet open={paramsSheetOpen} onOpenChange={setParamsSheetOpen} title="This conversation's settings">{paramsPanel}</Sheet>
      ) : (
        <Dialog open={settingsOpen} onOpenChange={setSettingsOpen} title="This session's settings" className="w-[min(44rem,94vw)]">{paramsPanel}</Dialog>
      )}
      {awaitingModel && (
        <LoadModelDialog role="llm" label="Language model" onClose={() => { setAwaitingModel(false); pendingSend.current = null; }}
          onReady={() => { setAwaitingModel(false); const run = pendingSend.current; pendingSend.current = null; run?.(); }} />)}
    </div>
  );
}
