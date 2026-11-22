import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowDownToLine, Check, Copy, SquareTerminal } from "lucide-react";
import type { JobProgress } from "../lib/api";
import type { LogLine, Tone } from "../lib/logBuffer";
import type { Hue, Run } from "../lib/terminal";
import { bytes, clockSpan, num } from "../lib/format";

// The output of a job, drawn like a terminal: a redraw overwrites its row, colours are kept, nothing is printed that is only noise (lib/terminal.ts decides that). It scrolls only its own
// box: to the newest row while the reader is at the end of it, and not at all once they scrolled up (a button brings them back). Older rows can be loaded on top without the row the
// reader was looking at moving. The rows are not announced as they arrive (a download writes several a second): the progress above the terminal is what tells a person where the job is.

const TONE_CLASS: Record<Tone, string> = { error: "text-crit", warn: "text-warn", ok: "text-ok", plain: "text-muted" };
const HUE_CLASS: Record<Hue, string> = { black: "text-faint", red: "text-crit", green: "text-ok", yellow: "text-warn", blue: "text-info", magenta: "text-brand-strong", cyan: "text-info", white: "text-fg" };
const STATE: Record<string, [string, string]> = {
  running: ["running", "text-brand-strong"], done: ["finished", "text-ok"], error: ["failed", "text-crit-soft"], cancelled: ["stopped", "text-muted"], interrupted: ["cut off", "text-warn"],
};

function runClass(run: Run, tone: Tone): string {
  const colour = run.fg ? HUE_CLASS[run.fg] : run.dim ? "text-faint" : TONE_CLASS[tone];
  return run.bold ? `${colour} font-semibold` : colour;
}

/** One output row. Memoised: a row that was already drawn is not drawn again when new ones arrive; `runs` is the same object until the row's content changes. */
export const LogRow = memo(function LogRow({ text, tone, runs }: { text: string; tone: Tone; runs?: readonly Run[] }) {
  if (!runs || runs.length === 0) return <div className={`whitespace-pre-wrap break-words ${TONE_CLASS[tone]}`}>{text || " "}</div>;
  return (
    <div className={`whitespace-pre-wrap break-words ${TONE_CLASS[tone]}`}>
      {runs.map((run, i) => <span key={i} className={runClass(run, tone)}>{run.text}</span>)}
    </div>
  );
});

/** The line under the terminal while a transfer runs: `62%  4.0 MB of 6.5 MB  2.4 MB/s  0:01 left` (empty for a job that does not report bytes). */
export function statusLine(p: JobProgress | null | undefined): string {
  if (!p || p.unit !== "bytes" || !p.total || p.processed == null) return "";
  const parts = [`${Math.min(100, Math.floor((100 * p.processed) / p.total))}%`, `${bytes(p.processed)} of ${bytes(p.total)}`];
  if (p.rate && p.rate > 0) parts.push(`${bytes(p.rate)}/s`);
  if (p.eta != null && p.eta >= 0 && p.processed < p.total) parts.push(`${clockSpan(p.eta)} left`);
  return parts.join("  ");
}

const small = "inline-flex items-center gap-1.5 border border-line bg-surface-2 px-2 py-0.5 text-[0.7rem] text-muted hover:border-line-strong hover:text-fg";

