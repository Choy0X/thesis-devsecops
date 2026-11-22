import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { choose } from "../test/select";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from 'react-router-dom';

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { trialCapability: vi.fn(), trainConfig: vi.fn(), trainProfiles: vi.fn(), trainResolve: vi.fn(), builds: vi.fn(), modelFit: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn(),
    createTrainingRun: vi.fn(), localModels: vi.fn(), modelShelf: vi.fn(), searchModels: vi.fn(), buildDetail: vi.fn(), trainSchedule: vi.fn(), externalRemove: vi.fn(), externalUpload: vi.fn(), externalCheck: vi.fn(), externalImport: vi.fn(), externalDiscard: vi.fn() } };
});
import { api, type TrainConfig } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ApiError } from "../lib/apiError";
import { TrainWizard, contractValue, differences } from "./TrainWizard";

const GB = 1024 ** 3;
const cfg: TrainConfig = {
  counts: { settings: 9, fixed: 0, changeable: 9 }, file_sha256: "a".repeat(64), unresolved: [],
  sections: [
    { key: "model", title: "Base model", fields: [{ key: "model.id", name: "id", kind: "text", value: "Qwen/Qwen2.5-Coder-7B", fixed: false }] },
    { key: "lora", title: "LoRA adapters", fields: [{ key: "lora.r", name: "r", kind: "int", value: 32, fixed: false }, { key: "lora.lora_alpha", name: "lora_alpha", kind: "int", value: 64, fixed: false },
      { key: "lora.lora_dropout", name: "lora_dropout", kind: "float", value: 0.05, fixed: false },
      { key: "lora.use_dora", name: "use_dora", kind: "bool", value: false, fixed: false }, { key: "lora.init_loftq", name: "init_loftq", kind: "bool", value: false, fixed: false }] },
    { key: "training", title: "Training", fields: [{ key: "training.learning_rate", name: "learning_rate", kind: "float", value: 0.0001, fixed: false },
      { key: "training.weight_decay", name: "weight_decay", kind: "float", value: 0.01, fixed: false }, { key: "training.warmup_ratio", name: "warmup_ratio", kind: "float", value: 0.03, fixed: false },
      { key: "training.max_grad_norm", name: "max_grad_norm", kind: "float", value: 1, fixed: false }, { key: "training.per_device_train_batch_size", name: "x", kind: "int", value: 1, fixed: false },
      { key: "training.optimizer", name: "optimizer", kind: "choice", value: "adamw_torch", choices: ["adamw_torch", "adafactor", "adamw_8bit"], fixed: false },
      { key: "training.scheduler", name: "scheduler", kind: "choice", value: "cosine", choices: ["cosine", "linear", "constant", "constant_with_warmup"], fixed: false },
      { key: "training.pack_sequences", name: "pack_sequences", kind: "bool", value: false, fixed: false }, { key: "training.warmup_steps", name: "warmup_steps", kind: "int", value: null, fixed: false }, { key: "training.evaluate_every_steps", name: "evaluate_every_steps", kind: "int", value: null, fixed: false },
      { key: "training.maximum_pilot_family_passes", name: "maximum_pilot_family_passes", kind: "float", value: 3, fixed: false },
      { key: "training.effective_sequence_batch_size", name: "effective_sequence_batch_size", kind: "int", value: 32, fixed: false },
      { key: "training.evaluate_at_family_passes", name: "evaluate_at_family_passes", kind: "list", value: [0.5, 1, 2, 3], fixed: false }] },
    { key: "context", title: "Context", fields: [{ key: "context.max_total_tokens", name: "max_total_tokens", kind: "int", value: 4096, fixed: false }] },
    { key: "data", title: "Data", fields: [{ key: "data.train_slice_start", name: "train_slice_start", kind: "float", value: 0, fixed: false },
      { key: "data.train_slice_end", name: "train_slice_end", kind: "float", value: 1, fixed: false }] },
    { key: "advanced", title: "Advanced", fields: [{ key: "advanced.online_rl", name: "online_rl", kind: "bool", value: false, fixed: false }, { key: "advanced.pcgrad", name: "pcgrad", kind: "bool", value: false, fixed: false }] },
  ],
};
const profiles = {
  contract: { label: "As in the contract", purpose: "The pinned configuration.", rationale: "No override.", overrides: {} },
  higher_quality: { label: "Higher quality", purpose: "A larger adapter.", rationale: "Rank doubled, alpha with it.", overrides: { "lora.r": 64, "lora.lora_alpha": 128 } },
  portable_trial: {label: 'Portable trial', purpose: 'Experimental source learning', rationale: 'Approved pinned recipe',
    revision: 'c03e6d358207e414f1eca0bb1891e29f1db0e242', tokenizer_revision: 'c03e6d358207e414f1eca0bb1891e29f1db0e242',
    overrides: {'model.id': 'Qwen/Qwen2.5-Coder-7B-Instruct', 'lora.r': 64, 'lora.lora_alpha': 128,
      'training.learning_rate': .00005, 'context.max_total_tokens': 4096, 'training.micro_batch_tuning': 'auto'}},
};
const fit = (over = {}) => ({ mode: "qlora", parts: { weights: 4 * GB, total: 9 * GB }, total: 9 * GB, notes: [], check: { gpu: "too_big", ram: "fits", disk: "unknown", runs_on: "cpu", gpu_free_bytes: 5 * GB, ram_free_bytes: 12 * GB }, ...over });

