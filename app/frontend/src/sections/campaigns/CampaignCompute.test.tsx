import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { CampaignCompute } from './CampaignCompute';
import { api } from '../../lib/api';

vi.mock('../../lib/api', () => ({api: {campaignComputeResources: vi.fn(), campaignPerformance: vi.fn(), stopCampaignGpuOwner: vi.fn(),campaignMiningPolicy:vi.fn()}}));
beforeEach(() => {
  vi.mocked(api.campaignComputeResources).mockResolvedValue({gpu: {cards: [{name: 'GTX 1660', used_mb: 700, total_mb: 6144, utilization_pct: 0}]},
    owner: {owner: 'training:test', pid: 7}, modes: ['cpu', 'cuda']} as any);
  vi.mocked(api.campaignPerformance).mockResolvedValue({schema: 'campaign-performance/1', counts: {cuda_items: 32}, timings: {api_wait: {seconds: 20, calls: 4}}} as any);
});
it('shows actual CUDA work and names the blocking owner', async () => {
  render(<CampaignCompute campaignId="test" />);
  expect(await screen.findByText(/training:test/)).toBeInTheDocument();
  expect(screen.getByText(/32 GPU items/)).toBeInTheDocument();
  expect(screen.getByText('0% (idle)')).toBeInTheDocument();
  expect(screen.getByRole('button', {name: 'Stop job and continue'})).toBeInTheDocument();
});

it('shows warmup separately and explains the campaign reservation', async () => {
  vi.mocked(api.campaignPerformance).mockResolvedValue({counts: {}, timings: {}, execution: {phase: 'warming_cuda', mode: 'cuda', worker_pid: 42, restarts: 0, warmup_seconds: 1.2, total_seconds: 3.2}} as any);
  render(<CampaignCompute campaignId="test" state="paused" />);
  expect(await screen.findByText('Warming CUDA')).toBeInTheDocument();
  expect(screen.getByText(/blocks local GPU chat/)).toBeInTheDocument();
  expect(screen.queryByText(/Auto calibration/)).not.toBeInTheDocument();
  expect(screen.queryByRole('option', {name: 'Auto'})).not.toBeInTheDocument();
});

it('shows active acquisition and distinguishes incomplete GPU measurements', async () => {
  vi.mocked(api.campaignPerformance).mockResolvedValue({counts:{cuda_items:20,gpu_timed_requests:2,gpu_untimed_requests:1}, timings:{},
    active_commits:[{commit_key:'sveltejs/kit@82712fc0',phase:'hydrating_context',elapsed_seconds:14,processed:31,total:128,unit:'files (budget)'}]} as any);
  render(<CampaignCompute campaignId="test" />);
  expect(await screen.findByText('Fetching repository context')).toBeInTheDocument();
  expect(screen.getByText(/31 \/ 128 files/)).toBeInTheDocument();
  expect(screen.getByText(/2 timed requests, 1 without complete timing/)).toBeInTheDocument();
  expect(screen.getByText(/GPU is reserved by training:test/)).toBeInTheDocument();
});

it('offers explicit mining migration only at a paused boundary',async () => {
  const updated=vi.fn();
  render(<CampaignCompute campaignId="test" state="paused" onUpdated={updated}/>);
  fireEvent.click(screen.getByRole('button',{name:'Use batched mining after resume'}));
  await waitFor(()=>expect(api.campaignMiningPolicy).toHaveBeenCalledWith('test'));
  expect(updated).toHaveBeenCalled();
});


it('distinguishes queued pipeline tasks from published items', async () => {
  vi.mocked(api.campaignPerformance).mockResolvedValue({counts:{},timings:{},pipeline:{engine_version:3,
    stages:{acquire:{queued:187,running:2,failed:0},parse:{queued:4,running:1,failed:0},context:{queued:2,running:1,failed:0},publish:{queued:1,running:1,failed:0}},
    completed:12,cache_bytes:1000000000,cache_limit_bytes:20000000000,first_result_seconds:4.2}} as any);
  render(<CampaignCompute campaignId="test" />);
  expect(await screen.findByRole('table',{name:'Mining pipeline queues'})).toBeInTheDocument();
  expect(screen.getByText('187')).toBeInTheDocument();
  expect(screen.getByText(/12 published items/)).toBeInTheDocument();
  expect(screen.getByText(/1.00 GB of 20.00 GB/)).toBeInTheDocument();
  expect(screen.getByText(/First published result: 4.2 s/)).toBeInTheDocument();
});

it('offers the experimental pipeline only through an explicit paused action', async () => {
  render(<CampaignCompute campaignId="test" state="paused" />);
  fireEvent.click(screen.getByRole('button',{name:'Try experimental pipeline after resume'}));
  await waitFor(()=>expect(api.campaignMiningPolicy).toHaveBeenCalledWith('test',3));
});

it('shows adaptive resource limits and offers explicit policy four migration',async () => {
  vi.mocked(api.campaignPerformance).mockResolvedValue({counts:{},timings:{},pipeline:{engine_version:4,completed:12,
    resource_state:'waiting_memory',stage_limits:{acquire:4,parse:5,context:8,publish:1},
    resources:{budget_bytes:1000000000,cap_bytes:3000000000,memory_wait_seconds:12},
    stages:{parse:{running:2}}}} as any);
  render(<CampaignCompute campaignId="test" state="partial"/>);
  expect(await screen.findByText(/Host budget: 1.00 GB of 3.00 GB/)).toBeInTheDocument();
  expect(screen.getByText(/Waiting for memory; campaign remains active/)).toBeInTheDocument();
  expect(screen.getByText(/Parsing: 2 active, up to 5/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Use adaptive pipeline after resume'}));
  await waitFor(()=>expect(api.campaignMiningPolicy).toHaveBeenCalledWith('test',4));
});


it('shows bounded paging admission without pretending RAM is available',async () => {
  vi.mocked(api.campaignPerformance).mockResolvedValue({counts:{},timings:{},pipeline:{engine_version:4,
    resources:{budget_bytes:1000000000,cap_bytes:3000000000,memory_wait_seconds:0,
      serial_admission:true,commit_available_bytes:2000000000}}} as any);
  render(<CampaignCompute campaignId="test"/>);
  expect(await screen.findByText(/bounded progress using Windows paging/)).toBeInTheDocument();
});
