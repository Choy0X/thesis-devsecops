import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { FinetuneRuns } from "./FinetuneRuns";
import { api, type FinetuneRun } from "../lib/api";
vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { ...actual.api, finetuneRuns: vi.fn(), finetuneRun: vi.fn(), runCurves: vi.fn(), localFit: vi.fn(), listJobs: vi.fn(), createTrainingRun: vi.fn() } };
});

it('clones through reserved creation and retains the same request ID after a name conflict', async () => {
  vi.mocked(api.finetuneRun).mockResolvedValue({name: run.name, prepared: {}, reexport: {
    tag: 'v1', purpose: 'train', revision: 'a'.repeat(40), tokenizer_revision: 'b'.repeat(40),
    overrides: {'lora.r': 64}, refresh: true}, state: {state: 'configured', blocked: [], evidence: {}}, imports: [], eval_imports: []} as any);
  vi.mocked(api.createTrainingRun).mockRejectedValue(new Error('A run with that name exists already.'));
  render(<ConfirmProvider><FinetuneRuns /></ConfirmProvider>);
  fireEvent.click(await screen.findByRole('button', {name: run.name}));
  fireEvent.click(await screen.findByText('Run provenance and prepare again'));
  fireEvent.change(await screen.findByLabelText('Name of the new run'), {target: {value: 'new-clone'}});
  fireEvent.click(screen.getByRole('button', {name: 'Prepare again'}));
  await waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledTimes(1));
  const first = vi.mocked(api.createTrainingRun).mock.calls[0][0];
  expect(first).toEqual({name: 'new-clone', request_id: expect.any(String), tag: 'v1', purpose: 'train',
    revision: 'a'.repeat(40), tokenizer_revision: 'b'.repeat(40), overrides: {'lora.r': 64, 'model.id': run.model}});
  expect(first).not.toHaveProperty('run');
  expect(first).not.toHaveProperty('refresh');
  await screen.findByText('A run with that name exists already.');
  fireEvent.click(screen.getByRole('button', {name: 'Prepare again'}));
  await waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.createTrainingRun).mock.calls[1][0].request_id).toBe(first.request_id);
});

it('blocks full-purpose cloning when current data preflight is blocked', async () => {
  vi.mocked(api.finetuneRun).mockResolvedValue({name: run.name, prepared: {}, reexport: {tag: 'v1', purpose: 'train'},
    full_training: {allowed: false, blockers: ['insufficient_independent_security_partitions_for_full_run'],
      integrity: {state: 'passed', blockers: []}, full_run: {state: 'blocked', blockers: [], limitations: []}, diagnostics: {state: 'blocked'}},
    state: {state: 'configured', blocked: [], evidence: {}}, imports: [], eval_imports: []} as any);
  render(<ConfirmProvider><FinetuneRuns /></ConfirmProvider>);
  fireEvent.click(await screen.findByRole('button', {name: run.name}));
  fireEvent.click(await screen.findByText('Run provenance and prepare again'));
  fireEvent.change(await screen.findByLabelText('Name of the new run'), {target: {value: 'new-full-run'}});
  expect(screen.getByRole('button', {name: 'Prepare again'})).toBeDisabled();
  expect(screen.getByText('Built, insufficient evidence for full training')).toBeVisible();
});

it('shows source-only evaluation unavailability and keeps benchmark import available', async () => {
  vi.mocked(api.finetuneRun).mockResolvedValue({name: run.name, prepared: {}, state: {state: 'trained', blocked: [], evidence: {}},
    evaluation_availability: {state: 'blocked', reason_code: 'unsupported_corpus', detail: 'No held-out reviewed security cohort.',
      remediation: 'Supply an independent frozen benchmark.'}, imports: [], eval_imports: []} as any);
  render(<ConfirmProvider><FinetuneRuns /></ConfirmProvider>);
  fireEvent.click(await screen.findByRole('button', {name: run.name}));
  expect(await screen.findByText(/No held-out reviewed security cohort/)).toBeVisible();
  fireEvent.click(screen.getByText('Diagnostics'));
  expect(await screen.findByRole('button', {name: 'Answer a few views'})).toBeDisabled();
  fireEvent.click(screen.getByText('Import outputs and evaluations'));
  expect(await screen.findByLabelText('Independent benchmark directory (optional)')).toBeVisible();
});

