import { useEffect, useMemo, useState } from "react";
import { Button, Panel } from "../components/ui";
import { Select } from "../components/kit";
import { api, type CompareGuard, type CompareStats, type ConfigDiff, type CweData, type EvalShared, type RunCompare } from "../lib/api";
import { num, pct } from "../lib/format";
import { useDevMode } from "../lib/devMode";
import { useRunningJobId } from "../lib/jobsList";
import { splitNote } from "./EvalRuns";
import { BreakdownTable, ClassTable, ConfusionTables, CweTable } from "./CompareTables";
import { CompareCurves } from "./CompareCurves";
import { CompareConfig } from "./CompareConfig";
import { LETTERS, buildReport } from "./compareReport";

export { discoveryOf } from "./compareReport";

// Two to four evaluation runs side by side. Each run is set against the first: they are compared only when what decides what a score means is the same in both (the input,
// the split, the dataset version and so on); a difference there is named and no verdict is given. When they are comparable, "better" is said only by a paired test on the items both
// answered, and "not enough data" is never turned into "no difference". The tables put every run in a column, say which split each was measured on, and the training curves are laid
// over each other.

const FIELD_WORDS: Record<string, string> = {
  input_policy_hash: "what the model was shown", context_policy: "the context policy", rag_enabled: "whether retrieval was on", "retrieval.embedder": "the retrieval embedder",
  "retrieval.kb_version": "the knowledge base", split: "the split", dataset_version: "the dataset version", external_data: "whether imported data was used", eval_view: "the evaluation views",
  prompt_version: "the prompt", loss_mode: "the loss mode", "engine.model": "the model that answered",
};
export const fieldWords = (f: string) => FIELD_WORDS[f] || f.replace(/[._]/g, " ");
const show = (v: unknown) => (v == null ? "not recorded" : typeof v === "string" ? (v.length > 24 ? `${v.slice(0, 12)}...` : v) : JSON.stringify(v));

export const VERDICT_WORDS: Record<CompareStats["verdict"], string> = {
  a_better: "Run A is better", b_better: "Run B is better", no_difference_shown: "No difference shown", not_enough_data: "Not enough data to say",
};
/** The verdict in words, with the letter of the run that was set against run A. */
export const verdictWords = (v: CompareStats["verdict"], other: string): string => (v === "b_better" ? `Run ${other} is better` : VERDICT_WORDS[v]);

function Guard({ g }: { g: CompareGuard }) {
  return (
    <div className="space-y-2 text-[0.82rem]">
      {!g.comparable && (
        <div role="alert" className="rounded-lg border border-warn-line bg-warn-tint px-3.5 py-3">
          <div className="font-medium text-warn">These runs are not comparable, so no verdict is given.</div>
          <ul className="mt-1.5 space-y-1 text-muted">
            {g.blockers.map((b) => (
              <li key={b.field}>{b.reason} <span className="text-faint">({fieldWords(b.field)}: A {show(b.a)}, B {show(b.b)})</span></li>))}
          </ul>
        </div>)}
      {g.unconfirmed.length > 0 && (
        <p className="text-[0.78rem] text-faint">Not confirmed to match, because neither run recorded it: {g.unconfirmed.map((u) => fieldWords(u.field)).join(", ")}.</p>)}
    </div>
  );
}

function Verdict({ s, other }: { s: CompareStats; other: string }) {
  const tone = s.verdict === "not_enough_data" ? "text-warn" : s.verdict === "no_difference_shown" ? "text-muted" : "text-ok";
  return (
    <div className="space-y-2 text-[0.82rem]">
      <p className={`font-medium ${tone}`}>{verdictWords(s.verdict, other)}. <span className="font-normal text-muted">{s.reason}</span></p>
      <div className="tnum flex flex-wrap gap-x-6 gap-y-1 text-muted">
        <span>{num(s.items)} items answered by both</span><span>both right {num(s.both_right)}</span><span>both wrong {num(s.both_wrong)}</span>
        <span>only A right {num(s.only_a)}</span><span>only {other} right {num(s.only_b)}</span>
      </div>
      <p className="tnum text-muted">
        Accuracy A {pct(s.accuracy_a)}, {other} {pct(s.accuracy_b)}; A minus {other} {pct(s.difference)}
        {s.interval ? ` (95% interval ${pct(s.interval[0])} to ${pct(s.interval[1])})` : ""}.
      </p>
      <p className="text-[0.74rem] text-faint">
        Rule: at least {s.rule.min_items} items and {s.rule.min_disagreements} where the runs disagree; {s.rule.test}, significance level {s.rule.alpha}. A run is called better only when the test passes.
      </p>
    </div>
  );
}

