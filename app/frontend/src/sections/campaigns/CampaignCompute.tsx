import { useCallback, useEffect, useState } from 'react';
import { api } from '../../lib/api';
import { Button, ErrorState, Panel } from '../../components/ui';
import { useWhileActive } from '../../components/ActivePage';
import { SelectField } from '../../components/kit';

export function CampaignCompute({campaignId, state, onUpdated}: {campaignId: string; state?: string; onUpdated?: () => void}) {
  const [resources, setResources] = useState<any>(null);
  const [performance, setPerformance] = useState<any>(null);
  const [error, setError] = useState<unknown>(null);
  const [stopping, setStopping] = useState(false);
  const [mode, setMode] = useState('cpu');
  const load = useCallback(() => Promise.all([api.campaignComputeResources(), api.campaignPerformance(campaignId)])
    .then(([r, p]) => {setResources(r); setPerformance(p); setError(null);}).catch(setError), [campaignId]);
  useEffect(() => {load();}, [load]);
  useWhileActive(load, 1000);
  const card = resources?.gpu?.cards?.[0];
  const owner = resources?.owner?.owner;
  const execution = performance?.execution;
  const counts = performance?.counts || {};
  const pipeline = performance?.pipeline;
  const pipelineStages = [{key:"acquire",label:"Acquisition"},{key:"parse",label:"Parsing and matching"},{key:"context",label:"Context and fix checks"},{key:"publish",label:"Publication"}];
  const commitPhases: Record<string, string> = {acquire:'Acquiring exact snapshots',parse:'Parsing and matching functions',context:'Collecting repository context',publish:'Publishing collected evidence',queued_parse:'Waiting for parser',queued_context:'Waiting for context collection',queued_publish:'Waiting for publication',queued_completed:'Publication complete',fetching_snapshot_group:'Fetching shared repository snapshots',metadata:'Reading commit metadata', fetching_code:'Fetching changed code', parsing:'Parsing and matching functions', hydrating_context:'Fetching repository context', checking_fix:'Checking fix provenance', persistence:'Saving collected evidence'};
  const phase = ({waiting_memory: 'Waiting for memory', waiting_gpu: 'Waiting for GPU', warming_cuda: 'Warming CUDA', processing: 'Processing', releasing_gpu: 'Releasing GPU', released: 'Resources released', cleanup_failed: 'Cleanup needs retry'} as Record<string, string>)[execution?.phase];
  return <Panel title="Compute and performance" desc="GPU work, SSD cache reuse, and network waits are measured separately.">
    {error != null && <ErrorState error={error} onRetry={load} />}
    <dl className="grid gap-2 text-sm sm:grid-cols-2">
      {execution && <div><dt className="text-muted">Execution</dt><dd>{phase || execution.phase} | {execution.mode?.toUpperCase()}{execution.worker_pid ? ` | worker ${execution.worker_pid}` : ''} | {execution.restarts ?? 0} restarts</dd></div>}
      <div><dt className="text-muted">GPU memory</dt><dd>{card ? `${card.name}: ${card.used_mb} / ${card.total_mb} MB` : resources?.gpu?.reason || 'Reading device status...'}</dd></div>
      <div><dt className="text-muted">GPU utilization</dt><dd>{typeof card?.utilization_pct === 'number' ? `${card.utilization_pct}%${card.utilization_pct === 0 ? ' (idle)' : ''}` : 'Unavailable'}</dd></div>
      {typeof performance?.elapsed_seconds === 'number' && <div><dt className="text-muted">Cumulative campaign active time</dt><dd>{(performance.elapsed_seconds/60).toFixed(1)} minutes</dd></div>}
      <div><dt className="text-muted">Campaign CUDA work</dt><dd>{performance?.counts?.cuda_items ?? 0} GPU items</dd></div>
      <div><dt className="text-muted">API wait</dt><dd>{(performance?.timings?.api_wait?.seconds ?? 0).toFixed(1)} seconds</dd></div>
      <div><dt className="text-muted">HTTP cache reuse</dt><dd>{performance?.counts?.http_cache_hits ?? 0} requests avoided</dd></div>
      <div><dt className="text-muted">Git blob batching</dt><dd>{counts.git_prefetch_unique_objects ?? 0} objects planned | {counts.git_prefetch_network_batches ?? 0} network batches</dd></div>
      {typeof performance?.recent_commits_per_minute === 'number' && <div><dt className="text-muted">Recent mining throughput</dt><dd>{performance.recent_commits_per_minute.toFixed(2)} commits/minute</dd></div>}
      <div><dt className="text-muted">CUDA timing coverage</dt><dd>{counts.gpu_timed_requests ?? 0} timed requests, {counts.gpu_untimed_requests ?? 0} without complete timing{counts.gpu_historical_items_without_timing_coverage ? ` | ${counts.gpu_historical_items_without_timing_coverage} historical items without coverage` : ''}</dd></div>
    </dl>
    {[3, 4].includes(pipeline?.engine_version) && <section className="mt-5 border-t border-line pt-4" aria-label="Mining pipeline">
      <p className="text-sm">{pipeline.engine_version === 4 ? 'Adaptive mining pipeline' : 'Experimental mining pipeline'} | {pipeline.completed ?? 0} published items</p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-sm tabular-nums" aria-label="Mining pipeline queues">
          <thead><tr className="border-b border-line text-muted"><th scope="col" className="py-2 pr-4 font-medium">Stage</th><th scope="col" className="px-3 py-2 text-right font-medium">Queued</th><th scope="col" className="px-3 py-2 text-right font-medium">Running</th><th scope="col" className="py-2 pl-3 text-right font-medium">Failed</th></tr></thead>
          <tbody>{pipelineStages.map(stage => <tr key={stage.key} className="border-b border-line"><th scope="row" className="py-2 pr-4 font-normal">{stage.label}</th><td className="px-3 py-2 text-right">{pipeline.stages?.[stage.key]?.queued ?? 0}</td><td className="px-3 py-2 text-right">{pipeline.stages?.[stage.key]?.running ?? 0}</td><td className="py-2 pl-3 text-right">{pipeline.stages?.[stage.key]?.failed ?? 0}</td></tr>)}</tbody>
        </table>
      </div>
      {pipeline.resources && <div className="mt-3 space-y-1 text-sm">
        <p>Host budget: {(pipeline.resources.budget_bytes/1e9).toFixed(2)} GB of {(pipeline.resources.cap_bytes/1e9).toFixed(2)} GB.</p>
        <p>Parsing: {pipeline.stages?.parse?.running ?? 0} active, up to {pipeline.stage_limits?.parse ?? 1}.</p>
        <p>Acquisition: {pipeline.stages?.acquire?.running ?? 0} active, up to {pipeline.stage_limits?.acquire ?? 1}. Context: {pipeline.stages?.context?.running ?? 0} active, up to {pipeline.stage_limits?.context ?? 1}.</p>
        {pipeline.resource_state === 'waiting_memory' && <p role="status">Waiting for memory; campaign remains active. Resources are checked every second.</p>}
        {pipeline.resources.serial_admission && <p role="status">Low memory: bounded progress using Windows paging under the 3 GB cap. Parallel work grows as RAM becomes available.</p>}
        <p className="text-muted">Memory wait: {(pipeline.resources.memory_wait_seconds ?? 0).toFixed(1)} s. The 3 GB cap includes campaign child processes.</p>
      </div>}
      <p className="mt-3 text-sm text-muted">SSD cache: {((pipeline.disk_budget?.bytes ?? pipeline.cache_bytes ?? 0)/1e9).toFixed(2)} GB of {((pipeline.disk_budget?.limit_bytes ?? pipeline.cache_limit_bytes ?? 20e9)/1e9).toFixed(2)} GB.</p>
      {pipeline.memory && <p className="mt-1 text-sm text-muted">Host private memory: {pipeline.memory.private_bytes == null ? 'measurement unavailable' : `${(pipeline.memory.private_bytes/2**30).toFixed(2)} GiB`} | Available RAM: {((pipeline.memory.available_bytes ?? 0)/2**30).toFixed(2)} GiB.</p>}
      {typeof pipeline.first_result_seconds === 'number' && <p className="mt-1 text-sm text-muted">First published result: {pipeline.first_result_seconds.toFixed(1)} s.</p>}
      {pipeline.bottleneck && <p className="mt-1 text-sm text-muted">Estimated bottleneck: {pipeline.bottleneck}.</p>}
      <p className="mt-2 max-w-prose text-sm text-muted">Queued tasks are lightweight references on the SSD. A published item has completed context collection, fix checks and database publication.</p>
    </section>}
    {performance?.active_commits?.length > 0 && <ul className="mt-4 divide-y divide-line text-sm" aria-label="Active mining commits">
      {performance.active_commits.map((commit: any) => <li key={commit.attempt_id || commit.commit_key} className="py-3">
        <p className="break-all">{commit.commit_key}</p>
        <p>{commitPhases[commit.phase] || commit.phase}</p>
        <p className="text-muted">Elapsed {commit.elapsed_seconds.toFixed(0)} s{commit.processed != null && ` | ${commit.processed} / ${commit.total} ${commit.unit}`}</p>
      </li>)}
    </ul>}
    {phase && <p className="mt-3 text-sm">{phase}</p>}
    {execution?.phase_started_at && execution.phase !== 'released' && <p className="mt-1 text-sm text-muted">Phase elapsed {Math.max(0, Date.now()/1000-execution.phase_started_at).toFixed(0)} s</p>}
    {execution?.cleanup_error && <p className="mt-2 text-sm text-error">{execution.cleanup_error}</p>}
    {execution && <p className="mt-2 text-sm text-muted">Queue {(execution.queue_seconds ?? 0).toFixed(1)} s | Startup {(execution.startup_seconds ?? 0).toFixed(1)} s | Warmup {(execution.warmup_seconds ?? 0).toFixed(1)} s | Processing {(execution.processing_seconds ?? 0).toFixed(1)} s | Total {(execution.total_seconds ?? 0).toFixed(1)} s</p>}
    <p className="mt-3 text-sm text-muted">CUDA reserves the GPU across all stages and blocks local GPU chat and generation. The GPU can be idle while downloads, Git, parsing or database writes use the CPU and network. Warmup is included in total time.</p>
    {owner && <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-line pt-3 text-sm">
      <span>GPU is reserved by {owner}{resources.owner.model ? ` (${resources.owner.model})` : ''}.</span>
      <Button disabled={stopping} onClick={async () => {setStopping(true); try {await api.stopCampaignGpuOwner(owner); await load();} catch (e) {setError(e);} finally {setStopping(false);}}}>Stop job and continue</Button>
      <span className="text-muted">Queued CUDA work waits for confirmed release.</span>
    </div>}
    {state && ['paused', 'interrupted', 'partial', 'failed'].includes(state) && <div className="mt-3 flex flex-wrap items-end gap-3 border-t border-line pt-3">
      <SelectField label="Compute mode after resume" value={mode} onValueChange={setMode} options={[{value: 'cpu', label: 'CPU'}, {value: 'cuda', label: 'CUDA'}]} />
      <Button onClick={async () => {try {await api.campaignComputePolicy(campaignId, mode); onUpdated?.();} catch (e) {setError(e);}}}>Apply compute policy</Button>
      <Button onClick={async () => {try {await api.campaignMiningPolicy(campaignId); onUpdated?.();} catch (e) {setError(e);}}}>Use batched mining after resume</Button>
      <Button onClick={async () => {try {await api.campaignMiningPolicy(campaignId, 3); onUpdated?.();} catch (e) {setError(e);}}}>Try experimental pipeline after resume</Button>
      <Button onClick={async () => {try {await api.campaignMiningPolicy(campaignId, 4); onUpdated?.();} catch (e) {setError(e);}}}>Use adaptive pipeline after resume</Button>
      <p className="w-full max-w-prose text-sm text-muted">Adaptive mining shares snapshots, adjusts parallel work to available memory, and waits without stopping under memory pressure. Total campaign memory is capped at 3 GB. The 30-minute performance target has not passed full-workload validation.</p>
      <p className="w-full max-w-prose text-sm text-muted">The experimental pipeline shares snapshots and uses an SSD queue, two Git fetches and one parser. Its additional disposable cache is limited to 20 GB. It does not switch a running campaign.</p>
      <p className="w-full text-sm text-muted">Batched mining uses up to four concurrent commits, two Git fetches and one parser process. Resource checks still apply. Collected evidence is retained.</p>
      <p className="w-full text-sm text-muted">This upgrades the unfinished campaign to manual context review. Completed collection work and historical results are retained. Resume when ready.</p>
    </div>}
    {performance && <details className="mt-3 text-sm"><summary className="cursor-pointer text-muted">Performance measurements</summary>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(performance, null, 2)}</pre></details>}
  </Panel>;
}