it('shows experimental trial progress and import evidence without the full training gate ladder', async () => {
  vi.mocked(api.finetuneRuns).mockResolvedValue({runs: [{...run, mode: 'trial', total_budget_minutes: 30}], total: 1, root: '', limit: 100, offset: 0});
  vi.mocked(api.finetuneRun).mockResolvedValue({name: run.name, prepared: {mode: 'trial'},
    state: {state: 'configured', evidence: {}, blocked: [{state: 'cpu_validated', reason: 'run the CPU check'},
      {state: 'gpu_smoke_tested', reason: 'needs GPU smoke'}]}, imports: [], eval_imports: [], trial_imports: []} as any);
  render(<ConfirmProvider><FinetuneRuns /></ConfirmProvider>);
  fireEvent.click(await screen.findByRole('button', {name: run.name}));
  expect(await screen.findByRole('list', {name: 'Trial progress'})).toHaveTextContent('Waiting for trial_outputs.zip');
  expect(screen.queryByRole('list', {name: 'Run progress'})).not.toBeInTheDocument();
  expect(screen.queryByText('Diagnostics')).not.toBeInTheDocument();
  expect(screen.queryByText(/gpu smoke tested/i)).not.toBeInTheDocument();
  expect(screen.getByLabelText('Path of trial_outputs.zip')).toBeInTheDocument();
});
vi.mock("../lib/jobsList", () => ({ useRunningJobId: () => null, refreshJobs: vi.fn() }));
const run: FinetuneRun = { name: "compact-run", state: "configured", dataset: { tag: "v1", status: "frozen", manifest_sha256: "dataset-hash" }, stage: 1, blocked: [], model: "org/VisibleModel", model_revision: "base-commit", purpose: "train", profile: "contract", overrides: 0, prepared_at: "2026-10-27T12:00:00Z", bundle: { path: "bundle", sha256: "bundle-hash", bytes: 1000, files: 3, warnings: [{ code: "note", detail: "Keep this warning visible." }] } };
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.finetuneRuns).mockResolvedValue({ runs: [run], total: 1, root: "", limit: 100, offset: 0 });
  vi.mocked(api.finetuneRun).mockResolvedValue({ name: run.name, prepared: {}, reexport: { tag: "v1" }, cpu_validation: null, state: { state: "configured", blocked: [], evidence: {} }, imports: [], eval_imports: [], local_gpu: null });
  vi.mocked(api.runCurves).mockResolvedValue({ sources: [] } as any);
  vi.mocked(api.localFit).mockResolvedValue({ available: false, reason: "No GPU available", pinned_base: null, default_model: "", running: false, gate: null });
  vi.mocked(api.listJobs).mockResolvedValue([]);
});
it("keeps a selected run compact and reveals existing actions contextually", async () => {
  render(<ConfirmProvider><FinetuneRuns /></ConfirmProvider>);
  fireEvent.click(await screen.findByRole("button", { name: run.name }));
  await screen.findByText("No evaluation metrics imported for this run.");
  expect(screen.getAllByText("VisibleModel").length).toBeGreaterThan(0);
  expect(screen.getByText("Keep this warning visible.")).toBeVisible();
  expect(screen.getByText("Training curves")).toBeVisible();
  expect(screen.getByRole("list", { name: "Run progress" })).toBeVisible();
  for (const label of ["Import outputs and evaluations", "Diagnostics", "Run provenance and prepare again", "Downloads and exports"]) {
    expect(screen.getByText(label).closest("details")).not.toHaveAttribute("open");
  }
  expect(screen.queryByText("Base commit")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Name of the new run")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Path of outputs.zip")).not.toBeInTheDocument();
  expect(api.localFit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Import outputs and evaluations"));
  expect(await screen.findByLabelText("Path of outputs.zip")).toBeVisible();
  expect(screen.getByLabelText("Path of eval_outputs.zip")).toBeVisible();
  fireEvent.click(screen.getByText("Run provenance and prepare again"));
  const name = await screen.findByLabelText("Name of the new run");
  expect(screen.getByText("Base commit")).toBeVisible();
  fireEvent.change(name, { target: { value: "saved-draft" } });
  fireEvent.click(screen.getByText("Run provenance and prepare again"));
  fireEvent.click(screen.getByText("Run provenance and prepare again"));
  expect(screen.getByLabelText("Name of the new run")).toHaveValue("saved-draft");
  fireEvent.click(screen.getByText("Diagnostics"));
  expect(await screen.findByRole("button", { name: "Run the CPU check" })).toBeVisible();
  expect(screen.queryByText('Train locally')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', {name: 'Run a QLoRA smoke'})).not.toBeInTheDocument();
  expect(api.localFit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Downloads and exports"));
  expect(await screen.findByRole("button", { name: "Download bundle" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Download notebook" })).toBeVisible();
});
