import { Checkbox, SelectField, TextField } from '../components/kit';
import { useCallback, useEffect, useRef, useState } from 'react';
import { contextReview, type ContextItemFilters, type ContextListItem } from '../lib/api';
import { Button, ErrorState, Panel } from '../components/ui';
import { ContextExchangePanel } from './ContextExchangePanel';
import { PairWindow } from './PairsBrowser';

const COST_FILTERS: ContextItemFilters = {language: '', granularity: 'units', status: 'pending', search: '', cwe: '', exclude_tests: true, usable_only: true, context_quality: 'needs_work', draft_quality: ''};
const ALL_FILTERS: ContextItemFilters = {language: '', granularity: '', status: '', search: '', cwe: '', exclude_tests: false, usable_only: false, context_quality: '', draft_quality: ''};

export function CampaignContextItems({campaignId}: {campaignId?: string}) {
  return <CampaignContextItemsView key={campaignId || 'collected'} campaignId={campaignId} />;
}

function CampaignContextItemsView({campaignId}: {campaignId?: string}) {
  const [rows, setRows] = useState<ContextListItem[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [viewing, setViewing] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectingAll, setSelectingAll] = useState(false);
  const [filters, setFilters] = useState<ContextItemFilters>(COST_FILTERS);
  const [draft, setDraft] = useState<ContextItemFilters>(COST_FILTERS);
  const dirty = JSON.stringify(filters) !== JSON.stringify(draft);
  const [selectionError, setSelectionError] = useState<unknown>(null);
  const pageRequest = useRef(0);
  const selectionRequest = useRef(0);
  const load = useCallback(async () => {
    const request = ++pageRequest.current;
    setLoading(true);
    try {
      const criteria = campaignId ? filters : {...filters, status: ''};
      const result = campaignId ? await contextReview.campaignItems(campaignId, offset, criteria) : await contextReview.collectedItems(offset, criteria);
      if (request !== pageRequest.current) return;
      setRows(result.items); setTotal(result.total ?? result.items.length); setError(null);
    } catch (failure) {
      if (request === pageRequest.current) setError(failure);
    } finally {
      if (request === pageRequest.current) setLoading(false);
    }
  }, [campaignId, offset, filters]);
  useEffect(() => { void load(); return () => { pageRequest.current++; }; }, [load]);
  useEffect(() => () => { selectionRequest.current++; }, []);
  const selectAll = async () => {
    const request = ++selectionRequest.current;
    setSelectingAll(true); setSelectionError(null);
    try {
      const criteria = campaignId ? filters : {...filters, status: ''};
      const snapshot = campaignId ? await contextReview.campaignItemIds(campaignId, criteria) : await contextReview.collectedItemIds(criteria);
      if (request !== selectionRequest.current) return;
      setSelected(snapshot.ids); setTotal(snapshot.total);
    } catch (failure) {
      if (request === selectionRequest.current) setSelectionError(failure);
    } finally {
      if (request === selectionRequest.current) setSelectingAll(false);
    }
  };
  const visibleIds = rows.map(row => row.pair_id as number);
  const selectedIds = new Set(selected);
  const visibleSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.has(id));
  const clearSelection = () => { selectionRequest.current++; setSelectingAll(false); setSelected([]); setSelectionError(null); };
  const edit = (patch: Partial<ContextItemFilters>) => { clearSelection(); setDraft(previous => ({...previous, ...patch})); };
  const apply = (next: ContextItemFilters) => { clearSelection(); pageRequest.current++; setLoading(true); setOffset(0); setFilters({...next}); };
  const canSelect = !loading && !dirty && error == null;
  return <Panel title="Optional context improvements" desc="Build with existing evidence, or improve selected items using local GPU generation or ChatGPT JSON. No approval or generation is required.">
    <form className="mb-5 space-y-3 border-b border-line pb-4" onSubmit={e => {e.preventDefault(); apply(draft);}}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <SelectField label="Language" value={draft.language || ''} onValueChange={value => edit({language: value})} options={['', 'javascript', 'typescript', 'tsx', 'vue', 'svelte', 'python', 'java', 'go', 'c', 'cpp', 'csharp', 'php', 'ruby', 'rust', 'kotlin', 'scala', 'swift'].map(value => ({value, label: value || 'All languages'}))} />
        <SelectField label="Item type" value={draft.granularity || ''} onValueChange={value => edit({granularity: value})} options={[{value: 'units', label: 'Functions and hunks'}, {value: 'function', label: 'Functions only'}, {value: 'hunk', label: 'Hunks only'}, {value: 'file', label: 'Whole file pairs'}, {value: '', label: 'All item types'}]} />
        {campaignId && <SelectField label="Generation state" value={draft.status || ''} onValueChange={value => edit({status: value})} options={[{value: 'pending', label: 'Pending'}, {value: 'failed', label: 'Failed'}, {value: 'needs_context', label: 'Needs more evidence'}, {value: 'awaiting_external', label: 'Awaiting external response'}, {value: 'completed', label: 'Draft completed'}, {value: 'queued', label: 'Queued'}, {value: '', label: 'All states'}]} />}
        <SelectField label="Explanation usability" value={draft.context_quality || ''} onValueChange={value => edit({context_quality: value})} options={[{value: 'needs_work', label: 'Missing or unusable explanations'}, {value: 'missing', label: 'Missing explanation'}, {value: 'weak', label: 'Unusable explanation'}, {value: 'strong', label: 'Usable explanation'}, {value: '', label: 'All explanations'}]} />
        <SelectField label="Existing model draft" value={draft.draft_quality || ''} onValueChange={value => edit({draft_quality: value})} options={[{value: 'needs_work', label: 'Missing, weak or partial'}, {value: 'missing', label: 'No draft'}, {value: 'weak', label: 'Weak / poor draft'}, {value: 'partial', label: 'Partial draft'}, {value: 'complete', label: 'Complete draft'}, {value: '', label: 'All drafts'}]} />
        <TextField label="Repository, package or file" value={draft.search || ''} maxLength={160} placeholder="File path or repository name" onChange={e => edit({search: e.target.value})} />
        <TextField label="CWE" value={draft.cwe || ''} placeholder="CWE-89" onChange={e => edit({cwe: e.target.value})} />

      </div>
      <div className="flex flex-wrap gap-4">
        <Checkbox label="Exclude test files" checked={!!draft.exclude_tests} onChange={e => edit({exclude_tests: e.target.checked})} />
        <Checkbox label="Exclude unavailable or tangled pairs" checked={!!draft.usable_only} onChange={e => edit({usable_only: e.target.checked})} />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit">Apply filters</Button>
        <Button variant="ghost" onClick={() => {setDraft(COST_FILTERS); apply(COST_FILTERS);}}>Cost-conscious preset</Button>
        <Button variant="ghost" onClick={() => {setDraft(ALL_FILTERS); apply(ALL_FILTERS);}}>Show all candidates</Button>
      </div>
      <p role="status" className="text-sm text-muted">{dirty ? 'Filters changed. Apply them before selecting items.' : loading ? 'Counting matching candidates...' : error ? 'Filter results unavailable. Retry before selecting.' : `${total.toLocaleString()} matching candidates. Select individual items, a page, or all matches across pages.`}</p>
      <p className="text-xs text-muted">Suggestions do not block dataset building. Filter changes clear the selection.</p>
      <details className="text-xs text-muted"><summary className="cursor-pointer">How suggestions are selected</summary><p className="mt-2">Only absent, placeholder, malformed, explicitly stale or unrelated explanations need improvement. Short advisories and missing model drafts do not make an explanation unusable. Repository evidence gaps require source recovery or verification. Admission checks run separately during the build.</p></details>
    </form>
    <div className="flex flex-wrap items-center gap-3">
      <Button disabled={loading} onClick={load}>Refresh item status</Button>
      <Button disabled={selectingAll || !canSelect || !total} onClick={selectAll}>Select matching items</Button>
      <Button disabled={selectingAll || !canSelect || !rows.length} onClick={() => setSelected(ids => visibleSelected ? ids.filter(id => !visibleIds.includes(id)) : [...ids, ...visibleIds.filter(id => !selectedIds.has(id))])}>{visibleSelected ? 'Deselect visible page' : 'Select visible page'}</Button>
      <Button disabled={!selected.length && !selectingAll} onClick={() => { selectionRequest.current++; setSelectingAll(false); setSelected([]); setSelectionError(null); }}>Clear selection</Button>
      <span role="status" className="text-sm tabular-nums">{selected.length} selected across pages</span>
    </div>
    {selectingAll && <p role="status" className="mt-2 text-sm text-muted">Selecting all matching candidates across pages...</p>}
    {selectionError != null && <div className="mt-3"><ErrorState error={selectionError} onRetry={canSelect ? selectAll : undefined} /></div>}
    {error != null && <ErrorState error={error} onRetry={load} />}
    {loading ? <p role="status" className="mt-3 text-sm text-muted">Loading context items...</p> : rows.length ? <div className="my-3 max-h-72 overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th>Select</th><th>Item</th><th>Explanation</th><th>Repository evidence</th>{campaignId && <th>Generation state</th>}<th>Details</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.pair_id} className="border-t border-line">
        <td><Checkbox aria-label={`Select context item ${row.pair_id}`} disabled={selectingAll || !canSelect} checked={selectedIds.has(row.pair_id)} onChange={e => setSelected(ids => e.target.checked ? [...ids, row.pair_id] : ids.filter(id => id !== row.pair_id))} /></td>
        <td className="py-2"><Button variant="ghost" onClick={() => setViewing(row.pair_id)}>{row.file_path || row.pair_id}</Button><span className="ml-2 text-xs text-muted">{row.language}</span></td>
        <td className="py-2"><div>Explanation: {row.explanation_state === 'present_usable' || row.context_quality === 'strong' ? 'usable' : row.explanation_state === 'unusable' || ['weak', 'partial'].includes(row.context_quality || '') ? 'unusable' : row.explanation_state || row.context_quality || 'unrated'}</div><div className="text-xs text-muted">Draft: {row.draft_quality || 'unrated'}</div></td>
        <td className="py-2 text-muted">{row.evidence_state === 'incomplete' ? 'Evidence incomplete' : 'Evaluated during build'}{!!row.evidence_reasons?.length && <p className="mt-1 text-xs">{row.evidence_reasons.join('; ')}</p>}</td>
        {campaignId && <td>{row.status}</td>}<td>{row.generation_eligible === false && (row.explanation_state === 'present_usable' || row.context_quality === 'strong') ? 'Existing explanation does not need generation.' : row.reason || (row.context_suggestions?.length ? row.context_suggestions.join('; ') : row.status === 'awaiting_external' ? 'Import the exported response. Automatic generation is paused for this item.' : row.draft_quality !== 'complete' ? row.draft_problem : '')}</td>
      </tr>)}</tbody></table></div> : <p className="mt-3 text-sm text-muted">No collected items match these filters. Adjust the quality filters or choose Show all candidates.</p>}
    <div className="my-3 flex flex-wrap items-center gap-3 text-sm"><Button disabled={loading || offset === 0} onClick={() => setOffset(n => Math.max(0, n-100))}>Previous</Button>
      <span className="tabular-nums">{offset + (rows.length ? 1 : 0)} to {offset+rows.length} of {total}</span><Button disabled={loading || offset+100 >= total} onClick={() => setOffset(n => n+100)}>Next</Button></div>
    <ContextExchangePanel ids={selected} campaignId={campaignId} onDone={() => {clearSelection(); void load();}} />
    {viewing !== null && <PairWindow id={viewing} onClose={() => setViewing(null)} onOther={setViewing} />}
  </Panel>;
}
