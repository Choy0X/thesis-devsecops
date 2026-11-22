import { Bot, Check, FilePlus, FileText, Folder, FolderSearch, GitBranch, Globe, ListChecks, Network, Pencil, Plug, Search, ShieldCheck, Terminal, Wrench, X } from "lucide-react";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Button } from "../kit/Button";
import { Spinner } from "../kit/Spinner";
import { CodeViewer } from "../code/CodeViewer";
import { Markdown } from "./Markdown";
import { argSummary, callText, editOf, fileViewOf, isMcp, linkCitations, previewOf, sourcesOfSteps, tasksOf, terminalOf, toolLabel, type AgentStep, type WebSource } from "./agentSteps";

// The work of an agent turn, step by step, the way Claude Code draws it: what the agent said before a step in a quieter voice, each tool call as a card (the call as the agent wrote it, its
// status, and what came back, open from the start; a long output shows its first lines and a button for the rest), the task list it keeps as a checklist, an edit as the old and the new text,
// and the last text as the answer. Nothing here is trusted markup: text goes through Markdown, results are shown as plain text.

/** Whether the cards of a thread are folded to their header line (the "Collapse all outputs" switch of the session). A card the reader opens or closes by hand follows the switch the next time it changes. */
export const CollapseOutputs = createContext(false);

/** Where the whole output of a cut command can be read from (the session it belongs to): ``(handle, start, lines) => the text``. None where the page has no session to ask. */
export type LoadOutput = (handle: string, start: number, lines: number) => Promise<{ text: string; more: number; total_lines: number }>;
export const OutputSource = createContext<LoadOutput | null>(null);
/** The pages of the whole session (not only this turn): an answer cites them by number, and a [n] becomes a link to the page. Without it a turn uses the pages of its own steps. */
export const SourcesContext = createContext<WebSource[] | null>(null);
/** Where the steps a subagent took are read from when the thread is shown again: ``(its session id) => its steps``. None where the page has no session to ask. */
export type LoadChild = (id: string) => Promise<AgentStep[]>;
export const ChildSource = createContext<LoadChild | null>(null);
const PART = 400;
const MOST = 6_000;

const CLIP_LINES = 20;
const CLIP_CHARS = 2_000;
const NL = String.fromCharCode(10);

type ToolStep = Extract<AgentStep, { kind: "tool" }>;

const ICONS: Record<string, ReactNode> = {
  list_files: <Folder size={14} aria-hidden />, find_files: <FolderSearch size={14} aria-hidden />, read_file: <FileText size={14} aria-hidden />, search_code: <Search size={14} aria-hidden />,
  project_graph: <Network size={14} aria-hidden />, scan_results: <ShieldCheck size={14} aria-hidden />, rescan_code: <ShieldCheck size={14} aria-hidden />,
  rescan_dependencies: <ShieldCheck size={14} aria-hidden />, write_file: <FilePlus size={14} aria-hidden />, edit_file: <Pencil size={14} aria-hidden />, run_command: <Terminal size={14} aria-hidden />,
  bash: <Terminal size={14} aria-hidden />, grep: <Search size={14} aria-hidden />, glob: <FolderSearch size={14} aria-hidden />, recall: <FileText size={14} aria-hidden />,
  git_status: <GitBranch size={14} aria-hidden />, git_log: <GitBranch size={14} aria-hidden />, checkpoint: <GitBranch size={14} aria-hidden />, update_task_list: <ListChecks size={14} aria-hidden />,
  websearch: <Globe size={14} aria-hidden />, webfetch: <Globe size={14} aria-hidden />, task: <Bot size={14} aria-hidden />,
};

/** What a subagent did for a task, nested under the task's card: every step and command of its own, with the output, the way the thread shows its own; then, below, what it answered. While it works the steps come
 * with the run; once the task is over they are read from the subagent's session. */
