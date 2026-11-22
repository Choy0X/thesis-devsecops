import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {beforeEach, afterEach, expect, test, vi} from 'vitest';
import userEvent from '@testing-library/user-event';
import {choose} from '../test/select';
import {ReviewExchangePanel} from './ReviewExchangePanel';
import {reviewExchange, type ReviewExchangeTask} from '../lib/api';

vi.mock('../lib/api', () => ({reviewExchange: {createExport: vi.fn(), exportStatus: vi.fn(), resumeExport: vi.fn(), cancelExport: vi.fn(),
  download: (id: string, index: number) => `/exports/${id}/${index}`, createValidation: vi.fn(), validationStatus: vi.fn(), startValidation: vi.fn(),
  createImport: vi.fn(), importStatus: vi.fn(), startImport: vi.fn()}}));
const task = (id: string, state: ReviewExchangeTask['state'] = 'queued'): ReviewExchangeTask => ({id, state, processed: 0, total: 1, elapsed_seconds: 0, items: [], missing: []});
const ready = (id: string): ReviewExchangeTask => ({...task(id, 'complete'), processed: 1, items: [{item_id: id, status: 'valid', completion: 'complete'}]});
const upload = (...names: string[]) => fireEvent.change(screen.getByLabelText('Returned review JSON files'), {target: {files: names.map(name => new File(['{}'], name))}});
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  vi.mocked(reviewExchange.createValidation).mockImplementation(async file => task(file.name));
  vi.mocked(reviewExchange.startValidation).mockImplementation(async id => ready(id));
  vi.mocked(reviewExchange.validationStatus).mockImplementation(async id => ready(id));
  vi.mocked(reviewExchange.createImport).mockImplementation(async id => task(`import-${id}`));
  vi.mocked(reviewExchange.startImport).mockImplementation(async id => ({...task(id, 'complete'), imported: 1, unchanged: 0, incomplete: 0, conflicts: 0}));
});
afterEach(() => vi.useRealTimers());

test('imports older review responses without reopening an approval queue or export workflow', async () => {
  render(<ReviewExchangePanel importOnly pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  expect(screen.getByLabelText('Returned review JSON files')).toHaveAttribute('multiple');
  expect(screen.queryByRole('button', {name: 'Prepare review JSON export'})).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Export scope')).not.toBeInTheDocument();
  expect(screen.queryByText(/require an independent human decision/)).not.toBeInTheDocument();
  upload('earlier.json');
  const response = await screen.findByRole('region', {name: 'Response earlier.json'});
  expect(await within(response).findByRole('button', {name: 'Queue import 1 selected items'})).toBeEnabled();
  expect(reviewExchange.createExport).not.toHaveBeenCalled();
});

test('exports all waiting rows by default with adjustable byte size and current page as an option', async () => {
  vi.mocked(reviewExchange.createExport).mockResolvedValue({export_id: 'export', state: 'complete', processed: 4000, total: 4000, elapsed_seconds: 1, max_file_bytes: 500000,
    prompt: 'Review prompt', failures: [], files: [{index: 0, packet_id: 'p', name: 'part.json', bytes: 42, item_count: 4000, sha256: 'hash'}]});
  render(<ReviewExchangePanel ecosystem="npm" pairKeys={['page-key']} waitingTotal={4000} onChanged={() => {}} />);
  expect(screen.getByLabelText('Maximum review JSON file size (MB)')).toHaveValue(40);
  fireEvent.change(screen.getByLabelText('Maximum review JSON file size (MB)'), {target: {value: '0.5'}});
  fireEvent.click(screen.getByRole('button', {name: 'Prepare review JSON export'}));
  expect(await screen.findByRole('link', {name: 'Download review JSON 1'})).toHaveAttribute('href', '/exports/export/0');
  expect(reviewExchange.createExport).toHaveBeenCalledWith({ecosystem: 'npm', max_file_bytes: 500000});
  await choose(userEvent.setup(), screen.getByRole('combobox', {name: 'Export scope'}), 'Current page (1)');
  fireEvent.click(screen.getByRole('button', {name: 'Prepare review JSON export'}));
  await waitFor(() => expect(reviewExchange.createExport).toHaveBeenLastCalledWith({ecosystem: 'npm', pair_keys: ['page-key'], max_file_bytes: 500000}));
});

test('runs at most two validation jobs and allows importing ready files while others validate', async () => {
  const onChanged = vi.fn();
  vi.mocked(reviewExchange.startValidation).mockImplementation(async id => id === 'a.json' ? ready(id) : task(id, 'running'));
  vi.mocked(reviewExchange.validationStatus).mockImplementation(async id => task(id, 'running'));
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={onChanged} />);
  upload('a.json', 'b.json', 'c.json', 'd.json');
  await waitFor(() => expect(reviewExchange.startValidation).toHaveBeenCalledTimes(3));
  expect(reviewExchange.startValidation).not.toHaveBeenCalledWith('d.json');
  expect(reviewExchange.createImport).not.toHaveBeenCalled();
  const first = screen.getByRole('region', {name: 'Response a.json'});
  fireEvent.click(within(first).getByRole('button', {name: 'Queue import 1 selected items'}));
  await waitFor(() => expect(reviewExchange.createImport).toHaveBeenCalledWith('a.json', ['a.json']));
  await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
  expect(screen.getByLabelText('Returned review JSON files')).toBeEnabled();
  upload('e.json');
  expect(screen.getByRole('region', {name: 'Response e.json'})).toBeTruthy();
});

