import type { CweData, DecisionCell, DiscoveryReport, EvalShared } from "../lib/api";
import { num, pct } from "../lib/format";
import { rateText } from "./EvalRuns";
import { LETTERS, alarmsText, cweText, discoveryOf, foundText, gridOf, insufficientText, keysOf, splitShort, withDelta } from "./compareReport";

// The runs side by side as tables. Every table says, first, which split each run was measured on; a cell with too few cases says so instead of showing a confident rate; and the
// breakdowns are the ones the evaluator records (by language and by ecosystem) and, joined from each run's dataset, by weakness class (CWE).

const TH = "py-2 pr-4 text-left font-medium";

function SplitLine({ runs }: { runs: EvalShared[] }) {
  return (
    <p className="mb-2 text-[0.74rem] text-faint">
      Measured on: {runs.map((r, i) => `${LETTERS[i]}, the ${splitShort(r)}`).join("; ")}.
    </p>);
}

function Wrap({ title, runs, children }: { title: string; runs: EvalShared[]; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="space-y-1">
      <h3 className="text-[0.8rem] font-medium">{title}</h3>
      <SplitLine runs={runs} />
      <div className="overflow-x-auto">{children}</div>
    </section>);
}

function Head({ runs, first }: { runs: EvalShared[]; first: string }) {
  return (
    <thead><tr className="border-b border-line text-[0.72rem] uppercase tracking-wide text-faint">
      <th className={TH}>{first}</th>{runs.map((_, i) => <th key={i} className={TH}>Run {LETTERS[i]}</th>)}
    </tr></thead>);
}

/** The headline numbers and the classes of answers, one column per run. */
export function ClassTable({ runs }: { runs: EvalShared[] }) {
  const reports = runs.map(discoveryOf);
  if (reports.some((r) => !r)) return <p className="text-[0.8rem] text-muted">One of the runs has no discovery answers, so there is nothing to put side by side.</p>;
  const ds = reports as DiscoveryReport[];
  const row = (label: string, f: (d: DiscoveryReport) => string) => (
    <tr key={label} className="border-b border-line/60"><td className="py-2 pr-4 text-fg">{label}</td>{ds.map((d, i) => <td key={i} className="py-2 pr-4">{f(d)}</td>)}</tr>);
  const critical = (d: DiscoveryReport) => (d.critical.n_critical ? `${rateText(d.critical.critical_detection_miss_rate)} (${d.critical.critical_detection_miss_rate.numerator} of ${d.critical.critical_detection_miss_rate.denominator})` : "no Critical case");
  return (
    <Wrap title="By kind of case" runs={runs}>
      <table className="w-full text-[0.82rem]">
        <Head runs={runs} first="Measure" />
        <tbody className="tnum text-muted">
          {row("Cases", (d) => num(d.overall.n))}
          {row("Vulnerable found", (d) => (d.overall.vulnerable.insufficient ? `too few (${d.overall.vulnerable.n})` : rateText(d.overall.vulnerable.ci?.issue_group, d.overall.vulnerable.recall)))}
          {row("False alarms", (d) => (d.overall.negative.insufficient ? `too few (${d.overall.negative.n})` : rateText(d.overall.negative.ci?.issue_group, d.overall.negative.fpr)))}
          {row("Insufficient context handled", (d) => insufficientText(d.overall))}
          {row("Critical not detected", critical)}
          {row("Abstained", (d) => pct(d.overall.abstention_rate))}
        </tbody>
      </table>
    </Wrap>);
}

/** One breakdown (by language or by ecosystem): for each value, what each run found and how often it raised a false alarm. */
export function BreakdownTable({ runs, by }: { runs: EvalShared[]; by: "by_language" | "by_ecosystem" }) {
  const reports = runs.map(discoveryOf);
  const keys = keysOf(reports, by);
  const title = by === "by_language" ? "By language" : "By ecosystem";
  if (!keys.length) return null;
  return (
    <Wrap title={title} runs={runs}>
      <table className="w-full text-[0.82rem]">
        <Head runs={runs} first={by === "by_language" ? "Language" : "Ecosystem"} />
        <tbody className="tnum text-muted">
          {keys.map((k) => <tr key={k} className="border-b border-line/60"><td className="py-2 pr-4 text-fg">{k}</td>{reports.map((d, i) => <Cell key={i} c={d?.[by]?.[k]} />)}</tr>)}
        </tbody>
      </table>
    </Wrap>);
}
function Cell({ c }: { c: DecisionCell | undefined }) {
  return (
    <td className="py-2 pr-4 align-top">
      <div>found {foundText(c)}</div><div className="text-faint">false alarms {alarmsText(c)}</div>
    </td>);
}