function SubagentSteps({ step }: { step: ToolStep }) {
  const load = useContext(ChildSource);
  const [fetched, setFetched] = useState<AgentStep[] | null>(null);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    if (step.children || !step.child || !load || step.state === "running") return;
    let live = true;
    load(step.child).then((s) => { if (live) setFetched(s); }).catch((e) => { if (live) setProblem(e instanceof Error ? e.message : "The subagent's steps could not be read."); });
    return () => { live = false; };
  }, [step.child, step.children, step.state]);                         // eslint-disable-line react-hooks/exhaustive-deps; ignored: read once for this task
  const steps = step.children ?? fetched;
  if (problem) return <p role="alert" className="text-warn">{problem}</p>;
  if (!steps) return step.child && step.state !== "running" ? <p className="text-faint">Reading what the subagent did...</p> : null;
  if (steps.length === 0) return null;
  return (
    <div aria-label="What the subagent did" className="border-l-2 border-line pl-3">
      <AgentActivity steps={steps} streaming={step.state === "running"} />
    </div>
  );
}

const SAFE_HTTP = /^https?:\/\//i;

/** What a web search found, one entry for each page: its number (what an answer cites), the title as a link out, the site and the excerpt. A result is never opened from here: the link goes to the page itself. */
function WebResults({ step }: { step: ToolStep }) {
  const items = sourcesOfSteps([step]);
  if (items.length === 0) return null;
  return (
    <ol aria-label="Search results" className="space-y-1.5">
      {items.map((r) => (
        <li key={r.no} className="flex items-start gap-2 text-[0.76rem]">
          <span aria-label={`Source ${r.no}`} className="mt-0.5 shrink-0 border border-line px-1 font-mono text-[0.68rem] text-muted">{r.no}</span>
          <span className="min-w-0">
            {SAFE_HTTP.test(r.url) ? <a href={r.url} target="_blank" rel="noopener noreferrer" className="break-words text-brand-strong underline hover:text-fg">{r.title}</a> : <span>{r.title}</span>}
            <span className="block truncate text-faint">{r.domain}</span>
            {r.snippet && <span className="block text-muted">{r.snippet}</span>}
          </span>
        </li>))}
    </ol>
  );
}

function State({ step }: { step: ToolStep }) {
  if (step.state === "running") return <span className="flex shrink-0 items-center gap-1 text-faint"><Spinner size={12} />Working</span>;
  if (step.state === "waiting") return <span className="shrink-0 text-warn">Waiting for you</span>;
  if (step.state === "failed") return <span className="flex shrink-0 items-center gap-1 text-warn"><X size={12} aria-hidden />Failed</span>;
  return <span className="flex shrink-0 items-center gap-1 text-faint"><Check size={12} aria-hidden /><span className="sr-only">Done</span></span>;
}

export function EditDiff({ path, old, next }: { path: string; old: string; next: string }) {
  return (
    <div className="font-mono text-[0.74rem]">
      {path && <p className="mb-1 text-muted">{path}</p>}
      <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words border border-diff-del bg-surface px-2 py-1 text-diff-del"><span className="sr-only">Removed: </span>{old.split("\n").map((l) => `- ${l}`).join("\n")}</pre>
      <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words border border-diff-add bg-surface px-2 py-1 text-diff-add"><span className="sr-only">Added: </span>{next.split("\n").map((l) => `+ ${l}`).join("\n")}</pre>
    </div>
  );
}

function TaskChecklist({ args }: { args: string }) {
  const tasks = tasksOf(args);
  if (tasks.length === 0) return null;
  return (
    <ul aria-label="Task list" className="space-y-0.5 border border-line bg-surface-2 px-3 py-2 text-[0.8rem]">
      {tasks.map((t, i) => (
        <li key={i} className={`flex items-start gap-2 ${t.done ? "text-muted line-through" : ""}`}>
          <span aria-hidden className={`mt-[0.2rem] flex h-3.5 w-3.5 shrink-0 items-center justify-center border ${t.done ? "border-brand-line text-fg" : "border-line"}`}>{t.done && <Check size={10} />}</span>
          <span className="sr-only">{t.done ? "Done: " : "To do: "}</span>{t.text}
        </li>))}
    </ul>
  );
}

