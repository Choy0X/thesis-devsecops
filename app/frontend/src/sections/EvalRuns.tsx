import { useState } from "react";
import { Panel } from "../components/ui";
import { Select, Hint } from "../components/kit";
import type { DecisionCell, DiscoveryReport, EvalShared, RatioCi, RepairReport, TrackBlocked } from "../lib/api";
import { num, pct } from "../lib/format";

// What the shared evaluator measured for a fine-tuning evaluation run. Every number comes with the cases it rests on; a cell with too few cases says so
// instead of showing a confident rate, an interval comes from resampling whole groups, and a track with no attempts is shown as not measured, never as zero.

const blocked = (x: unknown): x is TrackBlocked => !!x && (x as TrackBlocked).status === "blocked";

/** A rate with its interval, or the reason there is none ("no interval: one group only"). */
export function rateText(ci: RatioCi | null | undefined, rate: number | null | undefined = ci?.estimate): string {
  if (rate == null) return "no cases";
  const base = pct(rate);
  if (!ci) return base;
  if (ci.ci) return `${base} (95% interval ${pct(ci.ci[0])} to ${pct(ci.ci[1])})`;
  return `${base} (no interval: ${ci.status === "insufficient_clusters" ? "only one group of cases" : ci.status.replace(/_/g, " ")})`;
}

/** What the split means for a reader: validation numbers chose the checkpoint, so they are never a held-out result. */
export function splitNote(split: string | null | undefined, role: string | null | undefined): string {
  if (!split) return "The split these numbers were measured on is not recorded.";
  if (split === "val") {
    return `Measured on the validation split${role ? ` (${role} role)` : ""}. Validation numbers were used to choose the checkpoint and the thresholds, so they are not a held-out result.`;
  }
  return `Measured on the ${split} split.`;
}

/** Why a run does not count as an evaluation of a control, or null when it does. */
export function whyNotCounted(info: EvalShared["run_info"]): string | null {
  if (!info || info.counts_as_evaluation !== false) return null;
  const why = [info.smoke_test ? "a software check on a small model" : "", info.partial ? "capped at a few views" : ""].filter(Boolean).join(" and ");
  return `Kept as evidence only: this run is ${why || "not complete"}, so it never makes a control evaluated.`;
}

function Cell({ c }: { c: DecisionCell }) {
  const v = c.vulnerable, n = c.negative;
  return (
    <>
      <td className="py-2 pr-4">{num(c.n)}</td>
      <td className="py-2 pr-4">{pct(c.coverage)}</td>
      <td className="py-2 pr-4">{v.insufficient ? <span className="text-warn">too few ({v.n})</span> : <>{rateText(v.ci?.issue_group, v.recall)} <span className="text-faint">{v.detected} of {v.n}</span></>}</td>
      <td className="py-2 pr-4">{n.insufficient ? <span className="text-warn">too few ({n.n})</span> : <>{rateText(n.ci?.issue_group, n.fpr)} <span className="text-faint">{n.false_positives} of {n.n}</span></>}</td>
      <td className="py-2">{pct(c.abstention_rate)}</td>
    </>
  );
}

function DecisionTable({ report }: { report: DiscoveryReport }) {
  const groups: [string, Record<string, DecisionCell>][] = [["Ecosystem", report.by_ecosystem], ["Language", report.by_language]];
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[0.82rem]">
        <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint">
          <th className="py-2 pr-4 font-medium">Slice</th><th className="py-2 pr-4 font-medium">Cases</th><th className="py-2 pr-4 font-medium">Answered</th>
          <th className="py-2 pr-4 font-medium"><Hint label="Vulnerable cases the model found. An abstention, a malformed answer or a timeout counts as not found.">Vulnerable found</Hint></th>
          <th className="py-2 pr-4 font-medium"><Hint label="Cases with no finding that the model flagged.">False alarms</Hint></th><th className="py-2 font-medium">Abstained</th></tr></thead>
        <tbody className="tnum">
          <tr className="border-b border-line/60"><td className="py-2 pr-4 text-fg">All</td><Cell c={report.overall} /></tr>
          {groups.flatMap(([label, cells]) => Object.entries(cells).map(([name, c]) => (
            <tr key={`${label}:${name}`} className="border-b border-line/60 text-muted">
              <td className="py-2 pr-4"><span className="text-faint">{label} </span>{name}</td><Cell c={c} />
            </tr>)))}
        </tbody>
      </table>
    </div>
  );
}

function CriticalBlock({ report }: { report: DiscoveryReport["critical"] }) {
  const rows: [string, RatioCi][] = [["A Critical vulnerability was not detected", report.critical_detection_miss_rate],
    ["A Critical vulnerability was found but its severity was set too low", report.severity_underclassification_rate]];
  return (
    <div className="space-y-2 text-[0.82rem]">
      {report.n_critical === 0 ? <p className="text-muted">There is no Critical case in this run, so neither error can be measured.</p> : rows.map(([label, r]) => (
        <div key={label} className="rounded-lg border border-line bg-surface-2 px-3.5 py-2.5">
          <div className="font-medium">{label}</div>
          <div className="tnum mt-0.5 text-muted">{rateText(r)} <span className="text-faint">{r.numerator} of {r.denominator}</span></div>
          {r.error && <div className="mt-0.5 text-[0.74rem] text-faint">{r.error}</div>}
        </div>))}
      <p className="text-[0.76rem] text-faint">
        {report.n_critical} Critical case{report.n_critical === 1 ? "" : "s"}; {report.severity_missing} without a usable severity answer; {report.severity_unknown_truth} vulnerable
        {report.severity_unknown_truth === 1 ? " case" : " cases"} with no known severity (counted apart, never assumed not Critical).
      </p>
    </div>
  );
}