/** The confusion matrix of each run: what the truth was against what the answer was; for the runs after the first, each count shows its change from the first run's. */
export function ConfusionTables({ runs }: { runs: EvalShared[] }) {
  const reports = runs.map(discoveryOf);
  const { truths, outcomes } = gridOf(reports);
  if (!truths.length) return null;
  return (
    <Wrap title="What each answer was, against the truth" runs={runs}>
      <div className="grid gap-4 lg:grid-cols-2">
        {runs.map((r, i) => {
          const m = reports[i]?.overall.confusion, ref = reports[0]?.overall.confusion ?? {};
          return (
            <table key={r.tag} aria-label={`Confusion matrix of run ${LETTERS[i]}`} className="w-full text-[0.78rem]">
              <caption className="pb-1 text-left text-[0.74rem] text-faint">Run {LETTERS[i]}{i ? ", change from run A in brackets" : ""}</caption>
              <thead><tr className="border-b border-line text-left text-[0.7rem] uppercase tracking-wide text-faint"><th className={TH}>Truth</th>{outcomes.map((o) => <th key={o} className={TH}>{o.replace(/_/g, " ")}</th>)}</tr></thead>
              <tbody className="tnum text-muted">
                {truths.map((t) => (
                  <tr key={t} className="border-b border-line/60"><td className="py-1.5 pr-3 text-fg">{t.replace(/_/g, " ")}</td>
                    {outcomes.map((o) => <td key={o} className="py-1.5 pr-3">{m ? withDelta(m[t]?.[o] ?? 0, i ? ref[t]?.[o] ?? 0 : null) : "not measured"}</td>)}</tr>))}
              </tbody>
            </table>);
        })}
      </div>
    </Wrap>);
}

/** The runs by weakness class. `data` is undefined while it loads and null when it could not be read. */
export function CweTable({ runs, data }: { runs: EvalShared[]; data: CweData | null | undefined }) {
  if (data === undefined) return null;
  if (data === null) return <p className="text-[0.76rem] text-faint">The table by weakness class (CWE) could not be read.</p>;
  const why = data.runs.map((r, i) => (r.available ? null : <li key={r.tag + i}>Run {LETTERS[i]} has no weakness classes: {r.reason ?? "unknown"}.</li>)).filter(Boolean);
  const unmatched = data.runs.map((r, i) => (r.available && r.unmatched ? <li key={r.tag + i}>Run {LETTERS[i]}: {num(r.unmatched)} of its {num(r.items)} cases are not in its dataset's views and are left out.</li> : null)).filter(Boolean);
  return (
    <Wrap title="By weakness class (CWE)" runs={runs}>
      {data.cwes.length > 0 && (
        <table className="w-full text-[0.82rem]">
          <Head runs={runs} first="Weakness class" />
          <tbody className="tnum text-muted">
            {data.cwes.map((c) => (
              <tr key={c.cwe} className="border-b border-line/60"><td className="py-2 pr-4 text-fg">{c.cwe}</td>
                {c.cells.map((cell, i) => { const t = cweText(cell, data.runs[i]?.min_cell ?? 20); return <td key={i} className="py-2 pr-4 align-top"><div>found {t.found}</div><div className="text-faint">false alarms {t.alarms}</div></td>; })}</tr>))}
          </tbody>
        </table>)}
      {(why.length > 0 || unmatched.length > 0) && <ul className="mt-2 space-y-0.5 text-[0.76rem] text-muted">{why}{unmatched}</ul>}
      <p className="mt-2 text-[0.74rem] text-faint">{data.note}</p>
    </Wrap>);
}