test('preserves per-upload limits and previews after another file fails', async () => {
  vi.mocked(reviewExchange.createValidation).mockImplementation(async file => {if (file.name === 'bad.json') throw new Error('Invalid JSON'); return task(file.name);});
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  const size = screen.getByLabelText('Maximum review JSON file size (MB)');
  fireEvent.change(size, {target: {value: '0.5'}});
  upload('good.json', 'bad.json');
  fireEvent.change(size, {target: {value: '80'}});
  await screen.findByText('Invalid JSON');
  expect(reviewExchange.createValidation).toHaveBeenCalledWith(expect.objectContaining({name: 'good.json'}), 500000);
  expect(within(screen.getByRole('region', {name: 'Response good.json'})).getByRole('button', {name: 'Queue import 1 selected items'})).toBeEnabled();
  expect(screen.getByRole('button', {name: 'Retry validation'})).toBeEnabled();
});

test('keeps capacity refusals queued, retries with backoff, and bounds import jobs independently', async () => {
  vi.useFakeTimers();
  vi.mocked(reviewExchange.startValidation).mockRejectedValueOnce({status: 409}).mockImplementation(async id => ready(id));
  vi.mocked(reviewExchange.startImport).mockImplementation(async id => task(id, 'running'));
  vi.mocked(reviewExchange.importStatus).mockImplementation(async id => task(id, 'running'));
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  upload('a.json', 'b.json', 'c.json', 'd.json');
  await act(async () => {await vi.advanceTimersByTimeAsync(100);});
  expect(screen.getByText(/Waiting for validation capacity/)).toBeTruthy();
  await act(async () => {await vi.advanceTimersByTimeAsync(2100);});
  for (const button of screen.getAllByRole('button', {name: 'Queue import 1 selected items'})) fireEvent.click(button);
  await act(async () => {await vi.advanceTimersByTimeAsync(100);});
  expect(reviewExchange.startImport).toHaveBeenCalledTimes(2);
  expect(screen.getByText(/2 importing/)).toBeTruthy();
  expect(vi.mocked(reviewExchange.createValidation).mock.calls.filter(([file]) => file.name === 'a.json')).toHaveLength(1);
});

test('polls real tasks, retries interrupted imports by id, and refreshes once after completion', async () => {
  vi.useFakeTimers(); const onChanged = vi.fn();
  vi.mocked(reviewExchange.startValidation).mockImplementation(async id => task(id, 'running'));
  vi.mocked(reviewExchange.startImport).mockImplementationOnce(async id => task(id, 'running'));
  vi.mocked(reviewExchange.importStatus).mockImplementationOnce(async id => ({...task(id, 'interrupted'), error: 'Worker interrupted'}));
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={onChanged} />);
  upload('a.json');
  await act(async () => {await vi.advanceTimersByTimeAsync(1100);});
  expect(reviewExchange.validationStatus).toHaveBeenCalledWith('a.json');
  fireEvent.click(screen.getByRole('button', {name: 'Queue import 1 selected items'}));
  await act(async () => {await vi.advanceTimersByTimeAsync(1100);});
  expect(screen.getByText('Worker interrupted')).toBeTruthy();
  expect(onChanged).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: 'Retry import'}));
  await act(async () => {await vi.advanceTimersByTimeAsync(5000);});
  expect(reviewExchange.createImport).toHaveBeenCalledTimes(1);
  expect(reviewExchange.startImport).toHaveBeenCalledTimes(2);
  expect(onChanged).toHaveBeenCalledOnce();
});

