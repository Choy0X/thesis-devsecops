import { useEffect, useState } from "react";
import type { JobProgress } from "../lib/api";
import { amountOf, bytes, clockSpan, num } from "../lib/format";
import { Progress } from "./hud/Progress";

// Where a job is, in numbers a person can read: a title, which step of several, how much of how much, how fast and how long to go, a wait with its countdown, counts, a quota. The same strip
// for every stage; a download adds the files done and the file being fetched. The title is the only part announced to assistive technology (it changes when the job moves to another
// step, not several times a second); the bar says its percentage in words.

function useNow(active: boolean) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

const clock = (epoch: number) => new Date(epoch * 1000).toLocaleTimeString();
const span = clockSpan;

/** `bare` leaves out the frame and the margin, for a strip that sits inside a frame of its own (the panel of a download). */
export function ProgressStrip({ p, live, bare = false }: { p: JobProgress; live: boolean; bare?: boolean }) {
  const waiting = live && !!p.until && (p.state === "pause" || p.state === "rate_limited");
  const now = useNow(live);
  // A number that stops changing is easy to mistake for a crash. Said in words only once it has sat still a while (a healthy job's numbers move too fast for this to show), so a real
  // wait is never silently indistinguishable from a stuck one.
  const staleSeconds = live && typeof p.ts === "number" ? Math.max(0, Math.round(now - p.ts)) : null;
  const stale = staleSeconds != null && staleSeconds >= 15 ? `last moved ${span(staleSeconds)} ago` : "";
  const pct = p.total ? Math.min(100, (100 * (p.processed || 0)) / p.total) : null;
  const title = p.label
    || (p.state === "mining" && p.batch ? `Batch ${p.batch}${p.batches ? ` of ${num(p.batches)}` : ""}` : "")
    || (p.state === "pause" ? "Waiting for the next batch" : "")
    || (p.state === "rate_limited" ? "GitHub rate limit reached" : "")
    || (p.state === "done" ? "Finished" : p.state || "");
  const limited = p.state === "rate_limited";
  const stepOf = p.steps && p.steps > 1 && p.step ? `Step ${p.step} of ${p.steps}` : "";
  const speed = live && p.rate && p.rate > 0 ? (p.unit === "bytes" ? `${bytes(p.rate)}/s` : `${num(Math.round(p.rate))}/s`) : "";
  const left = live && p.eta != null && p.eta >= 0 && p.total ? `${span(p.eta)} left` : "";
  const files = p.files_total ? `${num(p.files ?? 0)} of ${num(p.files_total)} ${p.files_total === 1 ? "file" : "files"}` : "";
  const there = p.resumed_from ? `continued from ${amountOf(p.resumed_from, p.unit)}` : "";

  return (
    <div className={bare ? "text-[0.8rem]" : `mt-3 border px-3.5 py-3 text-[0.8rem] ${limited && live ? "border-warn-line bg-warn-tint" : "border-line bg-surface-2"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span role="status" className={`font-medium ${limited && live ? "text-warn" : ""}`}>{stepOf && <span className="mr-2 font-normal text-faint">{stepOf}</span>}{title}</span>
        {p.total && p.unit === "percent" ? <span className="tnum text-muted">{(pct ?? 0).toFixed(0)}%</span>
          : p.total ? <span className="tnum text-muted">{amountOf(p.processed || 0, p.unit)} / {amountOf(p.total, p.unit)}
          {pct !== null && <span className="text-faint"> · {pct.toFixed(1)}%</span>}</span>
          : p.unit && p.processed ? <span className="tnum text-muted">{amountOf(p.processed, p.unit)} so far</span> : null}
      </div>
      {(speed || left) && <div className="mt-1 tnum text-[0.74rem] text-muted">{[speed, left].filter(Boolean).join(" · ")}</div>}
      {stale && <div className={`mt-1 tnum text-[0.74rem] ${staleSeconds! >= 120 ? "text-warn" : "text-muted"}`}>{stale}</div>}
      {(files || p.current || there) && (
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 tnum text-[0.74rem] text-muted">
          {files && <span>{files}</span>}
          {p.current && <span className="break-all font-mono text-[0.72rem]">{p.current}</span>}
          {there && <span className="text-faint">{there}</span>}
        </div>
      )}
      {pct !== null && <Progress className="mt-2" value={p.processed || 0} max={p.total} segments={40} label={title || "Progress"} />}
      {waiting && p.until && (
        <div className={`mt-2 tnum ${limited ? "text-warn" : "text-muted"}`}>
          {limited ? "Resumes" : "Next batch"} at {clock(p.until)} · in <span className="font-medium">{span(p.until - now)}</span>
          {p.reason && <span className="text-faint"> · {p.reason}</span>}
        </div>
      )}
      {(p.counts || p.quota) && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.74rem] text-muted tnum">
          {Object.entries(p.counts || {}).map(([k, v]) => <span key={k}>{k.replace(/_/g, " ")}: {num(v)}</span>)}
          {p.quota && <span className="text-faint">GitHub API quota: {num(p.quota.remaining)} / {num(p.quota.limit)}
            {p.quota.reset ? ` · resets ${clock(p.quota.reset)}` : ""}</span>}
        </div>
      )}
    </div>
  );
}
