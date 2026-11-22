import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, test, vi} from 'vitest';
import {api} from '../lib/api';
import {AutomaticRecoveryDetails} from './AutomaticRecoveryDetails';

vi.mock('../lib/api', () => ({api: {automaticRecoveryAttempts: vi.fn(), automaticRecoveryItems: vi.fn(),
  automaticRecoveryDetail: vi.fn(), retryAutomaticRecoveryItems: vi.fn()}}));
const workflow = 'b'.repeat(32), attempt = 'c'.repeat(32), itemId = 'a'.repeat(64);
const item = {item_id: itemId, record_id: 'record-a', status: 'unavailable', retryable: true,
  reasons: ['exact_source_unavailable:before:index.js'], missing_facts: ['before:definition:helper'],
  source_locations: [{side: 'before', path: 'index.js', revision: 'd'.repeat(40)}]};
const page = {items: [item], total: 201, limit: 100, offset: 0, history_available: true};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.automaticRecoveryAttempts).mockResolvedValue({items: [{attempt_id: attempt, action: 'repair', state: 'done'}], total: 1, limit: 100, offset: 0});
  vi.mocked(api.automaticRecoveryItems).mockResolvedValue(page);
  vi.mocked(api.retryAutomaticRecoveryItems).mockResolvedValue({job_id: 'retry-job', attempt_id: 'e'.repeat(32), selected: 1});
  vi.mocked(api.automaticRecoveryDetail).mockResolvedValue({...item, original: {context_before: 'ORIGINAL_CONTEXT', context_after: '{}'},
    recovered: {context_before: 'RECOVERED_CONTEXT', context_after: '{}'},
    diff: {context_before: {unified: '+ recovered', truncated: true}}, source_bindings: {}, artifacts: {}, observations: {items: []}});
});

test('loads metadata pages and fetches immutable context only when opened', async () => {
  render(<AutomaticRecoveryDetails workflow={workflow} initialAttempt={attempt} active={false} onClose={vi.fn()} />);
  expect(await screen.findByText('record-a')).toBeInTheDocument();
  expect(screen.getByText('Missing facts: before:definition:helper')).toBeInTheDocument();
  expect(api.automaticRecoveryDetail).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: 'Next items'}));
  await waitFor(() => expect(api.automaticRecoveryItems).toHaveBeenLastCalledWith(workflow, attempt, 100, 'all', false, expect.any(AbortSignal)));
  fireEvent.click(await screen.findByRole('button', {name: 'View context'}));
  expect(await screen.findByText('ORIGINAL_CONTEXT')).toBeInTheDocument();
  expect(screen.getByText('RECOVERED_CONTEXT')).toBeInTheDocument();
  expect(screen.getByText('Diff preview limited. Full original and recovered contexts are retained above.')).toBeInTheDocument();
});

test('filters failures on the server and retries only explicit selection', async () => {
  render(<AutomaticRecoveryDetails workflow={workflow} initialAttempt={attempt} active={false} onClose={vi.fn()} />);
  await screen.findByText('record-a');
  fireEvent.click(screen.getByLabelText('Outcome'));
  fireEvent.click(await screen.findByRole('option', {name: /^unavailable$/}));
  await waitFor(() => expect(api.automaticRecoveryItems).toHaveBeenLastCalledWith(workflow, attempt, 0, 'unavailable', false, expect.any(AbortSignal)));
  fireEvent.click(await screen.findByLabelText('Select record-a'));
  fireEvent.click(screen.getByRole('button', {name: 'Retry selected items (1)'}));
  await waitFor(() => expect(api.retryAutomaticRecoveryItems).toHaveBeenCalledWith(workflow, attempt, [itemId]));
  expect(await screen.findByText(/Retry job retry-job queued for 1 items/)).toBeInTheDocument();
});

test('does not launch selected retries while automatic collection owns the source', async () => {
  render(<AutomaticRecoveryDetails workflow={workflow} initialAttempt={attempt} active onClose={vi.fn()} />);
  fireEvent.click(await screen.findByLabelText('Select record-a'));
  expect(screen.getByRole('button', {name: 'Retry selected items (1)'})).toBeDisabled();
  expect(api.retryAutomaticRecoveryItems).not.toHaveBeenCalled();
});

test('old aggregate attempts are not presented as having item history', async () => {
  vi.mocked(api.automaticRecoveryItems).mockResolvedValue({...page, items: [], total: 0, history_available: false});
  render(<AutomaticRecoveryDetails workflow={workflow} initialAttempt={attempt} active={false} onClose={vi.fn()} />);
  expect(await screen.findByText('This earlier attempt has no per-item history. Its aggregate result remains preserved.')).toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

test('closing the details aborts page-specific requests', async () => {
  const view = render(<AutomaticRecoveryDetails workflow={workflow} initialAttempt={attempt} active={false} onClose={vi.fn()} />);
  await screen.findByText('record-a');
  const signal = vi.mocked(api.automaticRecoveryItems).mock.calls[0][5]!;
  expect(signal.aborted).toBe(false);
  view.unmount();
  expect(signal.aborted).toBe(true);
});
