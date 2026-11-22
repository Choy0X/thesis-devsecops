import { SelectField } from '../components/kit';
import { Checkbox } from '../components/kit';
import { lazy, Suspense, useEffect, useState } from "react";
import { contextReview } from "../lib/api";
import { Button, ErrorState, Panel } from "../components/ui";
import { RunConsole } from "../components/RunConsole";
import { JsonContextExchange } from "./JsonContextExchange";
import { ExplanationAutomation } from './ExplanationAutomation';
const EarlierResponses = lazy(() => import('./ReviewExchangePanel').then(module => ({default: module.ReviewExchangePanel})));

export function ContextExchangePanel({ ids, campaignId, onDone, method = 'all' }: { ids: number[]; campaignId?: string; onDone: () => void; method?: 'all' | 'local' | 'json' | 'free_api' }) {
  const [status, setStatus] = useState<Awaited<ReturnType<typeof contextReview.status>> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [model, setModel] = useState("");
  const [force, setForce] = useState(false);
  const [earlierResponses, setEarlierResponses] = useState(false);
  const refresh = () => contextReview.status().then(setStatus).catch(setError);
  useEffect(() => { if (!['all', 'local'].includes(method)) return; refresh(); const timer = setInterval(refresh, 10000); return () => clearInterval(timer); }, [method]);
  const slot = status?.execution?.slots?.llm;
  const cpu = String(slot?.runs_on || "").toLowerCase().includes("cpu");
  return <Panel title="Explanation drafts" desc="Optional source-grounded improvements using a local model, a dedicated free provider or ChatGPT JSON. Drafts retain their AI provenance. Admission uses repository evidence.">
    <div className="space-y-5">
      {['all', 'local'].includes(method) && <div className="border-b border-line pb-4">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span>{ids.length} selected items</span><span className="font-mono text-muted">{status?.model || "Reading model configuration"}</span>
          <Button size="sm" onClick={refresh}>Refresh device</Button>
        </div>
        {cpu && <p role="status" className="mt-2 text-sm text-warn">CPU execution is blocked for local context generation. This model must load with GPU acceleration; CPU fallback is disabled.</p>}
        {slot && <p className="mt-2 text-xs text-muted">Execution: {slot.runs_on || "not loaded"} | Engine: {slot.engine || "pending"} | GPU offload: {slot.gpu_layers ?? slot.n_gpu_layers ?? "reported when loaded"}</p>}
        {status?.execution?.exclusive && <p role="status" className="mt-2 text-sm text-warn">Local training holds the device. Generation will wait in the resource queue.</p>}
        <SelectField label="Generation model" value={model} onValueChange={setModel} options={[{value: '', label: `Configured local model: ${status?.model || 'none'}`}, ...(status?.models || []).map(m => ({value: m.id, label: m.id}))]} />
        <Checkbox checked={force} onChange={event => setForce(event.target.checked)} label="Generate a new version for completed drafts" />
        <div className="mt-3"><RunConsole stage="context_review_local" label="Generate locally" params={{ ids, model: model || status?.model, force, campaign_id: campaignId }}
          disabled={!ids.length || (!model && (!status?.configured || status.engine !== "local"))} onDone={onDone}
          hint="Select an installed local model in Model Hub / Settings. Completed items resume; failed items receive up to three attempts." /></div>
      </div>}
      {['all', 'json'].includes(method) && <JsonContextExchange key={campaignId || "review"} ids={ids} campaignId={campaignId} onDone={onDone} />}
      {['all', 'free_api'].includes(method) && <ExplanationAutomation ids={method === 'free_api' ? ids : undefined} />}
      {method === 'all' && <div className="border-t border-line pt-3"><Button variant="ghost" aria-expanded={earlierResponses} onClick={() => setEarlierResponses(value => !value)}>Import earlier JSON responses</Button>
        {earlierResponses && <Suspense fallback={<p role="status" className="mt-2 text-sm text-muted">Loading earlier response importer...</p>}><EarlierResponses importOnly pairKeys={[]} waitingTotal={0} onChanged={onDone} /></Suspense>}
      </div>}
      {error != null && <ErrorState error={error} />}
    </div>
  </Panel>;
}