const show = () => render(<MemoryRouter><ConfirmProvider><TrainWizard onDone={() => undefined} /></ConfirmProvider></MemoryRouter>);
const next = () => fireEvent.click(screen.getByRole("button", { name: "Next" }));

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.trainConfig).mockResolvedValue(cfg);
  vi.mocked(api.trainProfiles).mockResolvedValue({ profiles });
  vi.mocked(api.trialCapability).mockResolvedValue({allowed: true, blockers: []});
  vi.mocked(api.builds).mockResolvedValue({ builds: [{ tag: "v1", status: "frozen", path: "p", counts: { records: 10, groups: 4, views: 30, duplicates: 0 }, tier_mix: { auto_checked: 8, human_reviewed: 2 }, versions: {}, blocked: {}, files: {} },
    { tag: "preview", status: "preview", path: "q", counts: { records: 5, groups: 2, views: 9, duplicates: 0 }, tier_mix: { auto_checked: 5 }, versions: {}, blocked: {}, files: {} }], next_version: "v2", next_versions: {} });
  vi.mocked(api.trainResolve).mockImplementation(async (o) => ({ ok: true, problems: [], changed: Object.entries(o).map(([key, value]) => ({ key, default: 0, value })), fingerprint: "f".repeat(64) }));
  vi.mocked(api.modelFit).mockResolvedValue(fit() as any);
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.localModels).mockResolvedValue({ hub_cache: "C:/hub", hub_cache_exists: true, models_dir: null, models_dir_exists: false, total_bytes: 0,
    models: [{ training_eligible: true, id: "Qwen/Qwen2.5-Coder-0.5B", source: "hub_cache", path: "C:/hub/q", bytes: GB, revisions: ["a"], main_revision: "a", safetensors: true, gguf: [], weights: true, config: true }] });
  vi.mocked(api.buildDetail).mockResolvedValue({ manifest: {security_readiness: {training_ready: true, blockers: []}}, stats: { splits: { groups: { train: 677, val: 34 } } } });
  vi.mocked(api.createTrainingRun).mockResolvedValue({name: 'run3', run_id: 'a'.repeat(32), request_id: 'request', job_id: 'j1'});
  vi.mocked(api.trainSchedule).mockImplementation(async (q) => {
    const steps = q.steps ?? Math.ceil((q.epochs ?? 0) * q.families / q.effective_batch);
    return { steps, family_passes: Math.round(steps * q.effective_batch / q.families * 1e4) / 1e4, sequences: steps * q.effective_batch, families: q.families, effective_batch: q.effective_batch,
      evaluation_steps: q.evaluate_every_steps ? [q.evaluate_every_steps] : [11, 22], last_evaluation_step: 22, steps_after_last_evaluation: steps - 22 };
  });
});

