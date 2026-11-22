import { Check, Copy, GitFork, Pencil, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ChatUsage, StoredMessage } from "../../lib/api";
import { num } from "../../lib/format";
import { Button, Textarea } from "../ui";
import { IconButton } from "../kit";
import { AgentActivity } from "./AgentActivity";
import type { AgentStep } from "./agentSteps";
import { Markdown } from "./Markdown";

// One message of a conversation. A question is the person's words; an answer is markdown with the model's thinking apart (collapsed once the answer has begun), the files and results it read,
// and the speed it was written at. The actions are always on the page (not shown only under the pointer, so the keyboard and a touch screen have them too): Copy on both, Regenerate on the
// last answer, Edit and send again on a question.

export type ViewMessage = StoredMessage & { streaming?: boolean;
  /** The files named with @ in an owner's instruction that the server read and gave to the model. */
  attachments?: { path: string; cut: boolean }[];
  /** An agent turn's own work, in order (text said, tool calls with their results, the answer last), drawn by AgentActivity in place of the single block of words and the lines of what was
   * looked at that a chat answer has. ``content`` is then the final answer alone, for Copy. */
  steps?: AgentStep[];
  /** What the run is busy with while it has nothing to show yet (the one-time check of how the model takes tools): drawn only while the message is being written, gone with the first step. */
  status?: string;
  /** This message is the local, never-saved reply to a slash command (``/status``, ``/context``, ...), not a real answer: shown with its own small header, its footer (copy/regenerate,
   * model and speed) and the "did not read any file" line left out, since neither applies to it. */
  command?: { name: string; ok: boolean } };

/** What an answer cost, in words: "412 tokens, 38.2 tokens a second, 10.8 s (prompt 1,204 tokens)"; leaves out what is not known. */
export function usageWords(u: ChatUsage | null | undefined): string {
  if (!u) return "";
  const parts: string[] = [];
  if (u.completion_tokens != null) parts.push(`${num(u.completion_tokens)} ${u.completion_tokens === 1 ? "token" : "tokens"}`);
  if (u.tokens_per_second != null && u.tokens_per_second > 0) parts.push(`${u.tokens_per_second.toFixed(1)} tokens a second`);
  if (u.seconds != null && u.seconds > 0) parts.push(`${u.seconds < 10 ? u.seconds.toFixed(1) : Math.round(u.seconds)} s`);
  const prompt = u.prompt_tokens != null ? `prompt ${num(u.prompt_tokens)} ${u.prompt_tokens === 1 ? "token" : "tokens"}` : "";
  return [parts.join(", "), prompt && `(${prompt})`].filter(Boolean).join(" ");
}

function useCopy(text: string) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current != null) window.clearTimeout(timer.current); }, []);
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); }
    catch { return; }                                                          // ignored: a window that refuses the clipboard shows no "Copied", so nobody is told something false
    setCopied(true);
    if (timer.current != null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1800);
  };
  return { copied, copy };
}

function CopyButton({ text, what }: { text: string; what: string }) {
  const { copied, copy } = useCopy(text);
  return (
    <>
      <IconButton label={copied ? "Copied" : `Copy ${what}`} size="sm" onClick={copy}>{copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}</IconButton>
      <span role="status" className="sr-only">{copied ? "Copied to the clipboard" : ""}</span>
    </>
  );
}

function Question({ m, busy, onEdit, editNote }: { m: ViewMessage; busy: boolean; onEdit?: (id: string, text: string) => void; editNote?: string }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(m.content);
  if (editing) {
    return (
      <li className="ml-auto w-full max-w-[85%] space-y-2 border border-brand-line bg-surface-2 p-3">
        <span className="sr-only">Editing your question</span>
        <Textarea aria-label="Your question, edited" value={draft} rows={3} autoFocus onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") { setEditing(false); setDraft(m.content); } }} />
        <p className="text-[0.74rem] text-faint">{editNote ?? "Sending it again removes the answers that came after this question."}</p>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => { setEditing(false); setDraft(m.content); }}>Cancel</Button>
          <Button variant="primary" disabled={!draft.trim() || busy} onClick={() => { setEditing(false); onEdit?.(m.id, draft.trim()); }}>Send again</Button>
        </div>
      </li>
    );
  }
  return (
    <li className="ml-auto max-w-[85%]">
      <div className="border border-line bg-surface-2 px-3 py-2 text-[0.86rem] whitespace-pre-wrap break-words"><span className="sr-only">You: </span>{m.content}</div>
      {m.attachments && m.attachments.length > 0 && (
        <ul aria-label="Attached files" className="mt-1 flex flex-wrap justify-end gap-1">
          {m.attachments.map((a) => <li key={a.path} className="border border-line px-1.5 py-0.5 font-mono text-[0.7rem] text-muted" title={a.cut ? "Only the start of this file was given to the model" : "The whole file was given to the model"}>{a.path}{a.cut ? " (start)" : ""}</li>)}
        </ul>)}
      <div className="mt-0.5 flex justify-end gap-0.5">
        <CopyButton text={m.content} what="your question" />
        {onEdit && <IconButton label="Edit and send again" size="sm" disabled={busy} onClick={() => { setDraft(m.content); setEditing(true); }}><Pencil size={14} aria-hidden /></IconButton>}
      </div>
    </li>
  );
}

