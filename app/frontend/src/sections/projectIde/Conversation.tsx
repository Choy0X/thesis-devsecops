import { Textarea, IdeSelect } from "./Controls";
import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Bold, Code2, Link, LoaderCircle, Paperclip, Square, Terminal, X } from "lucide-react";
import { clockSpan } from "../../lib/format";
import { Markdown, type OpenWorkspaceReference } from "../../components/chat/Markdown";
import { activeRun, describe, type Chat, type ChatDetail, type Model, type TurnMode } from "./client";
import { commandUsage, parseCommand, type ScanCommand } from "./state";
import { ScanHistory } from "./ScanHistory";
import type { ScanProgressSnapshot } from "./ScanProgress";
import { ResumePicker } from "./ResumePicker";
import { useWorkspaceState } from "../../lib/workspaceState";
import { friendlyRunError } from "./runError";
import { PlanningQuestion } from "./PlanningQuestion";
import { InstallModel } from "../OllamaModels";

// Actions at the end of the model menu; they never become the conversation's model.
const REFRESH_MODELS = "__refresh_models";
const ADD_MODEL = "__add_model";

function RunActivity({ detail }: { detail: ChatDetail }) {
  const run = activeRun(detail.runs);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  if (!run) return null;
  const activity = detail.activity?.run_id === run.id ? detail.activity : null;
  const start = activity?.started_at ? Date.parse(activity.started_at) : NaN;
  const elapsed = Number.isFinite(start) ? Math.max(0, Math.floor((now - start) / 1000)) : null;
  const message = activity?.message || run.message || (run.state === "queued" ? "Waiting to start..." : run.state === "running" ? "Working on your request..." : run.state.replace(/_/g, " "));
  const waiting = run.state === "waiting_approval" || run.state === "waiting_input";
  return <div className="chat-run-activity" role="status"><span className="chat-activity-icon">{waiting ? <span className="chat-wait-dot" /> : <LoaderCircle size={15} className="chat-spin" />}</span><span>{message}</span>{elapsed !== null && <time aria-label="Time in current phase">{clockSpan(elapsed)}</time>}</div>;
}

function toolLabel(name?: string) {
  const labels: Record<string, string> = {
    read: "Read file", read_file: "Read file", glob: "Find files", list_files: "List files",
    grep: "Search code", search_files: "Search code", search_project: "Search project", bash: "Run command", edit: "Edit file",
    edit_file: "Edit file", create_file: "Create file", write: "Write file", apply_patch: "Apply changes",
    run_tests: "Run tests", task: "Delegate task", todowrite: "Update plan", ask_user: "Ask a planning question", webfetch: "Read source",
  };
  return labels[name || ""] || name?.replace(/_/g, " ") || "Tool activity";
}