test('keeps rejected and missing items out of selection and exposes incomplete outcomes', async () => {
  vi.mocked(reviewExchange.startValidation).mockResolvedValue({...ready('valid'), missing: ['not-returned'], items: [
    {item_id: 'valid', status: 'valid', completion: 'needs_context'},
    {item_id: 'stale', status: 'stale', reason: 'Source changed'},
    {item_id: 'invalid', status: 'invalid', reason: 'Unsupported evidence'},
  ]});
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  upload('a.json');
  await screen.findByRole('button', {name: 'Queue import 1 selected items'});
  fireEvent.click(screen.getByText('Review items and choose what to import'));
  expect(screen.getByLabelText('Import a.json stale')).toBeDisabled();
  expect(screen.getByLabelText('Import a.json invalid')).not.toBeChecked();
  expect(screen.getByText('Needs context. Remains waiting.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name: 'Queue import 1 selected items'}));
  await waitFor(() => expect(reviewExchange.createImport).toHaveBeenCalledWith('valid', ['valid']));
});

test('blocks invalid size and rejects oversized responses before upload', async () => {
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={1} onChanged={() => {}} />);
  const input = screen.getByLabelText('Returned review JSON files');
  expect(input).toHaveAttribute('multiple'); expect(input).toHaveAttribute('accept', '.json,application/json');
  for (const value of ['', '0', '-1']) {
    fireEvent.change(screen.getByLabelText('Maximum review JSON file size (MB)'), {target: {value}});
    expect(input).toBeDisabled();
    expect(screen.getByRole('button', {name: 'Prepare review JSON export'})).toBeDisabled();
  }
  fireEvent.change(screen.getByLabelText('Maximum review JSON file size (MB)'), {target: {value: '0.000001'}});
  upload('large.json');
  await screen.findByText(/File exceeds 1 bytes/);
  expect(reviewExchange.createValidation).not.toHaveBeenCalled();
});

test('reports candidates completed while export was waiting without blocking other files', async () => {
  vi.mocked(reviewExchange.createExport).mockResolvedValue({export_id: 'skip-export', state: 'complete', total: 2, processed: 2,
    elapsed_seconds: 1, max_file_bytes: 40000000, prompt: 'Review prompt', failures: [], files: [],
    skipped: [{pair_id: 7, reason: 'No longer waiting after export was requested'}]});
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={2} onChanged={() => {}} />);
  fireEvent.click(screen.getByRole('button', {name: 'Prepare review JSON export'}));
  expect(await screen.findByText(/1 candidate.*left the waiting queue/)).toBeTruthy();
});

test('does not start a validation if its metadata arrives after unmount', async () => {
  let resolve!: (value: ReviewExchangeTask) => void;
  vi.mocked(reviewExchange.createValidation).mockReturnValue(new Promise(done => {resolve = done;}));
  const view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  upload('a.json'); view.unmount();
  await act(async () => {resolve(task('a.json'));});
  expect(reviewExchange.startValidation).not.toHaveBeenCalled();
});

test('shows rejected import reasons even when the job itself completed', async () => {
  vi.mocked(reviewExchange.startImport).mockImplementation(async id => ({...task(id, 'complete'), imported: 0, rejected: 1,
    items: [{item_id: 'a.json', status: 'stale', reason: 'Evidence changed after validation'}]}));
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  upload('a.json');
  fireEvent.click(await screen.findByRole('button', {name: 'Queue import 1 selected items'}));
  await screen.findByText(/1 rejected/);
  expect(screen.getByText('Evidence changed after validation')).toBeTruthy();
});

test('restores server-known tasks by GET and warns about files that were not uploaded', async () => {
  vi.mocked(reviewExchange.startValidation).mockImplementation(async id => task(id, 'running'));
  vi.mocked(reviewExchange.createValidation).mockImplementation(async file => task(file.name));
  let view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  upload('a.json', 'b.json', 'c.json');
  await waitFor(() => expect(reviewExchange.startValidation).toHaveBeenCalledTimes(2));
  view.unmount(); vi.clearAllMocks();
  vi.mocked(reviewExchange.validationStatus).mockImplementation(async id => ready(id));
  view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  await waitFor(() => expect(reviewExchange.validationStatus).toHaveBeenCalledTimes(2));
  expect(reviewExchange.createValidation).not.toHaveBeenCalled();
  expect(reviewExchange.startValidation).not.toHaveBeenCalled();
  expect(await screen.findAllByRole('button', {name: 'Queue import 1 selected items'})).toHaveLength(2);
  expect(screen.getByText(/1 file.*reselect/i)).toBeTruthy();
});

