import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {beforeEach, expect, test, vi} from 'vitest';
import {choose} from '../test/select';
import {Review} from './Review';
import {api, type ReviewItem} from '../lib/api';

vi.mock('../lib/api', () => ({api: {reviewSummary: vi.fn(), reviewQueue: vi.fn(), reviewErrorRates: vi.fn(), reviewItem: vi.fn(), reviewDecision: vi.fn()}}));
vi.mock('./ReviewExchangePanel', () => ({ReviewExchangePanel: ({waitingTotal, pairKeys, onChanged}: {waitingTotal: number; pairKeys: string[]; onChanged: () => void}) => <div>
  <p>Exchange waiting {waitingTotal}: {pairKeys.join(',')}</p><button onClick={onChanged}>Simulate completed import</button>
</div>}));
const row: ReviewItem = {record_id: 'record', pair_key: 'pair', ecosystem: 'npm', package: 'example', repository: 'repo',
  granularity: 'function', function_name: 'parse', file_path: 'source.js', language: 'js', evidence_level: 'direct', attribution_basis: 'fix', mechanism: 'injection',
  gate: {passed: true, tier: 'auto_checked', failed: [], not_evaluated: []}, effective: {review_status: 'llm_adjudicated', label_state: 'vulnerability_supported'}};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.reviewSummary).mockResolvedValue({rows: 20, candidates: 20, auto_checked: 20, tiers: {}, failed: {}, not_evaluated: [], decisions: 0, audit_id: null, rubric_version: 'v1', audit_error_rate: 'unestimated'});
  vi.mocked(api.reviewQueue).mockImplementation(async query => ({total: query.review_state === 'ai_adjudicated' ? 1 : 19, limit: query.limit!, offset: query.offset!, items: [row]}));
  vi.mocked(api.reviewErrorRates).mockResolvedValue({audit_id: null, status: 'unestimated', n_sampled: 0, n_reviewed: 0, errors: 0, rate: null, wilson_low: null, wilson_high: null, failed_strata: [], uncertain_strata: []});
});

test('filters AI adjudications independently and retains the full waiting count for exchange', async () => {
  render(<Review />);
  await screen.findByText('Exchange waiting 19: pair');
  expect(api.reviewQueue).toHaveBeenCalledWith({limit: 100, offset: 0, ecosystem: '', review_state: 'waiting'});
  await choose(userEvent.setup(), screen.getByRole('combobox', {name: 'Review state'}), 'AI adjudicated');
  await screen.findByText(/1 AI adjudicated, available for independent human review/);
  expect(api.reviewQueue).toHaveBeenCalledWith({limit: 1, offset: 0, ecosystem: '', review_state: 'waiting'});
  expect(screen.getByText('Exchange waiting 19: pair')).toBeTruthy();
  const count = vi.mocked(api.reviewQueue).mock.calls.length;
  fireEvent.click(screen.getByRole('button', {name: 'Simulate completed import'}));
  await waitFor(() => expect(vi.mocked(api.reviewQueue).mock.calls.length).toBeGreaterThan(count));
});

test('human decisions on AI-adjudicated items still use the independent human review API', async () => {
  vi.mocked(api.reviewItem).mockResolvedValue({record_id: 'record', row: {code_before: 'source', code_after: 'fixed'}, gate: row.gate,
    decision: null, effective: row.effective, rubric_version: 'v1'});
  vi.mocked(api.reviewDecision).mockResolvedValue({ok: true, decision_id: 1});
  render(<Review />);
  fireEvent.click(await screen.findByRole('button', {name: /example.*source.js/}));
  const reviewer = await screen.findByLabelText('Reviewer');
  fireEvent.change(reviewer, {target: {value: 'Independent reviewer'}});
  fireEvent.click(screen.getByRole('button', {name: 'Save decision'}));
  await waitFor(() => expect(api.reviewDecision).toHaveBeenCalledWith(expect.objectContaining({pair_key: 'pair', review_status: 'human_reviewed', reviewer: 'Independent reviewer'})));
});

test('shows the saved AI proposal without filling the independent human decision form', async () => {
  vi.mocked(api.reviewItem).mockResolvedValue({record_id: 'record', row: {code_before: 'source', code_after: 'fixed'}, gate: row.gate,
    decision: {reviewer: 'external-ai:review-model', review_status: 'llm_adjudicated', label_state: 'vulnerability_supported',
      fields: {violated_invariant: 'Imported proposal for independent checking', _review_exchange: {digest: 'private-internal-receipt'}}, notes: 'Review evidence independently'},
    effective: row.effective, rubric_version: 'v1'});
  render(<Review />);
  fireEvent.click(await screen.findByRole('button', {name: /example.*source.js/}));
  expect(await screen.findByText('Read current saved review')).toBeTruthy();
  expect(screen.getByText(/Imported proposal for independent checking/)).toBeTruthy();
  expect(screen.getByLabelText('Violated invariant')).toHaveValue('');
  expect(screen.queryByText(/private-internal-receipt/)).toBeNull();
});