/** What a call returned, as text: the first lines of it, and a button for all of it when there is more. */
function Output({ text, failed, recovery, total, live }: { text: string; failed: boolean; recovery?: string | null; total?: number | null; live?: boolean }) {
  const [all, setAll] = useState(false);
  const load = useContext(OutputSource);
  const [whole, setWhole] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState("");
  const loadAll = async () => {
    if (!load || !recovery) return;
    setLoading(true); setProblem("");
    try {
      const parts: string[] = [];
      let start = 1, more = 1;
      while (more > 0 && start <= MOST) { const got = await load(recovery, start, PART); if (!got.text) break; parts.push(got.text); more = got.more; start += PART; }
      setWhole(parts.join(NL) + (more > 0 ? `${NL}... ${more} more lines; ask for them with recall(${recovery}).` : ""));
    } catch (e) { setProblem(e instanceof Error ? e.message : "The rest could not be read."); } finally { setLoading(false); }
  };
  const shown = whole ?? text;
  const lines = shown.split(NL);
  const long = lines.length > CLIP_LINES || shown.length > CLIP_CHARS;
  let head = (live ? lines.slice(-CLIP_LINES) : lines.slice(0, CLIP_LINES)).join(NL);                      // a command that is still running shows its last lines: they are the new ones
  if (head.length > CLIP_CHARS) { const cut = head.slice(0, CLIP_CHARS); const at = cut.lastIndexOf(NL); head = at > CLIP_CHARS / 2 ? cut.slice(0, at) : cut; }
  return (
    <div className="space-y-1">
      <pre role="log" aria-label={live ? "Output so far" : "Output"} tabIndex={0}
        className={`max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[0.74rem] focus-visible:outline focus-visible:outline-1 focus-visible:outline-brand-strong ${failed ? "text-warn" : "text-muted"}`}>{all || !long ? shown : head}</pre>
      <div className="flex flex-wrap items-center gap-2">
        {long && <Button size="sm" variant="ghost" onClick={() => setAll(!all)}>{all ? "Show fewer lines" : `Show all ${lines.length} lines`}</Button>}
        {recovery && load && whole === null && <Button size="sm" variant="ghost" loading={loading} onClick={() => void loadAll()}>{total ? `Load all ${total.toLocaleString()} lines` : "Load all lines"}</Button>}
        {problem && <span role="alert" className="text-warn">{problem}</span>}
      </div>
    </div>
  );
}

