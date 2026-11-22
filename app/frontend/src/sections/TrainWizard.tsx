import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Field, Input, Panel, Skeleton } from "../components/ui";
import { Checkbox, SelectField, Hint } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { Stepper, type StepItem } from "../components/hud/Stepper";
import { api, errorText, type BuildList, type ProfileBuild, type RunProfile, type ScheduleInfo, type SecurityReadiness, type TrainingAssessment, type TrainConfig, type TrainResolve, type TrialCapability } from "../lib/api";
import { TrainingReadiness, fullTrainingBlocked } from '../components/TrainingReadiness';
import { ModelPicker } from "../components/ModelPicker";
import { ImportDataset } from "../components/ImportDataset";
import { useConfirm } from "../components/hud/AlertDialog";
import { num } from "../lib/format";
import { useWorkspaceState } from "../lib/workspaceState";

// Preparing a run in four steps: what is trained and how (fixed in this phase), which dataset, which settings (a profile, then any value changed by hand, checked
// against the contract as it is typed), and a review with the memory estimate and what will be written. The settings go to the job as overrides of the contract;
// the run keeps them and its hash covers them. Nothing is trained here: the result is a bundle and a notebook, or a run for the local GPU check.

const STEPS: StepItem[] = [
  { id: "model", label: "Model and method" },
  { id: "data", label: "Dataset" },
  { id: "settings", label: "Settings" },
  { id: "review", label: "Review and prepare" },
];

type Knob = { key: string; label: string; hint: string; kind: "int" | "float" };
export const KNOBS: Knob[] = [
  { key: "lora.r", label: "Adapter rank", hint: "How much the adapter can learn. Larger costs adapter memory.", kind: "int" },
  { key: "lora.lora_alpha", label: "Adapter alpha", hint: "The adapter's scale; the contract keeps it at twice the rank.", kind: "int" },
  { key: "lora.lora_dropout", label: "Adapter dropout", hint: "From 0 up to, not including, 1.", kind: "float" },
  { key: "training.learning_rate", label: "Learning rate", hint: "The step size; larger than 0.", kind: "float" },
  { key: "training.weight_decay", label: "Weight decay", hint: "0 or more.", kind: "float" },
  { key: "training.warmup_ratio", label: "Warm-up share", hint: "The share of steps spent warming up; from 0 up to, not including, 1.", kind: "float" },
  { key: "training.warmup_steps", label: "Warm-up steps", hint: "Instead of a share: warm up for this many steps. Set the warm-up share to 0 to use it.", kind: "int" },
  { key: "training.maximum_pilot_family_passes", label: "Epochs", hint: "How long to train, in family passes: the sequences drawn divided by the number of training families.", kind: "float" },
  { key: "training.evaluate_every_steps", label: "Evaluate every (steps)", hint: "Instead of at the family passes of the contract: evaluate every this many steps.", kind: "int" },
  { key: "training.max_grad_norm", label: "Gradient clip", hint: "Larger than 0.", kind: "float" },
];
/** Settings with a few named choices; the choices come from the contract's own description. */
export const CHOICES: { key: string; label: string; hint: string }[] = [
  { key: "training.optimizer", label: "Optimizer", hint: "adamw_8bit needs bitsandbytes on the machine that trains; adafactor keeps less state per weight." },
  { key: "training.scheduler", label: "Learning-rate schedule", hint: "constant has no warm-up." },
];
/** Which part of the training families to train on: a fixed order by hash, contiguous, the validation split never sliced. */
export const SLICE_KNOBS: Knob[] = [
  { key: "data.train_slice_start", label: "Train slice from", hint: "The share of the training families to start at, from 0 up to, not including, 1.", kind: "float" },
  { key: "data.train_slice_end", label: "Train slice to", hint: "The share to stop at, above the start and at most 1.", kind: "float" },
];

/** A value of the contract by dotted key, from the settings list. */
export function contractValue(cfg: TrainConfig | null, key: string): unknown {
  for (const s of cfg?.sections ?? []) for (const f of s.fields) if (f.key === key) return f.value;
  return undefined;
}