function Answer({ m, last, busy, onRegenerate, onFork }: { m: ViewMessage; last: boolean; busy: boolean; onRegenerate?: () => void; onFork?: (id: string) => void }) {
  const writing = !!m.streaming && !m.error;
  const speed = usageWords(m.usage);
  const modelLabel = m.model_label ?? m.model;
  const isCommand = !!m.command;
  return (
    <li className="max-w-[94%] space-y-1.5">
      <span className="sr-only">Assistant: </span>
      {isCommand && <p className="font-mono text-[0.74rem] text-faint">/{m.command!.name}</p>}
      {m.steps ? <AgentActivity steps={m.steps} streaming={writing} thinking={m.thinking} /> : (
        <>
          {m.thinking && (
            <details open={writing && !m.content} className="border border-line bg-surface-2 text-[0.78rem]">
              <summary className="cursor-pointer px-3 py-1.5 text-muted hover:text-fg">{writing && !m.content ? "Thinking..." : "Thinking"}</summary>
              <p className="max-h-60 overflow-y-auto whitespace-pre-wrap break-words border-t border-line px-3 py-2 text-muted">{m.thinking}</p>
            </details>)}
          {m.tools && m.tools.length > 0 && (
            <ul aria-label="What the assistant looked at" className="flex flex-wrap gap-1.5 text-[0.74rem]">
              {m.tools.map((t, i) => (
                <li key={i} className={`max-w-full break-words border px-2 py-0.5 ${t.ok ? "border-line text-muted" : "border-warn-line text-warn"}`}>{t.ok ? "Looked at" : "Could not"}: {t.summary}</li>))}
            </ul>)}
        </>)}
      {m.notices?.map((n, i) => <p key={i} role="status" className="text-[0.76rem] text-warn">{n}</p>)}
      {m.status && writing && <p role="status" className="text-[0.76rem] text-muted">{m.status}</p>}
      {!m.steps && (m.content ? <Markdown text={m.content} /> : writing && !m.thinking ? <p className="text-[0.8rem] text-muted">Writing...</p> : null)}
      {m.error && <p role="alert" className="text-[0.8rem] text-crit-soft">{m.error}</p>}
      {m.stopped && <p className="text-[0.76rem] text-faint">Stopped. What was written so far is kept.</p>}
      {!isCommand && m.sources && m.sources.length > 0 && <p className="text-[0.74rem] text-faint">A model can still misread what it reads; check the lines it names.</p>}
      {!isCommand && m.sources && m.sources.length === 0 && !m.streaming && !m.error && !m.stopped && <p className="text-[0.74rem] text-faint">The assistant did not read any file of the project for this answer.</p>}
      {!isCommand && !writing && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <div className="flex gap-0.5">
            {m.content && <CopyButton text={m.content} what="the answer" />}
            {last && onRegenerate && <IconButton label="Regenerate the answer" size="sm" disabled={busy} onClick={onRegenerate}><RefreshCw size={14} aria-hidden /></IconButton>}
            {onFork && <IconButton label="Fork a new session from here" size="sm" disabled={busy} onClick={() => onFork(m.id)}><GitFork size={14} aria-hidden /></IconButton>}
          </div>
          {(speed || modelLabel) && (
            <span className="flex min-w-0 items-center text-[0.72rem] text-faint">
              {modelLabel && <span className="min-w-0 max-w-[14rem] truncate" title={modelLabel}>{modelLabel}</span>}
              {modelLabel && speed && ", "}
              {speed && <span className="whitespace-nowrap">{speed}</span>}
            </span>)}
        </div>)}
    </li>
  );
}

export function MessageView({ message, last = false, busy = false, onRegenerate, onEdit, editNote, onFork }: { message: ViewMessage; last?: boolean; busy?: boolean; onRegenerate?: () => void;
  onEdit?: (id: string, text: string) => void; editNote?: string;
  /** Offered on an agent answer: starts a new session from the conversation up to the end of this turn. */
  onFork?: (id: string) => void }) {
  return message.role === "user"
    ? <Question m={message} busy={busy} onEdit={onEdit} editNote={editNote} />
    : <Answer m={message} last={last} busy={busy} onRegenerate={onRegenerate} onFork={onFork} />;
}
