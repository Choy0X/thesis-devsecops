import {useEffect, useRef, useState} from 'react';
import {Checkbox, FileInput, NumberField} from '../components/kit';
import {Button, ErrorState} from '../components/ui';
import {buttonStyles} from '../components/kit/Button';
import {contextReview, type ContextJsonExport, type ContextJsonPreview} from '../lib/api';

type Upload = {key: number; name: string; file: File; maxBytes: number; phase: 'queued' | 'validating' | 'ready' | 'failed'; report?: ContextJsonPreview; selected: Set<string>; error?: unknown; message?: string};
const active = (state: string) => ['pending', 'queued', 'running'].includes(state);

export function JsonContextExchange({ids, campaignId, onDone}: {ids: number[]; campaignId?: string; onDone: () => void}) {
  const [exported, setExported] = useState<ContextJsonExport | null>(null);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [busy, setBusy] = useState(false);
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState('');
  const [sizeMb, setSizeMb] = useState('40');
  const maxBytes = Math.round(Number(sizeMb) * 1_000_000);
  const validSize = sizeMb.trim() !== '' && Number(sizeMb) > 0 && Number.isSafeInteger(maxBytes) && maxBytes > 0;
  const sequence = useRef(0);
  const pendingUploads = useRef<Upload[]>([]);
  const draining = useRef(false);
  const alive = useRef(true);
  const operationBusy = busy || validating;
  const storageKey = `context-json:${campaignId || 'review'}`;
  const remember = (doc: ContextJsonExport) => {
    if (!alive.current) return;
    setExported(doc);
      try {localStorage.setItem(storageKey, doc.export_id);} catch { /* ignored: Server files remain saved. */ }
  };
  useEffect(() => {
    alive.current = true;
    let valid = true;
    let saved: string | null = null;
      try {saved = localStorage.getItem(storageKey);} catch { /* ignored: Storage can be disabled. */ }
    if (saved) contextReview.jsonStatus(saved).then(doc => {if (valid) {setExported(doc); setSizeMb(String(doc.max_file_bytes / 1_000_000));}}).catch(failure => {if (valid) setError(failure);});
    return () => {valid = false; alive.current = false;};
  }, [storageKey]);
  useEffect(() => {
    if (!exported || !active(exported.state)) return;
    let valid = true;
    const timer = setInterval(() => {
      contextReview.jsonStatus(exported.export_id).then(doc => {if (valid) setExported(doc);})
        .catch(failure => {if (valid) setError(failure);});
    }, 1000);
    return () => {valid = false; clearInterval(timer);};
  }, [exported?.export_id, exported?.state]);
  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError(null); setMessage('');
    try {await action();} catch (failure) {if (alive.current) setError(failure);}
    finally {if (alive.current) setBusy(false);}
  };
  const update = (key: number, change: Partial<Upload>) => {
    if (alive.current) setUploads(previous => previous.map(upload => upload.key === key ? {...upload, ...change} : upload));
  };
  const validate = async (upload: Upload) => {
    update(upload.key, {phase: 'validating', report: undefined, error: undefined, selected: new Set(), message: undefined});
    try {
      if (upload.file.size > upload.maxBytes) throw new Error(`File exceeds ${upload.maxBytes.toLocaleString()} UTF-8 bytes. Increase the maximum JSON file size or return smaller partial responses.`);
      const report = await contextReview.jsonPreview(upload.file, upload.maxBytes);
      update(upload.key, {phase: 'ready', report, selected: new Set(report.items.filter(item => item.status === 'valid').map(item => item.item_id)), error: undefined});
    } catch (failure) {update(upload.key, {phase: 'failed', error: failure});}
  };
  const drainUploads = async () => {
    if (draining.current) return;
    draining.current = true; setValidating(true);
    try {
      while (alive.current && pendingUploads.current.length) {
        await validate(pendingUploads.current.shift()!);
      }
    } finally {
      draining.current = false;
      if (alive.current) setValidating(false);
      else pendingUploads.current = [];
    }
  };
  const reset = () => {
    setExported(null); setError(null); setMessage('');
        try {localStorage.removeItem(storageKey);} catch { /* ignored: Existing artifacts are preserved. */ }
  };
  return <div className="space-y-5">
    <div>
      <h3 className="font-semibold">ChatGPT JSON exchange</h3>
      <p className="mt-1 max-w-prose text-sm text-muted">Export every selected item as one batch, split into standalone JSON files using your maximum file size. The default is 40 MB. There is no item or file count limit.</p>
      <p className="mt-2 max-w-prose text-sm text-muted">Each file includes the expert prompt, full selected evidence and a response template. Upload files to ChatGPT manually. Returned files can be imported independently, in any order.</p>
    </div>
    <div className="max-w-md"><NumberField label="Maximum JSON file size (MB)" value={sizeMb} min={0.000001} step="any"
      disabled={operationBusy || (!!exported && active(exported.state))} onChange={event => setSizeMb(event.target.value)}
      error={!validSize ? 'Enter a positive size of at least one byte.' : undefined}
      hint="1 MB = 1,000,000 UTF-8 bytes. Applies to new exports and returned-file uploads. Saved exports keep their original size." /></div>
    {!exported ? <div className="flex flex-wrap items-center gap-3">
      <Button disabled={operationBusy || !ids.length || !validSize} onClick={() => perform(async () => remember(await contextReview.jsonCreate(ids, campaignId, maxBytes)))}>Prepare JSON export</Button>
      <span className="text-sm text-muted tabular-nums">{ids.length.toLocaleString()} selected items</span>
    </div> : <div className="space-y-3">
      <p className="text-sm text-muted">Saved export maximum: {(exported.max_file_bytes / 1_000_000).toLocaleString()} MB ({exported.max_file_bytes.toLocaleString()} bytes per file).</p>
      <p role="status" className="text-sm tabular-nums">{exported.state}: {exported.processed.toLocaleString()} / {exported.total.toLocaleString()} items processed. {exported.files.length} files ready. Elapsed {Math.round(exported.elapsed_seconds)} s. {exported.failures.length} failures.</p>
      <div className="flex flex-wrap items-center gap-3">
        {active(exported.state) ? <Button disabled={busy} onClick={() => perform(async () => remember(await contextReview.jsonCancel(exported.export_id)))}>Stop preparation</Button>
          : exported.state !== 'complete' && <Button disabled={operationBusy} onClick={() => perform(async () => remember(await contextReview.jsonResume(exported.export_id)))}>Resume export</Button>}
        <Button disabled={busy} onClick={() => perform(async () => {await navigator.clipboard.writeText(exported.prompt); setMessage('Expert review prompt copied.');})}>Copy prompt</Button>
        <Button variant="ghost" disabled={operationBusy || active(exported.state)} onClick={reset}>Use current selection for a new export</Button>
      </div>
      {exported.files.length > 0 && <ul className="divide-y divide-line">
        {exported.files.map(file => <li key={file.packet_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
          <div className="min-w-0"><p className="break-all text-sm">{file.name}</p><p className="text-xs text-muted tabular-nums">{file.bytes.toLocaleString()} bytes | {file.item_count.toLocaleString()} items</p></div>
          <a className={buttonStyles({variant: 'subtle'})} href={contextReview.jsonDownload(exported.export_id, file.index)} download>Download JSON {file.index + 1}</a>
        </li>)}
      </ul>}
      {exported.failures.length > 0 && <details className="text-sm text-warn"><summary>Inspect export failures. Completed files remain available.</summary><ul>{exported.failures.map((failure, i) => <li key={i}>Item {failure.pair_id ?? 'preparation'}: {failure.reason}</li>)}</ul></details>}
      <p className="text-xs text-muted">This export keeps its saved selection. Changing filters does not alter prepared files. No earlier import is required to download a file.</p>
    </div>}
    <div className="border-t border-line pt-4">
      <label className="flex flex-wrap items-center gap-3 text-sm"><span>Returned JSON files</span>
        <FileInput aria-label="Returned JSON files" buttonLabel="Choose or add JSON files" multiple accept=".json,application/json" disabled={busy || !validSize} onChange={event => {
          const files = Array.from(event.target.files || []); event.target.value = '';
          if (!files.length) return;
          const incoming: Upload[] = files.map(file => ({key: ++sequence.current, name: file.name, file, maxBytes, phase: 'queued', selected: new Set<string>()}));
          setUploads(previous => [...previous, ...incoming]);
          pendingUploads.current.push(...incoming);
          void drainUploads();
        }} />
      </label>
      <p className="mt-2 text-xs text-muted">Select several files at once, or add more while validation runs. Files are validated one at a time using the maximum file size above. Older JSON packet responses are accepted. Imported explanations retain their model provenance and are checked automatically during dataset building.</p>
      {uploads.length > 0 && <p role="status" className="mt-3 text-sm tabular-nums">{uploads.length} returned files: {uploads.filter(upload => upload.phase === 'queued').length} queued, {uploads.filter(upload => upload.phase === 'validating').length} validating, {uploads.filter(upload => upload.phase === 'ready').length} ready, {uploads.filter(upload => upload.phase === 'failed').length} failed.</p>}
      <div className="mt-4 space-y-4">
        {uploads.map(upload => <section key={upload.key} className="border-t border-line pt-3" aria-label={`Response ${upload.name}`}>
          <h4 className="break-all text-sm font-semibold">{upload.name}</h4>
          {upload.error != null && <><ErrorState error={upload.error} /><Button size="sm" disabled={operationBusy || !validSize} onClick={() => {
            const retry: Upload = {...upload, maxBytes, phase: 'queued'};
            update(upload.key, {maxBytes, phase: 'queued', error: undefined});
            pendingUploads.current.push(retry); void drainUploads();
          }}>Retry validation</Button></>}
          {!upload.report && upload.error == null && <p role="status" className="text-sm text-muted">{upload.phase === 'validating' ? 'Validating this file...' : 'Queued for validation...'}</p>}
          {upload.report && <div className="mt-2 space-y-3">
            <p className="text-sm text-muted">{upload.report.items.filter(item => item.status === 'valid').length} valid, {upload.report.items.filter(item => item.status !== 'valid').length} rejected, {upload.report.missing.length} not returned. Partial responses are allowed.</p>
            {upload.report.items.some(item => item.status !== 'valid') && <ul className="text-sm text-warn" aria-label={`Rejection reasons for ${upload.name}`}>
              {Object.entries(upload.report.items.filter(item => item.status !== 'valid').reduce<Record<string, number>>((counts, item) => {
                const reason = item.reason || item.status; counts[reason] = (counts[reason] || 0) + 1; return counts;
              }, {})).map(([reason, count]) => <li key={reason}>{count}: {reason}</li>)}
            </ul>}
            <details><summary className="cursor-pointer text-sm">Review items and choose drafts to import</summary>
              <div className="mt-2 max-h-72 overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">Import</th><th>Item</th><th>Validation</th><th>Detail</th></tr></thead><tbody>
                {upload.report.items.map((item, index) => <tr key={`${item.item_id}:${index}`} className="border-t border-line">
                  <td className="p-2"><Checkbox aria-label={`Import ${upload.name} ${item.item_id}`} disabled={operationBusy || item.status !== 'valid'} checked={upload.selected.has(item.item_id)} onChange={event => {
                    const next = new Set(upload.selected); event.target.checked ? next.add(item.item_id) : next.delete(item.item_id); update(upload.key, {selected: next});
                  }} /></td><td className="break-all">{item.item_id}</td><td>{item.status}</td><td>{item.reason || item.outcome || 'Draft only'}</td>
                </tr>)}
              </tbody></table></div>
            </details>
            <Button size="sm" disabled={operationBusy || !upload.selected.size} onClick={() => perform(async () => {
              const result = await contextReview.jsonApply(upload.report!.upload_id, [...upload.selected]);
              update(upload.key, {report: {...result, upload_id: upload.report!.upload_id}, selected: new Set(), message: `${result.imported} imported, ${result.unchanged} already imported. Saved with model-draft provenance.`}); onDone();
            })}>Import {upload.selected.size} selected drafts from this file</Button>
          </div>}
          {upload.message && <p role="status" className="mt-2 text-sm text-ok">{upload.message}</p>}
        </section>)}
      </div>
    </div>
    {busy && <p role="status" className="text-sm text-muted">Processing context exchange...</p>}
    {message && <p role="status" className="text-sm text-ok">{message}</p>}
    {error != null && <ErrorState error={error} />}
    {error != null && !exported && <Button variant="ghost" disabled={busy} onClick={reset}>Clear saved export link</Button>}
  </div>;
}
