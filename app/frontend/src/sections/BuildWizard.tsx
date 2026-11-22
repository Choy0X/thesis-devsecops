import { useEffect, useRef, useState } from "react";
import { Button, ErrorState, Field, Input, Panel, Skeleton } from "../components/ui";
import { Radio, SelectField } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { Stepper, type StepItem } from "../components/hud/Stepper";
import { api, type BuildInfo, type BuildList, type ReviewSummary } from "../lib/api";
import { num } from "../lib/format";
import { BuildCard, SplitCheckPanel, TIER_NOTE } from "./DatasetBuild";
import { Review } from "./Review";
import { LineagePanel, MixPanel } from "../components/charts/Panels";

// One guided path to a frozen dataset: choose what is built, set the filters and the split, preview the result with its checks, review pairs by hand
// if wanted, then freeze it as the next unused version. The settings are kept in this component, so going back and forth loses nothing.

const STEPS: StepItem[] = [
  { id: "task", label: "Task and source" },
  { id: "settings", label: "Filters and split" },
  { id: "preview", label: "Readiness and checks" },
  { id: "review", label: "Review", optional: true },
  { id: "freeze", label: "Freeze" },
];
const TIER_CHOICES = ["auto_checked", "llm_adjudicated", "human_reviewed"];

export type WizardSettings = { minTier: string; injection: string; temporalCutoff?: string };

export const injectionValid = (v: string) => v !== "" && Number.isFinite(Number(v)) && Number(v) >= 0 && Number(v) <= 1;

export const STEP_IDS = STEPS.map((s) => s.id);

/** `start` is the id of the step to open on (a deep link such as /build?step=review); the steps before it count as reached. */
export function BuildWizard({ onOther, start }: { onOther?: () => void; start?: string | null }) {
  const first = Math.max(0, STEP_IDS.indexOf(start ?? ""));
  const [step, setStep] = useState(first);
  const [reached, setReached] = useState(first);
  const [settings, setSettings] = useState<WizardSettings>({ minTier: "human_reviewed", injection: "0.1" });
  const [list, setList] = useState<BuildList | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const load = () => api.builds().then((b) => { setList(b); setErr(null); }).catch(setErr);
  useEffect(() => { load(); }, []);                                           // eslint-disable-line react-hooks/exhaustive-deps

  const preview = list?.builds.find((b) => b.status !== "frozen") ?? null;
  const canNext = step === 1 ? injectionValid(settings.injection) : true;
  const go = (i: number) => { setStep(i); setReached((r) => Math.max(r, i)); };

  if (!list) return err != null ? <ErrorState error={err} title="Could not read the datasets" onRetry={load} /> : <Skeleton className="h-48 w-full" />;
  return (
    <div className="space-y-5">
      <Stepper steps={STEPS} current={step} reached={reached} onSelect={setStep} />
      <div aria-live="polite">
        {step === 0 && <TaskStep onOther={onOther} />}
        {step === 1 && <SettingsStep value={settings} onChange={setSettings} />}
        {step === 2 && <PreviewStep settings={settings} preview={preview} onBuilt={load} />}
        {step === 3 && <ReviewStep />}
        {step === 4 && <FreezeStep settings={settings} list={list} preview={preview} onFrozen={load} />}
      </div>
      <div className="flex items-center justify-between border-t border-line pt-4">
        <Button variant="ghost" disabled={step === 0} onClick={() => go(step - 1)}>Back</Button>
        {step < STEPS.length - 1 && (
          <Button variant="primary" disabled={!canNext} onClick={() => go(step + 1)}>
            {step === 3 ? "Continue to freeze" : "Next"}
          </Button>
        )}
      </div>
    </div>
  );
}

function TaskStep({ onOther }: { onOther?: () => void }) {
  const [sum, setSum] = useState<ReviewSummary | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => { api.reviewSummary().then(setSum).catch(() => setFailed(true)); }, []);
  return (
    <Panel title="What to build" desc="The reviewed dataset is the one models are trained and checked on.">
      <div role="radiogroup" aria-label="Dataset to build" className="space-y-2">
        <label className="flex cursor-pointer gap-3 border border-brand bg-brand-tint p-3.5 text-[0.82rem]">
          <Radio name="task" checked readOnly aria-label="Reviewed dataset" className="mt-1" />
          <span>
            <span className="block font-medium">Reviewed dataset</span>
            <span className="mt-0.5 block text-muted">Every unique code pair is a candidate record. The records are grouped, split into training and validation by a stable hash, and cut into
              the views of each task (finding the vulnerable place, with and without injected text, and proposing a patch). A manifest states how much of it was checked.</span>
          </span>
        </label>
      </div>
      <div className="mt-4 text-[0.82rem]">
        <div className="mb-1 font-medium">Source: the working store</div>
        {sum ? (
          <p className="text-muted"><b className="tnum font-medium text-fg">{num(sum.candidates)}</b> pairs can be candidates, <b className="tnum font-medium text-fg">{num(sum.auto_checked)}</b> of
            them passed the automatic checks, and <b className="tnum font-medium text-fg">{num(sum.decisions)}</b> have a decision by a person.</p>
        ) : failed ? <p className="text-muted">The counts could not be read. The build still reads the same store.</p> : <Skeleton className="h-5 w-72" />}
      </div>
      {onOther && <p className="mt-4 text-[0.78rem] text-faint">Looking for the code-pair or the severity dataset?{" "}
        <button type="button" onClick={onOther} className="text-brand-strong hover:underline">Open Other datasets</button>.</p>}
    </Panel>
  );
}

