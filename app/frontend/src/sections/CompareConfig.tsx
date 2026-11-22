import type { ConfigDiff, DatasetFacts } from "../lib/api";
import { num } from "../lib/format";
import { useDevMode } from "../lib/devMode";
import { LETTERS, datasetRows, hideHashes, shown } from "./compareReport";

// What differs between two runs, and only that: the settings of their training runs that are not the same, and the datasets they were trained on (the version, the status, the counts, the
// mix of review tiers, the languages). A setting both runs share is only counted. A run whose configuration cannot be found says so instead of being treated as the same.

const Missing = ({ letter, why }: { letter: string; why: string }) => <p className="text-[0.8rem] text-warn">The configuration of run {letter} could not be found: {why}</p>;

export function CompareConfig({ diff, other }: { diff: ConfigDiff; other: number }) {
  const [developer] = useDevMode();
  const letter = LETTERS[other];
  const rows = datasetRows(diff.dataset.a, diff.dataset.b);
  const langs = [...new Set([...Object.keys(diff.dataset.a?.languages ?? {}), ...Object.keys(diff.dataset.b?.languages ?? {})])].sort();
  const cell = (v: unknown) => hideHashes(shown(v), developer);
  return (
    <section aria-label={`What differs between run A and run ${letter}`} className="space-y-3">
      <h3 className="text-[0.8rem] font-medium">What differs between run A and run {letter}</h3>
      {!diff.a.available && <Missing letter="A" why={diff.a.reason ?? "unknown"} />}
      {!diff.b.available && <Missing letter={letter} why={diff.b.reason ?? "unknown"} />}
      {diff.a.available && diff.a.reason && <p className="text-[0.76rem] text-muted">Run A: {diff.a.reason}.</p>}
      {diff.b.available && diff.b.reason && <p className="text-[0.76rem] text-muted">Run {letter}: {diff.b.reason}.</p>}
      {diff.config.compared && !diff.config.training_compared && <p className="text-[0.76rem] text-muted">The training settings are compared only when both runs have a training configuration; only how the runs were evaluated is compared here.</p>}
      {diff.config.compared && (
        diff.config.differing.length === 0
          ? <p className="text-[0.8rem] text-muted">The two runs were trained with the same {num(diff.config.same)} settings.</p>
          : (
            <div className="overflow-x-auto">
              <table aria-label="Settings that differ" className="w-full text-[0.8rem]">
                <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-1.5 pr-4 font-medium">Setting</th><th className="pr-4 font-medium">Run A</th><th className="font-medium">Run {letter}</th></tr></thead>
                <tbody className="text-muted">
                  {diff.config.differing.map((d) => (
                    <tr key={d.key} className="border-b border-line/60"><td className="py-1.5 pr-4 font-mono text-[0.74rem] text-fg">{d.key}</td><td className="pr-4">{cell(d.a)}</td><td>{cell(d.b)}</td></tr>))}
                </tbody>
              </table>
              <p className="mt-1 text-[0.74rem] text-faint">{num(diff.config.differing.length)} differ; {num(diff.config.same)} are the same.</p>
            </div>))}
      {[["A", diff.dataset.a], [letter, diff.dataset.b]].map(([l, d]) => d && !(d as DatasetFacts).current && (
        <p key={l as string} className="text-[0.76rem] text-warn">The dataset folder of run {l as string} has changed since the run was prepared, so the counts below are of the folder now.</p>))}
      {diff.dataset.a && diff.dataset.b && (
        diff.dataset.same
          ? <p className="text-[0.8rem] text-muted">Both runs were trained on the same dataset: dataset_{diff.dataset.a.tag} ({diff.dataset.a.status}).</p>
          : (
            <div className="space-y-2">
              <p className="text-[0.8rem] text-warn">The runs were trained on different datasets: dataset_{diff.dataset.a.tag} against dataset_{diff.dataset.b.tag}.</p>
              {rows.length > 0 && (
                <table aria-label="Datasets that differ" className="w-full text-[0.8rem]">
                  <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-1.5 pr-4 font-medium">What</th><th className="pr-4 font-medium">Run A</th><th className="font-medium">Run {letter}</th></tr></thead>
                  <tbody className="tnum text-muted">{rows.map((r) => <tr key={r.field} className="border-b border-line/60"><td className="py-1.5 pr-4 text-fg">{r.field}</td><td className="pr-4">{r.a}</td><td>{r.b}</td></tr>)}</tbody>
                </table>)}
              {langs.length > 0 && JSON.stringify(diff.dataset.a.languages) !== JSON.stringify(diff.dataset.b.languages) && (
                <p className="text-[0.76rem] text-muted">Languages (records): {langs.map((l) => `${l} ${num(diff.dataset.a?.languages[l] ?? 0)} against ${num(diff.dataset.b?.languages[l] ?? 0)}`).join("; ")}.</p>)}
              {(diff.dataset.a.external || diff.dataset.b.external) && <p className="text-[0.76rem] text-warn">At least one of the datasets was imported from outside; such a run is not comparable with a run on a version built here.</p>}
            </div>))}
    </section>
  );
}
