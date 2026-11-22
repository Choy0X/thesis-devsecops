import type { Job } from "../lib/api";
import { Tooltip } from "./hud/Tooltip";
import { stageLabel } from "../lib/stageLabels";

// A strip of the most recent jobs, oldest on the left. A block is as wide as the job was long (on a logarithmic scale, so a one-second job and a
// one-hour job can share a strip). The state is said by the border and the filling as well as by colour: finished is solid, failed is hatched, cut off
// is hatched with a dotted border, stopped by the person is dashed, running is outlined and open on the right.

export const STRIP_JOBS = 24;
const HATCH = "repeating-linear-gradient(135deg, var(--crit-line) 0 2px, transparent 2px 5px)";

type Look = { word: string; className: string; hatch?: boolean };
const LOOK: Record<string, Look> = {
  done: { word: "finished", className: "border-ok-line bg-ok-tint" },
  error: { word: "failed", className: "border-crit-line bg-crit-tint", hatch: true },
  interrupted: { word: "cut off", className: "border-dotted border-crit-line bg-crit-tint", hatch: true },
  cancelled: { word: "stopped", className: "border-dashed border-line-strong bg-surface-2" },
  running: { word: "running", className: "border-brand bg-brand-tint border-r-0" },
};

export const seconds = (job: Job, now: number): number => {
  const start = Date.parse(job.started);
  if (Number.isNaN(start)) return 0;
  const end = job.status === "running" || !job.finished ? now : Date.parse(job.finished);
  return Math.max(0, Math.round(((Number.isNaN(end) ? now : end) - start) / 1000));
};

export const length = (s: number): string => (s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`);

/** The recent jobs that have a start time, oldest first, at most `STRIP_JOBS`. */
export function recent(jobs: Job[]): Job[] {
  return jobs.filter((j) => !Number.isNaN(Date.parse(j.started))).sort((a, b) => Date.parse(a.started) - Date.parse(b.started)).slice(-STRIP_JOBS);
}

export function JobStrip({ jobs, now = Date.now() }: { jobs: Job[]; now?: number }) {
  const list = recent(jobs);
  if (list.length < 2) return null;
  const weight = (j: Job) => Math.max(1, Math.log2(2 + seconds(j, now)));
  return (
    <section aria-label="Recent jobs over time">
      <ul className="flex h-5 gap-px" aria-label="Recent jobs, oldest first">
        {list.map((j) => {
          const look = LOOK[j.status] ?? LOOK.cancelled;
          return (
            <Tooltip key={j.id} label={`${stageLabel(j.stage)}: ${look.word}, ${length(seconds(j, now))}`}>
              <li tabIndex={0} style={{ flexGrow: weight(j), flexBasis: 0, minWidth: 4, backgroundImage: look.hatch ? HATCH : undefined }}
                aria-label={`${stageLabel(j.stage)}, ${look.word}, ${length(seconds(j, now))}`}
                className={`border outline-none focus-visible:ring-1 focus-visible:ring-brand-strong ${look.className}`} />
            </Tooltip>
          );
        })}
      </ul>
      <p className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[0.68rem] text-faint" aria-hidden>
        <span>solid: finished</span><span>hatched: failed</span><span>hatched, dotted: cut off</span><span>dashed: stopped</span><span>open end: running</span>
      </p>
    </section>
  );
}