describe("helpers", () => {
  it("reads a contract value by dotted key and lists differences against it", () => {
    expect(contractValue(cfg, "lora.r")).toBe(32);
    expect(contractValue(cfg, "nothing")).toBeUndefined();
    expect(differences({ "lora.r": 64 }, { "lora.r": 64, "lora.lora_alpha": 128 }, cfg)).toEqual([{ key: "lora.lora_alpha", from: 64, to: 128 }]);
    expect(differences({}, { "lora.r": 32 }, cfg)).toEqual([]);
  });
});

it('keeps training slices in Settings and uses automatic naming for new runs', async () => {
  show();
  await screen.findByRole('list', {name: 'Method'});
  next();
  expect(await screen.findByLabelText('Dataset')).toBeInTheDocument();
  expect(screen.queryByLabelText('Train slice from')).not.toBeInTheDocument();
  next();
  expect(await screen.findByLabelText('Train slice from')).toHaveValue('0');
  expect(screen.getByLabelText('Train slice to')).toHaveValue('1');
  next();
  expect(await screen.findByLabelText('Run name')).toHaveValue('');
  expect(screen.getByLabelText('Run name')).toHaveAttribute('placeholder', 'Automatic (runN)');
});

describe("TrainWizard", () => {
  it('prepares a portable trial with a total budget and no desktop hardware probe', async () => {
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal('WebSocket', Quiet);
    render(<MemoryRouter><ConfirmProvider><TrainWizard initialMode="trial" onDone={() => undefined} /></ConfirmProvider></MemoryRouter>);
    await screen.findByRole('list', {name: 'Method'});
    next(); next();
    expect(await screen.findByLabelText('Total budget (minutes)')).toHaveValue('30');
    next();
    const button = await screen.findByRole('button', {name: 'Prepare trial'});
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledWith(expect.objectContaining({mode: 'trial', total_budget_minutes: 30})));
    expect(api.createTrainingRun).toHaveBeenCalledWith(expect.objectContaining({profile: 'portable_trial',
      revision: profiles.portable_trial.revision, tokenizer_revision: profiles.portable_trial.tokenizer_revision,
      overrides: expect.objectContaining(profiles.portable_trial.overrides)}));
    expect(api.modelFit).not.toHaveBeenCalled();
  });
  it('shows dataset admission blockers before preparation and keeps software smoke available', async () => {
    vi.mocked(api.buildDetail).mockResolvedValue({manifest: {security_readiness: {training_ready: false, blockers: ['missing_supervised_training_views']}}, stats: {}} as any);
    show();
    await screen.findByRole('list', {name: 'Method'});
    next();
    expect(await screen.findByText('Built, insufficient evidence for full training')).toBeInTheDocument();
    expect(screen.getByText(/missing supervised training views/i)).toBeInTheDocument();
    expect(screen.queryByText(/No human-reviewed rows/)).not.toBeInTheDocument();
    next(); next();
    expect(await screen.findByRole('button', {name: 'Prepare run'})).toBeDisabled();
    await choose(userEvent.setup(), screen.getByRole('combobox', {name: 'Purpose'}), 'smoke');
    expect(screen.getByRole('button', {name: 'Prepare run'})).toBeEnabled();
  });
  it("starts with the supported model and method without unsupported choices", async () => {
    show();
    expect(await screen.findAllByText("Qwen/Qwen2.5-Coder-7B")).toHaveLength(2);
    expect(screen.getByRole("list", { name: "Method" })).toHaveTextContent("QLoRA");
    expect(screen.queryByRole("list", { name: "Not implemented" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Review and prepare/ })).toBeDisabled();
  });

  it("offers the datasets with their status and warns about one that is not frozen", async () => {
    show();
    await screen.findByRole("list", { name: "Method" });
    next();
    const select = await screen.findByRole("combobox", { name: "Dataset" });
    const user = userEvent.setup();
    await user.click(select);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["dataset_v1 (frozen)", "dataset_preview (preview)"]);
    await user.keyboard("{Escape}");
    expect(screen.getByText(/dataset_preview is not frozen/)).toBeInTheDocument();
    await choose(user, select, "dataset_v1 (frozen)");
    expect(screen.queryByText(/is not frozen/)).toBeNull();
    expect(screen.getByText("10 records, 30 training examples.")).toBeInTheDocument();
  });

  it("offers a slice of the training families, says what it trains on, and sends it as an override only when it differs from the whole", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j3", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j3", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    await screen.findByRole("list", { name: "Method" });
    next();
    expect(screen.queryByLabelText('Train slice from')).not.toBeInTheDocument();
    next();
    expect(await screen.findByText(/Trains on every training family\./)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Train slice to"), { target: { value: "0.5" } });
    expect(screen.getByText(/from 0% to 50% of them, in a fixed order/)).toBeInTheDocument();
    expect(screen.getByText(/The validation split is never sliced/)).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Summary" })).toHaveTextContent("data.train_slice_end 0.5");
    fireEvent.change(screen.getByLabelText("Train slice to"), { target: { value: "1" } });
    expect(screen.getByRole("complementary", { name: "Summary" })).toHaveTextContent(/Settings that differ from the contract\s*none/);
    fireEvent.change(screen.getByLabelText("Train slice from"), { target: { value: "0.25" } });
    next();
    fireEvent.click(await screen.findByRole("button", { name: "Prepare run" }));
    await vi.waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledWith({ mode: "full", request_id: expect.any(String), tag: "preview", stage: 1, purpose: "train", overrides: { "data.train_slice_start": 0.25 }, profile: "custom" }));
  });

  it("chooses another base model in the first step, keeps it when a profile is picked, and sends it as a setting of the run", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j4", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j4", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Use Qwen/Qwen2.5-Coder-0.5B" }));
    expect(screen.getByTestId("chosen-model")).toHaveTextContent("Qwen/Qwen2.5-Coder-0.5B");
    expect(screen.getByRole("complementary", { name: "Summary" })).toHaveTextContent("model.id Qwen/Qwen2.5-Coder-0.5B");
    await waitFor(() => expect(api.trainResolve).toHaveBeenLastCalledWith({ "model.id": "Qwen/Qwen2.5-Coder-0.5B" }));
    next(); next();
    fireEvent.click(await screen.findByRole("radio", { name: /Higher quality/ }));
    await waitFor(() => expect(api.trainResolve).toHaveBeenLastCalledWith({ "lora.r": 64, "lora.lora_alpha": 128, "model.id": "Qwen/Qwen2.5-Coder-0.5B" }));
    next();
    expect(api.modelFit).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Prepare run" }));
    await vi.waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ overrides: { "lora.r": 64, "lora.lora_alpha": 128, "model.id": "Qwen/Qwen2.5-Coder-0.5B" }, profile: "custom" })));
  });

  it("going back to the contract's model removes the setting", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Use Qwen/Qwen2.5-Coder-0.5B" }));
    fireEvent.click(screen.getByRole("button", { name: "Use default model" }));
    expect(screen.getByTestId("chosen-model")).toHaveTextContent("Qwen/Qwen2.5-Coder-7B");
    expect(screen.getByRole("complementary", { name: "Summary" })).toHaveTextContent(/Settings that differ from the contract\s*none/);
  });

  it("offers DoRA, the optimizer and the schedule, and sends them as settings only when they differ from the contract's", async () => {
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    const summary = screen.getByRole("complementary", { name: "Summary" });
    const optimizer = await screen.findByRole("combobox", { name: "Optimizer" });
    const user = userEvent.setup();
    await user.click(optimizer);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["adamw_torch", "adafactor", "adamw_8bit"]);
    await user.click(screen.getByRole("option", { name: "adafactor" }));
    await choose(user, screen.getByRole("combobox", { name: "Learning-rate schedule" }), "linear");
    fireEvent.click(screen.getByLabelText("Use DoRA"));
    expect(summary).toHaveTextContent("training.optimizer adafactor");
    expect(summary).toHaveTextContent("training.scheduler linear");
    expect(summary).toHaveTextContent("lora.use_dora true");
    expect(screen.getByText(/The local model server cannot load such an adapter/)).toBeInTheDocument();
    await choose(user, optimizer, "adamw_torch");
    fireEvent.click(screen.getByLabelText("Use DoRA"));
    expect(summary).not.toHaveTextContent("training.optimizer");
    expect(summary).not.toHaveTextContent("lora.use_dora");
  });

  it("offers LoftQ, sends it only when on, and does not allow it together with DoRA", async () => {
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    const summary = screen.getByRole("complementary", { name: "Summary" });
    const loftq = await screen.findByLabelText("Start from the quantisation error (LoftQ)");
    expect(loftq).not.toBeChecked();
    fireEvent.click(loftq);
    expect(summary).toHaveTextContent("lora.init_loftq true");
    expect(screen.getByLabelText("Use DoRA")).toBeDisabled();
    fireEvent.click(loftq);
    expect(summary).not.toHaveTextContent("lora.init_loftq");
    fireEvent.click(screen.getByLabelText("Use DoRA"));
    expect(loftq).toBeDisabled();
  });

  it("offers packing, sends it only when on, and says what it does and does not do", async () => {
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    const summary = screen.getByRole("complementary", { name: "Summary" });
    const box = await screen.findByLabelText("Pack short examples into one row");
    expect(box).not.toBeChecked();
    expect(screen.getByText(/saves no time/)).toBeInTheDocument();
    fireEvent.click(box);
    expect(summary).toHaveTextContent("training.pack_sequences true");
    fireEvent.click(box);
    expect(summary).not.toHaveTextContent("training.pack_sequences");
  });

  it("shows the length of the run in steps from the training families, and what asking for steps or an evaluation interval changes", async () => {
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    const status = await screen.findByRole("status", { name: "Length of the run" });
    await waitFor(() => expect(status).toHaveTextContent("64 steps for 3 epochs"));
    expect(api.trainSchedule).toHaveBeenLastCalledWith(expect.objectContaining({ families: 677, effective_batch: 32, epochs: 3, evaluate_at: [0.5, 1, 2, 3] }));
    expect(status).toHaveTextContent("Evaluated at steps 11, 22.");
    expect(status).toHaveTextContent("The last 42 steps come after the last evaluation, so they can never be selected.");
    fireEvent.change(screen.getByLabelText("Epochs"), { target: { value: "1" } });
    await waitFor(() => expect(status).toHaveTextContent("22 steps for 1 epoch"));
    fireEvent.change(screen.getByLabelText("Evaluate every (steps)"), { target: { value: "5" } });
    await waitFor(() => expect(api.trainSchedule).toHaveBeenLastCalledWith(expect.objectContaining({ evaluate_every_steps: 5 })));
  });

  it("puts the warm-up conflict next to the value that causes it, in the contract's own words", async () => {
    vi.mocked(api.trainResolve).mockImplementation(async (o) => (o["training.warmup_steps"]
      ? { ok: false, problems: [{ key: "training.warmup_steps", reason: "set warmup_steps or warmup_ratio, not both (set warmup_ratio to 0 to warm up for a number of steps)" }], changed: [], fingerprint: null }
      : { ok: true, problems: [], changed: [], fingerprint: "f".repeat(64) }));
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    fireEvent.change(await screen.findByLabelText("Warm-up steps"), { target: { value: "20" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("not both");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Warm-up share"), { target: { value: "0" } });
  });

  it("stops after a number of steps when asked, and sends that as the run's steps", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j5", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j5", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next(); next();
    fireEvent.change(await screen.findByLabelText("Stop after (steps)"), { target: { value: "40" } });
    fireEvent.click(await screen.findByRole("button", { name: "Prepare run" }));
    await vi.waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledWith({ mode: "full", request_id: expect.any(String), tag: "preview", stage: 1, purpose: "train", steps: 40 }));
  });

  it("labels an imported dataset as external, says what that means, and removes it only after a question", async () => {
    const external = { tag: "ext_mine", status: "external", path: "p", counts: { records: 6, groups: 6, views: 24, duplicates: 0 }, tier_mix: {}, versions: {}, blocked: {}, files: {} };
    const builds = (extra: any[]) => ({ builds: [...extra, { tag: "v1", status: "frozen", path: "p", counts: { records: 10, groups: 4, views: 30, duplicates: 0 }, tier_mix: { auto_checked: 8, human_reviewed: 2 }, versions: {}, blocked: {}, files: {} }],
      next_version: "v2", next_versions: {} });
    vi.mocked(api.builds).mockResolvedValueOnce(builds([external])).mockResolvedValue(builds([]));
    vi.mocked(api.externalRemove).mockResolvedValue({ removed: "ext_mine", original_file: "mine.jsonl" });
    vi.mocked(api.buildDetail).mockRejectedValue(new ApiError({ status: 404, title: "Dataset no longer exists" }));
    show();
    await screen.findByRole("list", { name: "Method" });
    next();
    const select = await screen.findByRole("combobox", { name: "Dataset" });
    await userEvent.setup().click(select);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["dataset_ext_mine (external)", "dataset_v1 (frozen)"]);
    await userEvent.setup().keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Import a file" })).toBeInTheDocument();
    expect(screen.getByText(/is an external import\. Its origin and readiness checks/)).toBeInTheDocument();
    expect(screen.queryByText(/not human-reviewed|No human-reviewed rows/)).toBeNull();
    expect(screen.queryByText(/is not frozen/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove this import" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("no dataset the app built, frozen versions included, is ever removed here");
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.externalRemove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove this import" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove it" }));
    await waitFor(() => expect(api.externalRemove).toHaveBeenCalledWith("ext_mine"));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_v1 (frozen)"));
  });

  it("fills the values from a profile, gives the reason, and says changed by hand when a value is edited", async () => {
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    fireEvent.click(await screen.findByRole("radio", { name: /Higher quality/ }));
    expect(screen.getByLabelText("Adapter rank")).toHaveValue("64");
    expect(screen.getByLabelText("Adapter alpha")).toHaveValue("128");
    const summary = screen.getByRole("complementary", { name: "Summary" });
    expect(summary).toHaveTextContent("lora.r 64");
    fireEvent.change(screen.getByLabelText("Learning rate"), { target: { value: "0.00005" } });
    await waitFor(() => expect(summary).toHaveTextContent("changed by hand"));
    const list = screen.getByText(/Different from the Higher quality profile:/).closest("div") as HTMLElement;
    expect(within(list).getByText("training.learning_rate")).toBeInTheDocument();
    expect(list).toHaveTextContent(/training\.learning_rate: 0\.0001 to 0\.00005/);
  });

  it("checks the settings against the contract as they are typed and holds the person on the step while one is refused", async () => {
    vi.mocked(api.trainResolve).mockImplementation(async (o) => (o["lora.r"] === 0
      ? { ok: false, problems: [{ key: "lora.r", reason: "Input should be greater than or equal to 1" }], changed: [], fingerprint: null }
      : { ok: true, problems: [], changed: [], fingerprint: "f".repeat(64) }));
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    fireEvent.change(await screen.findByLabelText("Adapter rank"), { target: { value: "0" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Input should be greater than or equal to 1");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Adapter rank"), { target: { value: "16" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toBeEnabled());
  });

  it("puts a value back to the contract's own when it is typed back, so it is no longer an override", async () => {
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    fireEvent.change(await screen.findByLabelText("Adapter rank"), { target: { value: "16" } });
    fireEvent.change(screen.getByLabelText("Adapter rank"), { target: { value: "32" } });
    expect(screen.getByRole("complementary", { name: "Summary" })).toHaveTextContent(/Settings that differ from the contract\s*none/);
  });

  it("describes remote resource admission and prepares the run with the overrides and the profile", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next();
    fireEvent.click(await screen.findByRole("radio", { name: /Higher quality/ }));
    next();
    expect(await screen.findByText(/It measures accelerator memory, host RAM and disk space/)).toBeInTheDocument();
    expect(screen.getByText(/Run the notebook on Colab, Kaggle/)).toBeInTheDocument();
    expect(api.modelFit).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Run name"), { target: { value: "hq1" } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare run" }));
    await vi.waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledWith({ mode: "full", request_id: expect.any(String), name: "hq1", tag: "preview", stage: 1, purpose: "train", overrides: { "lora.r": 64, "lora.lora_alpha": 128 }, profile: "higher_quality" }));
  });

  it("sends no overrides at all when nothing differs from the contract", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j2", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j2", stage: "export_training_bundle", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next(); next();
    fireEvent.click(await screen.findByRole("button", { name: "Prepare run" }));
    await vi.waitFor(() => expect(api.createTrainingRun).toHaveBeenCalledWith({ mode: "full", request_id: expect.any(String), tag: "preview", stage: 1, purpose: "train" }));
  });

  it("prepares without contacting the desktop memory estimator", async () => {
    vi.mocked(api.modelFit).mockImplementation(() => Promise.reject(new Error("offline")));
    show();
    await screen.findByRole("list", { name: "Method" });
    next(); next(); next();
    expect(await screen.findByText(/Resources and throughput are measured there/)).toBeInTheDocument();
    expect(api.modelFit).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Prepare run" })).toBeEnabled();
  });
});

