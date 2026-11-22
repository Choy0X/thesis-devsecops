import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, ErrorState } from '../components/ui';
import { api, type BuildEvidencePage, type ProfileBuild } from '../lib/api';
import { useJob } from '../lib/useJob';
import { ContextRepairDialog } from './ContextRepairDialog';
import { QualificationWorkflowPanel } from './QualificationWorkflowPanel';

export function BuildEvidenceActions({build, onChanged, onCreated}: {build: ProfileBuild; onChanged: () => void; onCreated: (build: ProfileBuild) => void}) {
  const job = useJob();
  const [busy, setBusy] = useState(false);
  const [repairOpen, setRepairOpen] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const wasRunning = useRef(false);
  const actions = build.evidence_actions;
  const artifactAvailable = build.evidence_artifact_available ?? Boolean(actions?.repair.available ||
    build.files?.['records.parquet'] != null || ['frozen', 'published'].includes(build.status) || ['ready', 'historical'].includes(build.state ?? ''));
  useEffect(() => {if (build.evidence_job_id) job.attach(build.evidence_job_id);}, [build.evidence_job_id, job.attach]);
  useEffect(() => {
    if (wasRunning.current && ['done', 'error', 'cancelled', 'interrupted'].includes(job.status)) onChanged();
    wasRunning.current = job.status === 'running';
  }, [job.status, onChanged]);
  const act = async (action: 'repair' | 'verify_failed' | 'rebuild') => {
    setBusy(true); setError(null);
    try {
      if (action === 'rebuild') onCreated((await api.retryBuild(build.tag)).build);
      else {const result = await api.repairBuildEvidence(build.tag, action); job.attach(result.job_id); onChanged();}
    } catch (failure) {setError(failure);} finally {setBusy(false);}
  };
  const running = busy || job.status === 'running';
  return <div className="mt-4 space-y-3">
    {artifactAvailable && <QualificationWorkflowPanel tag={build.tag} onChanged={onChanged} />}
    {actions ? <>
      <div className="flex flex-wrap gap-2">
        <Button disabled={running || !actions.repair.available} onClick={() => void act('repair')}>Repair evidence</Button>
        <Button disabled={!artifactAvailable} onClick={() => setRepairOpen(true)}>Repair insufficient context</Button>
        <Button disabled={running || !actions.verify.available} onClick={() => void act('verify_failed')}>Retry verification</Button>
        <Button variant="primary" disabled={running || !actions.rebuild.available} onClick={() => void act('rebuild')}>Rebuild when changed</Button>
      </div>
      {[...new Set(Object.values(actions).filter(action => !action.available && action.reason).map(action => action.reason))].map(reason => <p key={reason} className="text-[0.78rem] text-muted">{reason}</p>)}
    </> : ['failed', 'interrupted'].includes(build.state ?? '') && <Button loading={busy} onClick={() => void act('rebuild')}>Retry build</Button>}
    {!actions && artifactAvailable && <Button onClick={() => setRepairOpen(true)}>Repair insufficient context</Button>}
    {job.id && <p role="status" className="text-[0.78rem] text-muted">Evidence job {job.status}{job.progress?.label ? `: ${job.progress.label}` : ''}{job.progress?.total != null ? ` (${job.progress.processed ?? 0} of ${job.progress.total})` : ''}</p>}
    {job.status === 'error' && <ErrorState error={job.failure || new Error(job.error || 'Evidence job failed. Inspect the evidence reasons and retry verification.')} />}
    {error != null && <ErrorState error={error} />}
    {actions && <EvidenceDetails key={build.tag} tag={build.tag} />}
    {repairOpen && <ContextRepairDialog tag={build.tag} sourceRecoveryAvailable={Boolean(actions?.repair.available) && !running} sourceRecoveryUnavailableReason={actions?.repair.reason} open onOpenChange={setRepairOpen} onChanged={onChanged} />}
  </div>;
}

function EvidenceDetails({tag}: {tag: string}) {
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<BuildEvidencePage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const request = useRef(0);
  const load = useCallback(async () => {
    const current = ++request.current; setLoading(true);
    try {const next = await api.buildEvidence(tag, offset); if (current === request.current) {setPage(next); setError(null);}}
    catch (failure) {if (current === request.current) setError(failure);}
    finally {if (current === request.current) setLoading(false);}
  }, [tag, offset]);
  useEffect(() => {if (open) void load(); return () => {request.current++;};}, [open, load]);
  return <details onToggle={event => {if (!event.currentTarget.open) request.current++; setOpen(event.currentTarget.open);}} className="text-[0.78rem]">
    <summary className="cursor-pointer text-muted">Evidence outcomes and exclusion reasons</summary>
    {open && <div className="mt-3 space-y-3">
      {error != null && <ErrorState error={error} onRetry={() => void load()} />}
      {loading && <p role="status">Reading evidence outcomes...</p>}
      {page && <>
        <dl className="flex flex-wrap gap-x-5 gap-y-1">{Object.entries(page.summary.reason_counts).map(([reason, count]) => <div key={reason} className="flex gap-2"><dt>{reason.replace(/_/g, ' ')}</dt><dd className="tabular-nums">{count.toLocaleString()}</dd></div>)}</dl>
        <div className="overflow-x-auto"><table className="w-full text-left" aria-label="Evidence outcomes"><thead><tr><th className="pb-2">Record</th><th>Outcome</th><th>Reasons</th><th>Proof</th></tr></thead><tbody>{page.items.map((item, index) => <tr key={`${item.record_id || item.pair_key}:${index}`} className="border-t border-line"><td className="py-2 pr-3 break-all">{item.pair_key || item.record_id}</td><td className="pr-3">{item.outcome.replace(/_/g, ' ')}</td><td className="pr-3">{item.reasons.map(reason => reason.replace(/_/g, ' ')).join('; ') || '-'}</td><td>{item.proof_status?.replace(/_/g, ' ') || 'Not established'}</td></tr>)}</tbody></table></div>
        {!page.total && <p className="text-muted">No evidence outcomes are recorded for this version.</p>}
        <div className="flex flex-wrap items-center gap-3"><Button variant="ghost" disabled={loading || !offset} onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous outcomes</Button><span className="tabular-nums">{page.total ? `${offset + 1}-${Math.min(offset + page.items.length, page.total)} of ${page.total}` : '0 outcomes'}</span><Button variant="ghost" disabled={loading || offset + 100 >= page.total} onClick={() => setOffset(value => value + 100)}>Next outcomes</Button></div>
      </>}
    </div>}
  </details>;
}