test('restores completed imports without importing again or repeating the parent notification', async () => {
  const onChanged = vi.fn();
  vi.mocked(reviewExchange.importStatus).mockImplementation(async id => ({...task(id, 'complete'), imported: 1}));
  let view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={onChanged} />);
  upload('a.json');
  fireEvent.click(await screen.findByRole('button', {name: 'Queue import 1 selected items'}));
  await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
  view.unmount(); vi.clearAllMocks();
  view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={onChanged} />);
  await waitFor(() => expect(reviewExchange.importStatus).toHaveBeenCalledWith('import-a.json'));
  await screen.findByText(/1 imported, 0 unchanged/);
  expect(reviewExchange.createImport).not.toHaveBeenCalled();
  expect(reviewExchange.startImport).not.toHaveBeenCalled();
  expect(onChanged).not.toHaveBeenCalled();
});

test('restores the most recent export by GET without automatically resuming it', async () => {
  localStorage.setItem('review-json-export-v1', 'saved-export');
  vi.mocked(reviewExchange.exportStatus).mockResolvedValue({export_id: 'saved-export', state: 'interrupted', total: 1, processed: 0, elapsed_seconds: 1,
    max_file_bytes: 40000000, prompt: 'Review prompt', failures: [], files: []});
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  await screen.findByRole('button', {name: 'Resume review export'});
  expect(reviewExchange.exportStatus).toHaveBeenCalledWith('saved-export');
  expect(reviewExchange.createExport).not.toHaveBeenCalled();
  expect(reviewExchange.resumeExport).not.toHaveBeenCalled();
});

test('preserves imports waiting for a slot across remount and starts them with their original selection', async () => {
  vi.useFakeTimers();
  let freeFirstSlot = false;
  vi.mocked(reviewExchange.startImport).mockImplementation(async id => task(id, 'running'));
  vi.mocked(reviewExchange.importStatus).mockImplementation(async id => task(id, freeFirstSlot && id === 'import-a.json' ? 'complete' : 'running'));
  let view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  upload('a.json', 'b.json', 'c.json');
  await act(async () => {await vi.advanceTimersByTimeAsync(100);});
  for (const button of screen.getAllByRole('button', {name: 'Queue import 1 selected items'})) fireEvent.click(button);
  await act(async () => {await vi.advanceTimersByTimeAsync(100);});
  expect(reviewExchange.startImport).toHaveBeenCalledTimes(2);
  expect(screen.getByText(/1 queued, 2 importing/)).toBeTruthy();
  view.unmount();
  view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  await act(async () => {await vi.advanceTimersByTimeAsync(100);});
  expect(screen.getByRole('region', {name: 'Import 3 from c.json'})).toBeTruthy();
  expect(screen.getByText(/1 queued, 2 importing/)).toBeTruthy();
  expect(reviewExchange.createImport).toHaveBeenCalledTimes(2);
  expect(JSON.parse(localStorage.getItem('review-json-import-tasks-v1')!).missingFiles).toBe(0);
  freeFirstSlot = true;
  await act(async () => {await vi.advanceTimersByTimeAsync(1000);});
  expect(reviewExchange.createImport).toHaveBeenCalledTimes(3);
  expect(reviewExchange.createImport).toHaveBeenLastCalledWith('c.json', ['c.json']);
  expect(reviewExchange.startImport).toHaveBeenLastCalledWith('import-c.json');
});

test('restores failed import creation with its selection and waits for explicit retry', async () => {
  vi.mocked(reviewExchange.createImport).mockRejectedValueOnce(new Error('Import metadata unavailable'));
  const view = render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  upload('a.json');
  fireEvent.click(await screen.findByRole('button', {name: 'Queue import 1 selected items'}));
  await screen.findByText('Import metadata unavailable');
  view.unmount();
  render(<ReviewExchangePanel pairKeys={[]} waitingTotal={0} onChanged={() => {}} />);
  const retry = await screen.findByRole('button', {name: 'Retry import'});
  expect(reviewExchange.createImport).toHaveBeenCalledTimes(1);
  fireEvent.click(retry);
  await waitFor(() => expect(reviewExchange.createImport).toHaveBeenCalledTimes(2));
  expect(reviewExchange.createImport).toHaveBeenLastCalledWith('a.json', ['a.json']);
});
