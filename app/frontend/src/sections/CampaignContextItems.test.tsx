import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { CampaignContextItems } from './CampaignContextItems';
import { contextReview } from '../lib/api';

const generation = vi.hoisted(() => vi.fn());
vi.mock('../components/RunConsole', () => ({ RunConsole: ({ label, params, disabled }: any) => <button disabled={disabled} onClick={() => generation(params)}>{label}</button> }));
vi.mock('./PairsBrowser', () => ({ PairWindow: () => null }));
vi.mock('../lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/api')>(),
  contextReview: {
    campaignItems: vi.fn(), campaignItemIds: vi.fn(), collectedItems: vi.fn(), collectedItemIds: vi.fn(),
    status: vi.fn(async () => ({ engine: 'local', configured: true, model: 'installed-model' })),
    export: vi.fn(async () => ({ packets: [], bytes: 0 })), preview: vi.fn(), apply: vi.fn(),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(contextReview.campaignItems).mockImplementation(async (_campaign, offset = 0) => ({
    items: [{ pair_id: offset + 1, file_path: `file-${offset + 1}.py`, language: 'python', status: 'pending' }], total: 201,
  }));
  vi.mocked(contextReview.campaignItemIds).mockResolvedValue({ ids: Array.from({ length: 201 }, (_, i) => i + 1), total: 201 });
});

test('offers optional collected-data improvements without an approval queue or campaign requirement', async () => {
  vi.mocked(contextReview.collectedItems).mockResolvedValue({items: [{pair_id: 7, file_path: 'src/path.js', context_quality: 'missing', context_suggestions: ['Missing caller source'], status: 'pending'}], total: 1, admission_policy: 'automatic-evidence/1', context_improvement_optional: true});
  vi.mocked(contextReview.collectedItemIds).mockResolvedValue({ids: [7], total: 1});
  render(<CampaignContextItems />);
  await screen.findByLabelText('Select context item 7');
  expect(contextReview.collectedItems).toHaveBeenCalledWith(0, expect.objectContaining({status: '', context_quality: 'needs_work'}));
  expect(screen.getByText('Missing caller source')).toBeInTheDocument();
  expect(screen.getByText('Suggestions do not block dataset building. Filter changes clear the selection.')).toBeInTheDocument();
  expect(screen.queryByText(/pending security review/)).not.toBeInTheDocument();
  expect(screen.getByLabelText('Returned JSON files')).toBeEnabled();
  fireEvent.click(screen.getByRole('button', {name: 'Select matching items'}));
  await waitFor(() => expect(contextReview.collectedItemIds).toHaveBeenCalledWith(expect.objectContaining({context_quality: 'needs_work'})));
  expect(await screen.findByText('1 selected across pages')).toBeInTheDocument();
  expect(generation).not.toHaveBeenCalled();
});

test('selects all matching items without an artificial selection limit', async () => {
  render(<CampaignContextItems campaignId="campaign-a" />);
  await screen.findByLabelText('Select context item 1');
  expect(contextReview.campaignItems).toHaveBeenCalledWith('campaign-a', 0, expect.objectContaining({
    granularity: 'units', exclude_tests: true, usable_only: true, status: 'pending', context_quality: 'needs_work', draft_quality: '',
  }));
  expect(screen.queryByLabelText(/Selection limit/)).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: 'Select matching items'}));
  await waitFor(() => expect(contextReview.campaignItemIds).toHaveBeenCalledWith('campaign-a', expect.objectContaining({status: 'pending'})));
  expect(await screen.findByText('201 selected across pages')).toBeTruthy();
  expect(generation).not.toHaveBeenCalled();
  expect(contextReview.export).not.toHaveBeenCalled();
});

test('shows actual context ratings separately from generation status', async () => {
  vi.mocked(contextReview.campaignItems).mockResolvedValueOnce({items: [{pair_id: 1, file_path: 'query.js',
    status: 'pending', context_quality: 'weak', draft_quality: 'partial', draft_problem: 'explanation is empty'}], total: 1});
  render(<CampaignContextItems campaignId="campaign-a" />);
  await screen.findByLabelText('Select context item 1');
  expect(screen.getByLabelText('Explanation usability')).toBeTruthy();
  expect(screen.getByLabelText('Existing model draft')).toBeTruthy();
  expect(screen.getByText('Explanation: unusable')).toBeTruthy();
  expect(screen.getByText('Draft: partial')).toBeTruthy();
  expect(screen.getByText('explanation is empty')).toBeTruthy();
});

test('separates adequate advisory text from evidence gaps and missing model drafts', async () => {
  vi.mocked(contextReview.campaignItems).mockResolvedValueOnce({items: [{pair_id: 1, file_path: 'query.js', status: 'pending', context_quality: 'strong', draft_quality: 'missing', explanation_state: 'present_usable', generation_eligible: false, evidence_state: 'incomplete', evidence_reasons: ['Imported helper unavailable']}], total: 1});
  render(<CampaignContextItems campaignId="campaign-a" />);
  await screen.findByLabelText('Select context item 1');
  expect(screen.getByText('Explanation: usable')).toBeInTheDocument();
  expect(screen.getByText('Evidence incomplete')).toBeInTheDocument();
  expect(screen.getByText('Imported helper unavailable')).toBeInTheDocument();
  expect(screen.getByText('Existing explanation does not need generation.')).toBeInTheDocument();
});

