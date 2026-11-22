import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { BuildEvidenceActions } from './BuildEvidenceActions';
import { api, type BuildEvidencePage, type ProfileBuild } from '../lib/api';

const attach = vi.hoisted(() => vi.fn());
vi.mock('./QualificationWorkflowPanel', () => ({QualificationWorkflowPanel: () => null}));
vi.mock('../lib/useJob', () => ({useJob: () => ({attach, status: 'idle', id: null, progress: null})}));
vi.mock('../lib/api', async original => ({...await original<typeof import('../lib/api')>(), api: {repairBuildEvidence: vi.fn(), retryBuild: vi.fn(), buildEvidence: vi.fn()}}));
vi.mock('./ContextRepairDialog', () => ({ContextRepairDialog: ({sourceRecoveryAvailable}: {sourceRecoveryAvailable?: boolean}) => <p>Saved recovery dialog. Recovery {sourceRecoveryAvailable ? 'available' : 'unavailable'}.</p>}));
const build = {tag: 'v2', state: 'blocked', evidence_actions: {repair: {available: true}, verify: {available: false, reason: 'No failed verification attempts.'}, rebuild: {available: false, reason: 'Evidence is unchanged.'}}} as ProfileBuild;
const outcomes = (offset: number): BuildEvidencePage => ({items: [{record_id: `${offset + 1}`, pair_key: `pair-${offset + 1}`, outcome: 'excluded', reasons: ['unverified_source'], proof_status: 'not_executed'}], total: 201, offset, limit: 100, summary: {counts: {excluded: 201}, reason_counts: {unverified_source: 201}, admission_policy: 'automatic-evidence/2'}});
beforeEach(() => {vi.clearAllMocks(); vi.mocked(api.buildEvidence).mockImplementation(async (_tag, offset = 0) => outcomes(offset));});

test('loads bounded evidence pages only after opening details', async () => {
  render(<BuildEvidenceActions build={build} onChanged={vi.fn()} onCreated={vi.fn()} />);
  expect(api.buildEvidence).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Evidence outcomes and exclusion reasons'));
  expect(await screen.findByText('pair-1')).toBeInTheDocument();
  expect(api.buildEvidence).toHaveBeenCalledWith('v2', 0);
  fireEvent.click(screen.getByRole('button', {name: 'Next outcomes'}));
  expect(await screen.findByText('pair-101')).toBeInTheDocument();
  expect(api.buildEvidence).toHaveBeenLastCalledWith('v2', 100);
});

test('attaches the durable repair job and exposes action failures', async () => {
  vi.mocked(api.repairBuildEvidence).mockResolvedValueOnce({job_id: 'repair-1'}).mockRejectedValueOnce(new Error('Repository source unavailable'));
  const changed = vi.fn();
  render(<BuildEvidenceActions build={build} onChanged={changed} onCreated={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', {name: 'Repair evidence'}));
  await waitFor(() => expect(attach).toHaveBeenCalledWith('repair-1'));
  expect(changed).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', {name: 'Repair evidence'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('Repository source unavailable');
});

test('ignores stale evidence page responses after closing details', async () => {
  let finish!: (value: BuildEvidencePage) => void;
  vi.mocked(api.buildEvidence).mockReturnValueOnce(new Promise(resolve => {finish = resolve;}));
  render(<BuildEvidenceActions build={build} onChanged={vi.fn()} onCreated={vi.fn()} />);
  const summary = screen.getByText('Evidence outcomes and exclusion reasons');
  fireEvent.click(summary);
  await waitFor(() => expect(api.buildEvidence).toHaveBeenCalledOnce());
  fireEvent.click(summary);
  await waitFor(() => expect(screen.queryByText('Reading evidence outcomes...')).not.toBeInTheDocument());
  await act(async () => {finish(outcomes(0));});
  expect(screen.queryByText('pair-1')).not.toBeInTheDocument();
});


test('keeps saved recovery history accessible when new evidence work is unavailable', async () => {
  const published = {...build, evidence_artifact_available: true, evidence_actions: {...build.evidence_actions!, repair: {available: false, reason: 'Evidence work is running.'}}};
  render(<BuildEvidenceActions build={published} onChanged={vi.fn()} onCreated={vi.fn()} />);
  const button = screen.getByRole('button', {name: 'Repair insufficient context'});
  expect(button).toBeEnabled();
  fireEvent.click(button);
  expect(await screen.findByText('Saved recovery dialog. Recovery unavailable.')).toBeInTheDocument();
});

test('does not expose context repair before an artifact exists', () => {
  render(<BuildEvidenceActions build={{...build, evidence_artifact_available: false}} onChanged={vi.fn()} onCreated={vi.fn()} />);
  expect(screen.getByRole('button', {name: 'Repair insufficient context'})).toBeDisabled();
});
