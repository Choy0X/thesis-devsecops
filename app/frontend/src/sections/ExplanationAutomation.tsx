import { useCallback, useEffect, useRef, useState } from 'react';
import { Checkbox, SelectField, TextField } from '../components/kit';
import { Button, ErrorState } from '../components/ui';
import { explanationProviders, api, type ExplanationProvider, type ExplanationProviderConfig, type ExplanationProviderProfile, type ExplanationProviderStatus } from '../lib/api';

export function ExplanationAutomation({ids}: {ids?: number[]}) {
  const [open, setOpen] = useState(false);
  return <details className="border-t border-line pt-3" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer text-[0.82rem] font-medium">Optional free explanation automation</summary>
    {open && <AutomationControls ids={ids} />}
  </details>;
}

function AutomationControls({ids}: {ids?: number[]}) {
  const [providers, setProviders] = useState<ExplanationProvider[]>([]);
  const [config, setConfig] = useState<ExplanationProviderConfig | null>(null);
  const [status, setStatus] = useState<ExplanationProviderStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [selectionMessage, setSelectionMessage] = useState('');
  const request = useRef(0);
  const load = useCallback(async () => {
    const current = ++request.current;
    try {
      const [catalog, settings, state] = await Promise.all([explanationProviders.catalog(), explanationProviders.config(), explanationProviders.status()]);
      if (current !== request.current) return;
      setProviders(catalog.providers); setConfig(settings); setStatus(state); setError(null);
    } catch (failure) { if (current === request.current) setError(failure); }
  }, []);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {void load();}, 5000);
    return () => {window.clearInterval(timer); request.current++;};
  }, [load]);
  const action = async (verb: 'start' | 'stop' | 'resume' | 'cancel') => {
    setBusy(true);
    try {await explanationProviders[verb](); await load();} catch (failure) {setError(failure);} finally {setBusy(false);}
  };
  return <div className="mt-3 space-y-4 text-[0.78rem]">
    <p className="max-w-prose text-muted">Public npm source only. Selective generation handles missing or unusable explanations. Existing advisories remain usable without a model draft. Repository evidence and dataset admission are checked separately.</p>
    {error != null && <ErrorState error={error} onRetry={() => void load()} />}
    {!config && !error && <p role="status">Reading dedicated provider settings...</p>}
    {status && <>
      <p role="status">Automation {status.enabled ? status.running ? 'running' : 'enabled, waiting' : 'disabled'}.</p>
      <dl className="flex flex-wrap gap-x-5 gap-y-1">{Object.entries(status.counts).map(([name, count]) => <div key={name} className="flex gap-2"><dt className="text-muted">{name.charAt(0).toUpperCase() + name.slice(1)}</dt><dd className="tabular-nums">{count.toLocaleString()}</dd></div>)}</dl>
      {status.last_error && <p role="alert" className="text-warn">{status.last_error}</p>}
      <div className="flex flex-wrap gap-2">
        {ids && <Button disabled={busy || !ids.length || !config?.enabled} onClick={async () => {setBusy(true); try {const result = await api.queueContextGaps(ids); setSelectionMessage(`${result.queued} queued, ${result.unchanged} unchanged, ${result.blocked} blocked`); await load();} catch (failure) {setError(failure);} finally {setBusy(false);}}}>Queue selected explanation gaps</Button>}
        <Button variant="primary" loading={busy} onClick={() => void action(status.enabled ? 'stop' : 'start')}>{status.enabled ? 'Pause free automation' : 'Enable free automation'}</Button>
        <Button disabled={busy} onClick={() => void action('resume')}>Resume waiting jobs</Button>
        <Button disabled={busy || !(status.counts.queued + status.counts.waiting + status.counts.running)} onClick={() => void action('cancel')}>Cancel pending jobs</Button>
      </div>
      {selectionMessage && <p role="status">{selectionMessage}</p>}
    </>}
    {config && <div className="divide-y divide-line">{providers.map(provider => <ProviderForm key={provider.id} provider={provider} profile={config.profiles.find(profile => profile.provider_id === provider.id)} onSaved={load} />)}</div>}
  </div>;
}

