import {useEffect, useRef, useState} from 'react';
import {Checkbox, FileInput, NumberField, SelectField} from '../components/kit';
import {Button, ErrorState, Panel} from '../components/ui';
import {buttonStyles} from '../components/kit/Button';
import {reviewExchange, type ContextJsonExport, type ReviewExchangeTask} from '../lib/api';

type Phase = 'queued' | 'starting' | 'active' | 'ready' | 'complete' | 'failed';
type Work = {key: number; name: string; phase: Phase; file?: File; maxBytes?: number; validationId?: string; itemIds?: string[];
  task?: ReviewExchangeTask; selected: Set<string>; error?: unknown; attempts: number; nextAt: number; capacity?: boolean;
  restoreCheck?: boolean; restoredSelected?: string[]; notified?: boolean};
const queueStorageKey = (kind: string) => `review-json-${kind}-tasks-v1`;
function savedQueue(kind: string): {entries: Work[]; missingFiles: number} {
  try {
    const data = JSON.parse(localStorage.getItem(queueStorageKey(kind)) || '{}');
    const entries: Work[] = (Array.isArray(data.tasks) ? data.tasks : []).filter((entry: any) => entry && typeof entry.name === 'string'
      && (typeof entry.id === 'string' || (kind === 'import' && typeof entry.validationId === 'string' && Array.isArray(entry.itemIds) && entry.itemIds.some((id: unknown) => typeof id === 'string')))).map((entry: any, index: number) => ({
      key: index + 1, name: entry.name, phase: !entry.id && entry.phase === 'failed' ? 'failed' : 'queued', maxBytes: typeof entry.maxBytes === 'number' ? entry.maxBytes : undefined,
      validationId: typeof entry.validationId === 'string' ? entry.validationId : undefined,
      itemIds: Array.isArray(entry.itemIds) ? entry.itemIds.filter((id: unknown) => typeof id === 'string') : [],
      selected: new Set<string>(), restoredSelected: Array.isArray(entry.selected) ? entry.selected.filter((id: unknown) => typeof id === 'string') : undefined,
      notified: entry.notified === true, restoreCheck: typeof entry.id === 'string', attempts: 0, nextAt: 0,
      error: !entry.id && entry.phase === 'failed' ? new Error('Import metadata was not created. Retry to continue with the saved selection.') : undefined,
      task: typeof entry.id === 'string' ? {id: entry.id, state: 'queued', processed: 0, total: 0, elapsed_seconds: 0, items: [], missing: []} : undefined,
    }));
    return {entries, missingFiles: Number.isSafeInteger(data.missingFiles) && data.missingFiles > 0 ? data.missingFiles : 0};
  } catch {return {entries: [], missingFiles: 0};}
}
const activeExport = (state: string) => ['pending', 'queued', 'running'].includes(state);
const working = (work: Work) => work.phase === 'starting' || work.phase === 'active';

