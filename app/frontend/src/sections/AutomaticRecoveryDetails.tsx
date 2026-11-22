import {useEffect, useState} from 'react';
import {Dialog} from '../components/hud/Dialog';
import {Button, ErrorState, Field} from '../components/ui';
import {Select} from '../components/kit/Select';
import {Checkbox} from '../components/kit/Toggles';
import {api, type AutomaticRecoveryAttemptPage, type AutomaticRecoveryDetail, type AutomaticRecoveryPage} from '../lib/api';

export function AutomaticRecoveryDetails({workflow, initialAttempt, active, onClose}: {
  workflow: string; initialAttempt: string; active: boolean; onClose: () => void;
}) {
  const [attempt, setAttempt] = useState(initialAttempt);
  const [attemptOffset, setAttemptOffset] = useState(0);
  const [attempts, setAttempts] = useState<AutomaticRecoveryAttemptPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [outcome, setOutcome] = useState('all');
  const [retryable, setRetryable] = useState(false);
  const [page, setPage] = useState<AutomaticRecoveryPage | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [item, setItem] = useState<string | null>(null);
  const [observation, setObservation] = useState('');
  const [detail, setDetail] = useState<AutomaticRecoveryDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [retryResult, setRetryResult] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController(); let mounted = true;
    const load = async () => {
      try {const next = await api.automaticRecoveryAttempts(workflow, attemptOffset, controller.signal); if (mounted) setAttempts(next);}
      catch (failure) {if (mounted && !controller.signal.aborted) setError(failure);}
    };
    void load(); const timer = window.setInterval(() => void load(), 5000);
    return () => {mounted = false; controller.abort(); window.clearInterval(timer);};
  }, [workflow, attemptOffset]);
  useEffect(() => {
    const controller = new AbortController(); let mounted = true;
    setPage(null); setSelected(new Set()); setItem(null); setDetail(null); setError(null);
    const load = async () => {
      try {const next = await api.automaticRecoveryItems(workflow, attempt, offset, outcome, retryable, controller.signal);
        if (mounted) {setPage(next); setError(null);}}
      catch (failure) {if (mounted && !controller.signal.aborted) setError(failure);}
    };
    void load(); const timer = window.setInterval(() => void load(), 5000);
    return () => {mounted = false; controller.abort(); window.clearInterval(timer);};
  }, [workflow, attempt, offset, outcome, retryable]);
  useEffect(() => {
    const controller = new AbortController(); let mounted = true; setDetail(null);
    if (item) void api.automaticRecoveryDetail(workflow, attempt, item, observation || undefined, controller.signal)
      .then(next => {if (mounted) setDetail(next);})
      .catch(failure => {if (mounted && !controller.signal.aborted) setError(failure);});
    return () => {mounted = false; controller.abort();};
  }, [workflow, attempt, item, observation]);
  const retry = async () => {
    setBusy(true); setError(null);
    try {const next = await api.retryAutomaticRecoveryItems(workflow, attempt, [...selected]);
      setRetryResult(`Retry job ${next.job_id} queued for ${next.selected} items.`); setSelected(new Set());}
    catch (failure) {setError(failure);} finally {setBusy(false);}
  };
  const sourceClass = 'max-h-64 overflow-auto whitespace-pre-wrap break-words border border-line bg-surface-2 p-3 font-mono text-[0.76rem]';
  const text = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return <Dialog open onOpenChange={open => {if (!open) onClose();}} title="Recovery item details"
    description="Source-bound outcomes and immutable context revisions. Repairs do not create security proof." className="w-[min(66rem,95vw)]">
    <div className="space-y-4 text-[0.8rem]">
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Attempt"><Select value={attempt} onValueChange={value => {setAttempt(value); setOffset(0);}} options={[
          ...(!attempts?.items.some(value => value.attempt_id === attempt) ? [{value: attempt, label: attempt.slice(0, 12)}] : []),
          ...(attempts?.items.map(value => ({value: value.attempt_id, label: `${value.action.replace(/_/g, ' ')} / ${value.state} / ${value.attempt_id.slice(0, 12)}`})) ?? []),
        ]} /></Field>
        <Field label="Outcome"><Select value={outcome} onValueChange={value => {setOutcome(value); setOffset(0);}}
          options={['all', 'unavailable', 'unsupported', 'excluded', 'not_verified', 'env_error', 'failed', 'repaired', 'unchanged', 'verified'].map(value => ({value, label: value.replace(/_/g, ' ')}))} /></Field>
        <Checkbox className="self-end pb-2" label="Retryable only" checked={retryable} onCheckedChange={checked => {setRetryable(checked); setOffset(0);}} />
      </div>
      {attempts && attempts.total > 100 && <div className="flex gap-3"><Button variant="ghost" disabled={!attemptOffset} onClick={() => setAttemptOffset(value => Math.max(0, value - 100))}>Previous attempts</Button><Button variant="ghost" disabled={attemptOffset + attempts.items.length >= attempts.total} onClick={() => setAttemptOffset(value => value + 100)}>Next attempts</Button></div>}
      {error != null && <ErrorState error={error} />}
      {!page && !error && <p role="status">Loading recovery outcomes...</p>}
      {page?.history_available === false && <p className="text-muted">This earlier attempt has no per-item history. Its aggregate result remains preserved.</p>}
      {page && page.history_available !== false && <>
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="ghost" disabled={!page.items.some(value => value.retryable)} onClick={() => setSelected(new Set(page.items.filter(value => value.retryable).map(value => value.item_id)))}>Select retryable items on this page</Button>
          <Button disabled={active || busy || !selected.size} loading={busy} onClick={() => void retry()}>Retry selected items ({selected.size})</Button>
        </div>
        {active && <p className="text-muted">Selected retries are available after the automatic workflow stops.</p>}
        {retryResult && <p role="status">{retryResult} The new attempt appears in the attempt list when its worker starts.</p>}
        <div className="overflow-x-auto"><table className="w-full text-left" aria-label="Automatic recovery outcomes">
          <thead className="text-muted"><tr><th className="pr-3">Select</th><th className="pr-3">Item and source</th><th className="pr-3">Outcome</th><th>Details</th></tr></thead>
          <tbody>{page.items.map(value => <tr key={value.item_id} className="border-t border-line align-top">
            <td className="py-3 pr-3"><Checkbox aria-label={`Select ${value.record_id}`} disabled={!value.retryable} checked={selected.has(value.item_id)} onCheckedChange={checked => setSelected(previous => {const next = new Set(previous); if (checked) next.add(value.item_id); else next.delete(value.item_id); return next;})} /></td>
            <td className="max-w-56 break-words py-3 pr-3"><p className="font-mono text-[0.76rem]">{value.record_id}</p>{value.source_locations?.map(location => <p key={location.side} className="text-muted">{location.side}: {location.path}{location.revision ? ` @ ${location.revision.slice(0, 12)}` : ''}</p>)}</td>
            <td className="max-w-72 break-words py-3 pr-3"><p>{value.status.replace(/_/g, ' ')}{value.cached ? ' (reused)' : ''}</p>{value.reasons.map((reason, index) => <p key={index} className="text-muted">{reason}</p>)}{!!value.missing_facts?.length && <p className="text-warn">Missing facts: {value.missing_facts.join(', ')}</p>}{value.outcome_summary_truncated && <p className="text-muted">Summary shortened. Open context for the complete outcome.</p>}</td>
            <td className="py-3"><Button variant="ghost" onClick={() => {setItem(value.item_id); setObservation('');}}>View context</Button></td>
          </tr>)}</tbody>
        </table></div>
        {!page.total && <p className="text-muted">No recorded items match these filters.</p>}
        <div className="flex flex-wrap items-center gap-3"><Button variant="ghost" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous items</Button><span className="tabular-nums text-muted">{page.total ? `${offset + 1}-${Math.min(offset + page.items.length, page.total)} of ${page.total}` : '0 items'}</span><Button variant="ghost" disabled={offset + page.items.length >= page.total} onClick={() => setOffset(value => value + 100)}>Next items</Button></div>
      </>}
      {item && !detail && !error && <p role="status">Loading immutable context details...</p>}
      {detail && <section aria-label="Immutable recovery context" className="space-y-3 border-t border-line pt-4">
        <h3 className="font-medium">{detail.record_id}</h3>
        {!!detail.observations.items.length && <Field label="Observation"><Select value={observation} onValueChange={setObservation}
          options={[{value: '', label: 'Latest'}, ...detail.observations.items.filter(value => value.observation_id).map(value => ({value: value.observation_id!, label: `${value.cursor}: ${value.status}`}))]} /></Field>}
        <div className="grid gap-3 sm:grid-cols-2">{['context_before', 'context_after'].map(side => <div key={side} className="min-w-0 space-y-2"><h4>Original {side === 'context_before' ? 'before' : 'after'} context</h4><pre className={sourceClass}>{text(detail.original[side])}</pre><h4>Recovered {side === 'context_before' ? 'before' : 'after'} context</h4><pre className={sourceClass}>{text(detail.recovered[side])}</pre></div>)}</div>
        <details open><summary>Context changes</summary>{Object.entries(detail.diff).map(([side, value]) => <div key={side} className="mt-2 space-y-1"><p className="text-muted">{side.replace(/_/g, ' ')}</p>{value.truncated && <p className="text-warn">Diff preview limited. Full original and recovered contexts are retained above.</p>}<pre className={sourceClass}>{value.unified || 'No changes.'}</pre></div>)}</details>
        <details><summary>Source bindings and repair identity</summary><pre className={sourceClass}>{text({repair_revision_id: detail.repair_revision_id, bindings: detail.source_bindings, artifacts: detail.artifacts})}</pre></details>
        <details><summary>Complete outcome</summary><pre className={sourceClass}>{text(detail.outcome ?? {status: detail.status, reasons: detail.reasons})}</pre></details>
      </section>}
    </div>
  </Dialog>;
}