function SettingsStep({ value, onChange }: { value: WizardSettings; onChange: (v: WizardSettings) => void }) {
  const ok = injectionValid(value.injection);
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel title="Filters" desc="Which candidates may supervise a model.">
        <SelectField label="Lowest review tier that is included" aria-label="Lowest review tier" value={value.minTier} onValueChange={(v) => onChange({ ...value, minTier: v })}
          options={TIER_CHOICES.map((t) => ({ value: t, label: t.replace(/_/g, " ") }))} />
        <p className="mt-1.5 text-[0.76rem] text-muted">{TIER_NOTE[value.minTier]}. Tiers below this one are left out of the views.</p>
        <div className="mt-4 w-52">
          <Field label="Share with an injection variant" hint="A number from 0 to 1: the share of discovery views that also get a variant with injected text.">
            <Input inputMode="decimal" aria-label="Share with an injection variant" aria-invalid={!ok} value={value.injection}
              onChange={(e) => onChange({ ...value, injection: e.target.value.replace(/[^0-9.]/g, "") })} />
          </Field>
          {!ok && <p role="alert" className="mt-1 text-[0.76rem] text-crit-soft">Enter a number from 0 to 1.</p>}
        </div>
      </Panel>
      <Panel title="Split" desc="Fixed by the project, so a result can be repeated.">
        <Field label="Temporal evaluation cutoff (optional)"><Input type="date" value={value.temporalCutoff || ''} onChange={event => onChange({...value, temporalCutoff: event.target.value})} /></Field>
        <ul className="space-y-1.5 text-[0.82rem] text-muted">
          <li>Strict publishing separates training, selection, calibration and an untouched test partition.</li>
          <li>A group is the unit of the split: all pairs of one CVE stay on the same side, chosen by a stable hash, never by random rows.</li>
          <li>Pairs that look like each other across the split are found by the leakage check in the next steps.</li>
        </ul>
      </Panel>
    </div>
  );
}

function PreviewStep({ preview }: { settings: WizardSettings; preview: BuildInfo | null; onBuilt: () => void }) {
  return (
    <div className="space-y-5">
      <Panel title="Security readiness" desc="Review candidate labels and source evidence before publishing an immutable dataset.">
        <p className="text-sm text-muted">Automatic checks and generated context remain provisional. Publishing records readiness blockers; a full training run requires strict admission and passing correctness diagnostics.</p>
      </Panel>
      {preview && <details><summary className="cursor-pointer text-sm text-muted">Previous working artifact</summary><BuildCard b={preview} /></details>}
      {preview && <MixPanel dataset={preview.tag} />}
      <LineagePanel />
      <SplitCheckPanel />
    </div>
  );
}

function ReviewStep() {
  return (
    <div className="space-y-4">
      <p className="text-[0.82rem] text-muted">Optional. Pairs that passed the automatic checks can be reviewed by hand here. Each decision is saved at once and counts the next time a preview is
        built, so build the preview again afterwards if you review anything.</p>
      <Review />
    </div>
  );
}

function FreezeStep({ settings, list, preview, onFrozen }: { settings: WizardSettings; list: BuildList; preview: BuildInfo | null; onFrozen: () => void }) {
  const version = useRef(list.next_version).current;                       // the version this freeze will take, as it was when the step opened
  const [froze, setFroze] = useState(false);
  const made = froze ? list.builds.find((b) => b.tag === version && b.status === "frozen") ?? null : null;
  return (
    <div className="space-y-5">
      <Panel title={`Freeze as dataset_${version}`} desc="A freeze takes the next unused version, records the hashes of its files and saves the split manifest. Earlier versions are left alone.">
        <dl className="mb-4 grid grid-cols-[12rem_1fr] gap-x-4 gap-y-1.5 text-[0.82rem]">
          <dt className="text-muted">Lowest review tier</dt><dd>{settings.minTier.replace(/_/g, " ")}</dd>
          <dt className="text-muted">Share with an injection variant</dt><dd className="tnum">{settings.injection}</dd>
          <dt className="text-muted">Earlier working artifact</dt><dd>{preview ? `${num(preview.counts.records)} records in ${num(preview.counts.groups)} groups` : "none required; publishes directly from the working store"}</dd>
        </dl>
        <RunConsole stage="build_reviewed_dataset" label="Build and freeze" onDone={() => { setFroze(true); onFrozen(); }}
          params={{ freeze: true, injection_rate: Number(settings.injection) || 0, min_tier: settings.minTier,
                    ...(settings.temporalCutoff ? {temporal_cutoff: settings.temporalCutoff} : {}) }}
          confirmText={`Freeze a new version (dataset_${version})? Freezing records the hashes of its files, and the version cannot be changed afterwards.`}
          hint="Writes the next unused version and leaves earlier ones alone." />
      </Panel>
      {made && (
        <div role="status" aria-label="Frozen dataset">
          <p className="mb-2 text-[0.82rem] font-medium">Frozen as dataset_{made.tag}.</p>
          <BuildCard b={made} />
        </div>
      )}
    </div>
  );
}
