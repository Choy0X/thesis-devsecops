import {beforeEach, expect, test, vi} from 'vitest';
import {api, reviewExchange} from './api';
import {request} from './request';
vi.mock('./request', async original => ({...await original<typeof import('./request')>(), request: vi.fn()}));
beforeEach(() => vi.clearAllMocks());

test('uses isolated review exchange endpoints and sends the raw file with encoded filename', async () => {
  const file = new File(['{}'], 'review + returned.json');
  await reviewExchange.createExport({ecosystem: 'npm', max_file_bytes: 40000000});
  expect(request).toHaveBeenLastCalledWith('/api/data/review/exchange/exports', {method: 'POST', body: JSON.stringify({ecosystem: 'npm', max_file_bytes: 40000000})});
  await reviewExchange.createValidation(file, 500000);
  expect(request).toHaveBeenLastCalledWith('/api/data/review/exchange/validations?max_file_bytes=500000&filename=review+%2B+returned.json', {method: 'POST', headers: {'content-type': 'application/json'}, body: file});
  await reviewExchange.startValidation('id/one');
  expect(request).toHaveBeenLastCalledWith('/api/data/review/exchange/validations/id%2Fone/start', {method: 'POST'});
  await reviewExchange.createImport('validation', ['pair']);
  expect(request).toHaveBeenLastCalledWith('/api/data/review/exchange/imports', {method: 'POST', body: JSON.stringify({validation_id: 'validation', selected: ['pair']})});
  await reviewExchange.startImport('id/two');
  expect(request).toHaveBeenLastCalledWith('/api/data/review/exchange/imports/id%2Ftwo/start', {method: 'POST'});
});

test('status requests only read metadata without caching and review filters are additive', async () => {
  for (const [read, path] of [[reviewExchange.exportStatus, 'exports'], [reviewExchange.validationStatus, 'validations'], [reviewExchange.importStatus, 'imports']] as const) {
    await read('id');
    expect(request).toHaveBeenLastCalledWith(`/api/data/review/exchange/${path}/id`, {ttl: 0});
  }
  await api.reviewQueue({review_state: 'ai_adjudicated', limit: 20});
  expect(request).toHaveBeenLastCalledWith('/api/data/review/queue?review_state=ai_adjudicated&limit=20');
});
