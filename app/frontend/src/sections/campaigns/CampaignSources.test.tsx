import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { CampaignSources } from './CampaignSources';
import { api } from '../../lib/api';

vi.mock('../../lib/api', () => ({api: {campaignSources: vi.fn(), campaignResolveSources: vi.fn(), campaignResume: vi.fn()}}));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.campaignSources).mockResolvedValue({items: [
    {commit_key: 'missing-a', reason: 'exact commit unavailable', attempts: 9, retryable: false, status: 'failed'},
    {commit_key: 'missing-b', reason: 'repository unavailable', attempts: 3, retryable: false, status: 'failed'},
  ], total: 2, unresolved: 2, skipped: 0});
  vi.mocked(api.campaignResolveSources).mockResolvedValue({ok: true, resume_required: false, campaign: {} as any, unresolved: 0, skipped: 2});
  vi.mocked(api.campaignResume).mockResolvedValue({ok: true} as any);
});
test('offers explicit skip while preserving failure details and starts no work', async () => {
  const updated = vi.fn();
  render(<CampaignSources campaignId="campaign" onUpdated={updated} />);
  await screen.findByText('exact commit unavailable');
  fireEvent.click(screen.getByRole('button', {name: 'Skip 2 sources'}));
  await waitFor(() => expect(api.campaignResolveSources).toHaveBeenCalledWith('campaign', 'skip', ['missing-a', 'missing-b']));
  expect(api.campaignResume).not.toHaveBeenCalled();
  await waitFor(() => expect(updated).toHaveBeenCalled());
});
test('retries only selected sources and invokes normal admission and resume', async () => {
  vi.mocked(api.campaignResolveSources).mockResolvedValue({ok: true, resume_required: true, campaign: {} as any, unresolved: 2, skipped: 0});
  render(<CampaignSources campaignId="campaign" onUpdated={() => undefined} />);
  fireEvent.click(await screen.findByLabelText('Select source missing-b'));
  fireEvent.click(screen.getByRole('button', {name: 'Retry 1 source'}));
  await waitFor(() => expect(api.campaignResolveSources).toHaveBeenCalledWith('campaign', 'retry', ['missing-a']));
  await waitFor(() => expect(api.campaignResume).toHaveBeenCalledWith('campaign'));
});
test('reports blocked resume without disguising it as a successful retry', async () => {
  vi.mocked(api.campaignResolveSources).mockResolvedValue({ok: true, resume_required: true, campaign: {} as any, unresolved: 2, skipped: 0});
  vi.mocked(api.campaignResume).mockRejectedValue(new Error('GPU held by another job'));
  render(<CampaignSources campaignId="campaign" onUpdated={() => undefined} />);
  fireEvent.click(await screen.findByRole('button', {name: 'Retry 2 sources'}));
  expect(await screen.findByText(/GPU held by another job/)).toBeTruthy();
});
