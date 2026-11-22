import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, test, vi} from 'vitest';
import {ContextRepairDialog} from './ContextRepairDialog';
import {api, type ContextRepairPage} from '../lib/api';

const attach = vi.hoisted(() => vi.fn());
vi.mock('../lib/useJob', () => ({useJob: () => ({attach, status: 'idle', id: null, progress: null})}));
vi.mock('./ContextExchangePanel', () => ({ContextExchangePanel: ({method}: {method: string}) => <p>Selected method: {method}</p>}));
vi.mock('../lib/api', async original => ({...await original<typeof import('../lib/api')>(), api: {contextRepairItems: vi.fn(), contextRepairIds: vi.fn(), repairContext: vi.fn(), cancelJob: vi.fn(), contextRepairAttempts: vi.fn(), contextRepairOutcomes: vi.fn(), contextRepairEvents: vi.fn(), contextRepairDetail: vi.fn(), cancelContextRepairAttempt: vi.fn()}}));
const page: ContextRepairPage = {items: [{record_id: 'r1', pair_key: 'pair-one', missing_facts: ['before:definition:guard'], explanation_state: 'present_usable', generation_eligible: false, reasons: []}], total: 144, offset: 0, limit: 100, summary: {total: 144, source_gaps: 144, explanation_eligible: 0, source_manifest_sha256: 'hash'}};
beforeEach(() => {vi.clearAllMocks(); localStorage.clear(); vi.mocked(api.contextRepairItems).mockResolvedValue(page); vi.mocked(api.contextRepairIds).mockResolvedValue({ids: ['r1', 'r2'], total: 2}); vi.mocked(api.contextRepairAttempts).mockResolvedValue({items: [], total: 0, offset: 0, limit: 100}); vi.mocked(api.contextRepairEvents).mockResolvedValue({items: [], total: 0, next_cursor: 0, has_more: false, limit: 100});});

test('asks for method first and distinguishes evidence gaps from useful explanations', async () => {
  render(<ContextRepairDialog tag="v10" open onOpenChange={vi.fn()} onChanged={vi.fn()} />);
  expect(api.contextRepairItems).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('radio', {name: /JSON exchange/}));
  fireEvent.click(screen.getByRole('button', {name: 'Continue'}));
  expect(await screen.findByText('pair-one')).toBeInTheDocument();
  expect(screen.getByText(/No items need explanation generation/)).toBeInTheDocument();
  expect(screen.getByRole('button', {name: 'Continue with selected method'})).toBeDisabled();
  expect(screen.getByRole('button', {name: 'Recover source evidence'})).toBeInTheDocument();
});

test('selects across pages and launches only selected source recovery', async () => {
  vi.mocked(api.repairContext).mockResolvedValue({job_id: 'repair-job'});
  render(<ContextRepairDialog tag="v10" open onOpenChange={vi.fn()} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', {name: 'Continue'}));
  await screen.findByText('pair-one');
  fireEvent.click(screen.getByRole('button', {name: 'Select all matching items'}));
  await waitFor(() => expect(screen.getByText('2 selected')).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', {name: 'Recover source evidence'}));
  await waitFor(() => expect(api.repairContext).toHaveBeenCalledWith('v10', ['r1', 'r2'], 'source'));
  expect(attach).toHaveBeenCalledWith('repair-job');
});

test('method handoff revalidates selection before opening the shared exchange', async () => {
  vi.mocked(api.contextRepairItems).mockResolvedValue({...page, summary: {...page.summary, explanation_eligible: 1}, items: [{...page.items[0], generation_eligible: true, missing_facts: [], explanation_state: 'missing'}]});
  vi.mocked(api.repairContext).mockResolvedValue({ids: [12]});
  render(<ContextRepairDialog tag="v10" open onOpenChange={vi.fn()} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('radio', {name: /JSON exchange/}));
  fireEvent.click(screen.getByRole('button', {name: 'Continue'}));
  await screen.findByText('pair-one');
  fireEvent.click(screen.getByRole('checkbox', {name: 'Select pair-one'}));
  fireEvent.click(screen.getByRole('button', {name: 'Continue with selected method'}));
  expect(await screen.findByText('Selected method: json')).toBeInTheDocument();
  expect(api.repairContext).toHaveBeenCalledWith('v10', ['r1'], 'json');
});


test('loads immutable original and repaired context only when an item is opened', async () => {
  vi.mocked(api.contextRepairDetail).mockResolvedValue({record_id: 'r1', original: {context_before: 'original body', context_after: '{}'}, repaired: {context_before: 'recovered body', context_after: '{}'}, diff: {context_before: '-original body\n+recovered body'}, repair_ids: ['repair-1'], source_binding: {record_id: 'r1'}, source_manifest_sha256: 'hash', outcome: {status: 'failed', retryable: true, error: 'Exact source unavailable', remaining_missing_facts: ['before:guard']}});
  render(<ContextRepairDialog tag="v10" open onOpenChange={vi.fn()} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', {name: 'Continue'}));
  await screen.findByText('pair-one');
  expect(api.contextRepairDetail).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: 'View context for pair-one'}));
  expect(await screen.findByText('original body')).toBeInTheDocument();
  expect(screen.getByText('recovered body')).toBeInTheDocument();
  expect(screen.getByText('Exact source unavailable')).toBeInTheDocument();
  expect(screen.getByText('Retry available')).toBeInTheDocument();
});

test('opens persisted attempt history without a local job and pages its outcomes', async () => {
  vi.mocked(api.contextRepairAttempts).mockResolvedValue({items: [{attempt_id: 'attempt-old', state: 'done', created: '2026-10-28T12:00:00Z', total: 101, counts: {failed: 101}}], total: 1, offset: 0, limit: 100});
  vi.mocked(api.contextRepairOutcomes).mockResolvedValue({items: [{record_id: 'r1', attempt_id: 'attempt-old', status: 'failed', error: 'Missing parent snapshot', retryable: true}], total: 101, offset: 0, limit: 100, attempt_id: 'attempt-old'});
  render(<ContextRepairDialog tag="v10" open onOpenChange={vi.fn()} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', {name: 'Open recovery history'}));
  expect(await screen.findByText('Missing parent snapshot')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', {name: 'Next outcomes'}));
  await waitFor(() => expect(api.contextRepairOutcomes).toHaveBeenCalledWith('v10', 'attempt-old', 100));
});


test('can fence a persisted running attempt without the original browser job', async () => {
  vi.mocked(api.contextRepairAttempts).mockResolvedValue({items: [{attempt_id: 'running-attempt', state: 'running', created: '2026-10-28T12:00:00Z', total: 1, counts: {running: 1}}], total: 1, offset: 0, limit: 100});
  vi.mocked(api.contextRepairOutcomes).mockResolvedValue({items: [{record_id: 'r1', status: 'running'}], total: 1, offset: 0, limit: 100, attempt_id: 'running-attempt'});
  vi.mocked(api.cancelContextRepairAttempt).mockResolvedValue({attempt_id: 'running-attempt', state: 'cancelled'});
  render(<ContextRepairDialog tag="v10" open onOpenChange={vi.fn()} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', {name: 'Open recovery history'}));
  fireEvent.click(await screen.findByRole('button', {name: 'Cancel attempt running-atte'}));
  await waitFor(() => expect(api.cancelContextRepairAttempt).toHaveBeenCalledWith('v10', 'running-attempt'));
});
