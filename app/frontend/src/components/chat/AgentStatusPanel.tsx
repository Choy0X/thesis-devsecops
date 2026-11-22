import { useEffect, useState } from "react";
import { api, type AgentDiffFile, type AgentMode, type AgentSession, type AgentUsageTotal, type EngineRun } from "../../lib/api";
import { cn } from "../../lib/cn";
import { Badge } from "../hud/Badge";
import { ChangesPanel } from "./ChangesPanel";
import { citedIn, sourcesOf } from "./agentSteps";
import type { ViewMessage } from "./MessageView";
import { TaskPanel } from "./TaskPanel";

// Agentic Mode's default right panel: not Settings (that's a dialog now, opened on purpose), but what the session is actually doing -- the sandbox's changes so far, the
// instruction it's working, its task list and the pages it found on the web. Sources are only the outside pages (the files it read and the commands it ran are in the steps of the thread),
// each with the number its answer cites. Each section is left out, not shown empty, when it has nothing to say yet, and the panel renders nothing at all (no header, no placeholder) on a
// session with nothing yet to report -- it grows into itself as the run produces things to show.

const KIND_TONE: Record<AgentDiffFile["kind"], string> = { added: "border-diff-add text-diff-add", changed: "border-warn-line text-warn", deleted: "border-diff-del text-diff-del" };

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5 border-t border-line pt-3 first:border-t-0 first:pt-0">
      <h2 className="text-[0.7rem] font-semibold uppercase tracking-wider text-faint">{title}</h2>
      {children}
    </div>
  );
}

const count = (n: number): string => n.toLocaleString("en-US");

/** What the session has cost, in words: "3,210 tokens written in 1 min 12 s over 14 rounds. The newest request was 5,120 tokens." Empty when no round has reported any. */
export function usageTotalWords(u: AgentUsageTotal | undefined): string {
  if (!u || !u.rounds) return "";
  const written = u.completion_tokens ?? 0;
  const seconds = u.seconds ?? 0;
  const time = seconds >= 60 ? `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s` : seconds >= 10 ? `${Math.round(seconds)} s` : seconds > 0 ? `${seconds.toFixed(1)} s` : "";
  const first = `${count(written)} ${written === 1 ? "token" : "tokens"} written${time ? ` in ${time}` : ""} over ${u.rounds} ${u.rounds === 1 ? "round" : "rounds"}.`;
  const speed = seconds > 0 && written > 0 ? ` About ${(written / seconds).toFixed(1)} tokens a second.` : "";
  const last = u.last_prompt_tokens ? ` The newest request was ${count(u.last_prompt_tokens)} tokens.` : "";
  return first + speed + last;
}