function ProviderForm({provider, profile, onSaved}: {provider: ExplanationProvider; profile?: ExplanationProviderProfile; onSaved: () => Promise<void>}) {
  const [model, setModel] = useState(profile?.model || provider.models[0] || '');
  const [enabled, setEnabled] = useState(profile?.enabled ?? false);
  const [free, setFree] = useState(profile?.free_account_confirmed ?? false);
  const [terms, setTerms] = useState(profile?.terms_confirmed ?? false);
  const [key, setKey] = useState('');
  const [account, setAccount] = useState(profile?.account_id || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const save = async (remove = false) => {
    setBusy(true); setError(null);
    try {
      if (remove) await explanationProviders.removeKey(provider.id);
      else await explanationProviders.saveProfile(provider.id, {model, enabled, free_account_confirmed: free, terms_confirmed: terms, ...(key ? {api_key: key} : {}), ...(account ? {account_id: account} : {})});
      setKey(''); await onSaved();
    } catch (failure) {setError(failure);} finally {setBusy(false);}
  };
  return <details className="py-3">
    <summary className="cursor-pointer text-[0.82rem] font-medium"><span>{provider.name}</span><span className="ml-3 text-[0.78rem] font-normal text-muted">{provider.automation_eligible === false ? 'Unavailable for automation' : profile?.eligible ? 'Ready' : 'Setup required'}</span></summary>
    <div className="mt-3 space-y-3">
    <p className="text-muted">{provider.free_verified ? 'Free route verified' : 'Free route unavailable'}{provider.verified_at ? ` (${provider.verified_at.slice(0, 10)})` : ''}. {provider.reason || profile?.blocked_reason}</p>
    {provider.automation_eligible !== false && <><div className="grid gap-3 md:grid-cols-2">
      <SelectField label={`${provider.name} model`} value={model} onValueChange={setModel} options={provider.models.map(value => ({value, label: value}))} />
      {provider.requires_key !== false && <TextField label={`${provider.name} API key`} type="password" autoComplete="new-password" value={key} onChange={event => setKey(event.target.value)} placeholder={profile?.has_key ? profile.key_masked || 'Stored securely' : 'Enter a dedicated key'} />}
      {provider.id === 'cloudflare_free' && <TextField label={`${provider.name} account identifier`} value={account} onChange={event => setAccount(event.target.value)} placeholder="32-character Cloudflare account ID" />}
    </div></>}
    <div className="flex flex-wrap gap-4">
      <Checkbox label={`Use ${provider.name}`} checked={enabled} onChange={event => setEnabled(event.target.checked)} />
      <Checkbox label={`Free account confirmed for ${provider.name}`} checked={free} onChange={event => setFree(event.target.checked)} />
      <Checkbox label={`Terms accepted for ${provider.name}`} checked={terms} onChange={event => setTerms(event.target.checked)} />
    </div>
    <details className="text-muted"><summary className="cursor-pointer">Eligibility, quotas and terms</summary><div className="mt-2 space-y-2"><p>{provider.eligibility}</p><p>{provider.quota}</p>{provider.restrictions.map(item => <p key={item}>{item}</p>)}<div className="flex flex-wrap gap-3">{provider.official_urls.map((url, index) => <a key={url} href={url} target="_blank" rel="noreferrer" className="text-brand-strong underline">Official terms {index + 1}</a>)}</div></div></details>
    {provider.automation_eligible !== false && <div className="flex flex-wrap gap-2"><Button loading={busy} onClick={() => void save()}>Save {provider.name}</Button>{profile?.has_key && <Button disabled={busy} onClick={() => void save(true)}>Remove {provider.name} key</Button>}</div>}
    {error != null && <ErrorState error={error} />}
    </div>
  </details>;
}