function RepairBlock({ report }: { report: RepairReport }) {
  const rows = Object.entries(report.by_input);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[0.82rem]">
        <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint">
          <th className="py-2 pr-4 font-medium">Input</th><th className="py-2 pr-4 font-medium">Cases</th><th className="py-2 pr-4 font-medium"><Hint label="A repair counts as verified only with a receipt.">Verified</Hint></th>
          <th className="py-2 pr-4 font-medium">Failed</th><th className="py-2 pr-4 font-medium">Environment error</th><th className="py-2 pr-4 font-medium">Not checked</th><th className="py-2 font-medium">Receipts</th></tr></thead>
        <tbody className="tnum text-muted">
          {rows.map(([name, r]) => (
            <tr key={name} className="border-b border-line/60">
              <td className="py-2 pr-4 text-fg">{name.replace(/_/g, " ")}</td><td className="py-2 pr-4">{num(r.n)}</td>
              <td className="py-2 pr-4">{rateText(r.ci, r.verified_rate)} <span className="text-faint">{r.verified} of {r.n}</span></td>
              <td className="py-2 pr-4">{num(r.failed)}</td><td className="py-2 pr-4">{num(r.env_error)}</td><td className="py-2 pr-4">{num(r.unavailable)}</td>
              <td className={`py-2 ${r.receipt_missing ? "text-warn" : ""}`}>{pct(r.receipt_coverage)}{r.receipt_missing ? ` (${r.receipt_missing} passed without one)` : ""}</td>
            </tr>))}
        </tbody>
      </table>
    </div>
  );
}

/** The evaluation runs the shared evaluator scored: pick a run and an arm, read each track with its denominators. */
export function EvalRuns({ runs }: { runs: EvalShared[] }) {
  const [tag, setTag] = useState(runs[runs.length - 1]?.tag);
  const run = runs.find((r) => r.tag === tag) || runs[runs.length - 1];
  const arms = Object.keys(run?.arms || {});
  const [armPick, setArm] = useState<string | null>(null);
  if (!run) return null;
  const arm = armPick && arms.includes(armPick) ? armPick : arms[0];
  const tracks = run.arms[arm] || {};
  const info = run.run_info;
  const discovery = tracks.generated_decision, repair = tracks.repair;
  const others = Object.entries(tracks).filter(([k]) => !["generated_decision", "repair"].includes(k));
  const notCounted = whyNotCounted(info);

  return (
    <Panel title="Evaluation runs" desc="Answers of a model to the validation views, scored by the shared evaluator. Nothing is dropped: an abstention or a failed answer on a vulnerable case is a miss.">
      <div className="grid gap-5 lg:grid-cols-[240px_1fr]">
        <ul className="space-y-1" aria-label="Evaluation runs">
          {runs.map((r) => (
            <li key={r.tag}>
              <button onClick={() => { setTag(r.tag); setArm(null); }}
                className={`flex w-full flex-col rounded-lg px-3 py-2 text-left transition-colors duration-150 ${r.tag === run.tag ? "bg-brand-tint text-fg" : "text-muted hover:bg-surface-2 hover:text-fg"}`}>
                <span className="break-all font-mono text-[0.76rem]">{r.tag}</span>
                <span className="text-[0.72rem] text-faint">{num(r.n_attempts)} attempts{r.run_info?.counts_as_evaluation === false ? ", not counted" : ""}</span>
              </button>
            </li>))}
        </ul>
        <div className="space-y-4">
          <div className="space-y-1 text-[0.8rem]">
            <p className="text-muted">{splitNote(run.split, info?.role)}</p>
            {notCounted && <p className="text-warn">{notCounted}</p>}
            {arms.length > 1 && (
              <div className="flex items-center gap-2 text-muted">Model
                <Select aria-label="Model arm" value={arm} onValueChange={setArm} className="min-w-[10rem]" options={arms.map((a) => ({ value: a, label: a }))} />
              </div>)}
          </div>

          <section>
            <h3 className="mb-2 text-[0.76rem] font-medium uppercase tracking-wide text-faint">Finding vulnerabilities</h3>
            {!discovery || blocked(discovery) ? <p className="text-[0.8rem] text-muted">Not measured: {discovery && blocked(discovery) ? discovery.reason : "no answers of this kind in the run"}.</p>
              : <><DecisionTable report={discovery} />
                <h3 className="mb-2 mt-4 text-[0.76rem] font-medium uppercase tracking-wide text-faint">Critical errors</h3>
                <CriticalBlock report={discovery.critical} /></>}
          </section>

          <section>
            <h3 className="mb-2 text-[0.76rem] font-medium uppercase tracking-wide text-faint">Repairing</h3>
            {!repair || blocked(repair) ? <p className="text-[0.8rem] text-muted">Not measured: {repair && blocked(repair) ? repair.reason : "no repairs in the run"}.</p> : <RepairBlock report={repair} />}
          </section>

          {others.length > 0 && (
            <ul className="space-y-1 text-[0.78rem] text-muted">
              {others.map(([name, report]) => (
                <li key={name}><span className="text-fg">{name.replace(/_/g, " ")}: </span>
                  {blocked(report) ? `not measured (${report.reason})` : `${num((report as { n?: number }).n)} attempts scored`}</li>))}
            </ul>)}
        </div>
      </div>
    </Panel>
  );
}
