import { num } from "../../lib/format";
import type { FlowReason, FlowStage, Lineage } from "../../lib/api";
import { ChartFrame, ChartTable } from "./ChartFrame";

// The way from advisory to dataset record, one row per stage. The solid part of a bar is what reached the stage; the hatched part is what dropped out on
// the way, with the reasons written under it. The stage names its unit (advisories, pairs, records) because one advisory gives several pairs, and a bar is
// compared only with the stages of the same unit. Hatching and words carry the meaning, not colour.

export type { FlowReason, FlowStage, Lineage };

const HATCH = "repeating-linear-gradient(135deg, var(--border-strong) 0 2px, transparent 2px 6px)";

export function FlowChart({ data, title = "From advisory to dataset", description = "How many reach each stage, and why the rest drop out." }:
  { data: Lineage; title?: string; description?: string }) {
  const stages = data.stages;
  const scale: Record<string, number> = {};
  stages.forEach((s) => { scale[s.unit] = Math.max(scale[s.unit] ?? 0, s.count + sum(s.lost)); });
  const table = <ChartTable caption={title} head={["Stage", "Unit", "Reached", "Dropped out", "Reasons"]}
    rows={stages.map((s) => [s.label, s.unit, num(s.count), num(sum(s.lost)), s.lost.filter((r) => r.count > 0).map((r) => `${r.label} ${num(r.count)}`).join("; ") || "none"])} />;
  const aside = Object.entries(data.set_aside ?? {}).filter(([, n]) => n > 0);
  return (
    <ChartFrame title={title} description={description} table={table}>
      <ol className="space-y-3">
        {stages.map((s) => {
          const lost = sum(s.lost);
          const total = scale[s.unit] || 1;
          const reasons = s.lost.filter((r) => r.count > 0);
          return (
            <li key={s.id} tabIndex={0} aria-label={`${s.label}: ${num(s.count)} ${s.unit}${lost ? `, ${num(lost)} dropped out` : ""}`}
              className="outline-none focus-visible:ring-1 focus-visible:ring-brand-strong">
              <div className="mb-1 flex items-baseline justify-between gap-3 text-[0.8rem]">
                <span className="font-medium">{s.label}</span>
                <span className="tnum text-muted"><b className="font-medium text-fg">{num(s.count)}</b> {s.unit}</span>
              </div>
              <div className="flex h-3 w-full bg-surface-2" aria-hidden>
                <div className="h-full bg-brand" style={{ width: `${(s.count / total) * 100}%` }} />
                {lost > 0 && <div className="h-full" style={{ width: `${(lost / total) * 100}%`, backgroundImage: HATCH }} />}
              </div>
              {reasons.length > 0 && (
                <p className="mt-1 text-[0.74rem] text-muted">
                  <span className="tnum">{num(lost)}</span> dropped out{s.reasons_overlap ? " (a pair can have more than one reason)" : ""}:{" "}
                  {reasons.map((r, i) => <span key={r.id}>{i > 0 ? ", " : ""}{r.label.toLowerCase()} <b className="tnum font-medium text-fg">{num(r.count)}</b></span>)}
                </p>
              )}
            </li>
          );
        })}
      </ol>
      {aside.length > 0 && <p className="mt-3 border-t border-line pt-2 text-[0.74rem] text-faint">Not counted above: {aside.map(([k, n]) => `${k} ${num(n)}`).join(", ")}.</p>}
    </ChartFrame>
  );
}

const sum = (rs: FlowReason[]) => rs.reduce((a, r) => a + r.count, 0);
