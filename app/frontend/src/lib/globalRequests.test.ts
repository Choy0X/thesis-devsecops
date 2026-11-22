import { afterEach, expect, it, vi } from 'vitest';
import { api } from './api';
import { resetRequests, setRequestScope } from './request';

afterEach(() => { resetRequests(); vi.unstubAllGlobals(); });

it('keeps shared shell metadata alive when the active page changes', async () => {
  const signals: AbortSignal[] = [];
  const complete: (() => void)[] = [];
  vi.stubGlobal('fetch', vi.fn((_url, init) => new Promise<Response>((resolve, reject) => {
    signals.push(init.signal);
    init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    complete.push(() => resolve(new Response('{}', {status: 200})));
  })));
  const page = new AbortController(); setRequestScope(page);
  const requests = Promise.allSettled([api.getSettings(), api.compute(), api.listJobs()]);
  page.abort();
  const alive = signals.every(signal => !signal.aborted);
  complete.forEach(finish => finish());
  expect((await requests).every(result => result.status === 'fulfilled')).toBe(true);
  expect(alive).toBe(true);
});
