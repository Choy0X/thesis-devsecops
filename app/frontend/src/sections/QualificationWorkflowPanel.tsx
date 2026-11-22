import {useEffect, useRef, useState} from 'react';
import {Button, ErrorState} from '../components/ui';
import {api, type QualificationWorkflow} from '../lib/api';
import {AutomaticRecoveryDetails} from './AutomaticRecoveryDetails';

const activeStates = new Set(['queued', 'running', 'waiting', 'interrupted', 'cancelling']);

export function QualificationWorkflowPanel({tag, onChanged}: {tag: string; onChanged?: () => void}) {
  const [item, setItem] = useState<QualificationWorkflow | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [phases, setPhases] = useState<Array<{id: string; phase: string; state: string; wave: number}>>([]);
  const [recoveryAttempt, setRecoveryAttempt] = useState<string | null>(null);
  const requestKey = useRef<string>();
  const changed = useRef(onChanged);
  changed.current = onChanged;
  useEffect(() => {
    const controller = new AbortController();
    let mounted = true;
    setItem(null); setPhases([]); setError(null); requestKey.current = undefined;
    void api.qualificationWorkflows(tag, controller.signal).then(page => {
      if (mounted) setItem(page.items[0] ?? null);
    }).catch(failure => {if (mounted && !controller.signal.aborted) setError(failure);});
    return () => {mounted = false; controller.abort();};
  }, [tag]);
  const id = item?.id;
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    let mounted = true;
    let running = activeStates.has(item?.state ?? '');
    const refresh = async () => {
      try {
        const next = await api.qualificationWorkflow(id, controller.signal);
        if (!mounted) return;
        if (running && !activeStates.has(next.workflow.state)) changed.current?.();
        running = activeStates.has(next.workflow.state);
        setItem(next.workflow); setPhases(next.phases.items); setError(null);
      } catch (failure) {if (mounted && !controller.signal.aborted) setError(failure);}
    };
    void refresh();
    // A retained failed workflow can be resumed by the jobs workspace or a
    // second client. Refresh only this mounted detail so it observes that claim.
    const timer = window.setInterval(() => {void refresh();}, 5000);
    return () => {mounted = false; controller.abort(); window.clearInterval(timer);};
  }, [id]);
  const start = async () => {
    setBusy(true); setError(null);
    requestKey.current ??= crypto.randomUUID();
    try {
      if (item?.state === 'failed') {
        await api.startJob('qualify_npm_dataset', {workflow_id: item.id});
        setItem({...item, state: 'queued', reason: 'Retry accepted; waiting for the workflow worker.'});
      } else {
        const next = await api.startQualificationWorkflow(tag, requestKey.current); setItem(next.workflow);
      }
    }
    catch (failure) {setError(failure);}
    finally {setBusy(false);}
  };
  const stop = async () => {
    if (!item) return;
    setBusy(true);
    try {setItem((await api.cancelQualificationWorkflow(item.id)).workflow);}
    catch (failure) {setError(failure);}
    finally {setBusy(false);}
  };
  const active = item != null && activeStates.has(item.state);
  const count = item?.phase_progress;
  return <section aria-label="Automatic evidence workflow" className="space-y-2 border-t border-line pt-3 text-[0.78rem]">
    <div className="flex flex-wrap items-center gap-3">
      <Button loading={busy} disabled={active} onClick={() => void start()}>{item?.state === 'failed' ? 'Retry evidence workflow' : 'Automate evidence'}</Button>
      {active && <Button variant="ghost" disabled={busy} onClick={() => void stop()}>Stop evidence workflow</Button>}
      {item && <span role="status">{item.state.replace(/_/g, ' ')}{item.phase ? ` / ${item.phase.replace(/_/g, ' ')}` : ''}</span>}
    </div>
    {item?.reason && item.state !== 'running' && <p>{item.reason}</p>}
    {item?.pending_verification?.reason && <p className="text-warn">Verification waiting: {item.pending_verification.reason}</p>}
    {count?.label && <p>{count.label}</p>}
    {count?.total != null && <p className="tabular-nums">{count.processed ?? 0} of {count.total}</p>}
    {item?.dataset_tag && <p className="text-muted">Dataset: {item.dataset_tag}</p>}
    {item?.run_preparation?.state === 'pending_data_qualification' && <p className="text-muted">Run preparation waits for data qualification.</p>}
    {!!phases.length && <details><summary className="cursor-pointer text-muted">Workflow history</summary>
      <ul className="mt-2 space-y-1">{phases.map(phase => <li key={phase.id} className="flex flex-wrap items-center gap-3">Wave {phase.wave}: {phase.phase.replace(/_/g, ' ')} / {phase.state}{['repair', 'verify'].includes(phase.phase) && <Button variant="ghost" onClick={() => setRecoveryAttempt(phase.id)}>View item outcomes</Button>}</li>)}</ul>
    </details>}
    {item && recoveryAttempt && <AutomaticRecoveryDetails key={`${item.id}:${recoveryAttempt}`} workflow={item.id} initialAttempt={recoveryAttempt} active={active} onClose={() => setRecoveryAttempt(null)} />}
    {error != null && <ErrorState error={error} />}
  </section>;
}
