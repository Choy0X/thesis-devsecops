import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, test, vi} from 'vitest';
import {api} from '../lib/api';
import {QualificationWorkflowPanel} from './QualificationWorkflowPanel';

vi.mock('../lib/api', () => ({api: {
  qualificationWorkflows: vi.fn(), startQualificationWorkflow: vi.fn(),
  qualificationWorkflow: vi.fn(), cancelQualificationWorkflow: vi.fn(),
  startJob: vi.fn(),
}}));

const item = {id: 'a'.repeat(32), parent_tag: 'v11', dataset_tag: 'v12', state: 'running', phase: 'repair',
  phase_progress: {label: 'Recovering exact source evidence', processed: 12, total: 128},
  run_preparation: {state: 'pending_data_qualification'}};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.qualificationWorkflows).mockResolvedValue({items: [], total: 0, limit: 100, offset: 0});
  vi.mocked(api.qualificationWorkflow).mockResolvedValue({workflow: item, phases: {items: [], total: 0, limit: 100, offset: 0}});
});

test('starts the full automatic evidence workflow without starting model training', async () => {
  vi.mocked(api.startQualificationWorkflow).mockResolvedValue({workflow: item, job_id: 'worker'});
  render(<QualificationWorkflowPanel tag="v11" />);
  fireEvent.click(await screen.findByRole('button', {name: 'Automate evidence'}));
  expect(await screen.findByText('Recovering exact source evidence')).toBeInTheDocument();
  expect(screen.getByText('12 of 128')).toBeInTheDocument();
  expect(api.startQualificationWorkflow).toHaveBeenCalledWith('v11', expect.any(String));
  expect(screen.getByText('Run preparation waits for data qualification.')).toBeInTheDocument();
});

test('reopens durable waiting work and allows cancellation', async () => {
  const waiting = {...item, state: 'waiting', reason: 'GitHub quota resets later'};
  vi.mocked(api.qualificationWorkflows).mockResolvedValue({items: [waiting], total: 1, limit: 100, offset: 0});
  vi.mocked(api.qualificationWorkflow).mockResolvedValue({workflow: waiting, phases: {items: [], total: 0, limit: 100, offset: 0}});
  vi.mocked(api.cancelQualificationWorkflow).mockResolvedValue({workflow: {...waiting, state: 'cancelled'}});
  render(<QualificationWorkflowPanel tag="v11" />);
  expect(await screen.findByText('GitHub quota resets later')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', {name: 'Stop evidence workflow'}));
  await waitFor(() => expect(api.cancelQualificationWorkflow).toHaveBeenCalledWith(item.id));
});

test('does not call an unqualified published dataset training ready', async () => {
  const blocked = {...item, state: 'blocked', phase: 'preflight', reason: 'Operational qualification shortages remain',
    shortages: ['insufficient_validated_vulnerability_fix_families']};
  vi.mocked(api.qualificationWorkflows).mockResolvedValue({items: [blocked], total: 1, limit: 100, offset: 0});
  vi.mocked(api.qualificationWorkflow).mockResolvedValue({workflow: blocked, phases: {items: [], total: 0, limit: 100, offset: 0}});
  render(<QualificationWorkflowPanel tag="v11" />);
  expect(await screen.findByText('Operational qualification shortages remain')).toBeInTheDocument();
  expect(screen.queryByText('Ready for training')).not.toBeInTheDocument();
});

test('active phase replaces the old queued reason', async () => {
  const running = {...item, reason: 'Waiting for the qualification worker.'};
  vi.mocked(api.qualificationWorkflows).mockResolvedValue({items: [running], total: 1, limit: 100, offset: 0});
  vi.mocked(api.qualificationWorkflow).mockResolvedValue({workflow: running, phases: {items: [], total: 0, limit: 100, offset: 0}});
  render(<QualificationWorkflowPanel tag="v11" />);
  expect(await screen.findByText('Recovering exact source evidence')).toBeInTheDocument();
  expect(screen.queryByText('Waiting for the qualification worker.')).not.toBeInTheDocument();
});

test('shows deferred verification while independent collection continues', async () => {
  const running = {...item, phase: 'acquire_sources', pending_verification: {reason: 'Runtime memory admission is waiting'}};
  vi.mocked(api.qualificationWorkflows).mockResolvedValue({items: [running], total: 1, limit: 100, offset: 0});
  vi.mocked(api.qualificationWorkflow).mockResolvedValue({workflow: running, phases: {items: [], total: 0, limit: 100, offset: 0}});
  render(<QualificationWorkflowPanel tag="v11" />);
  expect(await screen.findByText('Verification waiting: Runtime memory admission is waiting')).toBeInTheDocument();
});

test('retries the existing failed workflow instead of creating another request', async () => {
  const failed = {...item, state: 'failed', reason: 'A retained build needs retry'};
  vi.mocked(api.qualificationWorkflows).mockResolvedValue({items: [failed], total: 1, limit: 100, offset: 0});
  vi.mocked(api.qualificationWorkflow).mockResolvedValue({workflow: failed, phases: {items: [], total: 0, limit: 100, offset: 0}});
  vi.mocked(api.startJob).mockResolvedValue({id: 'retry-job', status: 'running'} as any);
  render(<QualificationWorkflowPanel tag="v11" />);
  fireEvent.click(await screen.findByRole('button', {name: 'Retry evidence workflow'}));
  await waitFor(() => expect(api.startJob).toHaveBeenCalledWith('qualify_npm_dataset', {workflow_id: item.id}));
  expect(api.startQualificationWorkflow).not.toHaveBeenCalled();
  expect(screen.getByRole('button', {name: 'Automate evidence'})).toBeDisabled();
});