function ToolOutput({ tool, output, onOpenReference }: { tool?: string; output: unknown; onOpenReference?: OpenWorkspaceReference }) {
  const search = tool === "search_project" || tool === "search_files";
  if (!search || typeof output !== "object" || !output || !("hits" in output) || !Array.isArray(output.hits)) return <pre>{describe(output)}</pre>;
  const hits = output.hits as { path: string; start_line: number; end_line: number; excerpt: string }[];
  const notes = "omissions" in output && Array.isArray(output.omissions) ? output.omissions.filter(note => typeof note === "string") : [];
  const partial = "complete" in output && output.complete === false;
  return <div className="chat-search-evidence">
    {!hits.length && <p>No matching source found.</p>}
    {partial && <p>Partial search coverage</p>}
    {!!notes.length && <ul>{notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}
    {hits.map((hit, index) => {
      const label = <code>{hit.path}:{hit.start_line}{hit.end_line !== hit.start_line ? `-${hit.end_line}` : ""}</code>;
      return <div key={index}>
        {onOpenReference && typeof hit.path === "string" ? <button type="button" className="chat-file-reference" onClick={event => onOpenReference({ path: hit.path, line: Number.isSafeInteger(hit.start_line) ? hit.start_line : undefined }, event.currentTarget)}>{label}</button> : label}
        <pre>{hit.excerpt}</pre>
      </div>;
    })}
  </div>;
}

export function Conversation({ detail, draftKey = detail?.chat.id ?? "new", models, modelId, onModel, dirtyCount, busy, scanBusy = busy, onSend, onScan, onCancel, onPermission, onAnswer, onAddSource, sources = [], onRemoveSource, history, onNew, onDiff, onPropose, onResumeSaved, scanProgress, startingScan, onOpenReference, onRefreshModels }: {
  draftKey?: string;
  onRefreshModels?: () => void;
  history?: { chats: Chat[]; query: string | null; onOpen: (query: string) => void; onClose: () => void; onPick: (id: string) => void;
    onUpdate?: (id: string, fields: { title?: string; pinned?: boolean }) => Promise<void>; onDelete?: (id: string) => Promise<void> };
  scanProgress?: ScanProgressSnapshot | null;
  startingScan?: ScanCommand | null;
  onNew?: () => void;
  onDiff?: () => void;
  onPropose?: (findingId: string) => void;
  onOpenReference?: OpenWorkspaceReference;
  onResumeSaved?: () => void;
  scanBusy?: boolean;
  detail: ChatDetail | null; models: Model[]; modelId: string; onModel: (id: string) => void;
  dirtyCount: number; busy: boolean; onSend: (text: string, save: boolean, mode: TurnMode) => Promise<void>;
  onScan: (command: Extract<ScanCommand, { kind: "scan" }>) => Promise<void>;
  onCancel: () => void; onPermission: (id: string, reply: "once" | "reject") => Promise<void>;
  onAnswer: (id: string, answers: string[][]) => Promise<void>;
  onAddSource?: () => void; sources?: { id: string; label: string; primary?: boolean }[]; onRemoveSource?: (id: string) => void;
}) {
  const [text, setText] = useWorkspaceState(`agent.draft.${draftKey}`, "");
  const [dismissed, setDismissed] = useState(false);
  const [dirtyChoice, setDirtyChoice] = useState(false);
  const [error, setError] = useState("");
  const [help, setHelp] = useState(false);
  const [planning, setPlanning] = useState(false);
  const resumeQuery = history?.query ?? null;
  const [sending, setSending] = useState(false);
  const submitting = useRef(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const messagesEnd = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const [awayFromLatest, setAwayFromLatest] = useState(false);
  useEffect(() => { if (followLatest.current) messagesEnd.current?.scrollIntoView({ block: "end" }); }, [detail?.messages, detail?.runs, scanProgress, startingScan]);
  const format = (before: string, after: string, fallback: string) => {
    const field = composer.current; if (!field) return;
    const start = field.selectionStart, end = field.selectionEnd;
    const selected = text.slice(start, end) || fallback;
    setText(text.slice(0, start) + before + selected + after + text.slice(end));
    requestAnimationFrame(() => { field.focus(); field.setSelectionRange(start + before.length, start + before.length + selected.length); });
  };
  const parsed = parseCommand(text);
  const commandMenu = resumeQuery === null && !dismissed && parsed.kind === "menu" ? parsed : null;
  const isStandalone = (command: ReturnType<typeof parseCommand>) => command.kind === "scan" && (command.scope === "project_code" || command.dependency_mode === "versions");
  const commandBusy = isStandalone(parsed) ? scanBusy : busy;
  const localCommand = parsed.kind !== "text" && parsed.kind !== "scan" && (parsed.kind !== "plan" || !parsed.goal);
  const archived = Boolean(detail?.chat.archived);
  const savedModel = detail?.chat.model?.kind === "scan_only" ? undefined : detail?.chat.model;
  const catalogMatch = savedModel && models.find(model => model.id === savedModel.id || (savedModel.tag && model.tag === savedModel.tag));
  const modelChanged = Boolean(savedModel?.digest && catalogMatch?.digest && savedModel.digest !== catalogMatch.digest) ||
    (catalogMatch?.tool_protocol === "text" && savedModel?.tool_protocol !== "text");
  const savedChoice = savedModel && modelChanged ? { ...savedModel, id: savedModel.id + ":previous", available: false } : savedModel;
  const visibleModels = savedChoice && !models.some(model => model.id === savedChoice.id) ?
    [...models, { ...savedChoice, available: !modelChanged && Boolean(catalogMatch && catalogMatch.available !== false) }] : models;
  const textOnly = (savedChoice || models.find(model => model.id === modelId))?.tool_protocol === "text";
  // The conversation keeps the model it last ran with; when that tag was removed from Ollama, say so and offer the others.
  const savedMissing = Boolean(savedChoice && !archived && visibleModels.find(model => model.id === savedChoice.id)?.available === false);
  const [addingModel, setAddingModel] = useState(false);
  const pickModel = (id: string) => {
    if (id === REFRESH_MODELS) { onRefreshModels?.(); return; }
    if (id === ADD_MODEL) { setAddingModel(true); return; }
    onModel(id);
  };
  const run = activeRun(detail?.runs || []);
  const recovery = detail?.recovery;
  const progress = detail?.progress;
  const next = progress?.items.find(item => item.status === "in_progress") || progress?.items.find(item => item.status === "pending");
  const empty = !detail?.messages.length && !detail?.runs.length && !archived && !startingScan;
  const submit = async (save = false, bypassDirty = false, input = text) => {
    if (!input.trim() || submitting.current) return;
    const command = parseCommand(input);
    setDismissed(false); setError(""); setHelp(false);
    if (command.kind === "menu") { setText(input); return; }
    if (command.kind === "invalid") { setError(command.usage); return; }
    if (command.kind === "help") { setText(""); setHelp(true); return; }
    if (command.kind === "resume") {
      if (history) { setText(""); history.onOpen(command.query); }
      return;
    }
    if (command.kind === "new") { setText(""); onNew?.(); composer.current?.focus(); return; }
    if (command.kind === "diff") { setText(""); onDiff?.(); return; }
    if ((isStandalone(command) ? scanBusy : busy) || run || archived || recovery?.question) return;
    if (command.kind === "plan" && textOnly) { setError("Choose a model with project tools to use /plan."); return; }
    if (command.kind === "plan" && !command.goal) { setPlanning(true); setText(""); composer.current?.focus(); return; }
    if (command.kind === "text" && dirtyCount && !bypassDirty) { setDirtyChoice(true); return; }
    submitting.current = true; setSending(true);
    try {
      if (command.kind === "scan") await onScan(command);
      else {
        await onSend(command.kind === "plan" ? command.goal : input, save, planning || command.kind === "plan" ? "plan" : "build");
        setPlanning(false);
      }
      setText(current => current === text ? "" : current);
      setDirtyChoice(false);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { submitting.current = false; setSending(false); }
  };
  return <section className={empty ? "ide-conversation is-empty" : "ide-conversation"} aria-label="Agent conversation">
    <div className="ide-messages" tabIndex={0} role="region" aria-label="Conversation history" onScroll={event => { const list = event.currentTarget; followLatest.current = list.scrollHeight - list.scrollTop - list.clientHeight < 80; setAwayFromLatest(!followLatest.current); }}>
      {(!detail?.messages.length && !detail?.runs.length && !startingScan) && <div className="ide-conversation-empty">
        {!detail && <h2>New conversation</h2>}
        {archived ? <p>No messages in this conversation.</p> : <>
          {visibleModels.some(model => model.available !== false)
            ? <p>Ask about your project or open Commands with <kbd>Ctrl+K</kbd>.</p>
            : <p><a href="/models">Install a chat model</a> to send a message. Code and dependency checks are available now.</p>}
        </>}
      </div>}
      {detail?.messages.filter(message => message.parts.length).map(message => <article className={`ide-message ide-message-${message.info.role}`} key={message.info.id}>
        <strong>{message.info.role === "user" ? <span className="sr-only">You</span> : message.info.role === "assistant" ? <><span className="chat-agent-icon"><Terminal size={13} aria-hidden /></span>Assistant</> : message.info.role}</strong>
        <div className="chat-message-content">{message.parts.map((part, i) => part.type === "text" ? <Markdown key={i} text={part.text || ""} onOpenReference={onOpenReference} /> : part.type === "tool" ? <details className="ide-tool" key={i}><summary>{toolLabel(part.tool)}{part.state?.status && <span>{part.state.status}</span>}</summary>{part.state?.input != null && <><small>Input</small><pre>{describe(part.state.input)}</pre></>}{part.state?.output != null && <><small>Output</small><ToolOutput tool={part.tool} output={part.state.output} onOpenReference={onOpenReference} /></>}{part.state?.error != null && <pre className="ide-error">{describe(part.state.error)}</pre>}</details> : part.type === "reasoning" ? <details key={i}><summary>Reasoning</summary><pre>{part.text}</pre></details> : null)}</div>
      </article>)}
      <ScanHistory runs={detail?.runs || []} progress={scanProgress} starting={startingScan} activity={detail?.activity} onCancel={onCancel} onPropose={archived || !!run || busy ? undefined : onPropose} />
      {detail?.runs.filter(item => item.kind !== "scan" && item.state === "failed").slice(-1).map(item => <div className="chat-run-error" role="status" key={item.id}><strong>The request could not complete</strong><p>{friendlyRunError(item.error || item.result)}</p></div>)}
      {detail?.permissions.map(permission => <div className="ide-pending" key={permission.id}><strong>Permission required: {permission.permission || "Run tool"}</strong><pre>{describe(permission.patterns || permission.metadata || permission)}</pre><div className="ide-toolbar"><button disabled={busy} onClick={() => void onPermission(permission.id, "once")}>Approve once</button><button disabled={busy} onClick={() => void onPermission(permission.id, "reject")}>Reject</button></div></div>)}
      {detail && run && run.kind !== "scan" && <RunActivity detail={detail} />}
      <div ref={messagesEnd} />
    </div>
    {awayFromLatest && <button className="ide-latest" aria-label="Jump to latest message" onClick={() => { followLatest.current = true; setAwayFromLatest(false); messagesEnd.current?.scrollIntoView({ block: "end" }); }}><ArrowDown size={14} />Latest message</button>}
    {(progress && (next || recovery) || !!detail?.questions.length) && <div className="chat-agent-state">
      {progress && (next || recovery) && <details className="chat-checkpoint" open={recovery && !recovery.question ? true : undefined}>
        <summary><span>{recovery ? "Saved work" : progress.mode === "plan" ? "Plan progress" : "Task progress"}</span>
          {next && <span className="chat-checkpoint-step">{next.content}</span>}
          {!!progress.items.length && <span className="chat-checkpoint-count">{progress.items.filter(item => item.status === "completed").length}/{progress.items.length}</span>}
        </summary>
        {!!progress.items.length && <ol>{progress.items.map((item, index) => <li key={index}><span>{item.content}</span><small>{item.status.replace(/_/g, " ")}</small></li>)}</ol>}
        {progress.last_tool && <p>Last tool: {toolLabel(progress.last_tool.tool)}{progress.last_tool.path && ` ${progress.last_tool.path}`} ({progress.last_tool.outcome || progress.last_tool.status}{progress.last_tool.exit_code !== undefined && `, exit ${progress.last_tool.exit_code}`})</p>}
        {recovery && !recovery.question && <>
          {recovery.uncertain.length > 0 && <p>An interrupted operation needs verification before it can be repeated.</p>}
          <button type="button" onClick={onResumeSaved} disabled={busy || !onResumeSaved}>Resume saved work</button>
        </>}
      </details>}
      {detail?.questions.map(question => <PlanningQuestion key={question.id} request={question} onReply={answers => onAnswer(question.id, answers)} />)}
    </div>}
    <div className="ide-composer">
      {resumeQuery !== null && history && <ResumePicker chats={history.chats} currentId={detail?.chat.id || ""} query={resumeQuery} onPick={history.onPick}
        onUpdate={history.onUpdate} onDelete={history.onDelete} onClose={() => { history.onClose(); composer.current?.focus(); }} />}
      {commandMenu && <div className="ide-command-menu" role="menu" aria-label={commandMenu.choices.every(choice => choice.command.startsWith("/scan ")) ? "Scan commands" : "Terminal commands"} onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); setText(""); setDismissed(true); composer.current?.focus(); }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); const choices = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button")); const index = choices.indexOf(document.activeElement as HTMLButtonElement); choices[(index + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length]?.focus(); }
      }}>{commandMenu.choices.map(choice => <button key={choice.command} role="menuitem" onClick={() => { composer.current?.focus(); void submit(false, false, choice.command); }}><code>{choice.command}</code><span>{choice.label}</span></button>)}</div>}
      {error && <p className="ide-error" role="alert">{error}</p>}
      {help && <p className="chat-command-help" role="status">{commandUsage}</p>}
      {dirtyChoice && <div className="ide-dirty-choice" role="status"><strong>{dirtyCount} unsaved {dirtyCount === 1 ? "file" : "files"}</strong><p>The agent reads saved workspace files.</p><div className="ide-toolbar"><button disabled={busy} onClick={() => void submit(true, true)}>Save and send</button><button disabled={busy} onClick={() => void submit(false, true)}>Send saved files</button><button onClick={() => setDirtyChoice(false)}>Cancel</button></div></div>}
      {!!sources.filter(source => !source.primary).length && <div className="chat-source-chips" aria-label="Chat sources">{sources.filter(source => !source.primary).map(source => <span key={source.id}><Paperclip size={12} /><span>{source.label}</span>{onRemoveSource && <button aria-label={`Remove source ${source.label}`} disabled={busy || !!run} onClick={() => onRemoveSource(source.id)}><X size={11} /></button>}</span>)}</div>}
      <div className="chat-prompt-row"><Textarea ref={composer} aria-label="Message agent" placeholder={archived ? "Type /resume or /new" : textOnly ? "Ask about project context or paste code to review" : "Message the agent or type / for commands"} rows={2} value={text} onChange={event => { setText(event.target.value); setDismissed(false); setDirtyChoice(false); setError(""); setHelp(false); if (resumeQuery !== null) history?.onClose(); }} onKeyDown={event => {
        if (event.key === "ArrowDown" && commandMenu) { event.preventDefault(); (event.currentTarget.closest(".ide-composer")?.querySelector('[role="menuitem"]') as HTMLElement | null)?.focus(); }
        if (event.key === "Escape") { setDismissed(true); setDirtyChoice(false); if (resumeQuery !== null) history?.onClose(); if (text.startsWith("/")) setText(""); }
        if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); }
      }} onInput={event => { const field = event.currentTarget; field.style.height = "auto"; field.style.height = `${Math.min(220, field.scrollHeight)}px`; }} /></div>
      <div className="ide-composer-controls">
        <div className="chat-composer-tools"><button className="ide-icon" aria-label="Add source" title="Add source" disabled={!onAddSource || busy || textOnly} onClick={onAddSource}><Paperclip size={16} /></button><button className="ide-icon" aria-label="Bold text" title="Bold text" onClick={() => format("**", "**", "text")}><Bold size={15} /></button><button className="ide-icon" aria-label="Code block" title="Code block" onClick={() => format("```\n", "\n```", "code")}><Code2 size={16} /></button><button className="ide-icon" aria-label="Insert link" title="Insert link" onClick={() => format("[", "](https://)", "link text")}><Link size={15} /></button></div>
        {planning && !textOnly && <div className="chat-planning-draft"><span>Planning next message</span><button type="button" className="ide-icon" aria-label="Cancel planning" onClick={() => setPlanning(false)}><X size={13} /></button></div>}
        <IdeSelect aria-label="Agent model" value={savedChoice?.id || (detail?.chat.model?.kind === "scan_only" ? "" : modelId)} onChange={event => pickModel(event.target.value)} disabled={busy || sending || !!run || archived}><option value="" disabled>Select model</option>{visibleModels.map(model => <option key={model.id} value={model.id} disabled={model.available === false}>{model.name}{model.tag && model.tag !== model.name ? " / " + model.tag : ""}{model.tool_protocol === "text" ? " / Text only" : model.coding_status === "failed" ? " / Tool check failed" : model.kind === "hosted" ? " / Hosted" : model.location === "cloud" ? " / Ollama Cloud" : ""}{model.available === false ? " (not installed)" : ""}</option>)}{onRefreshModels && <option value={REFRESH_MODELS}>Refresh model list</option>}<option value={ADD_MODEL}>Add model...</option></IdeSelect>
        {run && <button className="ide-icon chat-stop" aria-label="Stop active run" onClick={onCancel}><Square size={15} /></button>}
        {(!run || localCommand) && <button className="ide-send" aria-label={localCommand ? "Run command" : "Send message"} disabled={parsed.kind === "menu" || (!localCommand && (commandBusy || archived || !!recovery?.question)) || sending || !text.trim()} onClick={() => void submit()}><ArrowUp size={17} /></button>}
      </div>
    </div>
    {addingModel && <InstallModel embedding={false} onOpenChange={open => { setAddingModel(open); if (!open) onRefreshModels?.(); }} />}
    {savedMissing && <p className="chat-composer-note" role="status">This conversation used {savedChoice?.name || savedChoice?.tag}, which is no longer installed in Ollama. Choose another model in the model menu, or add one there.</p>}
    <small className="chat-composer-note">{archived ?"Archived conversation. History is read-only." : textOnly ? <>Text-only review. <a href="/models">Install a tool-capable model</a> for project inspection and edits.</> : planning ? "Planning can inspect the project and propose changes." : <>Use <code>/plan</code> for a read-only implementation plan.</>}</small>
  </section>;
}


