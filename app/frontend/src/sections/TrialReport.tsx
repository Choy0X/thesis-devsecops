import type {TrialImport} from '../lib/api';

const loss = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(4) : 'Not measured';

export function TrialReport({trial}: {trial: TrialImport}) {
  const comparison = trial.reports.comparison;
  const examples = Array.isArray(comparison?.examples) ? comparison.examples : [];
  const config = trial.config;
  return <section aria-label={`Trial ${trial.import_id}`} className="space-y-3 border border-line p-3 text-[0.8rem]">
    <h3 className="font-medium">{trial.state} trial, {trial.total_budget_minutes} minute budget</h3>
    <p>Experimental source learning evidence. This does not count as full training or security evaluation.{trial.software_test ? ' Software test on a tiny model.' : ''}</p>
    {config.runtime_recipe?.backend === 'tpu' && <p className="text-warn">Experimental TPU recipe: symmetric INT4 with BF16. This is separate from CUDA NF4. Hardware compatibility and available HBM are checked during execution; a completed comparison within 30 minutes is not guaranteed.</p>}
    {trial.adapter_evidence === 'unavailable_in_compact_recovery' && <p className="text-warn">Adapter verification unavailable: the compact recovery archive omitted its weights. Raw reports are preserved; no verified improvement claim is available. The full files remain in the remote workspace.</p>}
    <p>Phase: {String(config.phase ?? 'not recorded')}. {Number(config.steps_done ?? 0)} of {config.planned_steps == null ? 'unknown' : Number(config.planned_steps)} steps.
      {typeof config.remaining_seconds === 'number' ? ` ${Math.max(0, Math.round(config.remaining_seconds))} seconds remained when saved.` : ''}</p>
    {config.reason && <p className="text-warn">{String(config.reason)}</p>}
    <table className="w-full text-left tnum" aria-label="Paired source loss"><thead><tr><th>Measure</th><th>Untouched base</th><th>Tuned adapter</th></tr></thead>
      <tbody><tr><th className="py-2 font-normal">Token mean completion loss</th><td>{loss(comparison?.baseline?.token_mean_loss)}</td><td>{loss(comparison?.tuned?.token_mean_loss)}</td></tr>
        <tr><th className="font-normal">Views scored</th><td>{comparison?.baseline?.scored_views ?? 'Not measured'}</td><td>{comparison?.tuned?.scored_views ?? 'Not measured'}</td></tr></tbody></table>
    {typeof comparison?.loss_delta_tuned_minus_base === 'number' && <p>Tuned minus base loss: {loss(comparison.loss_delta_tuned_minus_base)}. A negative value describes lower loss on this bounded sample.</p>}
    {!comparison && <p className="text-muted">Paired comparison was not completed. Available evidence is preserved below.</p>}
    {examples.length > 0 && <div className="space-y-2"><h4 className="font-medium">Raw paired examples</h4>
      {examples.map((example: Record<string, any>, index: number) => <details key={String(example.view_id ?? index)} className="border-t border-line pt-2">
        <summary className="cursor-pointer">{String(example.view_id ?? index)}: {String(example.task ?? '')}</summary>
        <div className="mt-2 grid gap-3 md:grid-cols-2">{(['base', 'tuned'] as const).map(side => <div key={side}>
          <p className="font-medium">{side === 'base' ? 'Untouched base' : 'Tuned adapter'} ({String(example[side]?.state ?? 'not available')})</p>
          <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[0.75rem]">{String(example[side]?.text ?? 'No generated output')}</pre>
          {example[side]?.hit_limit && <p className="text-warn">Output reached its token limit.</p>}
        </div>)}</div>
      </details>)}</div>}
    <details><summary className="cursor-pointer">Remote resource report</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[0.72rem]">{trial.reports.resources ? JSON.stringify(trial.reports.resources, null, 2) : 'Resource measurements were not available.'}</pre></details>
    <details><summary className="cursor-pointer">Progress and available reports</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[0.72rem]">{JSON.stringify({progress: config, baseline: trial.reports.baseline_report, tuned: trial.reports.tuned_report}, null, 2)}</pre></details>
  </section>;
}