/** The choices the contract offers for a setting, by dotted key. */
export function choicesOf(cfg: TrainConfig | null, key: string): string[] {
  for (const s of cfg?.sections ?? []) for (const f of s.fields) if (f.key === key) return (f.choices ?? []).map(String);
  return [];
}

/** The settings that differ between two sets of overrides, over the contract's defaults, as rows with both values. */
export function differences(a: Record<string, unknown>, b: Record<string, unknown>, cfg: TrainConfig | null): { key: string; from: unknown; to: unknown }[] {
  const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
  return keys.map((key) => ({ key, from: key in a ? a[key] : contractValue(cfg, key), to: key in b ? b[key] : contractValue(cfg, key) }))
    .filter((r) => JSON.stringify(r.from) !== JSON.stringify(r.to));
}

export const show = (v: unknown): string => (Array.isArray(v) ? v.join(", ") : v === undefined || v === null ? "not set" : String(v));

export function TrainWizard({ onDone, initialMode = 'full' }: { onDone: () => void; initialMode?: 'trial' | 'full' }) {
  const confirm = useConfirm();
  const [step, setStep] = useWorkspaceState("train.step", 0);
  const [reached, setReached] = useWorkspaceState("train.reached", 0);
  const [cfg, setCfg] = useState<TrainConfig | null>(null);
  const [profiles, setProfiles] = useState<Record<string, RunProfile> | null>(null);
  const [builds, setBuilds] = useState<BuildList | null>(null);
  const [profile, setProfile] = useWorkspaceState("train.profile", "contract");
  const [overrides, setOverrides] = useWorkspaceState<Record<string, unknown>>("train.overrides", {});
  const [typed, setTyped] = useWorkspaceState<Record<string, string>>("train.typed", {});
  const [tag, setTag] = useWorkspaceState("train.tag", "preview");
  const [run, setRun] = useWorkspaceState("train.newRunName", "");
  const [creation, setCreation] = useWorkspaceState("train.creation", {fingerprint: '', requestId: ''});
  const [stage, setStage] = useWorkspaceState("train.stage", "1");
  const [purpose, setPurpose] = useWorkspaceState("train.purpose", "train");
  const [mode, setMode] = useWorkspaceState<'trial' | 'full'>('train.mode', initialMode);
  const [budget, setBudget] = useWorkspaceState('train.totalBudget', '30');
  const [trialCapability, setTrialCapability] = useState<TrialCapability | null>(null);
  const [minutes, setMinutes] = useWorkspaceState("train.minutes", "");
  const [lossMode, setLossMode] = useWorkspaceState("train.lossMode", "");
  const [modelId, setModelId] = useWorkspaceState("train.modelId", "");                       // "" = the contract's base model
  const [stopAfter, setStopAfter] = useWorkspaceState("train.stopAfter", "");
  const [families, setFamilies] = useState<number | null>(null);
  const [schedule, setSchedule] = useState<ScheduleInfo | null>(null);
  const [check, setCheck] = useState<TrainResolve | null>(null);
  const [failed, setFailed] = useState("");
  const [datasetError, setDatasetError] = useState("");
  const [datasetReadiness, setDatasetReadiness] = useState<SecurityReadiness | null>(null);
  const [datasetAssessment, setDatasetAssessment] = useState<TrainingAssessment | null>(null);
  const selectedTag = useRef(tag);
  selectedTag.current = tag;
  const selectionRevision = useRef(0);
  const catalogRevision = useRef(0);
  const mounted = useRef(true);
  const selectDataset = (next: string) => { selectedTag.current = next; selectionRevision.current += 1; setDatasetError(""); setTag(next); };
  const reloadBuilds = async (choose?: string) => {
    if (choose) selectDataset(choose);
    const request = ++catalogRevision.current;
    try {
      const page = await api.builds();
      if (!mounted.current || request !== catalogRevision.current) return;
      setBuilds(page);
      const wanted = selectedTag.current;
      const selection = selectionRevision.current;
      const current = () => mounted.current && request === catalogRevision.current && selection === selectionRevision.current && selectedTag.current === wanted;
      setDatasetError("");
      if (page.builds.some(build => build.tag === wanted)) return;
      try {
        const detail = await api.buildDetail(wanted);
        if (!current()) return;
        const row = detail.build;
        const manifest = detail.manifest ?? {};
        const extra: ProfileBuild = { ...row, tag: wanted, status: row?.status ?? manifest.dataset_status ?? manifest.status ?? "unknown", path: row?.path ?? "", counts: row?.counts ?? manifest.counts ?? {}, tier_mix: row?.tier_mix ?? manifest.tier_mix ?? {}, versions: row?.versions ?? manifest.versions ?? {}, blocked: row?.blocked ?? {}, files: row?.files ?? {} };
        setBuilds(previous => current() ? { ...(previous ?? page), builds: [...(previous ?? page).builds.filter(build => build.tag !== wanted), extra] } : previous);
      } catch (error) {
        if (!current()) return;
        if (error != null && typeof error === "object" && "status" in error && error.status === 404) selectDataset(page.builds[0]?.tag ?? "preview");
        else setDatasetError(`Could not check dataset_${wanted}: ${errorText(error)}`);
      }
    } catch (error) {
      if (mounted.current && request === catalogRevision.current) setDatasetError(`Could not read dataset versions: ${errorText(error)}`);
    }
  };

  useEffect(() => {
    mounted.current = true;
    api.trainConfig().then(setCfg).catch((e) => setFailed(errorText(e)));
    api.trainProfiles().then((r) => setProfiles(r.profiles)).catch((e) => setFailed(errorText(e)));
    void reloadBuilds();
    return () => { mounted.current = false; catalogRevision.current += 1; };
  }, []);
  const removeImport = async (t: string, file: string | undefined) => {
    const ok = await confirm({ title: `Remove dataset_${t}?`, description: `The imported dataset${file ? ` made from ${file}` : ""} is deleted from this computer. Your original file is not touched, and no dataset the app built, frozen versions included, is ever removed here. Runs that were prepared from it keep their own copy in their bundle.`,
      confirmLabel: "Remove it", cancelLabel: "Keep it", tone: "critical" });
    if (!ok) return;
    try { await api.externalRemove(t); await reloadBuilds(); } catch (e) { setFailed(errorText(e)); }
  };

  // The model chosen in the first step is one more setting of the run; a profile changes the others but never the model.
  const contractModel = String(contractValue(cfg, "model.id") ?? "");
  const trialProfile = profiles?.portable_trial;
  const defaultModel = mode === 'trial' ? String(trialProfile?.overrides['model.id'] ?? '') : contractModel;
  const eff = useMemo(() => {
    const selected = mode === 'trial' ? {...trialProfile?.overrides, ...overrides} : overrides;
    return modelId ? {...selected, 'model.id': modelId} : selected;
  }, [overrides, modelId, mode, trialProfile]);
  const dora = Boolean(eff["lora.use_dora"] ?? contractValue(cfg, "lora.use_dora"));
  const loftq = Boolean(eff["lora.init_loftq"] ?? contractValue(cfg, "lora.init_loftq"));
  const pack = Boolean(eff["training.pack_sequences"] ?? contractValue(cfg, "training.pack_sequences"));

  // The settings are checked against the contract after each change (a short wait while typing), so a refusal appears next to the value that caused it.
  const latest = useRef(0);
  useEffect(() => {
    const id = ++latest.current;
    const t = window.setTimeout(() => { api.trainResolve(eff).then((r) => { if (id === latest.current) setCheck(r); }).catch(() => undefined); }, 250);   // ignored: a failed check leaves the last answer
    return () => window.clearTimeout(t);
  }, [eff]);

  // The number of training families of the chosen dataset, read once per dataset, for the length of the run in steps.
  useEffect(() => {
    if (step < 1) return;
    let live = true;
    setFamilies(null);
    setDatasetReadiness(null);
    setDatasetAssessment(null);
    api.buildDetail(tag).then((d) => { if (live) {
      setFamilies(Number(d.stats?.splits?.groups?.train) || null);
      setDatasetReadiness(d.manifest?.security_readiness ?? d.build?.readiness ?? null);
      setDatasetAssessment(d.full_training ?? d.build?.full_training ?? null);
    } }).catch(() => undefined);   // ignored: backend preflight remains authoritative if detail is unavailable
    return () => { live = false; };
  }, [step >= 1, tag]);                                                // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (mode !== 'trial') return;
    let current = true;
    setTrialCapability(null);
    api.trialCapability(tag).then(value => {if (current) setTrialCapability(value);})
      .catch(error => {if (current) setTrialCapability({allowed: false, blockers: [errorText(error)]});});
    return () => {current = false;};
  }, [mode, tag]);
  const effectiveBatch = Number(contractValue(cfg, "training.effective_sequence_batch_size") ?? 32);
  const epochs = Number(eff["training.maximum_pilot_family_passes"] ?? contractValue(cfg, "training.maximum_pilot_family_passes") ?? 3);
  const every = Number(eff["training.evaluate_every_steps"] ?? 0) || undefined;
  const stopSteps = Number(stopAfter) || undefined;
  const evaluateAt = (contractValue(cfg, "training.evaluate_at_family_passes") as number[] | undefined) ?? [];
  useEffect(() => {
    if (!families || step < 2) { setSchedule(null); return; }
    let live = true;
    api.trainSchedule({ families, effective_batch: effectiveBatch, ...(stopSteps ? { steps: stopSteps } : { epochs }), evaluate_every_steps: every, evaluate_at: evaluateAt })
      .then((s) => { if (live) setSchedule(s); }).catch(() => { if (live) setSchedule(null); });
    return () => { live = false; };
  }, [families, effectiveBatch, epochs, every, stopSteps, step]);       // eslint-disable-line react-hooks/exhaustive-deps

  const base = String(eff['model.id'] ?? defaultModel);
  const defaultTrialModel = mode === 'trial' && base === trialProfile?.overrides['model.id'];
  const trialCustom = mode === 'trial' && differences(trialProfile?.overrides ?? {}, eff, cfg).length > 0;

  const choose = (name: string) => { setProfile(name); setOverrides({ ...(profiles?.[name]?.overrides ?? {}) }); setTyped({}); };
  const setKnob = (k: Knob, text: string) => {
    setTyped((t) => ({ ...t, [k.key]: text }));
    const n = Number(text);
    setOverrides((o) => {
      const next = { ...o };
      if (text.trim() === "" || !Number.isFinite(n) || n === contractValue(cfg, k.key)) delete next[k.key]; else next[k.key] = k.kind === "int" ? Math.trunc(n) : n;
      return next;
    });
  };
  const fromProfile = profiles?.[profile]?.overrides ?? {};
  const custom = differences(fromProfile, overrides, cfg).length > 0;
  const profileDiff = useMemo(() => differences(fromProfile, overrides, cfg), [fromProfile, overrides, cfg]);
  const fromContract = useMemo(() => differences({}, eff, cfg), [eff, cfg]);
  const chosenBuild = builds?.builds.find((b) => b.tag === tag);
  const readiness = datasetReadiness ?? chosenBuild?.readiness;
  const assessment = datasetAssessment ?? chosenBuild?.full_training;
  const datasetBlocked = fullTrainingBlocked(readiness, assessment) || chosenBuild?.state === 'blocked' || chosenBuild?.counts.views === 0;
  const readinessNotice = mode === 'trial' ? <p role="status" className="text-[0.8rem] text-muted">{trialCapability === null ? 'Checking portable trial support...' : trialCapability.allowed ? 'Source learning trial available. A fresh adapter uses a bounded, separately held out source sample. Results remain experimental.' : `Portable trial unavailable: ${trialCapability.blockers.join('; ')}`}</p> : <TrainingReadiness readiness={readiness} assessment={assessment} purpose={purpose} />;
  const settingsOk = check === null || check.ok;
  const canNext = step === 2 ? settingsOk && (mode !== 'trial' || Number(budget) > 0) : true;
  const go = (i: number) => { setStep(i); setReached((r) => Math.max(r, i)); };
  const setChoice = (key: string, value: string) => setOverrides((o) => { const next = { ...o }; if (value === String(contractValue(cfg, key))) delete next[key]; else next[key] = value; return next; });
  const setFlag = (key: string, on: boolean) => setOverrides((o) => { const next = { ...o }; if (on === Boolean(contractValue(cfg, key))) delete next[key]; else next[key] = on; return next; });
  const knobField = (k: Knob) => {
    const def = contractValue(cfg, k.key);
    const shown = typed[k.key] ?? String(overrides[k.key] ?? def ?? "");
    const problem = check && !check.ok ? (check.problems ?? []).find((p) => p.key === k.key) : undefined;
    return (
      <Field key={k.key} label={k.label} hint={`${k.hint} The contract uses ${show(def)}.`}>
        <Input inputMode="decimal" aria-label={k.label} aria-invalid={!!problem} value={shown} onChange={(e) => setKnob(k, e.target.value)} />
        {problem && <p role="alert" className="mt-1 text-[0.76rem] text-crit-soft">{problem.reason}</p>}
      </Field>);
  };
  const sliceFrom = Number(eff["data.train_slice_start"] ?? contractValue(cfg, "data.train_slice_start") ?? 0);
  const sliceTo = Number(eff["data.train_slice_end"] ?? contractValue(cfg, "data.train_slice_end") ?? 1);

  if (failed) return <Panel title="Prepare a run"><p role="alert" className="text-[0.82rem] text-crit-soft">The setup could not be read: {failed}</p></Panel>;
  if (!cfg || !profiles) return <Skeleton className="h-48 w-full" />;

  return (
    <Panel title="Prepare a run" desc="Choose a model, dataset and settings, then prepare a pinned training bundle.">
      <SelectField label="Run mode" aria-label="Run mode" value={mode} onValueChange={value => setMode(value as 'trial' | 'full')}
        options={[{value: 'trial', label: 'Portable trial (30 minutes by default)'}, {value: 'full', label: 'Full training'}]} />
      <Stepper steps={STEPS} current={step} reached={reached} onSelect={setStep} />
      <div className="mt-4 grid gap-5 lg:grid-cols-[1fr_16rem]">
        <div aria-live="polite" className="min-w-0 space-y-4">
          {step === 0 && (
            <div className="space-y-3 text-[0.82rem]">
              <ModelPicker value={base} contractDefault={defaultModel} onChange={(id) => setModelId(id === defaultModel ? "" : id)} />
              <ul className="flex flex-wrap gap-1.5" aria-label="Method">
                {["QLoRA: the model held in 4 bits", "LoRA adapters on every attention and feed-forward projection", "Completion-only loss", "One adapter, the base weights stay untouched"].map((c) => (
                  <li key={c} className="border border-line bg-surface-2 px-2 py-0.5 text-[0.74rem] text-muted">{c}</li>))}
              </ul>
            </div>
          )}

          {step === 1 && (
            <div className="space-y-3">
              <SelectField label="Dataset" aria-label="Dataset" value={tag} onValueChange={selectDataset}
                options={[...(builds?.builds ?? []).map((b) => ({ value: b.tag, label: `dataset_${b.tag} (${b.status})` })), ...(!builds?.builds.some(b => b.tag === tag) ? [{ value: tag, label: `dataset_${tag}` }] : [])]} />
              {datasetError && <p role="alert" className="text-[0.8rem] text-crit-soft">{datasetError}</p>}
              {readinessNotice}
              <div className="flex flex-wrap items-center gap-2"><ImportDataset onImported={(t) => { void reloadBuilds(t); }} /></div>
              {chosenBuild && <p className="text-[0.8rem] text-muted">{num(chosenBuild.counts.records)} records, {num(chosenBuild.counts.views)} training examples.</p>}
              {chosenBuild && chosenBuild.status === "external" && (
                <div className="space-y-1 border border-line bg-surface-2 px-3 py-2 text-[0.78rem]">
                  <p>dataset_{chosenBuild.tag} is an external import. Its origin and readiness checks are recorded separately from datasets built by this app.</p>
                  <Button variant="ghost" onClick={() => void removeImport(chosenBuild.tag, undefined)}>Remove this import</Button>
                </div>)}
              {chosenBuild && chosenBuild.status !== "frozen" && chosenBuild.status !== "external" && <p className="text-[0.78rem] text-warn">dataset_{chosenBuild.tag} is not frozen: the bundle and the run record say so. Freeze a version on Build dataset for a result you intend to cite.</p>}
            </div>
          )}

          {step === 2 && mode === 'trial' && <div className="space-y-3 text-[0.82rem]">
            <Field label="Total budget (minutes)" hint="Covers setup, resource checks, baseline, training, comparison and output export. A short budget may produce partial evidence.">
              <Input aria-label="Total budget (minutes)" inputMode="decimal" value={budget} onChange={event => setBudget(event.target.value.replace(/[^0-9.]/g, ''))} />
            </Field>
            <p>The runtime selects a safe micro-batch from measured accelerator memory. No GPU model needs to be chosen here.</p>
            <p>Targets up to 256 training views, 24 held-out views and 64 training families with seed 17. Actual coverage is recorded in the bundle.</p>
          </div>}
          {step === 2 && mode === 'full' && (
            <div className="space-y-4">
              <div role="radiogroup" aria-label="Profile" className="grid gap-2 sm:grid-cols-2">
                {Object.entries(profiles).filter(([name]) => name !== 'portable_trial').map(([name, p]) => (
                  <button key={name} type="button" role="radio" aria-checked={profile === name && !custom} onClick={() => choose(name)}
                    className={`border p-3 text-left text-[0.8rem] ${profile === name ? "border-brand bg-brand-tint" : "border-line hover:border-line-strong"}`}>
                    <span className="block font-medium">{p.label}{profile === name && custom ? " (changed by hand)" : ""}</span>
                    <span className="mt-0.5 block text-muted">{p.purpose}</span>
                  </button>))}
              </div>

              {contractValue(cfg, "data.train_slice_start") !== undefined && <div className="space-y-2">
                <div className="grid gap-3 sm:grid-cols-2">{SLICE_KNOBS.map(knobField)}</div>
                <p className="text-[0.78rem] text-muted">
                  {sliceFrom === 0 && sliceTo === 1 ? "Trains on every training family."
                    : `Trains on the training families from ${Math.round(sliceFrom * 100)}% to ${Math.round(sliceTo * 100)}% of them, in a fixed order that does not change with the file or a seed. Slices that meet cover every family once and share none.`}
                  {" "}The validation split is never sliced.
                </p>
              </div>}
              <div className="grid gap-3 sm:grid-cols-2">
                {KNOBS.map(knobField)}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {CHOICES.map((c) => (
                  <SelectField key={c.key} label={c.label} aria-label={c.label} hint={`${c.hint} The contract uses ${show(contractValue(cfg, c.key))}.`}
                    value={String(eff[c.key] ?? contractValue(cfg, c.key) ?? "")} onValueChange={(v) => setChoice(c.key, v)} options={choicesOf(cfg, c.key).map((o) => ({ value: String(o), label: String(o) }))} />))}
              </div>
              <Checkbox checked={dora} disabled={loftq} onCheckedChange={(v) => setFlag("lora.use_dora", v)} label="Use DoRA"
                description="Weight-decomposed adapters. It adds a trained scale to every adapter projection. The local model server cannot load such an adapter, so it could not be served from this app. Not together with LoftQ." />
              <Checkbox checked={loftq} disabled={dora} onCheckedChange={(v) => setFlag("lora.init_loftq", v)} label="Start from the quantisation error (LoftQ)"
                description="Before the first step it sets the adapter so that the 4-bit weights plus the adapter are closer to the full-precision model. On Qwen2.5-Coder-0.5B that closed about a seventh of the loss gap to full precision and took about 20 seconds; a larger model takes longer. It reads the model's own files on this computer, and whether it makes the trained adapter better has not been measured. Not together with DoRA." />
              <Checkbox checked={pack} onCheckedChange={(v) => setFlag("training.pack_sequences", v)} label="Pack short examples into one row"
                description="Examples that fit together are put one after another in a row, and nothing attends across them: each keeps its own positions, its masked prompt and its supervised end, and the loss and the update are those of separate rows (tested on the processor). With the micro-batch of 1 this project uses it saves no time, so it only matters if you change that. Not for the decision stage." />
              <div role="status" aria-label="Length of the run" className="border border-line bg-surface-2 px-4 py-3 text-[0.8rem]">
                {schedule ? (
                  <>
                    <p><b className="font-medium">{schedule.steps} steps</b> {stopSteps ? "(stopped there, as asked)" : `for ${epochs} epoch${epochs === 1 ? "" : "s"}`}. That is {schedule.family_passes} epochs of {schedule.families} training families, {schedule.effective_batch} sequences a step, in whole steps.
                      {schedule.evaluation_steps.length ? ` Evaluated at step${schedule.evaluation_steps.length === 1 ? "" : "s"} ${schedule.evaluation_steps.join(", ")}.` : " Never evaluated."}</p>
                    {schedule.steps_after_last_evaluation > 0 && <p className="mt-1 text-warn">The last {schedule.steps_after_last_evaluation} steps come after the last evaluation, so they can never be selected.</p>}
                  </>) : (
                  <p className="text-muted">An epoch is a family pass: the sequences drawn divided by the number of training families, so steps = epochs x families / {effectiveBatch}. {families === null ? "The number of families is shown once the dataset is read." : ""}</p>)}
                <p className="mt-1 text-[0.74rem] text-faint">An epoch here is not one visit to every example: the sampler draws by task share and family, so a rare example is seen more than once and a common one may be skipped within an epoch.</p>
              </div>
              {check && !check.ok && (check.problems ?? []).filter((p) => !KNOBS.some((k) => k.key === p.key) && !SLICE_KNOBS.some((k) => k.key === p.key)).map((p) => <p key={p.key} role="alert" className="text-[0.78rem] text-crit-soft">{p.key}: {p.reason}</p>)}
              {custom && profileDiff.length > 0 && (
                <div className="text-[0.78rem]">
                  <p className="mb-1 font-medium">Different from the {profiles[profile]?.label ?? "profile"} profile:</p>
                  <ul className="space-y-0.5 text-muted">{profileDiff.map((r) => <li key={r.key}><span className="font-mono text-[0.74rem]">{r.key}</span>: {show(r.from)} to {show(r.to)}</li>)}</ul>
                </div>
              )}
            </div>
          )}

          {step === 3 && (
            <div className="space-y-4">
              {readinessNotice}
              <div className="grid gap-3 sm:grid-cols-3">
                <Field label="Run name" hint="Leave empty to allocate the next unused run number."><Input aria-label="Run name" placeholder="Automatic (runN)" value={run} onChange={(e) => setRun(e.target.value.replace(/[^A-Za-z0-9_.-]/g, ""))} /></Field>
                {mode === 'full' && <><SelectField label="Stage" aria-label="Stage" hint="1: language-model loss. 2: adds the decision head and the pair loss." value={stage} onValueChange={setStage}
                  options={[{ value: "1", label: "1" }, { value: "2", label: "2" }]} />
                <SelectField label="Purpose" aria-label="Purpose" hint="smoke: a short run to check memory and speed." value={purpose} onValueChange={setPurpose}
                  options={[{ value: "train", label: "train" }, { value: "smoke", label: "smoke" }]} />
                <SelectField label="Loss" aria-label="Loss" hint="Stage 1 control: the ordinary loss (B2) or field-balanced targets (B3)." value={lossMode} onValueChange={setLossMode}
                  options={[{ value: "", label: "ordinary (B2)" }, { value: "present_field_normalized_ce", label: "field-balanced (B3)" }]} />
                <Field label="Session minutes" hint="Stop with a checkpoint before this many minutes of a paid runtime are used.">
                  <Input aria-label="Session minutes" inputMode="numeric" value={minutes} onChange={(e) => setMinutes(e.target.value.replace(/[^0-9]/g, ""))} />
                </Field>
                <Field label="Stop after (steps)" hint="Optional: train for this many steps instead of the epochs above.">
                  <Input aria-label="Stop after (steps)" inputMode="numeric" value={stopAfter} onChange={(e) => setStopAfter(e.target.value.replace(/[^0-9]/g, ""))} />
                </Field></>}
              </div>
              <div role="status" className="border border-line bg-surface-2 px-4 py-3 text-[0.82rem]">
                <p>Run the notebook on Colab, Kaggle or another remote runtime. It measures accelerator memory, host RAM and disk space before loading the pinned model.</p>
                <p className="mt-1">Resources and throughput are measured there. Preparation creates a portable bundle on this computer.</p>
                {mode === 'trial' && <p className="mt-1">Total budget: {budget} minutes. Baseline and tuned outputs use the same held-out examples. This is source learning evidence, with no security evaluation.</p>}
              </div>
              <RunConsole stage="export_training_bundle" label={mode === 'trial' ? 'Prepare trial' : 'Prepare run'} oneShot onDone={() => {setCreation({fingerprint: '', requestId: ''}); onDone();}} disabled={!settingsOk || (mode === 'trial' ? !trialCapability?.allowed || !(Number(budget) > 0) : purpose === 'train' && datasetBlocked)}
                params={{ ...(run ? {name: run} : {}), tag, mode, ...(mode === 'trial' ? {stage: 1, purpose: 'train', total_budget_minutes: Number(budget), profile: trialCustom ? 'custom' : 'portable_trial',
                  ...(defaultTrialModel ? {revision: trialProfile?.revision, tokenizer_revision: trialProfile?.tokenizer_revision} : {}), overrides: eff} : {stage: Number(stage), purpose, ...(minutes ? { session_minutes: Number(minutes) } : {}), ...(lossMode ? { loss_mode: lossMode } : {}),
                  ...(stopSteps ? { steps: stopSteps } : {}), ...(fromContract.length ? { overrides: eff, profile: custom || eff !== overrides ? "custom" : profile } : {}) }) }}
                launch={async params => {
                  const fingerprint = JSON.stringify(params);
                  const requestId = creation.fingerprint === fingerprint && creation.requestId ? creation.requestId : crypto.randomUUID();
                  setCreation({fingerprint, requestId});
                  const created = await api.createTrainingRun({...params, request_id: requestId});
                  return {id: created.job_id};
                }}
                match={job => !!creation.requestId && job.params?.creation_request_id === creation.requestId}
                hint="Writes the bundle and the notebook; nothing is trained." />
            </div>
          )}

          <div className="flex items-center justify-between border-t border-line pt-3">
            <Button variant="ghost" disabled={step === 0} onClick={() => go(step - 1)}>Back</Button>
            {step < STEPS.length - 1 && <Button variant="primary" disabled={!canNext} onClick={() => go(step + 1)}>Next</Button>}
          </div>
        </div>

        <aside aria-label="Summary" className="h-fit border border-line bg-surface-2 p-3 text-[0.78rem]">
          <p className="mb-2 text-[0.7rem] font-semibold  text-faint">Your run so far</p>
          <dl className="space-y-1.5">
            <div><dt className="text-faint">Base model</dt><dd className="break-all font-mono text-[0.72rem]">{base}</dd></div>
            <div><dt className="text-faint">Dataset</dt><dd>dataset_{tag}{chosenBuild ? ` (${chosenBuild.status})` : ""}</dd></div>
            <div><dt className="text-faint">Execution</dt><dd>{mode === 'trial' ? `Experimental trial, ${budget} minutes` : 'Full training'} in a remote runtime</dd></div>
            <div><dt className="text-faint">Profile</dt><dd>{mode === 'trial' ? trialProfile?.label : profiles[profile]?.label}{(mode === 'trial' ? trialCustom : custom) ? ", changed by hand" : ""}</dd></div>
            <div><dt className="text-faint">Settings that differ from the contract</dt>
              <dd>{fromContract.length === 0 ? "none" : <ul className="space-y-0.5">{fromContract.map((r) => <li key={r.key}><span className="font-mono text-[0.7rem]">{r.key}</span> {show(r.to)}</li>)}</ul>}</dd></div>
            {check?.fingerprint && <div><dt className="text-faint">Settings fingerprint</dt><dd className="font-mono text-[0.68rem]">{check.fingerprint.slice(0, 12)}</dd></div>}
          </dl>
        </aside>
      </div>
    </Panel>
  );
}