test('changing and applying filters clears hidden selections and uses the same filter for selection', async () => {
  render(<CampaignContextItems campaignId="campaign-a" />);
  fireEvent.click(await screen.findByLabelText('Select context item 1'));
  expect(screen.getByText('1 selected across pages')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('Repository, package or file'), {target: {value: 'query'}});
  expect(screen.getByText('0 selected across pages')).toBeTruthy();
  expect(screen.getByRole('button', {name: 'Select matching items'})).toBeDisabled();
  fireEvent.click(screen.getByRole('button', {name: 'Apply filters'}));
  await waitFor(() => expect(contextReview.campaignItems).toHaveBeenLastCalledWith('campaign-a', 0, expect.objectContaining({search: 'query'})));
  await waitFor(() => expect(screen.getByRole('button', {name: 'Select matching items'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button', {name: 'Select matching items'}));
  await waitFor(() => expect(contextReview.campaignItemIds).toHaveBeenLastCalledWith('campaign-a', expect.objectContaining({search: 'query'})));
});

test('selects the ordered campaign ID snapshot and preserves selection across pages for local generation', async () => {
  render(<CampaignContextItems campaignId="campaign-a" />);
  await screen.findByLabelText('Select context item 1');
  fireEvent.click(screen.getByRole('button', { name: 'Select matching items' }));
  expect(await screen.findByText('201 selected across pages')).toBeTruthy();
  expect(contextReview.campaignItemIds).toHaveBeenCalledWith('campaign-a', expect.objectContaining({status: 'pending'}));
  expect(contextReview.export).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(await screen.findByLabelText('Select context item 101')).toBeChecked();
  fireEvent.click(screen.getByRole('button', { name: 'Deselect visible page' }));
  expect(screen.getByText('200 selected across pages')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Select visible page' }));
  fireEvent.click(screen.getByRole('button', { name: 'Generate locally' }));
  expect(generation).toHaveBeenCalledWith(expect.objectContaining({ ids: expect.arrayContaining([1, 101, 201]), campaign_id: 'campaign-a' }));
  expect(generation.mock.calls[0][0].ids).toHaveLength(201);
  fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
  expect(screen.getByText('0 selected across pages')).toBeTruthy();
  expect(screen.getByLabelText('Returned JSON files')).toBeEnabled();
});

test('resets pagination and selection on campaign change and ignores a late select-all response', async () => {
  let finish!: (value: { ids: number[]; total: number }) => void;
  vi.mocked(contextReview.campaignItemIds).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const view = render(<CampaignContextItems campaignId="campaign-a" />);
  await screen.findByLabelText('Select context item 1');
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await screen.findByLabelText('Select context item 101');
  fireEvent.click(screen.getByRole('button', { name: 'Select matching items' }));
  view.rerender(<CampaignContextItems campaignId="campaign-b" />);
  await screen.findByLabelText('Select context item 1');
  await act(async () => { finish({ ids: [101, 999], total: 2 }); });
  expect(screen.getByText('0 selected across pages')).toBeTruthy();
  expect(contextReview.campaignItems).toHaveBeenLastCalledWith('campaign-b', 0, expect.objectContaining({status: 'pending'}));
  expect(screen.getByLabelText('Select context item 1')).not.toBeChecked();
});

test('keeps selection after a snapshot error and allows retrying select all', async () => {
  vi.mocked(contextReview.campaignItemIds).mockRejectedValueOnce(new Error('Snapshot unavailable'));
  render(<CampaignContextItems campaignId="campaign-a" />);
  fireEvent.click(await screen.findByLabelText('Select context item 1'));
  fireEvent.click(screen.getByRole('button', { name: 'Select matching items' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Snapshot unavailable');
  expect(screen.getByText('1 selected across pages')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Select matching items' }));
  await waitFor(() => expect(screen.getByText('201 selected across pages')).toBeTruthy());
});

test('clearing selection cancels a pending select-all result', async () => {
  let finish!: (value: { ids: number[]; total: number }) => void;
  vi.mocked(contextReview.campaignItemIds).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  render(<CampaignContextItems campaignId="campaign-a" />);
  fireEvent.click(await screen.findByLabelText('Select context item 1'));
  fireEvent.click(screen.getByRole('button', { name: 'Select matching items' }));
  fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
  await act(async () => { finish({ ids: [1, 2], total: 2 }); });
  expect(screen.getByText('0 selected across pages')).toBeTruthy();
});

test('manual selections include every explicitly checked item across pages', async () => {
  render(<CampaignContextItems campaignId="campaign-a" />);
  await screen.findByLabelText('Select context item 1');
  fireEvent.click(screen.getByLabelText('Select context item 1'));
  fireEvent.click(screen.getByRole('button', {name: 'Next'}));
  fireEvent.click(await screen.findByLabelText('Select context item 101'));
  expect(screen.getByText('2 selected across pages')).toBeTruthy();
});

test('editing a filter invalidates a late selection response', async () => {
  let finish!: (value: {ids: number[]; total: number}) => void;
  vi.mocked(contextReview.campaignItemIds).mockReturnValueOnce(new Promise(resolve => {finish = resolve;}));
  render(<CampaignContextItems campaignId="campaign-a" />);
  await screen.findByLabelText('Select context item 1');
  fireEvent.click(screen.getByRole('button', {name: 'Select matching items'}));
  fireEvent.change(screen.getByLabelText('Repository, package or file'), {target: {value: 'other-repo'}});
  await act(async () => {finish({ids: [999], total: 1});});
  expect(screen.getByText('0 selected across pages')).toBeTruthy();
  expect(screen.getByRole('button', {name: 'Select matching items'})).toBeDisabled();
});
