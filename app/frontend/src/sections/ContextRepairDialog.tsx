import {useEffect, useRef, useState} from 'react';
import {Dialog} from '../components/hud/Dialog';
import {Button, ErrorState} from '../components/ui';
import {Checkbox, SelectField} from '../components/kit';
import {api, type ContextRepairPage, type ContextRepairDetail, type ContextRepairAttempts, type ContextRepairOutcomes, type ContextRepairEvents, type ContextRepairOutcome} from '../lib/api';
import {useJob} from '../lib/useJob';
import {ContextExchangePanel} from './ContextExchangePanel';

const METHODS = [
  {id: 'local', label: 'Local model', description: 'Generate source-grounded explanations on your GPU.'},
  {id: 'json', label: 'JSON exchange', description: 'Export evidence and import returned JSON files.'},
  {id: 'free_api', label: 'Free LLM API', description: 'Use explicitly enabled free provider profiles.'},
] as const;
type Method = typeof METHODS[number]['id'];

export function ContextRepairDialog({tag, open, onOpenChange, onChanged, sourceRecoveryAvailable = true, sourceRecoveryUnavailableReason}: {tag: string; open: boolean; onOpenChange: (open: boolean) => void; onChanged: () => void; sourceRecoveryAvailable?: boolean; sourceRecoveryUnavailableReason?: string}) {
  const [detailTarget, setDetailTarget] = useState<{record: string; attempt?: string} | null>(null);
  const [attemptId, setAttemptId] = useState<string | undefined>();
  const [step, setStep] = useState(0);
  const [method, setMethod] = useState<Method>('local');
  const [page, setPage] = useState<ContextRepairPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [outcomeFilter, setOutcomeFilter] = useState('all');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [resolved, setResolved] = useState<number[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const request = useRef(0);
  const job = useJob();
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && ['done', 'error', 'cancelled', 'interrupted'].includes(job.status)) onChanged();
    wasRunning.current = job.status === 'running';
  }, [job.status, onChanged]);
  const storage = `context-repair-job:${tag}`;
  useEffect(() => {const saved = localStorage.getItem(storage); if (saved) job.attach(saved);}, [storage, job.attach]);
  useEffect(() => {
    const current = ++request.current;
    if (!open || step !== 1) return;
    setPage(null); setError(null);
    void api.contextRepairItems(tag, offset, outcomeFilter).then(value => {if (current === request.current) setPage(value);}).catch(failure => {if (current === request.current) setError(failure);});
    return () => {request.current++;};
  }, [tag, offset, open, step, outcomeFilter]);
  const perform = async (action: () => Promise<void>) => {setBusy(true); setError(null); try {await action();} catch (failure) {setError(failure);} finally {setBusy(false);}};
  const change = (id: string) => setSelected(previous => {const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next;});
  const recover = () => perform(async () => {
    const result = await api.repairContext(tag, [...selected], 'source');
    if (!result.job_id) throw new Error('No recovery job was created.');
    localStorage.setItem(storage, result.job_id); job.attach(result.job_id); setAttemptId(result.attempt_id); setDetailTarget(null); setResolved(null); setStep(2); onChanged();
  });
  const continueMethod = () => perform(async () => {const result = await api.repairContext(tag, [...selected], method); setResolved(result.ids ?? []); setStep(2);});
  const zero = page?.summary.explanation_eligible === 0;
  return <Dialog open={open} onOpenChange={onOpenChange} title="Repair insufficient context" description={`dataset_${tag}. Recover source facts or improve missing explanations without changing the frozen dataset.`} className="w-[min(58rem,95vw)]">
    <div className="space-y-4 text-[0.82rem]">
      <ol aria-label="Repair steps" className="flex flex-wrap gap-x-5 gap-y-2 border-b border-line pb-3 text-muted">{['Method', 'Items', 'Progress and results'].map((label, index) => <li key={label} aria-current={index === step ? 'step' : undefined} className={index === step ? 'font-medium text-fg' : ''}>{index + 1}. {label}</li>)}</ol>
      {step === 0 && <>
        <div role="radiogroup" aria-label="Explanation method" className="grid gap-3 sm:grid-cols-3">{METHODS.map(option => <button type="button" role="radio" aria-checked={method === option.id} key={option.id} onClick={() => setMethod(option.id)} className={`space-y-2 border p-4 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-strong ${method === option.id ? 'border-brand bg-brand-tint' : 'border-line'}`}><span className="block font-medium">{option.label}</span><span className="block text-[0.78rem] text-muted">{option.description}</span></button>)}</div>
        <p className="text-muted">Missing source evidence is recovered from exact repository snapshots. An LLM cannot supply security proof or invent missing source facts.</p>
        <Button variant="primary" onClick={() => setStep(1)}>Continue</Button>
        <Button variant="ghost" onClick={() => {setResolved(null); setStep(2);}}>Open recovery history</Button>
      </>}
      {step === 1 && <>
        {!page && !error && <p role="status">Reading context candidates...</p>}
        {page && <>
          <SelectField label="Recovery outcome" value={outcomeFilter} onValueChange={value => {setOutcomeFilter(value); setOffset(0); setSelected(new Set());}} options={[{value: 'all', label: 'All insufficient-context items'}, {value: 'unresolved', label: 'Unresolved after recovery'}, {value: 'failed', label: 'Failed recovery'}, {value: 'repaired', label: 'Source context repaired'}]} />
          <div className="flex flex-wrap items-center justify-between gap-3"><p>{selected.size} selected</p><div className="flex flex-wrap gap-2"><Button disabled={busy || !page.total} onClick={() => void perform(async () => {setSelected(new Set((await api.contextRepairIds(tag, outcomeFilter)).ids));})}>Select all matching items</Button><Button variant="ghost" disabled={!selected.size} onClick={() => setSelected(new Set())}>Clear selection</Button></div></div>
          {zero && <p role="status" className="border-l-2 border-warn pl-3 text-muted">No items need explanation generation. Existing explanations are usable; recover the missing source evidence instead.</p>}
          <div className="overflow-x-auto"><table className="w-full text-left" aria-label="Insufficient context items"><thead className="text-muted"><tr><th className="pr-3">Select</th><th className="pr-3">Item</th><th className="pr-3">Missing evidence</th><th className="pr-3">Explanation</th><th>Recovery result</th><th className="pl-3">Context</th></tr></thead><tbody>{page.items.map(item => <tr key={item.record_id} className="border-t border-line align-top"><td className="py-3 pr-3"><Checkbox aria-label={`Select ${item.pair_key}`} checked={selected.has(item.record_id)} onCheckedChange={() => change(item.record_id)} /></td><td className="max-w-64 break-all py-3 pr-3 font-mono text-[0.76rem]">{item.pair_key}</td><td className="py-3 pr-3 text-[0.78rem]">{item.missing_facts.length ? <details><summary className="cursor-pointer">{item.missing_facts.length} source facts</summary><ul className="mt-2 space-y-1 text-muted">{item.missing_facts.map(fact => <li key={fact}>{fact}</li>)}</ul></details> : 'Revalidation required'}</td><td className="py-3 pr-3 text-[0.78rem]">{item.explanation_state.replace(/_/g, ' ')}</td><td className="py-3 text-[0.78rem]">{item.recovery ? <details><summary className="cursor-pointer">{item.recovery.status.replace(/_/g, ' ')}</summary>{item.recovery.error && <p className="text-warn">{item.recovery.error}</p>}{[...(item.recovery.reasons ?? []), ...(item.recovery.remaining_missing_facts ?? [])].map((reason, index) => <p key={index} className="mt-1 text-muted">{reason}</p>)}<p className="mt-1 text-muted">Requires admission revalidation in a new build.</p></details> : 'Not attempted'}</td><td className="py-3 pl-3"><Button variant="ghost" aria-label={`View context for ${item.pair_key}`} onClick={() => setDetailTarget({record: item.record_id})}>View context</Button></td></tr>)}</tbody></table></div>
          {!page.total && <p>No insufficient-context records in this dataset.</p>}
          <div className="flex flex-wrap items-center gap-3 text-muted"><Button variant="ghost" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous</Button><span>{page.total ? `${offset+1}-${Math.min(offset+page.items.length, page.total)} of ${page.total}` : '0 items'}</span><Button variant="ghost" disabled={offset+page.items.length >= page.total} onClick={() => setOffset(value => value+100)}>Next</Button></div>
          <div className="flex flex-wrap gap-3 border-t border-line pt-4"><Button variant="primary" loading={busy} disabled={!selected.size || !sourceRecoveryAvailable || job.status === 'running'} onClick={() => void recover()}>Recover source evidence</Button><Button disabled={busy || !selected.size || zero} onClick={() => void continueMethod()}>Continue with selected method</Button></div>
          {!sourceRecoveryAvailable && <p role="status" className="text-muted">{sourceRecoveryUnavailableReason || "New source recovery is unavailable. Saved attempts and context remain readable."}</p>}
          <p className="text-[0.76rem] text-muted">This list describes the frozen version. Execution revalidates selected sources. Repaired evidence requires a new dataset build.</p>
        </>}
        <Button variant="ghost" onClick={() => setStep(0)}>Back to method</Button>
      </>}
      {step === 2 && <>
        {resolved ? <ContextExchangePanel ids={resolved} method={method} onDone={onChanged} /> : <div className="space-y-3">
          {job.id && <p role="status">Source recovery: {job.status}{job.progress?.label ? `. ${job.progress.label}` : ''}</p>}
          {job.progress?.total != null && <p>{job.progress.processed ?? 0} of {job.progress.total}</p>}
          {job.progress && 'counts' in job.progress && <dl className="flex flex-wrap gap-4">{Object.entries(job.progress.counts as Record<string, number> ?? {}).map(([name, count]) => <div key={name}><dt className="text-muted">{name.replace(/_/g, ' ')}</dt><dd className="tabular-nums">{count}</dd></div>)}</dl>}
          {job.status === 'running' && job.id && <Button onClick={() => void perform(async () => {if (attemptId) await api.cancelContextRepairAttempt(tag, attemptId); await api.cancelJob(job.id!); onChanged();})}>Cancel recovery</Button>}
          {['done', 'cancelled', 'interrupted', 'error'].includes(job.status) && <p className="text-muted">Completed outcomes are retained. Unresolved items can be retried. Rebuild a new version to revalidate admission; this frozen dataset is unchanged.</p>}
          {job.status === 'error' && <ErrorState error={job.failure || new Error(job.error || 'Recovery failed.')} />}
          <RecoveryHistory tag={tag} preferredAttempt={attemptId} onDetail={(record, attempt) => setDetailTarget({record, attempt})} onRetry={() => void perform(async () => {setSelected(new Set((await api.contextRepairIds(tag, 'unresolved')).ids)); setOutcomeFilter('unresolved'); setOffset(0); setDetailTarget(null); setStep(1);})} />
        </div>}
        <Button variant="ghost" onClick={() => {setStep(1); setResolved(null);}}>Back to items</Button>
      </>}
      {detailTarget && open && step !== 0 && <ContextDetail key={`${tag}:${detailTarget.record}:${detailTarget.attempt ?? "latest"}`} tag={tag} record={detailTarget.record} attempt={detailTarget.attempt} onClose={() => setDetailTarget(null)} />}
      {error != null && <ErrorState error={error} />}
    </div>
  </Dialog>;
}


