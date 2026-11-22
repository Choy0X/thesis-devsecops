import { Fragment } from "react";
import type { Metrics } from "../lib/api";
import { pct, fixed, num } from "../lib/format";
import { CRITICAL_MEANING, criticalCountText, criticalMiss } from "../lib/critical";
import { Hint } from "./kit";

const SEV = ["Recommendation", "Warning", "Critical"];

// Confusion matrix as a labelled heat grid (drawn, not a chart lib).
export function ConfusionMatrix({ m }: { m: Metrics }) {
  const labels = m.confusion_matrix_labels || SEV;
  const cm = m.confusion_matrix || [];
  const max = Math.max(1, ...cm.flat());
  return (
    <div className="inline-grid gap-1" style={{ gridTemplateColumns: `auto repeat(${labels.length}, 3.2rem)` }}>
      <div />
      {labels.map((l) => <div key={l} className="pb-1 text-center font-mono text-[0.68rem] text-faint">{l.slice(0, 4)}</div>)}
      {cm.map((row, i) => (
        <Fragment key={i}>
          <div className="pr-2 text-right font-mono text-[0.68rem] text-faint self-center">{labels[i].slice(0, 4)}</div>
          {row.map((v, jx) => {
            const t = v / max;
            const diag = i === jx;
            return (
              <div key={`${i}-${jx}`} title={`${labels[i]} → ${labels[jx]}: ${v}`}
                className="grid h-11 place-items-center rounded-md tnum text-[0.8rem] font-medium"
                style={{
                  background: diag
                    ? `color-mix(in oklab, var(--info) ${12 + t * 70}%, var(--surface-2))`
                    : `color-mix(in oklab, var(--crit) ${t * 55}%, var(--surface-2))`,
                  color: t > 0.5 ? "var(--fg)" : "var(--muted)",
                }}>{v}</div>
            );
          })}
        </Fragment>
      ))}
    </div>
  );
}

export function MetricRow({ m }: { m: Metrics }) {
  const miss = criticalMiss(m);
  const fn = miss?.rate ?? null;
  return (
    <div className="grid grid-cols-4 gap-px overflow-hidden rounded-lg border border-line bg-line">
      {[["accuracy", pct(m.accuracy)], ["macro F1", fixed(m.macro_f1)],
        ["Critical missed", fn == null ? "—" : pct(fn)], ["n", num(m.n)]].map(([k, v]) => (
        <div key={k} className={`bg-surface px-4 py-3 ${k === "Critical missed" ? "relative" : ""}`}>
          <div className="text-[0.72rem] text-muted">{k}{k === "n" && m.split ? ` (${m.split} split)` : ""}</div>
          <div className={`mt-0.5 tnum text-[1.15rem] font-semibold tracking-tight ${
            k === "Critical missed" && fn != null && fn > 0.2 ? "text-crit" : ""}`}>{v}</div>
          {k === "Critical missed" && (
            <div className="mt-0.5 text-[0.7rem] leading-snug text-faint"><Hint label={CRITICAL_MEANING}>{criticalCountText(miss)}</Hint></div>
          )}
        </div>
      ))}
    </div>
  );
}

// Per-class precision/recall/F1 as grouped horizontal bars.
export function PerClass({ m }: { m: Metrics }) {
  return (
    <div className="space-y-3">
      {SEV.map((cls) => {
        const pc = m.per_class?.[cls];
        if (!pc) return null;
        const sev = cls === "Critical" ? "var(--sev-critical)" : cls === "Warning" ? "var(--sev-medium)" : "var(--info)";
        return (
          <div key={cls}>
            <div className="mb-1 flex items-center justify-between text-[0.78rem]">
              <span className="flex items-center gap-2"><span className="h-2 w-2 rounded-full" style={{ background: sev }} />{cls}</span>
              <span className="tnum text-faint">n={pc.support}</span>
            </div>
            <div className="grid grid-cols-3 gap-2">
              {([["P", pc.precision], ["R", pc.recall], ["F1", pc.f1]] as const).map(([k, v]) => (
                <div key={k}>
                  <div className="mb-0.5 flex justify-between text-[0.68rem] text-faint"><span>{k}</span><span className="tnum">{fixed(v, 2)}</span></div>
                  <div className="h-1.5 rounded-full bg-surface-2"><div className="h-full rounded-full" style={{ width: `${v * 100}%`, background: sev }} /></div>
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