// Reserve a slot before any request, including upload/metadata creation. GET only observes work;
// every job is explicitly started, and a capacity refusal returns the reserved slot to the queue.
function useWorkQueue(kind: 'validation' | 'import', onComplete?: () => void) {
  const [saved] = useState(() => savedQueue(kind));
  const [entries, setEntries] = useState<Work[]>(saved.entries);
  const current = useRef<Work[]>(saved.entries);
  const sequence = useRef(saved.entries.length);
  const alive = useRef(true);
  const polling = useRef(new Set<number>());
  const notified = useRef(new Set(saved.entries.filter(entry => entry.notified).map(entry => entry.key)));
  const complete = useRef(onComplete);
  complete.current = onComplete;
  const replace = (next: Work[]) => {
    current.current = next;
    if (alive.current) {
      setEntries(next);
      try {localStorage.setItem(queueStorageKey(kind), JSON.stringify({missingFiles: kind === 'validation' ? next.filter(entry => !entry.task).length : 0,
        tasks: next.filter(entry => entry.task || (kind === 'import' && entry.validationId && entry.itemIds?.length)).map(entry => ({id: entry.task?.id, name: entry.name, phase: entry.phase, maxBytes: entry.maxBytes,
          validationId: entry.validationId, itemIds: entry.itemIds, selected: entry.restoredSelected ?? (entry.phase === 'ready' ? [...entry.selected] : undefined), notified: entry.notified}))}));}
    catch { /* ignored: Server tasks remain recoverable if browser storage is unavailable. */ }
    }
  };
  const patch = (key: number, changes: Partial<Work>) => {
    if (alive.current) replace(current.current.map(entry => entry.key === key ? {...entry, ...changes} : entry));
  };
  const accept = (entry: Work, task: ReviewExchangeTask) => {
    if (!alive.current) return;
    const phase: Phase = task.state === 'complete' ? (kind === 'validation' ? 'ready' : 'complete')
      : ['failed', 'interrupted'].includes(task.state) ? 'failed' : 'active';
    const validIds = phase === 'ready' ? new Set(task.items.filter(item => item.status === 'valid').map(item => item.item_id)) : undefined;
    const notify = phase === 'complete' && !notified.current.has(entry.key);
    if (notify) notified.current.add(entry.key);
    patch(entry.key, {task, phase, capacity: false, restoreCheck: false, restoredSelected: undefined, notified: phase === 'complete' || entry.notified,
      error: phase === 'failed' ? new Error(task.error || `The ${kind} job was ${task.state}. Retry to resume it.`) : undefined,
      ...(validIds ? {selected: entry.restoredSelected ? new Set(entry.restoredSelected.filter(id => validIds.has(id))) : validIds} : {})});
    if (notify) complete.current?.();
  };
  const start = async (entry: Work) => {
    patch(entry.key, {phase: 'starting', error: undefined, capacity: false});
    try {
      let task = entry.task;
      if (entry.restoreCheck && task) {
        task = await (kind === 'validation' ? reviewExchange.validationStatus(task.id) : reviewExchange.importStatus(task.id));
        if (!alive.current) return;
        patch(entry.key, {task, restoreCheck: false});
        if (task.state !== 'queued') {accept(entry, task); return;}
      }
      if (!task) {
        if (kind === 'validation') {
          if (entry.file!.size > entry.maxBytes!) throw new Error(`File exceeds ${entry.maxBytes!.toLocaleString()} bytes. Add a smaller response or increase the size before adding this file again.`);
          task = await reviewExchange.createValidation(entry.file!, entry.maxBytes!);
        } else task = await reviewExchange.createImport(entry.validationId!, entry.itemIds!);
        if (!alive.current) return;
        patch(entry.key, {task});
      }
      if (task.state !== 'complete') task = await (kind === 'validation' ? reviewExchange.startValidation(task.id) : reviewExchange.startImport(task.id));
      accept(entry, task);
    } catch (error) {
      const capacity = (error as {status?: number})?.status === 409;
      patch(entry.key, capacity ? {phase: 'queued', capacity: true, attempts: entry.attempts + 1,
        nextAt: Date.now() + Math.min(30_000, 1000 * 2 ** Math.min(entry.attempts + 1, 5))}
        : {phase: 'failed', error});
    }
  };
  const pump = () => {
    if (!alive.current) return;
    let available = 2 - current.current.filter(working).length;
    for (const entry of current.current) {
      if (available <= 0) break;
      if (entry.phase === 'queued' && entry.nextAt <= Date.now()) {available--; void start(entry);}
    }
  };
  const poll = () => {
    for (const entry of current.current) {
      if (entry.phase !== 'active' || !entry.task || polling.current.has(entry.key)) continue;
      polling.current.add(entry.key);
      (kind === 'validation' ? reviewExchange.validationStatus(entry.task.id) : reviewExchange.importStatus(entry.task.id))
        .then(task => accept(entry, task)).catch(error => patch(entry.key, {error}))
        .finally(() => polling.current.delete(entry.key));
    }
  };
  useEffect(() => {pump();}, [entries]);
  useEffect(() => {
    alive.current = true;
    const timer = setInterval(() => {pump(); poll();}, 1000);
    return () => {alive.current = false; clearInterval(timer);};
  }, []);
  const add = (incoming: Omit<Work, 'key' | 'phase' | 'selected' | 'attempts' | 'nextAt'>[]) => replace([...current.current,
    ...incoming.map(entry => ({...entry, key: ++sequence.current, phase: 'queued' as const, selected: new Set<string>(), attempts: 0, nextAt: 0}))]);
  const retry = (entry: Work) => patch(entry.key, {phase: 'queued', error: undefined, capacity: false, attempts: 0, nextAt: 0});
  return {entries, add, patch, retry, missingFiles: saved.missingFiles};
}