function ToolBlock({ step }: { step: ToolStep }) {
  const collapsed = useContext(CollapseOutputs);
  const [open, setOpen] = useState(!collapsed);
  useEffect(() => setOpen(!collapsed), [collapsed]);
  if (step.name === "update_task_list" && step.state !== "failed") return <li><TaskChecklist args={step.args} /></li>;
  const edit = step.name === "edit_file" ? editOf(step.args) : null;
  const file = fileViewOf(step);
  const arg = argSummary(step.name, step.args);
  const body = step.state === "running" || step.state === "waiting" ? "" : previewOf(step);
  const live = step.state === "running" ? (step.live ?? "") : "";
  const web = step.name === "websearch" && step.state === "ok" && sourcesOfSteps([step]).length > 0;
  const term = step.state === "ok" || step.state === "failed" ? terminalOf(step) : null;
  const ran = term?.ranAs.filter((r) => !r.startsWith("(built in)")) ?? [];
  return (
    <li className={`border text-[0.78rem] ${step.state === "failed" ? "border-warn-line" : "border-line"} bg-surface-2`}>
      <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary className="flex cursor-pointer items-center gap-2 px-2.5 py-1.5 text-muted hover:text-fg">
          <span className="shrink-0">{ICONS[step.name] ?? (isMcp(step.name) ? <Plug size={14} aria-hidden /> : <Wrench size={14} aria-hidden />)}</span>
          <span className="shrink-0 font-medium text-fg">{toolLabel(step.name)}</span>
          {arg && <span className="min-w-0 flex-1 truncate font-mono" title={arg}>{arg}</span>}
          {!arg && <span className="flex-1" />}
          <State step={step} />
        </summary>
        {open && (
          <div className="space-y-1.5 border-t border-line px-3 py-2">
            {!edit && <p className="whitespace-pre-wrap break-words font-mono text-[0.74rem] text-fg"><span className="sr-only">Call: </span>{callText(step.name, step.args)}</p>}
            {ran.length > 0 && <p className="break-words font-mono text-[0.72rem] text-faint">ran as: {ran.join("; ")}</p>}
            {term?.condensed && (
              <p className="text-[0.74rem] text-faint">
                <span className="border border-line px-1.5 py-0.5">rtk left out {term.hidden ? `${term.hidden.toLocaleString()} lines` : "some lines"}</span>{" "}
                {term.hiddenHandle ? <>they come back with recall <code className="font-mono">{term.hiddenHandle}</code></> : "run it again with raw set to true for all of it"}
              </p>)}
            {step.summary && step.summary.trim() !== body.trim() && !web && <p className="text-muted">{step.summary}</p>}
            {step.name === "webfetch" && step.state === "ok" && sourcesOfSteps([step])[0] && <WebResults step={step} />}
            {step.name === "task" && <SubagentSteps step={step} />}
            {edit ? <EditDiff path={edit.path} old={edit.old} next={edit.new} />
              : web ? <WebResults step={step} />
              : file ? <CodeViewer code={file.code} path={file.path} startLine={file.startLine} label={`${file.path}, from line ${file.startLine}`} fallback={<Output text={body} failed={false} />} />
              : live ? <Output text={live} failed={false} live />
              : body ? <Output text={body} failed={step.state === "failed"} recovery={term?.recovery?.startsWith("out_") ? term.recovery : null} total={term?.lines ?? null} />
              : step.state === "running" ? <p className="text-faint">Working...</p> : null}
          </div>)}
      </details>
    </li>
  );
}

function ThinkBlock({ text, open }: { text: string; open: boolean }) {
  return (
    <li>
      <details open={open} className="border border-line bg-surface-2 text-[0.78rem]">
        <summary className="cursor-pointer px-3 py-1.5 text-muted hover:text-fg">{open ? "Thinking..." : "Thinking"}</summary>
        <p className="max-h-60 overflow-y-auto whitespace-pre-wrap break-words border-t border-line px-3 py-2 text-muted">{text}</p>
      </details>
    </li>
  );
}

/** The steps of one agent turn. ``thinking`` is the saved reasoning of the final answer (steps carry their own while a run is live). */
export function AgentActivity({ steps, streaming, thinking }: { steps: AgentStep[]; streaming: boolean; thinking?: string }) {
  const lastIndex = steps.length - 1;
  const session = useContext(SourcesContext);
  const known = session ?? sourcesOfSteps(steps);
  return (
    <ol aria-label="What the agent did" className="space-y-1.5">
      {thinking && !steps.some((s) => s.kind === "think") && <ThinkBlock text={thinking} open={false} />}
      {steps.map((s, i) => {
        if (s.kind === "think") return <ThinkBlock key={i} text={s.text} open={streaming && i === lastIndex} />;
        if (s.kind === "tool") return <ToolBlock key={s.id || i} step={s} />;
        const narration = steps[i + 1]?.kind === "tool" || (steps[i + 1] && steps[i + 1].kind !== "text");           // said before a step; the last text is the answer
        return <li key={i} className={narration ? "text-[0.84rem] text-muted" : ""}><Markdown text={narration ? s.text : linkCitations(s.text, known)} /></li>;
      })}
      {streaming && steps.length === 0 && <li className="text-[0.8rem] text-muted">Working...</li>}
    </ol>
  );
}
