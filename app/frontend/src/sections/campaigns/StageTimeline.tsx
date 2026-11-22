import { ChartFrame, ChartTable } from "../../components/charts/ChartFrame";
import type { AnalyticsStep, CampaignStepStatus } from "../../lib/api";
import { stageWord, STEP_WORDS } from "./campaignFormat";
import { stamp } from "./eventText";

// The steps of a campaign on one time line: each bar runs from when the step started to when it ended (to now while it runs). What each step ended as is written beside
// its bar, and the bar differs by pattern as well as colour (a failed step is hatched, a step stopped at the quota or paused is dotted, a skipped one is an outline).

const HATCH = "repeating-linear-gradient(135deg, var(--border-strong) 0 2px, transparent 2px 6px)";
const DOTS = "radial-gradient(var(--border-strong) 1px, transparent 1.2px)";

const LOOK: Record<CampaignStepStatus, { cls: string; style?: React.CSSProperties }> = {
  done: { cls: "bg-brand" }, running: { cls: "bg-brand-strong" }, error: { cls: "border border-crit-line bg-surface-2", style: { backgroundImage: HATCH } },
  paused: { cls: "border border-line-strong bg-surface-2", style: { backgroundImage: DOTS, backgroundSize: "5px 5px" } },
  quota: { cls: "border border-warn-line bg-surface-2", style: { backgroundImage: DOTS, backgroundSize: "5px 5px" } },
  skipped: { cls: "border border-line-strong" }, pending: { cls: "" },
};

const span = (s: number | null) => (s == null ? "—" : s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`);
const who = (s: AnalyticsStep) => `${s.target === "*" ? "Whole campaign" : s.target}: ${stageWord(s.stage)}`;

export function StageTimeline({ steps, now }: { steps: AnalyticsStep[]; now: string }) {
  const clock = Date.parse(now);
  const began = steps.filter((s) => s.started_at).map((s) => Date.parse(s.started_at!));
  const t0 = began.length ? Math.min(...began) : clock;
  const t1 = Math.max(clock, ...steps.map((s) => (s.finished_at ? Date.parse(s.finished_at) : 0)), t0 + 1000);
  const x = (t: number) => (100 * (t - t0)) / (t1 - t0);
  const table = <ChartTable caption="The steps of the campaign" head={["Step", "State", "Started", "Time", "Tries", "Left"]}
    rows={steps.map((s) => [who(s), STEP_WORDS[s.status], stamp(s.started_at), span(s.seconds), s.attempts || "—", s.eta_seconds != null ? span(s.eta_seconds) : "—"])} />;
  return (
    <ChartFrame title="Progress and stage timeline" description="Each step from its start to its end. A step that is running shows how long it may still take only when it reports how far it has got." table={table}>
      {began.length === 0 ? <p className="text-[0.8rem] text-muted">No step has started yet.</p> : (
        <>
          <ol aria-label="Steps on a time line" className="space-y-1.5">
            {steps.map((s) => {
              const start = s.started_at ? Date.parse(s.started_at) : null;
              const end = s.finished_at ? Date.parse(s.finished_at) : s.status === "running" ? clock : start;
              const look = LOOK[s.status];
              return (
                <li key={`${s.target}|${s.stage}`} tabIndex={0} data-status={s.status}
                  aria-label={`${who(s)}: ${STEP_WORDS[s.status]}${s.seconds != null ? `, ${span(s.seconds)}` : ""}${s.eta_seconds != null ? `, about ${span(s.eta_seconds)} left` : ""}`}
                  className="grid grid-cols-[minmax(9rem,14rem)_1fr_minmax(7rem,10rem)] items-center gap-3 text-[0.78rem] outline-none focus-visible:ring-1 focus-visible:ring-brand-strong">
                  <span className="truncate" title={who(s)}>{who(s)}</span>
                  <span aria-hidden className="relative h-3 bg-surface-2">
                    {start != null && end != null && (
                      <span className={`absolute inset-y-0 ${look.cls}`} style={{ left: `${x(start)}%`, width: `${Math.max(0.8, x(end) - x(start))}%`, ...look.style }} />
                    )}
                  </span>
                  <span className="tnum text-muted">{STEP_WORDS[s.status]}{s.seconds != null ? ` · ${span(s.seconds)}` : ""}{s.eta_seconds != null ? ` · ~${span(s.eta_seconds)} left` : ""}</span>
                </li>
              );
            })}
          </ol>
          <p className="tnum mt-2 flex justify-between text-[0.72rem] text-faint"><span>{stamp(new Date(t0).toISOString())}</span><span>{stamp(new Date(t1).toISOString())}</span></p>
        </>
      )}
    </ChartFrame>
  );
}
