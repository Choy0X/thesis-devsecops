import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2, CircleSlash, ListTodo, Loader2 } from "lucide-react";
import type { Job } from "../lib/api";
import { Dev } from "../lib/devMode";
import { refreshJobs, useJobs } from "../lib/jobsList";
import { jobLabel } from "../lib/stageLabels";
import { amountOf } from "../lib/format";
import { Progress } from "./hud/Progress";
import { Sheet } from "./hud/Dialog";
import { JobStrip } from "./JobStrip";
import { useJobAlerts } from "../lib/jobAlerts";
import { AlertsSwitch } from "./AlertsSwitch";
import { api } from "../lib/api";
import { Button } from "./ui";

// The one place to see background work: what is running (a named step, a bar and the time it has taken), what failed or was cut off
// (always shown, with the reason in plain words), and what finished. Technical identifiers and the log are for Developer mode only.

const FAILED = new Set(["error", "interrupted"]);
const elapsed = (from: string, to?: string, now = Date.now()): string => {
  const start = Date.parse(from);
  if (Number.isNaN(start)) return "";
  const s = Math.max(0, Math.round(((to ? Date.parse(to) : now) - start) / 1000));
  return s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
};
/** The first line of an error, which is what a person needs (the rest is a traceback). */
const reason = (job: Job): string => {
  const lines = (job.error || "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  return (lines[lines.length - 1] ?? "").slice(0, 200) || (job.status === "interrupted" ? "The app was closed while it was running." : "It stopped with an error.");
};

function Row({ job, now }: { job: Job; now: number }) {
  const p = job.progress;
  const known = p && typeof p.total === "number" && p.total > 0;
  const download = job.stage === "hf_download" || job.stage === "install_model";          // with a stop that keeps what was fetched (a conversion that is cut leaves nothing that blocks the next try)
  const unit = p?.unit ?? (job.stage === "hf_download" ? "bytes" : undefined);             // bytes and files are shown as sizes and counts with the word
  return (
    <li className="border border-line bg-surface-2 px-3 py-2.5" data-status={job.status}>
      <div className="flex items-center gap-2 text-[0.84rem]">
        {job.status === "running" ? <Loader2 size={14} className="animate-spin text-brand-strong" aria-hidden />
          : FAILED.has(job.status) ? <AlertTriangle size={14} className="text-crit-soft" aria-hidden />
          : job.status === "cancelled" ? <CircleSlash size={14} className="text-faint" aria-hidden />
          : <CheckCircle2 size={14} className="text-ok" aria-hidden />}
        <span className="font-medium">{jobLabel(job)}</span>
        <span className="ml-auto font-mono text-[0.72rem] text-faint">{elapsed(job.started, job.status === "running" ? undefined : job.finished, now)}</span>
      </div>
      {job.status === "running" && (
        <div className="mt-2 space-y-1">
          <p className="text-[0.76rem] text-muted">{p?.label || "Working"}{p?.steps && p.steps > 1 && p.step ? ` · step ${p.step} of ${p.steps}` : ""}{known && p!.unit === "percent" ? ` · ${Math.round(p!.processed ?? 0)}%` : known ? ` · ${amountOf(p!.processed ?? 0, unit)} of ${amountOf(p!.total!, unit)}` : p?.unit && p.processed ? ` · ${amountOf(p.processed, unit)} so far` : ""}</p>
          {known && <Progress value={p!.processed ?? 0} max={p!.total!} segments={20} label={`${jobLabel(job)} progress`} />}
          {download && <Button variant="ghost" aria-label={job.stage === "install_model" ? "Stop the install" : "Stop the download"} onClick={() => void api.cancelJob(job.id).catch(() => undefined) /* ignored: the tray shows the job's real state on its next refresh */}>{job.stage === "install_model" ? "Stop (keeps what is downloaded and converted)" : "Stop (keeps what is downloaded)"}</Button>}
        </div>
      )}
      {FAILED.has(job.status) && <p className="mt-1.5 text-[0.78rem] text-crit-soft">{job.status === "interrupted" ? "Cut off: " : "Failed: "}{reason(job)}</p>}
      {job.status !== "running" && (
        <Button variant="ghost" size="sm" className="mt-1.5" aria-label={`Dismiss ${jobLabel(job)}`}
          onClick={() => void api.forgetJob(job.id).then(refreshJobs).catch(() => undefined) /* ignored: a job somehow running again by the time this reaches the backend is left alone there, and the tray shows that on its next refresh */}>
          Dismiss
        </Button>)}
      <Dev><p className="mt-1 break-all font-mono text-[0.68rem] text-faint">job {job.id} · {job.status}</p></Dev>
    </li>
  );
}

function Group({ title, jobs, now }: { title: string; jobs: Job[]; now: number }) {
  if (!jobs.length) return null;
  return (
    <section>
      <h3 className="mb-1.5 text-[0.7rem] font-semibold uppercase tracking-wider text-faint">{title} · {jobs.length}</h3>
      <ul className="space-y-2">{jobs.map((j) => <Row key={j.id} job={j} now={now} />)}</ul>
    </section>
  );
}

export function JobsTray() {
  const jobs = useJobs();
  useJobAlerts();
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(Date.now());
  const running = jobs.filter((j) => j.status === "running");
  const failed = jobs.filter((j) => FAILED.has(j.status));
  const finished = jobs.filter((j) => j.status === "done" || j.status === "cancelled");
  useEffect(() => {
    if (!open || !running.length) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [open, running.length]);

  return (
    <>
      <button onClick={() => setOpen(true)} aria-label={`Jobs: ${running.length} running, ${failed.length} failed`}
        className="relative inline-flex items-center gap-1.5 border border-line bg-surface-2 px-2.5 py-1 text-[0.76rem] text-muted hover:border-line-strong hover:text-fg">
        <ListTodo size={14} aria-hidden />Jobs
        {running.length > 0 && <span className="bg-brand px-1.5 font-mono text-[0.68rem] text-on-brand">{running.length}</span>}
        {failed.length > 0 && <span aria-hidden className="h-2 w-2 bg-crit" />}
      </button>
      <Sheet open={open} onOpenChange={setOpen} title="Jobs" description="Background work: what is running, what stopped, what finished.">
        <div className="space-y-5">
          {jobs.length === 0 && <p className="text-[0.84rem] text-muted">Nothing has run yet. Work you start (collecting, mining, building, training) appears here.</p>}
          <JobStrip jobs={jobs} now={now} />
          <Group title="Running" jobs={running} now={now} />
          <Group title="Failed or cut off" jobs={failed.slice(0, 10)} now={now} />
          <Group title="Finished" jobs={finished.slice(0, 10)} now={now} />
          <div className="border-t border-line pt-3"><AlertsSwitch /></div>
          <Dev><Link to="/settings?tab=developer" onClick={() => setOpen(false)} className="text-[0.78rem] text-brand-strong hover:underline">Open the full activity log</Link></Dev>
        </div>
      </Sheet>
    </>
  );
}