function Progress({entry, kind}: {entry: Work; kind: 'validation' | 'import'}) {
  const task = entry.task;
  const label = entry.capacity ? `Waiting for ${kind} capacity. Retrying automatically.`
    : entry.phase === 'queued' ? `Queued for ${kind}.`
    : entry.phase === 'starting' ? `Starting ${kind}...`
    : entry.phase === 'ready' ? 'Validation complete.' : entry.phase === 'active' ? `${kind === 'import' ? 'Importing' : 'Validating'}...`
    : entry.phase === 'complete' ? 'Import complete.' : `${kind === 'import' ? 'Import' : 'Validation'} stopped.`;
  return <div className="space-y-1 text-sm text-muted">
    <p role="status">{label}{task ? ` ${task.processed.toLocaleString()} / ${task.total.toLocaleString()} items. Elapsed ${Math.round(task.elapsed_seconds)} s.` : ''}</p>
    {task && working(entry) && <progress className="h-1.5 w-full accent-brand" aria-label={`${kind} progress for ${entry.name}`} value={task.processed} max={Math.max(task.total, 1)} />}
    {entry.maxBytes != null && <p className="text-xs">Upload limit: {(entry.maxBytes / 1_000_000).toLocaleString()} MB.</p>}
  </div>;
}

export function ReviewExchangePanel({ecosystem, pairKeys, waitingTotal, onChanged, importOnly = false}: {
  ecosystem?: string; pairKeys: string[]; waitingTotal: number; onChanged: () => void; importOnly?: boolean;
}) {
  const [sizeMb, setSizeMb] = useState('40');
  const [scope, setScope] = useState('waiting');
  const [exported, setExported] = useState<ContextJsonExport | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState<unknown>(null);
  const [message, setMessage] = useState('');
  const maxBytes = Math.round(Number(sizeMb) * 1_000_000);
  const validSize = sizeMb.trim() !== '' && Number(sizeMb) > 0 && Number.isSafeInteger(maxBytes) && maxBytes > 0;
  const validations = useWorkQueue('validation');
  const imports = useWorkQueue('import', onChanged);
  const alive = useRef(true);
  const exportPolling = useRef(false);
  useEffect(() => {alive.current = true; return () => {alive.current = false;};}, []);
  useEffect(() => {
    if (importOnly) return;
    let valid = true;
    let savedId: string | null = null;
    try {savedId = localStorage.getItem('review-json-export-v1');} catch { /* ignored: Optional browser storage. */ }
    if (savedId) {
      setExportBusy(true);
      reviewExchange.exportStatus(savedId).then(doc => {if (valid) setExported(doc);})
        .catch(error => {if (valid) setExportError(error);}).finally(() => {if (valid) setExportBusy(false);});
    }
    return () => {valid = false;};
  }, [importOnly]);
  useEffect(() => {
    if (!exported || !activeExport(exported.state)) return;
    let valid = true;
    const timer = setInterval(() => {
      if (exportPolling.current) return;
      exportPolling.current = true;
      reviewExchange.exportStatus(exported.export_id).then(doc => {if (valid) {setExported(doc); setExportError(null);}})
        .catch(error => {if (valid) setExportError(error);}).finally(() => {exportPolling.current = false;});
    }, 1000);
    return () => {valid = false; clearInterval(timer);};
  }, [exported?.export_id, exported?.state]);
  const exportAction = async (action: () => Promise<ContextJsonExport>) => {
    setExportBusy(true); setExportError(null); setMessage('');
    try {
      const doc = await action();
      if (alive.current) {
        setExported(doc);
      try {localStorage.setItem('review-json-export-v1', doc.export_id);} catch { /* ignored: Server artifacts remain saved. */ }
      }
    }
    catch (error) {if (alive.current) setExportError(error);}
    finally {if (alive.current) setExportBusy(false);}
  };
  return <Panel title={importOnly ? 'Earlier JSON responses' : 'Review JSON exchange'} desc={importOnly ? 'Import responses from earlier review exports. Existing fingerprint checks and AI provenance are preserved.' : 'Validate and import returned JSON files independently.'}>
    <div className="space-y-5">
      <p className="max-w-prose text-sm text-muted">Imported responses retain AI provenance. Dataset builds revalidate source evidence and admission automatically. Incomplete, rejected and stale items keep their specific reasons.</p>
      <div className={importOnly ? 'max-w-md' : 'grid max-w-2xl gap-4 sm:grid-cols-2'}>
        <NumberField label="Maximum review JSON file size (MB)" min={0.000001} step="any" value={sizeMb} onChange={event => setSizeMb(event.target.value)}
          error={!validSize ? 'Enter a positive size of at least one byte.' : undefined}
          hint="Default 40 MB. 1 MB = 1,000,000 bytes. Each new export or upload keeps its chosen limit." />
        {!importOnly && <SelectField label="Export scope" value={scope} onValueChange={setScope} options={[
          {value: 'waiting', label: `All waiting candidates (${waitingTotal.toLocaleString()})`},
          {value: 'page', label: `Current page (${pairKeys.length.toLocaleString()})`},
        ]} hint="The server exports waiting candidates only. There is no item count limit." />}
      </div>
      {!importOnly && <div className="flex flex-wrap gap-3">
        <Button loading={exportBusy} disabled={!validSize || (scope === 'page' && !pairKeys.length) || !!exported && activeExport(exported.state)} onClick={() => exportAction(() => reviewExchange.createExport({
          ...(ecosystem ? {ecosystem} : {}), ...(scope === 'page' ? {pair_keys: pairKeys} : {}), max_file_bytes: maxBytes,
        }))}>Prepare review JSON export</Button>
      </div>}
      {exported && <div className="space-y-3 border-t border-line pt-4">
        <p role="status" className="text-sm tabular-nums">{exported.state}: {exported.processed.toLocaleString()} / {exported.total.toLocaleString()} candidates. {exported.files.length} files ready. Elapsed {Math.round(exported.elapsed_seconds)} s.</p>
        <p className="text-xs text-muted">Saved export limit: {(exported.max_file_bytes / 1_000_000).toLocaleString()} MB. Prepared files keep their saved selection when filters change.</p>
        {!!exported.skipped?.length && <details className="text-sm text-muted"><summary>{exported.skipped.length} candidates left the waiting queue before preparation.</summary>
          <ul>{exported.skipped.map(item => <li key={item.pair_id}>Item {item.pair_id}: {item.reason}</li>)}</ul>
        </details>}
        <div className="flex flex-wrap gap-3">
          {activeExport(exported.state) ? <Button disabled={exportBusy} onClick={() => exportAction(() => reviewExchange.cancelExport(exported.export_id))}>Stop review export</Button>
            : exported.state !== 'complete' && <Button disabled={exportBusy} onClick={() => exportAction(() => reviewExchange.resumeExport(exported.export_id))}>Resume review export</Button>}
          <Button disabled={exportBusy} onClick={async () => {try {await navigator.clipboard.writeText(exported.prompt); if (alive.current) setMessage('Review prompt copied.');} catch (error) {if (alive.current) setExportError(error);}}}>Copy review prompt</Button>
        </div>
        <ul className="divide-y divide-line">{exported.files.map(file => <li key={file.index} className="flex flex-wrap items-center justify-between gap-3 py-3">
          <div className="min-w-0 text-sm"><p className="break-all">{file.name}</p><p className="text-xs text-muted">{file.bytes.toLocaleString()} bytes | {file.item_count.toLocaleString()} items</p></div>
          <a className={buttonStyles({variant: 'subtle'})} href={reviewExchange.download(exported.export_id, file.index)} download>Download review JSON {file.index + 1}</a>
        </li>)}</ul>
        {!!exported.failures.length && <details className="text-sm text-warn"><summary>Export failures ({exported.failures.length})</summary><ul>{exported.failures.map((failure, index) => <li key={index}>{failure.reason}</li>)}</ul></details>}
      </div>}
      {exportError != null && <ErrorState error={exportError} />}
      {message && <p role="status" className="text-sm text-ok">{message}</p>}
      <div className="space-y-3 border-t border-line pt-4">
        <h3 className="text-sm font-semibold">Returned review files</h3>
        <FileInput aria-label="Returned review JSON files" multiple accept=".json,application/json" buttonLabel="Choose or add review JSON files" disabled={!validSize} onChange={event => {
          const files = Array.from(event.target.files || []); event.target.value = '';
          validations.add(files.map(file => ({name: file.name, file, maxBytes})));
        }} />
        <p className="max-w-prose text-xs text-muted">Add multiple files at any time. Up to two validations and two imports run concurrently. Validation never imports automatically. Partial responses are accepted.</p>
        {!!validations.missingFiles && <p role="status" className="text-sm text-warn">{validations.missingFiles} file(s) were not uploaded before this page closed. Reselect those files to continue.</p>}
        {!!validations.entries.length && <p role="status" className="text-sm tabular-nums">Validation: {validations.entries.filter(entry => entry.phase === 'queued').length} queued, {validations.entries.filter(working).length} validating, {validations.entries.filter(entry => entry.phase === 'ready').length} ready, {validations.entries.filter(entry => entry.phase === 'failed').length} failed.</p>}
        {validations.entries.map(entry => <section key={entry.key} aria-label={`Response ${entry.name}`} className="space-y-3 border-t border-line pt-4">
          <h4 className="break-all text-sm font-semibold">{entry.name}</h4>
          <Progress entry={entry} kind="validation" />
          {entry.error != null && <ErrorState error={entry.error} />}
          {entry.phase === 'failed' && <Button size="sm" onClick={() => validations.retry(entry)}>Retry validation</Button>}
          {entry.phase === 'ready' && entry.task && <>
            <p className="text-sm text-muted">{entry.task.items.filter(item => item.status === 'valid').length} valid, {entry.task.items.filter(item => item.status !== 'valid').length} rejected, {entry.task.missing.length} not returned.</p>
            <details><summary className="cursor-pointer text-sm">Review items and choose what to import</summary>
              <div className="mt-2 max-h-72 overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">Import</th><th className="p-2">Item</th><th className="p-2">Validation</th><th className="p-2">Outcome or reason</th></tr></thead><tbody>
                {entry.task.items.map((item, index) => <tr key={`${item.item_id}:${index}`} className="border-t border-line align-top">
                  <td className="p-2"><Checkbox aria-label={`Import ${entry.name} ${item.item_id}`} disabled={item.status !== 'valid'} checked={entry.selected.has(item.item_id)} onChange={event => {
                    const selected = new Set(entry.selected); event.target.checked ? selected.add(item.item_id) : selected.delete(item.item_id); validations.patch(entry.key, {selected});
                  }} /></td><td className="break-all p-2 font-mono text-xs">{item.item_id}</td><td className="p-2">{item.status}</td><td className="p-2">{item.reason || (item.completion === 'needs_context' ? 'Needs context. Remains waiting.' : item.label_state?.replace(/_/g, ' ') || 'AI adjudication')}</td>
                </tr>)}
              </tbody></table></div>
            </details>
            {!!entry.task.missing.length && <details className="text-xs text-muted"><summary>Items not returned</summary><ul>{entry.task.missing.map(id => <li key={id} className="break-all">{id}</li>)}</ul></details>}
            <Button size="sm" disabled={!entry.selected.size} onClick={() => {
              imports.add([{name: entry.name, validationId: entry.task!.id, itemIds: [...entry.selected]}]);
              validations.patch(entry.key, {selected: new Set()});
            }}>Queue import {entry.selected.size} selected items</Button>
          </>}
        </section>)}
      </div>
      {!!imports.entries.length && <div className="space-y-3 border-t border-line pt-4">
        <h3 className="text-sm font-semibold">Import queue</h3>
        <p role="status" className="text-sm tabular-nums">{imports.entries.filter(entry => entry.phase === 'queued').length} queued, {imports.entries.filter(working).length} importing, {imports.entries.filter(entry => entry.phase === 'complete').length} complete, {imports.entries.filter(entry => entry.phase === 'failed').length} failed.</p>
        {imports.entries.map(entry => <section key={entry.key} aria-label={`Import ${entry.key} from ${entry.name}`} className="space-y-2 border-t border-line pt-3">
          <h4 className="break-all text-sm font-medium">{entry.name}: {entry.itemIds!.length} selected items</h4>
          <Progress entry={entry} kind="import" />
          {entry.error != null && <ErrorState error={entry.error} />}
          {entry.phase === 'failed' && <Button size="sm" onClick={() => imports.retry(entry)}>Retry import</Button>}
          {entry.task && (entry.phase === 'complete' || entry.phase === 'failed') && <>
            <p className="text-sm text-muted">{entry.task.imported ?? 0} imported, {entry.task.unchanged ?? 0} unchanged, {entry.task.incomplete ?? 0} incomplete, {entry.task.conflicts ?? 0} conflicts, {entry.task.rejected ?? 0} rejected. Completed reviews are AI adjudications; incomplete and rejected items remain waiting.</p>
            {!!entry.task.items.filter(item => item.status !== 'valid').length && <ul aria-label={`Rejected import items from ${entry.name}`} className="space-y-1 text-sm text-warn">
              {entry.task.items.filter(item => item.status !== 'valid').map((item, index) => <li key={`${item.item_id}:${index}`}><span className="break-all font-mono text-xs">{item.item_id}</span>: {item.status}. <span>{item.reason || 'No reason returned.'}</span></li>)}
            </ul>}
            {!!entry.task.items.length && <details className="text-sm"><summary>All import item results</summary><ul className="mt-2 max-h-72 space-y-1 overflow-auto">
              {entry.task.items.map((item, index) => <li key={`${item.item_id}:${index}`} className="break-all">{item.item_id}: {item.import_state || item.status}{item.reason ? `. ${item.reason}` : ''}</li>)}
            </ul></details>}
          </>}
        </section>)}
      </div>}
    </div>
  </Panel>;
}