function GroupedComparison({ report, other }: { report: NonNullable<RunCompare['grouped_paired']>; other: string }) {
  const metrics = [['recall', 'Automatic detection recall'], ['false_positive_rate', 'False-positive rate'],
    ['precision', 'Precision'], ['f1', 'F1'], ['mcc', 'MCC'], ['coverage', 'Answered coverage']] as const;
  return <div className="space-y-2 text-[0.8rem]">
    <p className="text-muted">{num(report.independent_groups)} independent repository/issue group{report.independent_groups === 1 ? '' : 's'} across {num(report.views)} views. Differences show run {other} minus run A.</p>
    <table className="w-full text-left tnum" aria-label="Grouped paired metrics">
      <thead className="text-faint"><tr><th>Metric</th><th>Difference</th><th>95% interval</th><th>Evidence</th></tr></thead>
      <tbody>{metrics.map(([key, label]) => {
        const value = report[key];
        if (!value) return null;
        const format = (number: number | null) => key === 'mcc' ? (number == null ? 'unavailable' : number.toFixed(3)) : pct(number);
        return <tr key={key} className="border-t border-line"><td className="py-1">{label}</td><td>{format(value.estimate)}</td>
          <td>{value.interval_95 ? `${format(value.interval_95[0])} to ${format(value.interval_95[1])}` : 'unavailable'}</td>
          <td>{value.state.replace(/_/g, ' ')}</td></tr>;
      })}</tbody>
    </table>
    <p className="text-[0.74rem] text-faint">{report.scope}</p>
  </div>;
}

/** What a run is called in a list: its tag with the control it stands for. */
export const runLabel = (r: EvalShared): string => {
  const i = r.run_info;
  const bits = [i?.baseline, i?.arm, r.split].filter(Boolean);
  return bits.length ? `${r.tag} (${bits.join(", ")})` : r.tag;
};