export function JobTerminal({ title, rows, earlier, onLoadEarlier, status, progress, copyText, empty = "waiting for output...", none = "no output", ariaLabel, maxHeight = "max-h-72" }: {
  /** The readable name of what runs (the stage's name). */
  title: string;
  rows: readonly LogLine[];
  /** How many older rows are kept but not in `rows`. */
  earlier: number;
  onLoadEarlier: () => void;
  /** The job's state, for the word in the header and the status line. */
  status?: string;
  progress?: JobProgress | null;
  /** The text Copy puts on the clipboard (every kept row); the rows on screen when not given. */
  copyText?: () => string;
  /** What is said while the job runs and has written nothing yet. */
  empty?: string;
  /** What is said when the job is over and wrote nothing. */
  none?: string;
  /** The name of the log for assistive technology (the title of the job by default). */
  ariaLabel?: string;
  maxHeight?: string;
}) {
  const body = useRef<HTMLDivElement>(null);
  const following = useRef(true);                       // the reader is at the end of the output (or has asked to stay there)
  const anchor = useRef<number | null>(null);           // the scroll height before older rows were added on top
  const [follow, setFollow] = useState(true);           // the same, for what is drawn
  const [copied, setCopied] = useState<"" | "yes" | "no">("");
  const running = status === "running";

  useLayoutEffect(() => {
    const el = body.current;
    if (!el) return;
    if (anchor.current != null) { el.scrollTop += el.scrollHeight - anchor.current; anchor.current = null; }
    else if (following.current) el.scrollTop = el.scrollHeight;
  }, [rows]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(""), 2500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const setFollowing = (on: boolean) => { following.current = on; setFollow(on); };
  const onScroll = () => {
    const el = body.current;
    if (!el) return;
    const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (atEnd !== following.current) setFollowing(atEnd);
  };
  const toEnd = () => {
    setFollowing(true);
    const el = body.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  const loadEarlier = () => { anchor.current = body.current?.scrollHeight ?? null; onLoadEarlier(); };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText ? copyText() : rows.map((r) => r.text).join("\n"));
      setCopied("yes");
    } catch {
      setCopied("no");                                  // ignored: a clipboard that is refused is said so beside the button; nothing else depends on it
    }
  };

  const [word, tone] = STATE[status ?? ""] ?? ["", ""];
  const line = statusLine(progress);                    // only drawn while the job runs
  const lineCount = rows.length + earlier;
  return (
    <section className="hud-frame relative mt-3 border border-line bg-terminal">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-1.5">
        <SquareTerminal size={14} className="shrink-0 text-faint" aria-hidden />
        <span className="caps text-[0.68rem] text-faint">Output</span>
        <span className="font-mono text-[0.72rem] text-muted">{title}</span>
        {word && <span className={`caps text-[0.66rem] ${tone}`}>{word}</span>}
        <span className="tnum ml-auto font-mono text-[0.7rem] text-faint">{num(lineCount)} {lineCount === 1 ? "line" : "lines"}</span>
        <button type="button" onClick={copy} className={small} aria-label={`Copy the output of ${title}`}>
          {copied === "yes" ? <Check size={12} aria-hidden /> : <Copy size={12} aria-hidden />}{copied === "yes" ? "Copied" : copied === "no" ? "Not copied" : "Copy"}
        </button>
        <button type="button" onClick={() => (follow ? setFollowing(false) : toEnd())} aria-pressed={follow} className={small}>Follow</button>
      </header>
      <div ref={body} onScroll={onScroll} role="log" aria-label={ariaLabel ?? `Output of ${title}`} aria-live="off" tabIndex={0}
        className={`${maxHeight} overflow-auto p-3 font-mono text-[0.76rem] leading-[1.55] tnum outline-none focus-visible:ring-1 focus-visible:ring-brand-strong`}>
        {earlier > 0 && (
          <button type="button" onClick={loadEarlier} className="mb-2 block text-[0.72rem] text-brand-strong hover:underline">Load earlier lines ({num(earlier)} more)</button>
        )}
        {rows.length === 0 && <span className="text-faint">{running ? empty : none}</span>}
        {rows.map((r) => <LogRow key={r.id} text={r.text} tone={r.tone} runs={r.runs} />)}
      </div>
      {!follow && rows.length > 0 && (
        <button type="button" onClick={toEnd} className={`${small} absolute right-3 shadow-card ${running ? "bottom-11" : "bottom-3"}`}>
          <ArrowDownToLine size={12} aria-hidden />Jump to latest
        </button>
      )}
      {running && (
        <div className="flex items-center gap-2 border-t border-line px-3 py-1.5 font-mono text-[0.72rem] text-muted tnum">
          {line && <span className="whitespace-pre">{line}</span>}
          <span aria-hidden className="inline-block h-[0.95em] w-[0.5em] bg-fg opacity-80" />
        </div>
      )}
    </section>
  );
}