function OutcomeExplanation({outcome}: {outcome: ContextRepairOutcome}) {
  return <div className="space-y-1 text-[0.78rem]">
    <p>{outcome.status.replace(/_/g, ' ')}</p>{outcome.retryable != null && <p className="text-muted">{outcome.retryable ? 'Retry available' : 'No retry needed'}</p>}
    {outcome.error && <p className="text-warn break-words">{outcome.error}</p>}
    {(outcome.reasons ?? []).map((reason, index) => <p key={index} className="break-words text-muted">{reason}</p>)}
    {!!outcome.remaining_missing_facts?.length && <div><p className="font-medium">Remaining gaps</p><ul className="space-y-1 text-muted">{outcome.remaining_missing_facts.map((gap, index) => <li key={index} className="break-words">{gap}</li>)}</ul></div>}
    {outcome.legacy_binding && <p className="text-muted">Legacy outcome. Manifest association inferred from record identity; original repair details were not recorded.</p>}
  </div>;
}

function ContextDetail({tag, record, attempt, onClose}: {tag: string; record: string; attempt?: string; onClose: () => void}) {
  const [value, setValue] = useState<ContextRepairDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let active = true;
    void api.contextRepairDetail(tag, record, attempt).then(result => {if (active) setValue(result);}).catch(failure => {if (active) setError(failure);});
    return () => {active = false;};
  }, [tag, record, attempt]);
  const text = (body: unknown) => typeof body === 'string' ? body : JSON.stringify(body, null, 2);
  const sourceClass = 'max-h-64 overflow-auto whitespace-pre-wrap break-words border border-line bg-surface-2 p-3 font-mono text-[0.76rem]';
  return <section aria-label="Context detail" className="space-y-3 border-t border-line pt-4">
    <div className="flex items-center justify-between gap-3"><h3 className="min-w-0 break-all font-medium">Context for {record}</h3><Button variant="ghost" onClick={onClose}>Close context</Button></div>
    {!value && !error && <p role="status">Loading original and recovered context...</p>}
    {error != null && <ErrorState error={error} />}
    {value && <>
      {value.outcome && <OutcomeExplanation outcome={value.outcome} />}
      {value.detail_unavailable && <p className="text-muted">This outcome has no saved repair artifact. The original frozen context remains available.</p>}
      <div className="grid gap-3 sm:grid-cols-2">{(['context_before', 'context_after'] as const).map(side => <div key={side} className="min-w-0 space-y-2"><h4 className="font-medium">Original {side === 'context_before' ? 'before' : 'after'} context</h4><pre className={sourceClass}>{text(value.original[side])}</pre>{value.repaired && <><h4 className="pt-2 font-medium">Repaired {side === 'context_before' ? 'before' : 'after'} context</h4><pre className={sourceClass}>{text(value.repaired[side])}</pre></>}</div>)}</div>
      <details open><summary className="cursor-pointer font-medium">Context diff</summary>{Object.entries(value.diff).map(([side, diff]) => <div key={side} className="mt-2 space-y-1"><p className="text-muted">{side.replace(/_/g, ' ')}</p><pre className={sourceClass}>{diff || 'No changes.'}</pre></div>)}</details>
      <details><summary className="cursor-pointer text-muted">Repair identities and source bindings</summary><pre className={`${sourceClass} mt-2`}>{JSON.stringify({artifact_id: value.artifact_id, repair_ids: value.repair_ids, source_manifest_sha256: value.source_manifest_sha256, original_binding: value.source_binding, input_binding: value.input_binding, repaired_binding: value.repaired_binding}, null, 2)}</pre></details>
      <p className="text-[0.76rem] text-muted">Recovered context requires admission revalidation in a new dataset build.</p>
    </>}
  </section>;
}

