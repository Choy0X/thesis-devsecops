import { useEffect, useState } from 'react';
import { Button, ErrorState, Panel } from '../components/ui';
import { SelectField } from '../components/kit';
import { RunConsole } from '../components/RunConsole';

export function FullModelExport({run, imports}: {run: string; imports: Record<string, any>[]}) {
  const trained = imports.filter(item => item.type === 'trained');
  const [selected, setSelected] = useState(trained[0]?.import_id || '');
  const [format, setFormat] = useState('huggingface');
  const [files, setFiles] = useState<Record<string, {bytes: number; sha256: string}>>({});
  const [error, setError] = useState<unknown>(null);
  const base = `/api/finetune/runs/${encodeURIComponent(run)}/imports/${encodeURIComponent(selected)}/exports`;
  const load = () => selected && fetch(base).then(async r => {if (!r.ok) throw new Error(await r.text()); return r.json();}).then(r => {setFiles(r.files); setError(null);}).catch(setError);
  useEffect(() => {setFiles({}); load();}, [selected, run]);
  if (!trained.length) return null;
  return <Panel title="Full model export" desc="Merge the pinned base with a validated adapter. The original training import is preserved. Exporting does not establish security accuracy.">
    <div className="flex flex-wrap gap-4">
      <SelectField label="Training import" value={selected} onValueChange={setSelected} options={trained.map(item => ({value: item.import_id, label: item.import_id}))} />
      <SelectField label="Model format" value={format} onValueChange={setFormat} options={[{value: 'huggingface', label: 'Hugging Face, merged BF16'}, {value: 'gguf_q4_k_m', label: 'GGUF, merged Q4_K_M'}]} />
    </div>
    <p className="my-3 text-sm text-muted">Merging runs on CPU and needs enough system RAM and disk for the full base. GGUF uses the installed llama.cpp converter and quantizer.</p>
    {error != null && <ErrorState error={error} />}
    <RunConsole stage="export_full_model" label="Export full model" params={{run, import_id: selected, format}} onDone={() => {load();}} />
    <div className="mt-3 space-y-2">{Object.entries(files).map(([name, item]) => <div key={name} className="flex flex-wrap gap-3 text-sm">
      <a className="text-brand-strong underline" href={`${base}/${encodeURIComponent(name)}`} download>{name}</a>
      <span>{(item.bytes / 1024 ** 3).toFixed(2)} GiB</span><code className="break-all text-xs">SHA256 {item.sha256}</code>
    </div>)}</div>
    <Button variant="ghost" onClick={() => {load();}}>Refresh exports</Button>
  </Panel>;
}
