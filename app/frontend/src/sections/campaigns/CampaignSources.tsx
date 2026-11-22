import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type CampaignSourceFailure } from '../../lib/api';
import { Checkbox } from '../../components/kit';
import { Button, ErrorState, Panel } from '../../components/ui';

export function CampaignSources({campaignId, onUpdated}: {campaignId: string; onUpdated: () => void}) {
  const [rows, setRows] = useState<CampaignSourceFailure[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState('');
  const request = useRef(0);
  const load = useCallback(async () => {
    const current = ++request.current;
    setLoading(true);
    try {
      const data = await api.campaignSources(campaignId, offset);
      if (current !== request.current) return;
      setRows(data.items); setTotal(data.total);
      setSelected(data.items.filter(row => row.resolution?.action !== 'skip').map(row => row.commit_key));
      setError(null);
    } catch (failure) { if (current === request.current) setError(failure); }
    finally { if (current === request.current) setLoading(false); }
  }, [campaignId, offset]);
  useEffect(() => { void load(); return () => { request.current++; }; }, [load]);
  const act = async (action: 'retry' | 'skip') => {
    setBusy(true); setError(null); setMessage('');
    try {
      const result = await api.campaignResolveSources(campaignId, action, selected);
      if (result.resume_required) {
        setMessage('Sources queued. Checking resources before starting the retry...');
        await api.campaignResume(campaignId);
        setMessage('Selected sources are being retried. Recovered evidence will pass the collection checks again.');
      } else {
        setMessage('Selected sources explicitly skipped. Original failures are preserved; missing evidence is excluded.');
        await load();
      }
    } catch (failure) { setError(failure); }
    finally { setBusy(false); onUpdated(); }
  };
  return <Panel title="Unsolved source commits" desc="Retry selected sources, or explicitly skip them while preserving their failure history.">
    <p className="mb-3 text-sm text-muted">Skipping closes the collection decision. Missing source evidence stays excluded. Skipped sources can be selected for retry later.</p>
    <div className="flex flex-wrap items-center gap-3">
      <Button disabled={busy || loading || !selected.length} onClick={() => act('retry')}>Retry {selected.length} source{selected.length === 1 ? '' : 's'}</Button>
      <Button disabled={busy || loading || !selected.length} onClick={() => act('skip')}>Skip {selected.length} source{selected.length === 1 ? '' : 's'}</Button>
      <Button variant="ghost" disabled={busy || loading} onClick={load}>Refresh sources</Button>
      <span className="text-sm text-muted">{selected.length} selected</span>
    </div>
    {loading && <p className="mt-3 text-sm text-muted">Reading source failures...</p>}
    {message && <p role="status" className="mt-3 text-sm">{message}</p>}
    {error != null && <div className="mt-3"><ErrorState error={error} /></div>}
    {!loading && <details className="mt-3"><summary className="cursor-pointer text-sm">Source failures and explicit exclusions ({total})</summary>
      <div className="mt-3 max-h-64 overflow-auto"><table className="w-full text-left text-sm">
        <caption className="sr-only">Unsolved campaign source commits</caption>
        <thead><tr><th className="p-2">Select</th><th className="p-2">Exact source</th><th className="p-2">Failure</th><th className="p-2">Attempts</th><th className="p-2">Decision</th></tr></thead>
        <tbody>{rows.map(row => <tr key={row.commit_key} className="border-t border-line align-top">
          <td className="p-2"><Checkbox aria-label={`Select source ${row.commit_key}`} disabled={busy} checked={selected.includes(row.commit_key)} onChange={e => setSelected(ids => e.target.checked ? [...ids, row.commit_key] : ids.filter(id => id !== row.commit_key))} /></td>
          <td className="max-w-80 break-all p-2 font-mono text-xs">{row.commit_key}</td>
          <td className="max-w-96 break-words p-2">{row.reason || 'No source failure detail was recorded.'}</td>
          <td className="p-2 tabular-nums">{row.attempts}</td>
          <td className="p-2">{row.resolution?.action === 'skip' ? 'Explicitly skipped' : row.status === 'pending' ? 'Retry queued' : row.retryable ? 'Unsolved' : 'Unsolved; automatic retries exhausted'}</td>
        </tr>)}</tbody>
      </table></div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button disabled={busy || !rows.length} onClick={() => setSelected(rows.map(row => row.commit_key))}>Select visible sources</Button>
        <Button disabled={busy || !selected.length} onClick={() => setSelected([])}>Clear source selection</Button>
        <Button disabled={busy || offset === 0} onClick={() => setOffset(n => Math.max(0, n - 100))}>Previous sources</Button>
        <span className="text-sm tabular-nums">{total ? offset + 1 : 0} to {offset + rows.length} of {total}</span>
        <Button disabled={busy || offset + rows.length >= total} onClick={() => setOffset(n => n + 100)}>Next sources</Button>
      </div>
    </details>}
  </Panel>;
}