function RecoveryHistory({tag, preferredAttempt, onDetail, onRetry}: {tag: string; preferredAttempt?: string; onDetail: (record: string, attempt: string) => void; onRetry: () => void}) {
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [history, setHistory] = useState<ContextRepairAttempts | null>(null);
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState<string | undefined>(preferredAttempt);
  const [outcomeOffset, setOutcomeOffset] = useState(0);
  const [outcomes, setOutcomes] = useState<ContextRepairOutcomes | null>(null);
  const [events, setEvents] = useState<ContextRepairEvents['items']>([]);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {if (preferredAttempt) {setAttempt(preferredAttempt); setOutcomeOffset(0);}}, [preferredAttempt]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    setError(null);
    const load = async () => {
      try {
        const result = await api.contextRepairAttempts(tag, offset);
        if (!active) return;
        setHistory(result); setAttempt(previous => previous ?? result.items[0]?.attempt_id);
        if (result.items.some(item => item.state === 'running')) timer = setTimeout(() => void load(), 2000);
      } catch (failure) {if (active) setError(failure);}
    };
    void load();
    return () => {active = false; clearTimeout(timer);};
  }, [tag, offset, preferredAttempt, revision]);
  const running = history?.items.find(item => item.attempt_id === attempt)?.state === 'running';
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    setOutcomes(null);
    if (!attempt) return;
    const load = async () => {
      try {
        const result = await api.contextRepairOutcomes(tag, attempt, outcomeOffset);
        if (!active) return;
        setOutcomes(result);
        if (running) timer = setTimeout(() => void load(), 2000);
      } catch (failure) {if (active) setError(failure);}
    };
    void load();
    return () => {active = false; clearTimeout(timer);};
  }, [tag, attempt, outcomeOffset, running]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let cursor = 0;
    setEvents([]);
    if (!attempt) return;
    const load = async () => {
      try {
        const result = await api.contextRepairEvents(tag, attempt, cursor);
        if (!active) return;
        cursor = result.next_cursor;
        setEvents(previous => [...previous, ...result.items].slice(-100));
        if (result.has_more || running) timer = setTimeout(() => void load(), result.has_more ? 0 : 2000);
      } catch (failure) {if (active) setError(failure);}
    };
    void load();
    return () => {active = false; clearTimeout(timer);};
  }, [tag, attempt, running]);
  return <section aria-label="Recovery history" className="space-y-3 border-t border-line pt-4">
    <h3 className="font-medium">Recovery attempts</h3>
    {!history && !error && <p role="status">Loading saved recovery attempts...</p>}
    {error != null && <ErrorState error={error} />}
    {history && <>
      {!history.total && <p className="text-muted">No saved recovery attempts for this dataset.</p>}
      {!!history.total && <>
        <div className="overflow-x-auto"><table className="w-full text-left" aria-label="Recovery attempts"><thead className="text-muted"><tr><th className="pr-3">Attempt</th><th className="pr-3">Started</th><th className="pr-3">State</th><th>Items</th><th className="pl-3">Action</th></tr></thead><tbody>{history.items.map(item => <tr key={item.attempt_id} className="border-t border-line"><td className="py-2 pr-3"><Button variant="ghost" aria-pressed={attempt === item.attempt_id} onClick={() => {setAttempt(item.attempt_id); setOutcomeOffset(0);}}>{item.attempt_id.slice(0, 12)}</Button></td><td className="py-2 pr-3 text-[0.76rem]">{item.created}</td><td className="py-2 pr-3">{item.state}</td><td className="py-2 tabular-nums">{Object.entries(item.counts).map(([state, count]) => `${state}: ${count}`).join(', ')} / {item.total}</td><td className="py-2 pl-3">{item.state === 'running' && <Button variant="ghost" disabled={cancelling != null} aria-label={`Cancel attempt ${item.attempt_id.slice(0, 12)}`} onClick={() => {setCancelling(item.attempt_id); setError(null); void api.cancelContextRepairAttempt(tag, item.attempt_id).then(() => setRevision(value => value + 1)).catch(setError).finally(() => setCancelling(null));}}>Cancel attempt</Button>}</td></tr>)}</tbody></table></div>
        <div className="flex flex-wrap items-center gap-3"><Button variant="ghost" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous attempts</Button><span className="text-muted">{offset + 1}-{Math.min(offset + history.items.length, history.total)} of {history.total}</span><Button variant="ghost" disabled={offset + history.items.length >= history.total} onClick={() => setOffset(value => value + 100)}>Next attempts</Button></div>
        {attempt && !outcomes && !error && <p role="status">Loading recovery items...</p>}
        {outcomes && <>
          <h4 className="pt-2 font-medium">Attempt items</h4>
          <div className="overflow-x-auto"><table className="w-full text-left" aria-label="Recovery item outcomes"><thead className="text-muted"><tr><th className="pr-3">Item</th><th className="pr-3">Result and remaining gaps</th><th>Context</th></tr></thead><tbody>{outcomes.items.map(item => <tr key={item.record_id} className="border-t border-line align-top"><td className="max-w-48 break-all py-3 pr-3 font-mono text-[0.76rem]">{item.record_id}</td><td className="py-3 pr-3"><OutcomeExplanation outcome={item} /></td><td className="py-3"><Button variant="ghost" aria-label={`View context for ${item.record_id}`} onClick={() => onDetail(item.record_id!, attempt!)}>View context</Button></td></tr>)}</tbody></table></div>
          <div className="flex flex-wrap items-center gap-3"><Button variant="ghost" disabled={!outcomeOffset} onClick={() => setOutcomeOffset(value => Math.max(0, value - 100))}>Previous outcomes</Button><span className="text-muted">{outcomes.total ? `${outcomeOffset + 1}-${Math.min(outcomeOffset + outcomes.items.length, outcomes.total)} of ${outcomes.total}` : '0 items'}</span><Button variant="ghost" disabled={outcomeOffset + outcomes.items.length >= outcomes.total} onClick={() => setOutcomeOffset(value => value + 100)}>Next outcomes</Button><Button disabled={running} onClick={onRetry}>Select unresolved items</Button></div>
        </>}
        {!!events.length && <details><summary className="cursor-pointer text-muted">Recent activity ({events.length})</summary><ol className="mt-2 max-h-40 space-y-1 overflow-auto text-[0.76rem] text-muted">{events.map(event => <li key={event.cursor}>{event.created}. {event.kind.replace(/_/g, ' ')}{event.record_id ? `. ${event.record_id}` : ''}</li>)}</ol></details>}
      </>}
    </>}
  </section>;
}
