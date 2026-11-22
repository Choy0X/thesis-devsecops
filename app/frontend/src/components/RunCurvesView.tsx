import { useEffect, useMemo, useState } from "react";
import { api, type CurveSource, type RunCurves } from "../lib/api";
import type { Pt } from "../lib/lttb";
import { Curve, type Series } from "./charts/Curve";
import { Skeleton } from "./ui";

// What a run did while it trained, drawn from the steps the trainer logged: the loss, how large the updates were, and the loss of each part of the answer
// (the decision, the evidence and so on). A run with no log says so; it never shows a flat line.

export const points = (steps: number[], values: (number | null)[] | undefined): Pt[] =>
  values ? steps.flatMap((x, i) => (values[i] == null ? [] : [{ x, y: values[i] as number }])) : [];

export function seriesFor(s: CurveSource): { loss: Series[]; grad: Series[]; fields: Series[] } {
  const label = s.label === "run" ? "" : `${s.label}: `;
  return {
    loss: [{ name: `${label}loss`, points: points(s.steps, s.loss) }].filter((x) => x.points.length),
    grad: [{ name: `${label}gradient norm`, points: points(s.steps, s.grad_norm) }].filter((x) => x.points.length),
    fields: Object.entries(s.fields).map(([name, values]) => ({ name: `${label}${name.replace(/_/g, " ")}`, points: points(s.steps, values) })).filter((x) => x.points.length),
  };
}

/** The validation points of a source as a curve of validation loss by step (a point without a loss is left out). */
export const validationSeries = (s: CurveSource): Series[] => {
  const pts = (s.evaluations ?? []).flatMap((e) => (e.val_loss == null ? [] : [{ x: e.step, y: e.val_loss }]));
  return pts.length ? [{ name: "validation loss", points: pts }] : [];
};

export const LIVE_MS = 15_000;       // how often the curves are read again while a training of this computer is running

export function RunCurvesView({ name, live = false }: { name: string; live?: boolean }) {
  const [data, setData] = useState<RunCurves | null | undefined>(undefined);
  // A failed refresh keeps the curves already drawn; without any, the card says they could not be read. Read once when the run is opened and once more
  // when a training starts or ends (so the finished curves are there), and every LIVE_MS while it runs.
  const load = () => api.runCurves(name).then(setData).catch(() => setData((d) => (d === undefined ? null : d)));
  useEffect(() => { load(); }, [name, live]);                // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(load, LIVE_MS);
    return () => window.clearInterval(t);
  }, [name, live]);                                          // eslint-disable-line react-hooks/exhaustive-deps
  const groups = useMemo(() => (data?.sources ?? []).map((s) => ({ s, ...seriesFor(s) })), [data]);
  return (
    <div>
      <div className="mb-2 text-[0.76rem] font-medium uppercase tracking-wide text-faint">Training curves</div>
      {data === undefined ? <Skeleton className="h-24 w-full" /> : data === null ? <p className="text-[0.8rem] text-muted">The curves could not be read.</p>
        : groups.length === 0 ? <p className="text-[0.8rem] text-muted">This run has no training log yet. The curves appear once a run (or a local GPU check) has written one.</p>
        : (
          <div className="space-y-4">
            {groups.map(({ s, loss, grad, fields }) => (
              <div key={s.label} className="space-y-3">
                <p className="text-[0.78rem] text-muted">{s.label === "run" ? "The run" : s.label.startsWith("Import ") ? s.label : s.label.startsWith("train-") ? `Local training ${s.label}` : `Local check ${s.label}`}: {s.rows} steps logged{s.skipped ? `, ${s.skipped} lines could not be read` : ""}.</p>
                {s.analytics && <section className="space-y-2 text-[0.8rem]" aria-label={`Detailed analytics of ${s.label}`}>
                  <p className="text-muted">{s.analytics.run?.state ?? "State unavailable"}. {s.analytics.summary?.committed_steps ?? "Unavailable"} committed steps, {s.analytics.summary?.committed_tokens ?? "unavailable"} supervised tokens. History: {s.analytics.summary?.completeness?.replace(/_/g, " ") ?? "unavailable"}.</p>
                  {s.analytics.import_id && <a className="inline-block py-2 text-brand-strong underline underline-offset-4 focus-visible:outline focus-visible:outline-2" href={`/api/finetune/runs/${encodeURIComponent(name)}/imports/${encodeURIComponent(s.analytics.import_id)}/analytics`} download>Download complete analytics JSON</a>}
                  <details className="text-muted"><summary className="cursor-pointer py-2 focus-visible:outline focus-visible:outline-2">Dataset, environment and provenance</summary>
                    <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words text-[0.75rem]">{JSON.stringify({ dataset: s.analytics.dataset, environment: s.analytics.environment, provenance: s.analytics.provenance, warnings: s.analytics.warnings }, null, 2)}</pre>
                  </details>
                </section>}
                {loss.length > 0 && <Curve title="Loss" description="How wrong the model's answers were on the training examples, step by step." series={loss} xLabel="step" yLabel="loss" height={180} />}
                {validationSeries(s).length > 0 && <Curve title="Validation loss" description="The loss on the validation examples at each evaluation. The run keeps the state with the best selection metric, not the lowest training loss." series={validationSeries(s)} xLabel="step" yLabel="loss" height={160} />}
                {(s.evaluations ?? []).length > 0 && (
                  <table className="w-full text-[0.78rem]" aria-label={`Evaluations of ${s.label}`}>
                    <thead><tr className="border-b border-line text-left text-[0.7rem] uppercase tracking-wide text-faint"><th className="py-1 pr-3 font-medium">Step</th><th className="pr-3 font-medium">Epoch</th><th className="pr-3 font-medium">Metric</th><th className="pr-3 font-medium">Value</th><th className="font-medium">Examples</th></tr></thead>
                    <tbody>{(s.evaluations ?? []).map((e) => (
                      <tr key={e.step} className="border-b border-line/50"><td className="py-1 pr-3 tnum">{e.step}</td><td className="pr-3 tnum">{e.family_pass == null ? "" : e.family_pass.toFixed(2)}</td>
                        <td className="pr-3">{(e.metric_name ?? "metric").replace(/_/g, " ")}</td><td className="pr-3 tnum">{e.metric == null ? "" : e.metric.toFixed(4)}</td><td className="tnum">{e.n_examples ?? ""}</td></tr>))}
                    </tbody>
                  </table>)}
                {fields.length > 0 && <Curve title="Loss by part of the answer" description="The same loss split by the part of the answer it measures." series={fields} xLabel="step" yLabel="loss" height={180} />}
                {grad.length > 0 && <Curve title="Size of the updates" description="The gradient norm: very large values mean unstable training." series={grad} xLabel="step" yLabel="norm" height={160} />}
                {s.lr && <Curve title="Learning rate" series={[{ name: "learning rate", points: points(s.steps, s.lr) }]} xLabel="step" yLabel="learning rate" height={160} />}
                {s.supervised_tokens_per_second && <Curve title="Training throughput" description="Measured supervised target tokens per second, including device completion." series={[{ name: "supervised tokens per second", points: points(s.steps, s.supervised_tokens_per_second) }]} xLabel="step" yLabel="tokens / second" height={160} />}
                {s.step_seconds && <Curve title="Step duration" series={[{ name: "step duration", points: points(s.steps, s.step_seconds) }]} xLabel="step" yLabel="seconds" height={160} />}
              </div>
            ))}
          </div>
        )}
    </div>
  );
}