/** The newest run of a v2 session (why it stopped, what it has used), read when the Task tab is open and again whenever the session changes. Nothing when it cannot be read. */
function useLatestRun(session: AgentSession | null, active: boolean): EngineRun | null {
  const [run, setRun] = useState<EngineRun | null>(null);
  const key = session ? `${session.project_id}/${session.id}/${session.updated_at}/${session.status}` : "";
  useEffect(() => {
    if (!active || !session) return undefined;
    let stale = false;
    api.agentRuns(session.project_id, session.id).then((r) => { if (!stale) setRun(r.runs[0] ?? null); }).catch(() => { if (!stale) setRun(null); });
    return () => { stale = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the key says when the session changed
  }, [key, active]);
  return run;
}

function Tabs({ view, onView }: { view: "session" | "task"; onView: (v: "session" | "task") => void }) {
  const tab = (id: "session" | "task", label: string) => (
    <button type="button" role="tab" id={`agent-tab-${id}`} aria-selected={view === id} onClick={() => onView(id)}
      className={cn("border-b-2 px-2 py-1 text-[0.76rem]", view === id ? "border-brand-strong font-medium text-fg" : "border-transparent text-muted hover:text-fg")}>{label}</button>
  );
  return <div role="tablist" aria-label="Session or task" className="mb-3 flex border-b border-line">{tab("session", "Session")}{tab("task", "Task")}</div>;
}

export function AgentStatusPanel({ session, mode, messages, diff, busy = false, onChanged }: { session: AgentSession | null; mode: AgentMode; messages: ViewMessage[]; diff: AgentDiffFile[] | null;
  /** A run is in progress: what resets or rewrites the sandbox waits for it. */
  busy?: boolean;
  /** Called after the review changed something (an apply, a discard, a refresh, a rollback), so the list of changes is read again. */
  onChanged?: () => void }) {
  const hasTask = !!session?.task;
  const [view, setView] = useState<"session" | "task">("session");
  const run = useLatestRun(session, hasTask && view === "task");
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const sources = sourcesOf(messages);
  const cited = citedIn(messages);
  const showChanges = mode !== "plan" && diff && diff.length > 0;
  const showWorkingOn = lastUser || session?.instructions;
  const showSubagents = session && session.task_list.length > 0;
  const showSources = sources.length > 0;
  const spent = session ? usageTotalWords(session.usage_total) : "";
  if (!hasTask && !showChanges && !showWorkingOn && !showSubagents && !showSources && !spent) return null;
  if (hasTask && view === "task") return <div><Tabs view={view} onView={setView} /><div role="tabpanel" aria-labelledby="agent-tab-task"><TaskPanel task={session!.task!} run={run} /></div></div>;
  return (
    <div className="space-y-3">
      {hasTask && <Tabs view={view} onView={setView} />}
      {showChanges && (
        <Section title="Changes">
          {session ? <ChangesPanel projectId={session.project_id} files={diff} busy={busy} onChanged={onChanged ?? (() => undefined)} /> : (
            <ul className="space-y-1 text-[0.76rem]">
              {diff.map((f) => (
                <li key={f.file} className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate" title={f.rel}>{f.rel}</span>
                  <Badge className={KIND_TONE[f.kind]}>{f.kind}</Badge>
                </li>
              ))}
            </ul>)}
        </Section>
      )}
      {(lastUser || session?.instructions) && (
        <Section title="Working on">
          {session?.instructions && <p className="text-[0.76rem] text-muted">Instructions: {session.instructions}</p>}
          {lastUser && <p className="mt-1 text-[0.78rem]">{lastUser.content}</p>}
        </Section>
      )}
      {spent && (
        <Section title="Usage">
          <p className="text-[0.76rem] text-muted">{spent}</p>
        </Section>
      )}
      {session && session.task_list.length > 0 && (
        <Section title="Subagents">
          <ul aria-label="Task list" className="space-y-1 text-[0.78rem]">
            {session.task_list.map((t, i) => <li key={i} className={t.done ? "text-muted line-through" : ""}>{t.done ? "Done" : "Active"}: {t.text}</li>)}
          </ul>
        </Section>
      )}
      {sources.length > 0 && (
        <Section title="Sources">
          <ol aria-label="Sources" className="space-y-1.5">
            {sources.map((s) => (
              <li key={s.no} className="border border-line bg-surface-2 p-2 text-[0.74rem]">
                <div className="flex items-start gap-2">
                  <span aria-label={`Source ${s.no}`} className="mt-0.5 shrink-0 border border-line px-1 font-mono text-[0.68rem] text-muted">{s.no}</span>
                  <div className="min-w-0">
                    <a href={s.url} target="_blank" rel="noopener noreferrer" className="break-words font-medium text-brand-strong underline hover:text-fg">{s.title}</a>
                    <p className="truncate text-faint">{[s.domain, s.kind === "page" ? "read" : "", cited.has(s.no) ? "cited in the answer" : ""].filter(Boolean).join(" - ")}</p>
                    {s.snippet && <p className="mt-0.5 line-clamp-2 text-muted">{s.snippet}</p>}
                  </div>
                </div>
              </li>))}
          </ol>
        </Section>
      )}
    </div>
  );
}
