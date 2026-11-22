import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, test, vi} from 'vitest';
import {JsonContextExchange} from './JsonContextExchange';
import {contextReview, type ContextJsonExport} from '../lib/api';

const done: ContextJsonExport = {export_id: 'export', state: 'complete', total: 1201, processed: 1201, elapsed_seconds: 12,
  max_file_bytes: 40000000, prompt: 'Expert prompt', failures: [], files: [
    {index: 0, name: 'part-1.json', packet_id: 'p1', item_count: 1199, bytes: 39999999, sha256: 'hash'},
    {index: 1, name: 'part-2.json', packet_id: 'p2', item_count: 2, bytes: 1234, sha256: 'hash'}]};
vi.mock('../lib/api', () => ({contextReview: {jsonCreate: vi.fn(), jsonStatus: vi.fn(), jsonResume: vi.fn(), jsonCancel: vi.fn(),
  jsonPreview: vi.fn(), jsonApply: vi.fn(), jsonDownload: (id: string, i: number) => `/json/${id}/${i}`}}));
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  vi.mocked(contextReview.jsonCreate).mockResolvedValue(done);
  vi.mocked(contextReview.jsonStatus).mockResolvedValue(done);
  vi.mocked(contextReview.jsonPreview).mockImplementation(async file => ({upload_id: file.name, packet_id: 'packet', missing: ['missing'],
    items: [{item_id: 'valid', status: 'valid'}, {item_id: 'stale', status: 'stale', reason: 'source changed'}]}));
  vi.mocked(contextReview.jsonApply).mockResolvedValue({upload_id: 'returned', packet_id: 'packet', items: [], missing: [], imported: 1, unchanged: 0});
});

test('exports the entire selection with only byte boundaries and all files unlocked', async () => {
  const ids = Array.from({length: 1201}, (_, i) => i + 1);
  render(<JsonContextExchange ids={ids} campaignId="campaign" onDone={() => {}} />);
  expect(screen.getByLabelText('Maximum JSON file size (MB)')).toHaveValue(40);
  expect(screen.queryByText(/Next batch|ZIP|Selection limit/)).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: 'Prepare JSON export'}));
  expect(await screen.findByRole('link', {name: 'Download JSON 1'})).toHaveAttribute('href', '/json/export/0');
  expect(screen.getByRole('link', {name: 'Download JSON 2'})).toHaveAttribute('href', '/json/export/1');
  expect(contextReview.jsonCreate).toHaveBeenCalledWith(ids, 'campaign', 40000000);
  expect(screen.getByText(/1,199 items/)).toBeTruthy();
});

test('validates multiple files sequentially and imports independently without a selection', async () => {
  const onDone = vi.fn(); let concurrent = 0, maximum = 0;
  const preview = vi.mocked(contextReview.jsonPreview).getMockImplementation()!;
  vi.mocked(contextReview.jsonPreview).mockImplementation(async file => {
    concurrent++; maximum = Math.max(maximum, concurrent);
    const result = await preview(file); concurrent--; return result;
  });
  render(<JsonContextExchange ids={[]} onDone={onDone} />);
  const input = screen.getByLabelText('Returned JSON files');
  expect(input).toHaveAttribute('multiple');
  fireEvent.change(input, {target: {files: [new File(['{}'], 'first.json'), new File(['{}'], 'second.json')]}});
  await waitFor(() => expect(contextReview.jsonPreview).toHaveBeenCalledTimes(2));
  expect(maximum).toBe(1);
  await waitFor(() => expect(screen.getAllByRole('button', {name: 'Import 1 selected drafts from this file'})).toHaveLength(2));
  fireEvent.click(screen.getAllByRole('button', {name: 'Import 1 selected drafts from this file'})[1]);
  await waitFor(() => expect(contextReview.jsonApply).toHaveBeenCalledWith('second.json', ['valid']));
  expect(onDone).toHaveBeenCalledOnce();
});

test('rejects oversized files without uploading and preserves historical ZIP links', async () => {
  localStorage.setItem('context-zip:review', 'legacy');
  render(<JsonContextExchange ids={[]} onDone={() => {}} />);
  const huge = new File(['{}'], 'huge.json'); Object.defineProperty(huge, 'size', {value: 40000001});
  fireEvent.change(screen.getByLabelText('Returned JSON files'), {target: {files: [huge]}});
  expect(await screen.findByText(/File exceeds 40,000,000/)).toBeTruthy();
  expect(contextReview.jsonPreview).not.toHaveBeenCalled();
  expect(contextReview.jsonStatus).not.toHaveBeenCalled();
  expect(localStorage.getItem('context-zip:review')).toBe('legacy');
});

test('allows a smaller or larger file size and uses it for creation and imports', async () => {
  render(<JsonContextExchange ids={[7]} onDone={() => {}} />);
  const size = screen.getByLabelText('Maximum JSON file size (MB)');
  fireEvent.change(size, {target: {value: '0.5'}});
  fireEvent.click(screen.getByRole('button', {name: 'Prepare JSON export'}));
  await waitFor(() => expect(contextReview.jsonCreate).toHaveBeenCalledWith([7], undefined, 500000));
  fireEvent.click(screen.getByRole('button', {name: 'Use current selection for a new export'}));
  fireEvent.change(size, {target: {value: '80'}});
  const returned = new File(['{}'], 'large.json'); Object.defineProperty(returned, 'size', {value: 50000000});
  fireEvent.change(screen.getByLabelText('Returned JSON files'), {target: {files: [returned]}});
  await waitFor(() => expect(contextReview.jsonPreview).toHaveBeenCalledWith(returned, 80000000));
});

test('blocks export and upload when the size is invalid', () => {
  render(<JsonContextExchange ids={[7]} onDone={() => {}} />);
  for (const value of ['', '0', '-1']) {
    fireEvent.change(screen.getByLabelText('Maximum JSON file size (MB)'), {target: {value}});
    expect(screen.getByRole('button', {name: 'Prepare JSON export'})).toBeDisabled();
    expect(screen.getByLabelText('Returned JSON files')).toBeDisabled();
  }
});

test('can add multiple files while validation is running and continues after a failed file', async () => {
  let finishFirst: (value: any) => void = () => {};
  vi.mocked(contextReview.jsonPreview).mockImplementation(file => {
    if (file.name === 'first.json') return new Promise(resolve => {finishFirst = resolve;});
    if (file.name === 'bad.json') return Promise.reject(new Error('Invalid JSON'));
    return Promise.resolve({upload_id: file.name, packet_id: 'packet', missing: [], items: [{item_id: 'second', status: 'valid'}]});
  });
  render(<JsonContextExchange ids={[]} onDone={() => {}} />);
  const input = screen.getByLabelText('Returned JSON files');
  fireEvent.change(input, {target: {files: [new File(['{}'], 'first.json')]}});
  await waitFor(() => expect(contextReview.jsonPreview).toHaveBeenCalledTimes(1));
  expect(input).toBeEnabled();
  fireEvent.change(input, {target: {files: [new File(['bad'], 'bad.json'), new File(['{}'], 'second.json')]}});
  expect(contextReview.jsonPreview).toHaveBeenCalledTimes(1);
  finishFirst({upload_id: 'first', packet_id: 'packet', missing: [], items: [{item_id: 'first', status: 'valid'}]});
  await waitFor(() => expect(contextReview.jsonPreview).toHaveBeenCalledTimes(3));
  expect(await screen.findByText('Invalid JSON')).toBeTruthy();
  expect(screen.getAllByRole('button', {name: 'Import 1 selected drafts from this file'})).toHaveLength(2);
});
