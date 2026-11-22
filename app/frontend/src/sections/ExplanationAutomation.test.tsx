import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { ExplanationAutomation } from './ExplanationAutomation';
import { explanationProviders } from '../lib/api';

vi.mock('../lib/api', async original => ({...await original<typeof import('../lib/api')>(), explanationProviders: {
  catalog: vi.fn(), config: vi.fn(), status: vi.fn(), saveProfile: vi.fn(), removeKey: vi.fn(), start: vi.fn(), stop: vi.fn(), resume: vi.fn(), cancel: vi.fn(),
}}));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(explanationProviders.catalog).mockResolvedValue({providers: [{id: 'free', name: 'Free provider', models: ['free-model'], free_verified: true, eligibility: 'Public npm only', quota: 'Daily free quota', restrictions: ['No paid fallback'], official_urls: ['https://example.com/free'], verified_at: '2026-10-27'}]});
  vi.mocked(explanationProviders.config).mockResolvedValue({enabled: false, max_concurrency: 1, profiles: [{provider_id: 'free', model: 'free-model', enabled: false, free_account_confirmed: false, terms_confirmed: false, has_key: false, eligible: false, blocked_reason: 'Key required'}]});
  vi.mocked(explanationProviders.status).mockResolvedValue({enabled: false, running: false, counts: {queued: 0, running: 0, completed: 0, failed: 0, waiting: 0, cancelled: 0, blocked: 0}, last_error: null});
  vi.mocked(explanationProviders.start).mockResolvedValue({enabled: true, running: true, counts: {queued: 1, running: 0, completed: 0, failed: 0, waiting: 0, cancelled: 0, blocked: 0}, last_error: null});
});

test('loads provider controls on expansion and never enables automation implicitly', async () => {
  render(<ExplanationAutomation />);
  expect(explanationProviders.catalog).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Optional free explanation automation'));
  expect(await screen.findByText('Free provider')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Free provider'));
  expect(screen.getByText(/Public npm source only/)).toBeInTheDocument();
  expect(explanationProviders.start).not.toHaveBeenCalled();
  expect(screen.getByLabelText('Free provider API key')).toHaveAttribute('type', 'password');
  fireEvent.click(screen.getByRole('button', {name: 'Enable free automation'}));
  await waitFor(() => expect(explanationProviders.start).toHaveBeenCalledOnce());
});

test('saves a dedicated provider profile and clears the entered credential', async () => {
  vi.mocked(explanationProviders.saveProfile).mockResolvedValue({} as any);
  render(<ExplanationAutomation />);
  fireEvent.click(screen.getByText('Optional free explanation automation'));
  fireEvent.click(await screen.findByText('Free provider'));
  const key = await screen.findByLabelText('Free provider API key');
  fireEvent.change(key, {target: {value: 'test-only-key'}});
  fireEvent.click(screen.getByLabelText('Use Free provider'));
  fireEvent.click(screen.getByLabelText('Free account confirmed for Free provider'));
  fireEvent.click(screen.getByLabelText('Terms accepted for Free provider'));
  fireEvent.click(screen.getByRole('button', {name: 'Save Free provider'}));
  await waitFor(() => expect(explanationProviders.saveProfile).toHaveBeenCalledWith('free', expect.objectContaining({api_key: 'test-only-key', enabled: true, free_account_confirmed: true, terms_confirmed: true})));
  await waitFor(() => expect(key).toHaveValue(''));
});

test('shows backend errors and provides a retry for loading', async () => {
  vi.mocked(explanationProviders.catalog).mockRejectedValueOnce(new Error('Provider catalog unavailable'));
  render(<ExplanationAutomation />);
  fireEvent.click(screen.getByText('Optional free explanation automation'));
  expect(await screen.findByRole('alert')).toHaveTextContent('Provider catalog unavailable');
  fireEvent.click(screen.getByRole('button', {name: 'Try again'}));
  expect(await screen.findByText('Free provider')).toBeInTheDocument();
});

test('does not ask keyless free routes for credentials', async () => {
  vi.mocked(explanationProviders.catalog).mockResolvedValueOnce({providers: [{id: 'classic', name: 'Classic free', models: ['openai'], free_verified: true, requires_key: false, eligibility: 'Public npm only', quota: 'One request per 30 seconds', restrictions: [], official_urls: [], verified_at: '2026-10-27'}]});
  render(<ExplanationAutomation />);
  fireEvent.click(screen.getByText('Optional free explanation automation'));
  fireEvent.click(await screen.findByText('Classic free'));
  expect(screen.queryByLabelText('Classic free API key')).not.toBeInTheDocument();
});
