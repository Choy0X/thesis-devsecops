import { useEffect, useState } from "react";
import { api, type EvalShared, type Metrics, type PatchMetrics } from "../lib/api";
import { EvalRuns } from "./EvalRuns";
import { CompareRuns } from "./CompareRuns";
import { Panel, Skeleton, EmptyState, ErrorState } from "../components/ui";
import { ConfusionMatrix, MetricRow, PerClass } from "../components/Viz";
import { Icon } from "../components/Icon";
import { pct, fixed, num } from "../lib/format";
import { CRITICAL_MEANING, criticalCountText, criticalMiss } from "../lib/critical";
import { useOnActivate } from "../components/ActivePage";
import { Hint } from "../components/kit";

export function Results() {
  const [all, setAll] = useState<(Metrics | PatchMetrics | EvalShared)[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = () => {
    setError(null);
    api.experiments().then((e) => {
      setAll(e);
      const cls = e.filter((x: any) => typeof x.macro_f1 === "number");
      if (cls.length) setSel(cls[cls.length - 1].tag);
    }).catch(setError);
  };
  useEffect(load, []);
  useOnActivate(load);

  if (error) return <ErrorState error={error} title="Could not read the experiment results" onRetry={load} />;
  if (!all) return <Skeleton className="h-64 w-full" />;
  const exps = all.filter((e: any) => typeof e.macro_f1 === "number") as Metrics[];   // classification runs only
  const patches = all.filter((e) => (e as PatchMetrics).task === "patch") as PatchMetrics[];
  const evals = all.filter((e) => (e as EvalShared).schema === "eval-shared/1") as EvalShared[];
  if (!all.length) return (
    <Panel title="Results"><EmptyState icon={<Icon.results />} title="No experiment runs yet"
      hint="Run the engine (or import a fine-tuned arm's predictions), then compare runs here." /></Panel>
  );
  if (!exps.length) {
    if (!patches.length && !evals.length) return (
      <Panel title="Results"><EmptyState icon={<Icon.results />} title="No classification, patch or evaluation results yet"
        hint="Import an evaluation of a fine-tuned run on the Runs tab, or run the engine." /></Panel>);
    return <div className="space-y-5">{evals.length > 0 && <EvalRuns runs={evals} />}{evals.length > 1 && <CompareRuns runs={evals} />}{patches.length > 0 && <PatchTable runs={patches} />}</div>;
  }
  const current = exps.find((e) => e.tag === sel) || exps[0];

  return (
    <div className="space-y-5">
    <div className="grid gap-5 lg:grid-cols-[260px_1fr]">
      <Panel title="Runs" desc={`${exps.length} recorded`}>
        <ul className="space-y-1">
          {exps.map((e) => (
            <li key={e.tag}>
              <button onClick={() => setSel(e.tag)}
                className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left transition-colors duration-150 ${
                  e.tag === current.tag ? "bg-brand-tint text-fg" : "text-muted hover:bg-surface-2 hover:text-fg"}`}>
                <span className="font-mono text-[0.78rem]">{e.tag}</span>
                <span className="tnum text-[0.74rem]">{fixed(e.macro_f1, 2)}</span>
              </button>
            </li>
          ))}
        </ul>
      </Panel>

      <div className="space-y-5">
        <Panel title={<span className="font-mono">{current.tag}</span>} desc="Headline metrics. Critical false-negative rate is the safety metric.">
          <MetricRow m={current} />
        </Panel>
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel title="Confusion matrix" desc="Rows: true · columns: predicted.">
            <ConfusionMatrix m={current} />
          </Panel>
          <Panel title="Per-class performance" desc="Precision, recall, F1 by severity.">
            <PerClass m={current} />
          </Panel>
        </div>
        {exps.length > 1 && (
          <Panel title="Comparison grid" desc="All runs, scored by the same evaluator.">
            <div className="overflow-x-auto">
              <table className="w-full text-[0.82rem]">
                <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint">
                  <th className="py-2 pr-4 font-medium">Run</th><th className="py-2 pr-4 font-medium">n</th>
                  <th className="py-2 pr-4 font-medium">Accuracy</th><th className="py-2 pr-4 font-medium">Macro F1</th>
                  <th className="py-2 pr-4 font-medium">Split</th>
                  <th className="py-2 font-medium"><Hint label={CRITICAL_MEANING}>Critical missed</Hint></th></tr></thead>
                <tbody className="tnum">
                  {exps.map((e) => (
                    <tr key={e.tag} className={`border-b border-line/60 ${e.tag === current.tag ? "text-fg" : "text-muted"}`}>
                      <td className="py-2 pr-4 font-mono">{e.tag}</td><td className="py-2 pr-4">{num(e.n)}</td>
                      <td className="py-2 pr-4">{pct(e.accuracy)}</td><td className="py-2 pr-4">{fixed(e.macro_f1)}</td>
                      <td className="py-2 pr-4 text-faint">{e.split || "not recorded"}</td>
                      <td className={`py-2 ${e.critical_false_negative_rate != null && e.critical_false_negative_rate > 0.2 ? "text-crit" : ""}`}>
                        {e.critical_false_negative_rate == null ? "—" : pct(e.critical_false_negative_rate)}
                        <span className="ml-2 text-[0.72rem] text-faint">{criticalCountText(criticalMiss(e))}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        )}
      </div>
    </div>
    {evals.length > 0 && <EvalRuns runs={evals} />}{evals.length > 1 && <CompareRuns runs={evals} />}
    {patches.length > 0 && <PatchTable runs={patches} />}
    </div>
  );
}

function PatchTable({ runs }: { runs: PatchMetrics[] }) {
  const cols: [keyof PatchMetrics, string][] = [
    ["exact_match", "Exact"], ["edit_similarity", "Edit sim."], ["fix_line_recall", "Fix-line recall"],
    ["vuln_line_removal", "Vuln-line removal"], ["unchanged_rate", "Unchanged"],
    ["syntax_valid_rate", "Syntax ok"], ["plausible_fix_rate", "Plausible fix"]];
  return (
    <Panel title="Patch generation (vulnerable → fixed)"
      desc="Same prompt and evaluator for every arm. 'Plausible fix' is a static proxy; behavioural verification is the sandbox (RQ5).">
      <div className="overflow-x-auto">
        <table className="w-full text-[0.82rem]">
          <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint">
            <th className="py-2 pr-4 font-medium">Run</th><th className="py-2 pr-4 font-medium">n</th>
            {cols.map(([, l]) => <th key={l} className="py-2 pr-4 font-medium">{l}</th>)}</tr></thead>
          <tbody className="tnum">
            {runs.map((r) => (
              <tr key={r.tag} className="border-b border-line/60 text-muted">
                <td className="py-2 pr-4 font-mono text-fg">{r.tag}</td><td className="py-2 pr-4">{num(r.n)}</td>
                {cols.map(([k]) => {
                  const v = r[k] as number | null;
                  return <td key={String(k)} className="py-2 pr-4">{v == null ? "—" : pct(v)}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