it("preserves an unsaved training form across unmounts", async () => {
  const first = show();
  await screen.findByRole("list", { name: "Method" }); next(); next();
  const rank = await screen.findByLabelText("Adapter rank");
  fireEvent.change(rank, { target: { value: "48" } });
  first.unmount(); show();
  expect(await screen.findByLabelText("Adapter rank")).toHaveValue("48");
  expect(screen.queryByRole("list", { name: "Method" })).not.toBeInTheDocument();
});

it("preserves a selected dataset that is not the first version on remount", async () => {
  const existing = await api.builds();
  vi.mocked(api.builds).mockResolvedValue({ ...existing, builds: [{ ...existing.builds[0], tag: "first-version" }, { ...existing.builds[0], tag: "saved-version" }] });
  const first = show();
  await screen.findByRole("list", { name: "Method" }); next();
  const user = userEvent.setup();
  await choose(user, await screen.findByRole("combobox", { name: "Dataset" }), "dataset_saved-version (frozen)");
  first.unmount(); show();
  expect(await screen.findByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_saved-version");
  await waitFor(() => expect(api.builds).toHaveBeenCalledTimes(3));
  expect(screen.getByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_saved-version");
});

async function saveDatasetBeforePageChange() {
  const initial = await api.builds();
  const row = initial.builds[0];
  vi.mocked(api.builds).mockResolvedValue({ ...initial, builds: [{ ...row, tag: "first-version" }, { ...row, tag: "saved-version" }] });
  const first = show();
  await screen.findByRole("list", { name: "Method" }); next();
  await choose(userEvent.setup(), await screen.findByRole("combobox", { name: "Dataset" }), "dataset_saved-version (frozen)");
  first.unmount();
  const page = { ...initial, total: 200, limit: 100, offset: 0, builds: Array.from({ length: 100 }, (_, i) => ({ ...row, tag: `new-${i}` })) };
  vi.mocked(api.builds).mockResolvedValue(page);
  return { row, page };
}
it.each(["catalog", "legacy"])("keeps an off-page saved dataset using its %s detail", async kind => {
  const { row } = await saveDatasetBeforePageChange();
  vi.mocked(api.buildDetail).mockResolvedValue(kind === 'catalog'
    ? { manifest: {}, stats: {}, build: { ...row, tag: "saved-version" } } as any
    : { manifest: { dataset_status: 'frozen', counts: row.counts }, stats: {} });
  show();
  await waitFor(() => expect(api.buildDetail).toHaveBeenCalledWith("saved-version"));
  expect(screen.getByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_saved-version (frozen)");
  expect(api.builds).toHaveBeenCalledTimes(3);
  const user = userEvent.setup(); await user.click(screen.getByRole("combobox", { name: "Dataset" }));
  expect(screen.getAllByRole("option")).toHaveLength(101);
});
it("falls back only when the saved dataset detail returns 404", async () => {
  await saveDatasetBeforePageChange();
  vi.mocked(api.buildDetail).mockRejectedValue(new ApiError({ status: 404, title: "Dataset removed" }));
  show();
  await waitFor(() => expect(api.buildDetail).toHaveBeenCalledWith("saved-version"));
  expect(await screen.findByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_new-0 (frozen)");
});
it("keeps the saved dataset and an actionable error on a detail request failure", async () => {
  await saveDatasetBeforePageChange();
  vi.mocked(api.buildDetail).mockRejectedValue(new Error("Temporary dataset service failure"));
  show();
  expect(await screen.findByRole("alert")).toHaveTextContent("Temporary dataset service failure");
  expect(screen.getByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_saved-version");
});
it("does not let an old missing-detail response overwrite a newer selection", async () => {
  await saveDatasetBeforePageChange();
  let reject!: (error: Error) => void;
  vi.mocked(api.buildDetail).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
  show();
  await waitFor(() => expect(api.buildDetail).toHaveBeenCalledWith("saved-version"));
  await choose(userEvent.setup(), screen.getByRole("combobox", { name: "Dataset" }), "dataset_new-1 (frozen)");
  await act(async () => reject(new ApiError({ status: 404, title: "Old dataset not found" })));
  expect(screen.getByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_new-1 (frozen)");
});

it("checks the selected off-page import again after removing it before falling back", async () => {
  const { row } = await saveDatasetBeforePageChange();
  vi.mocked(api.buildDetail).mockClear();
  vi.mocked(api.buildDetail).mockResolvedValue({ manifest: {}, stats: {}, build: { ...row, tag: "saved-version", status: "external" } });
  vi.mocked(api.externalRemove).mockResolvedValue({ removed: "saved-version", original_file: null });
  show();
  const remove = await screen.findByRole("button", { name: "Remove this import" });
  vi.mocked(api.buildDetail).mockRejectedValue(new ApiError({ status: 404, title: "Imported dataset removed" }));
  fireEvent.click(remove);
  fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove it" }));
  await waitFor(() => expect(api.externalRemove).toHaveBeenCalledWith("saved-version"));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Dataset" })).toHaveTextContent("dataset_new-0 (frozen)"));
  expect(vi.mocked(api.buildDetail).mock.calls.filter(([tag]) => tag === 'saved-version')).toHaveLength(3);
  expect(api.builds).toHaveBeenCalledTimes(4);
});
