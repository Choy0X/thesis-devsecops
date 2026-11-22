import { useEffect, useMemo, useState } from "react";
import { api, type CurveSource, type EvalShared, type RunCurves } from "../lib/api";
import { Curve, MAX_SERIES, type Series } from "../components/charts/Curve";
import { LIVE_MS, points, validationSeries } from "../components/RunCurvesView";
import { LETTERS } from "./compareReport";

// The training curves of the runs being compared, laid over each other on one step axis. A run that is still training (its log ends sooner) is drawn against a finished one
// at the steps they share, and the numbers at the last step all of them have reached are written out.

/** The training log to draw for a run: the run's own, else the newest local training. */
export function sourceOf(c: RunCurves | null | undefined): CurveSource | null {
  const sources = c?.sources ?? [];
  return sources.find((s) => s.label === "run") ?? sources[sources.length - 1] ?? null;
}

const at = (s: Series, step: number): number | null => {
  let best: { x: number; y: number } | null = null;
  for (const p of s.points) if (p.x <= step && (!best || p.x > best.x)) best = p;
  return best ? best.y : null;
};

/** The series of each distinct training run among the compared evaluation runs (two evaluations of one training run draw its curve once), and the step every series has reached. */
export function overlay(runs: EvalShared[], data: Record<string, RunCurves | null>, kind: "loss" | "validation"): { series: Series[]; matching: number | null; values: string } {
  const seen = new Set<string>();
  const series: Series[] = [];
  runs.forEach((r, i) => {
    const name = r.run_info?.run;
    if (!name || seen.has(name)) return;
    seen.add(name);
    const src = sourceOf(data[name]);
    if (!src) return;
    const pts = kind === "loss" ? points(src.steps, src.loss) : validationSeries(src)[0]?.points ?? [];
    if (pts.length) series.push({ name: `${LETTERS[i]}: ${name}`, points: pts });
  });
  if (!series.length) return { series, matching: null, values: "" };
  const matching = Math.min(...series.map((s) => s.points[s.points.length - 1].x));
  const values = series.map((s) => { const y = at(s, matching); return `${s.name.split(":")[0]} ${y === null ? "none" : y.toFixed(4)}`; }).join(", ");
  return { series, matching, values };
}

export function CompareCurves({ runs, live = false }: { runs: EvalShared[]; live?: boolean }) {
  const names = useMemo(() => [...new Set(runs.map((r) => r.run_info?.run).filter((n): n is string => !!n))], [runs]);
  const [data, setData] = useState<Record<string, RunCurves | null> | null>(null);
  const key = names.join("|");
  const load = () => Promise.all(names.map((n) => api.runCurves(n).then((c) => [n, c] as const).catch(() => [n, null] as const))).then((rows) => setData(Object.fromEntries(rows)));
  useEffect(() => { setData(null); if (names.length) load(); }, [key]);                          // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!live || !names.length) return;
    const t = window.setInterval(load, LIVE_MS);
    return () => window.clearInterval(t);
  }, [key, live]);                                                                            // eslint-disable-line react-hooks/exhaustive-deps
  const loss = useMemo(() => (data ? overlay(runs, data, "loss") : null), [runs, data]);
  const validation = useMemo(() => (data ? overlay(runs, data, "validation") : null), [runs, data]);
  if (!names.length) return <p className="text-[0.8rem] text-muted">These runs do not say which training run they came from, so there are no curves to lay over each other.</p>;
  if (data === null) return null;
  if (!loss || loss.series.length === 0) return <p className="text-[0.8rem] text-muted">None of these runs has a training log, so there is nothing to lay over each other. A base model that was only evaluated has none.</p>;
  return (
    <div className="space-y-3">
      <Curve title="Training loss of the runs, by step" description="Each run's loss at each optimizer step, on one step axis. A run that is still training ends where its log ends." series={loss.series.slice(0, MAX_SERIES)} xLabel="step" yLabel="loss" height={200} />
      <p className="text-[0.76rem] text-muted">At step {loss.matching}, the last step every run here has reached: {loss.values}.{live ? " A training on this computer is being read again every 15 seconds." : ""}</p>
      {validation && validation.series.length > 0 && <Curve title="Validation loss of the runs, by step" description="The loss on the validation examples at each evaluation." series={validation.series.slice(0, MAX_SERIES)} xLabel="step" yLabel="loss" height={180} />}
    </div>);
}