/** Pick two to four evaluation runs and read them side by side, with the comparability check first. */
export function CompareRuns({ runs }: { runs: EvalShared[] }) {
  const [picks, setPicks] = useState<string[]>([runs[runs.length - 2]?.tag ?? "", runs[runs.length - 1]?.tag ?? ""]);
  const [outs, setOuts] = useState<(RunCompare | null)[]>([]);
  const [diffs, setDiffs] = useState<(ConfigDiff | null)[]>([]);
  const [cwe, setCwe] = useState<CweData | null | undefined>(undefined);
  const [error, setError] = useState("");
  const [developer] = useDevMode();
  const live = useRunningJobId("train_local") != null;
  const key = picks.join("|");
  const distinct = new Set(picks).size === picks.length && picks.every(Boolean);
  useEffect(() => {
    if (!distinct) { setOuts([]); setDiffs([]); setCwe(undefined); setError(""); return; }
    let on = true;
    setError("");
    Promise.all(picks.slice(1).map((other) => api.compareRuns(picks[0], other)))
      .then((r) => { if (on) setOuts(r); }).catch((e) => { if (on) { setOuts([]); setError(String(e?.message || e)); } });
    Promise.all(picks.slice(1).map((other) => api.compareConfig(picks[0], other).catch(() => null)))       // the settings are a help, never a reason to hide the verdict
      .then((r) => { if (on) setDiffs(r); });
    setCwe(undefined);
    api.compareCwe(picks).then((r) => { if (on) setCwe(r); }).catch(() => { if (on) setCwe(null); });          // the table says that it could not be read
    return () => { on = false; };
  }, [key]);                                                                                  // eslint-disable-line react-hooks/exhaustive-deps
  const chosen = picks.map((t) => runs.find((r) => r.tag === t)).filter((r): r is EvalShared => !!r);
  const ready = distinct && chosen.length === picks.length && outs.length === picks.length - 1;
  const setPick = (i: number, v: string) => setPicks((p) => p.map((x, j) => (j === i ? v : x)));
  const free = runs.find((r) => !picks.includes(r.tag));
  const report = useMemo(() => (ready ? buildReport(chosen, outs, developer, diffs, cwe ?? null) : ""), [ready, key, outs, diffs, cwe, developer]);          // eslint-disable-line react-hooks/exhaustive-deps
  const download = () => {
    const url = URL.createObjectURL(new Blob([report], { type: "text/markdown" }));
    const a = document.createElement("a");
    a.href = url; a.download = `comparison-${picks.join("-vs-").slice(0, 80)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const pick = (i: number) => {
    const label = `Run ${LETTERS[i]}`;
    return (
      <div key={i} className="flex items-center gap-2 text-[0.8rem] text-muted">{label}
        <Select aria-label={label} value={picks[i]} onValueChange={(v) => setPick(i, v)} className="max-w-[22rem]" options={runs.map((r) => ({ value: r.tag, label: runLabel(r) }))} />
        {i >= 2 && <Button variant="ghost" aria-label={`Remove ${label}`} onClick={() => setPicks((p) => p.filter((_, j) => j !== i))}>Remove</Button>}
      </div>);
  };

  return (
    <Panel title="Compare runs" desc="Two to four runs side by side, each set against the first, on the items both answered. A verdict is given only for runs that are comparable and only by a paired test."
      right={ready ? <Button variant="ghost" onClick={download}>Download the comparison</Button> : undefined}>
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-4">
          {picks.map((_, i) => pick(i))}
          {picks.length < 4 && free && <Button variant="ghost" onClick={() => setPicks((p) => [...p, free.tag])}>Add a run</Button>}
        </div>
        {picks.length === 2 && picks[0] === picks[1] && <p className="text-[0.8rem] text-muted">Choose two different runs.</p>}
        {picks.length > 2 && !distinct && <p className="text-[0.8rem] text-muted">Choose different runs.</p>}
        {error && <p role="alert" className="text-[0.8rem] text-crit">The runs could not be compared: {error}</p>}
        {ready && (
          <>
            {outs.map((out, i) => {
              const other = LETTERS[i + 1];
              return (
                <div key={other} className="space-y-2" aria-label={`Run A against run ${other}`} role="group">
                  {picks.length > 2 && <h3 className="text-[0.82rem] font-medium">Run A against run {other}</h3>}
                  <Guard g={out!.guard} />
                  {out!.warnings.map((w) => <p key={w} className="text-[0.78rem] text-warn">{w}</p>)}
                  {out!.guard.comparable && out!.stats && <Verdict s={out!.stats} other={other} />}
                  {out!.guard.comparable && !out!.stats && <p className="text-[0.8rem] text-warn">{out!.stats_note}</p>}
                  {out!.guard.comparable && out!.grouped_paired && <GroupedComparison report={out!.grouped_paired} other={other} />}
                </div>);
            })}
            <p className="text-[0.78rem] text-muted">{splitNote(chosen[0].split, chosen[0].run_info?.role)}{chosen.slice(1).some((r) => r.split !== chosen[0].split || r.run_info?.role !== chosen[0].run_info?.role)
              ? ` ${chosen.slice(1).map((r, i) => `Run ${LETTERS[i + 1]}: ${splitNote(r.split, r.run_info?.role)}`).join(" ")}` : ""}</p>
            <ClassTable runs={chosen} />
            <BreakdownTable runs={chosen} by="by_language" />
            <BreakdownTable runs={chosen} by="by_ecosystem" />
            <ConfusionTables runs={chosen} />
            <CweTable runs={chosen} data={cwe} />
            {diffs.map((d, i) => d && <CompareConfig key={LETTERS[i + 1]} diff={d} other={i + 1} />)}
            {outs.map((out, i) => out!.guard.comparable && out!.disagreements.length > 0 && (
              <details key={i} className="text-[0.8rem]">
                <summary className="cursor-pointer text-muted">
                  The {num(out!.disagreements_total)} items where run A and run {LETTERS[i + 1]} disagree{out!.disagreements_total > out!.disagreements.length ? ` (the first ${out!.disagreements.length})` : ""}
                </summary>
                <table className="mt-2 w-full text-[0.78rem]">
                  <thead><tr className="border-b border-line text-left text-[0.7rem] uppercase tracking-wide text-faint"><th className="py-1 pr-3 font-medium">Item</th><th className="pr-3 font-medium">Truth</th><th className="pr-3 font-medium">A answered</th><th className="font-medium">{LETTERS[i + 1]} answered</th></tr></thead>
                  <tbody className="text-muted">
                    {out!.disagreements.map((d) => (
                      <tr key={d.view_id} className="border-b border-line/60"><td className="py-1 pr-3 font-mono text-[0.72rem] text-fg">{d.view_id}</td><td className="pr-3">{d.truth}</td><td className="pr-3">{d.a}</td><td>{d.b}</td></tr>))}
                  </tbody>
                </table>
              </details>))}
            <section aria-label="Training curves" className="space-y-2">
              <h3 className="text-[0.8rem] font-medium">Training curves</h3>
              <CompareCurves runs={chosen} live={live} />
            </section>
          </>)}
      </div>
    </Panel>
  );
}
