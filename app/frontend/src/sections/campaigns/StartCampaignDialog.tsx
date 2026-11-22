import { useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "../../components/hud/Badge";
import { Dialog } from "../../components/hud/Dialog";
import { Stepper, type StepItem } from "../../components/hud/Stepper";
import { Button, EmptyState, ErrorState, Field, Input, Skeleton } from "../../components/ui";
import { Checkbox, Radio } from "../../components/kit";
import { useToast } from "../../components/Toast";
import { api, type CampaignOptions, type CampaignProblem, type CampaignResources, type CampaignVerdict } from "../../lib/api";
import { ApiError, errorText, type ApiReason } from "../../lib/apiError";
import { refreshCampaigns } from "../../lib/campaignsStore";
import { bytes } from "../../lib/format";
import { ResourceTable } from "./ResourceTable";
import { draftProblem, initialDraft, presetOf, requestOf, STAGE_ABOUT, stagesOfPreset, type Draft } from "./startRequest";

// Starting a campaign in three steps: which ecosystems, which stages and with what settings, and a check of the whole plan against the data and the computer
// before anything starts. The plan that was checked is the plan that starts. A refusal says what is short, with the numbers.

const STEPS: StepItem[] = [{ id: "ecosystems", label: "Ecosystems" }, { id: "stages", label: "Stages and settings" }, { id: "check", label: "Check and start" }];

export function StartCampaignDialog({ open, onClose, onStarted, initialEcosystems = [] }: { open: boolean; onClose: () => void; onStarted: (id: string) => void; initialEcosystems?: string[] }) {
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }} title="Start a campaign" description="It collects, mines and checks the advisories of the ecosystems you choose, and you can pause it whenever you like."
      className="w-[min(54rem,96vw)]">
      {open && <Body onClose={onClose} onStarted={onStarted} initialEcosystems={initialEcosystems} />}
    </Dialog>
  );
}

function Body({ onClose, onStarted, initialEcosystems }: { onClose: () => void; onStarted: (id: string) => void; initialEcosystems: string[] }) {
  const toast = useToast();
  const [options, setOptions] = useState<CampaignOptions | null>(null);
  const [optionsError, setOptionsError] = useState<unknown>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [step, setStep] = useState(0);
  const [reached, setReached] = useState(0);
  const [verdict, setVerdict] = useState<CampaignVerdict | null>(null);
  const [resources, setResources] = useState<CampaignResources | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [starting, setStarting] = useState(false);
  const [refused, setRefused] = useState<ApiReason[]>([]);
  const seq = useRef(0);

  const loadOptions = () => api.campaignOptions().then((o) => {
    setOptions(o); setOptionsError(null);
    setDraft((d) => d ?? initialDraft(o, initialEcosystems.filter((e) => o.targets.some((t) => t.name === e && !t.owner))));
  }).catch(setOptionsError);
  useEffect(() => { loadOptions(); }, []);                                             // eslint-disable-line react-hooks/exhaustive-deps

  const request = useMemo(() => (options && draft ? requestOf(draft, options) : null), [options, draft]);
  // The whole plan is judged when the last step is on screen and again whenever the choices change.
  useEffect(() => {
    if (step !== 2 || !request) return;
    const mine = ++seq.current;
    setChecking(true);
    const timer = window.setTimeout(() => {
      Promise.all([api.campaignValidate(request), api.campaignResources().catch(() => null)])
        .then(([v, r]) => { if (mine === seq.current) { setVerdict(v); setResources(r); setCheckError(null); setRefused([]); } })
        .catch((e) => { if (mine === seq.current) setCheckError(e); })
        .finally(() => { if (mine === seq.current) setChecking(false); });
    }, 150);
    return () => window.clearTimeout(timer);
  }, [step, request]);

  if (!options || !draft) return optionsError != null ? <ErrorState error={optionsError} title="Could not read what a campaign can use" onRetry={loadOptions} /> : <Skeleton className="h-64 w-full" />;
  const problem = draftProblem(draft);
  const stagesOn = Object.values(draft.stages).some(Boolean);
  const set = (patch: Partial<Draft>) => { setDraft({ ...draft, ...patch }); setVerdict(null); };
  const go = (i: number) => { setStep(i); setReached((r) => Math.max(r, i)); };

  const start = async () => {
    if (!request) return;
    setStarting(true);
    try {
      const made = await api.campaignCreate(request);
      toast.success(`Campaign ${made.campaign.campaign_id} started.`);
      await refreshCampaigns();
      onStarted(made.campaign.campaign_id);
    } catch (e) {
      if (e instanceof ApiError && e.reasons.length) setRefused(e.reasons);
      else toast.error(`Could not start the campaign: ${errorText(e)}`, e instanceof ApiError ? e.technical : undefined);
    } finally { setStarting(false); }
  };

  return (
    <div className="space-y-5">
      <Stepper steps={STEPS} current={step} reached={reached} onSelect={setStep} />
      <div aria-live="polite">
        {step === 0 && <EcosystemsStep options={options} draft={draft} set={set} />}
        {step === 1 && <StagesStep options={options} draft={draft} set={set} />}
        {step === 2 && <CheckStep verdict={verdict} resources={resources} checking={checking} error={checkError} refused={refused} onRetry={() => setDraft({ ...draft })} />}
      </div>
      <div className="flex items-center justify-between gap-3 border-t border-line pt-4">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <div className="flex items-center gap-3">
          {step > 0 && <Button variant="subtle" onClick={() => go(step - 1)}>Back</Button>}
          {step < 2 && <Button variant="primary" disabled={step === 0 ? !draft.ecosystems.length : !stagesOn} onClick={() => go(step + 1)}
            title={(step === 0 ? !draft.ecosystems.length : !stagesOn) ? problem ?? undefined : undefined}>Next</Button>}
          {step === 2 && <Button variant="primary" loading={starting} disabled={!verdict?.ok || checking || !!problem} onClick={start}>Start the campaign</Button>}
        </div>
      </div>
    </div>
  );
}

// -- step 1 ------------------------------------------------------------------------------------------------------------------

function EcosystemsStep({ options, draft, set }: { options: CampaignOptions; draft: Draft; set: (p: Partial<Draft>) => void }) {
  const free = options.targets.filter((t) => !t.owner).map((t) => t.name);
  const toggle = (name: string, on: boolean) => set({ ecosystems: on ? [...draft.ecosystems, name] : draft.ecosystems.filter((e) => e !== name) });
  if (!options.targets.length) {
    return <EmptyState title="There is no dump on this computer yet" hint="Download the dump of an ecosystem on the CVE Collections page first. A campaign works on the dumps that are here." />;
  }
  return (
    <div className="space-y-4">
      <fieldset>
        <legend className="mb-2 text-[0.84rem] font-medium">Which ecosystems? <span className="font-normal text-muted">Every dump on this computer is listed.</span></legend>
        <div className="mb-2 flex gap-2">
          <Button variant="ghost" className="h-7 px-2 text-[0.78rem]" onClick={() => set({ ecosystems: free })}>Choose all</Button>
          <Button variant="ghost" className="h-7 px-2 text-[0.78rem]" onClick={() => set({ ecosystems: [] })}>Clear</Button>
        </div>
        <ul className="divide-y divide-line border border-line">
          {options.targets.map((t) => (
            <li key={t.name} className="px-3 py-2">
              <Checkbox checked={draft.ecosystems.includes(t.name)} disabled={!!t.owner} onCheckedChange={(v) => toggle(t.name, v)}
                label={<span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="font-medium">{t.name}</span>
                  <span className="tnum text-muted">{bytes(t.dump_bytes)}</span>
                  {!t.mineable && <Badge>reads only</Badge>}
                  {t.owner && <Badge>held by {t.owner}</Badge>}
                  {t.stopped && <Badge>{t.stopped} was cut short</Badge>}
                </span>} />
              {!t.mineable && <p className="ml-7 mt-0.5 text-[0.76rem] text-muted">{t.reason}</p>}
              {t.owner && <p className="ml-7 mt-0.5 text-[0.76rem] text-muted">Another campaign is collecting this one. Pause it and let it finish, or delete it, to use this ecosystem.</p>}
            </li>
          ))}
        </ul>
      </fieldset>
      <div className="max-w-sm"><Field label="Name" hint="Optional. The campaign always has an id of its own."><Input value={draft.name} maxLength={60} onChange={(e) => set({ name: e.target.value })} /></Field></div>
    </div>
  );
}

// -- step 2 ------------------------------------------------------------------------------------------------------------------

function StagesStep({ options, draft, set }: { options: CampaignOptions; draft: Draft; set: (p: Partial<Draft>) => void }) {
  const preset = presetOf(options, draft.stages);
  const on = (s: string) => !!draft.stages[s];
  return (
    <div className="space-y-5">
      <fieldset>
        <legend className="mb-2 text-[0.84rem] font-medium">Start from <span className="font-normal text-muted">A starting point; every stage can still be switched.</span></legend>
        <div className="flex flex-wrap gap-2">
          {options.presets.map((p) => (
            <Radio key={p.id} name="preset" checked={preset === p.id} onChange={() => set({ stages: stagesOfPreset(options, p.id) })} label={p.name}
              className={`items-center border px-3 py-1.5 ${preset === p.id ? "border-brand bg-brand-tint" : "border-line hover:border-line-strong"}`} />
          ))}
          {preset === null && <Radio checked readOnly label="Custom" className="items-center border border-line-strong px-3 py-1.5" />}
        </div>
      </fieldset>

      <fieldset className="space-y-2 border border-line p-3">
        <legend className="px-1 text-[0.84rem] font-medium">Campaign computing</legend>
        <div className="flex flex-wrap gap-4">{(['cpu', 'cuda'] as const).map(mode => <Radio key={mode} name="compute" checked={(draft.computeMode || 'cpu') === mode}
          onChange={() => set({computeMode: mode})} label={{cpu: 'CPU', cuda: 'CUDA'}[mode]} />)}</div>
        <p className="text-sm text-muted">CUDA warms once and reserves the GPU for the whole campaign, including network waits. This blocks local GPU chat and generation. Downloads, Git, parsing and database writes still use the CPU and network.</p>
        <p className="text-sm text-muted">Model-written context is a separate manual step after collection. Select items there for GPU generation or a ChatGPT JSON exchange.</p>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="mb-2 text-[0.84rem] font-medium">Mining pipeline</legend>
        <div className="flex flex-wrap gap-4">
          <Radio name="mining-pipeline" checked={draft.miningVersion !== 4} onChange={() => set({miningVersion: 2})} label="Compatible" />
          <Radio name="mining-pipeline" checked={draft.miningVersion === 4} onChange={() => set({miningVersion: 4, pauseSeconds: "0"})} label="Adaptive (experimental)" />
        </div>
        <p className="text-sm text-muted">Adaptive mining shares snapshots and adjusts parallel work to available RAM, capped at 3 GB for the campaign and its child processes. Memory pressure waits without stopping the job. The 30-minute target is not yet validated.</p>
      </fieldset>
      <fieldset>
        <legend className="mb-2 text-[0.84rem] font-medium">Stages <span className="font-normal text-muted">They run in this order for each ecosystem.</span></legend>
        <ul className="divide-y divide-line border border-line">
          {options.stages.map((s) => (
            <li key={s.id} className="px-3 py-2">
              <Checkbox checked={on(s.id)} onCheckedChange={(v) => set({ stages: { ...draft.stages, [s.id]: v } })}
                label={<span><span className="font-medium">{s.label}</span>{s.scope === "campaign" && <span className="ml-2 text-[0.72rem] text-faint">once, at the end</span>}</span>}
                description={STAGE_ABOUT[s.id]} />
            </li>
          ))}
        </ul>
      </fieldset>

      {(on("mine") || on("recover") || on("dep_versions") || on("ingest")) && (
        <div className="grid gap-4 sm:grid-cols-3">
          {on("mine") && <Field label="Commits mined at once" hint={`1 to ${options.knob_limits.mine_workers?.[1] ?? 16}.`}><Input inputMode="numeric" value={draft.mineWorkers} onChange={(e) => set({ mineWorkers: e.target.value.replace(/\D/g, "") })} /></Field>}
          {on("mine") && <Field label="Most commits per ecosystem" hint="Empty: until none are left. A larger number continues later."><Input inputMode="numeric" placeholder="no cap" value={draft.quota} onChange={(e) => set({ quota: e.target.value.replace(/\D/g, "") })} /></Field>}
          {on("mine") && <Field label="Pause between batches (seconds)"><Input inputMode="numeric" value={draft.pauseSeconds} onChange={(e) => set({ pauseSeconds: e.target.value.replace(/\D/g, "") })} /></Field>}
          {on("recover") && <Field label="Most records to recover" hint="Empty: all that have no fix commit."><Input inputMode="numeric" placeholder="all" value={draft.recoverLimit} onChange={(e) => set({ recoverLimit: e.target.value.replace(/\D/g, "") })} /></Field>}
          {on("recover") && <Field label="GitHub requests at once" hint={`1 to ${options.knob_limits.recover_workers?.[1] ?? 32}.`}><Input inputMode="numeric" value={draft.recoverWorkers} onChange={(e) => set({ recoverWorkers: e.target.value.replace(/\D/g, "") })} /></Field>}
          {on("dep_versions") && <Field label="Registries asked at once"><Input inputMode="numeric" value={draft.depsWorkers} onChange={(e) => set({ depsWorkers: e.target.value.replace(/\D/g, "") })} /></Field>}
          {on("ingest") && <Checkbox className="self-end pb-2" checked={draft.refresh} onCheckedChange={(v) => set({ refresh: v })} label="Ask OSV for a newer dump first" description="Downloads only if OSV has a newer copy." />}
        </div>
      )}

      {on("enrich") && (
        <fieldset className="space-y-3 border border-line p-3">
          <legend className="px-1 text-[0.84rem] font-medium">Model-written context <span className="font-normal text-muted">Uses the model chosen in Settings.</span></legend>
          <div className="flex flex-wrap gap-2">
            {options.context_profiles.map((p) => (
              <Radio key={p.id} name="context" title={p.description} checked={draft.contextProfile === p.id} onChange={() => set({ contextProfile: p.id })} label={p.name}
                className={`items-center border px-3 py-1.5 ${draft.contextProfile === p.id ? "border-brand bg-brand-tint" : "border-line hover:border-line-strong"}`} />
            ))}
          </div>
          <p className="text-[0.78rem] text-muted">{options.context_profiles.find((p) => p.id === draft.contextProfile)?.description}</p>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Requests at once" hint={`${options.context_workers[0]} to ${options.context_workers[1]}.`}><Input inputMode="numeric" value={draft.contextWorkers} onChange={(e) => set({ contextWorkers: e.target.value.replace(/\D/g, "") })} /></Field>
            <Field label="Most pairs per ecosystem" hint="Empty: every pair that matches."><Input inputMode="numeric" placeholder="all" value={draft.contextCap} onChange={(e) => set({ contextCap: e.target.value.replace(/\D/g, "") })} /></Field>
          </div>
        </fieldset>
      )}

      {on("build") && (
        <fieldset className="space-y-2 border border-line p-3">
          <legend className="px-1 text-[0.84rem] font-medium">Draft dataset <span className="font-normal text-muted">Which profile it is built with.</span></legend>
          {options.build_profiles.map((p) => (
            <Radio key={p.id} name="build" checked={draft.buildProfile === p.id} onChange={() => set({ buildProfile: p.id })} label={p.name} description={p.description} />
          ))}
        </fieldset>
      )}
    </div>
  );
}

// -- step 3 ------------------------------------------------------------------------------------------------------------------

function Problems({ items, tone }: { items: CampaignProblem[] | ApiReason[]; tone: "error" | "warning" }) {
  if (!items.length) return null;
  return (
    <ul className="space-y-2" aria-label={tone === "error" ? "Problems" : "Notes"}>
      {items.map((p, i) => (
        <li key={`${p.code}-${i}`} className={`border-l-2 pl-3 text-[0.82rem] ${tone === "error" ? "border-crit-line" : "border-warn-line"}`}>
          <span className="mr-2 text-[0.7rem] font-semibold uppercase tracking-wider text-faint">{tone === "error" ? "Cannot start" : "Note"}</span>{p.text}
        </li>
      ))}
    </ul>
  );
}

function CheckStep({ verdict, resources, checking, error, refused, onRetry }: { verdict: CampaignVerdict | null; resources: CampaignResources | null; checking: boolean; error: unknown; refused: ApiReason[]; onRetry: () => void }) {
  if (error != null) return <ErrorState error={error} title="Could not check the plan" onRetry={onRetry} />;
  if (!verdict) return <div role="status" aria-label="Checking the plan"><Skeleton className="h-40 w-full" /></div>;
  return (
    <div className="space-y-4" aria-busy={checking}>
      <p className={`text-[0.88rem] font-medium ${verdict.ok ? "text-ok" : "text-crit-soft"}`}>{verdict.ok ? "This plan can start now." : "This plan cannot start yet."}{checking ? " Checking again..." : ""}</p>
      <Problems items={verdict.errors} tone="error" />
      {refused.length > 0 && <Problems items={refused} tone="error" />}
      <Problems items={verdict.warnings} tone="warning" />
      <div>
        <h3 className="mb-1 text-[0.84rem] font-medium">What runs for each ecosystem</h3>
        <ul className="divide-y divide-line border border-line text-[0.8rem]">
          {Object.entries(verdict.per_ecosystem).map(([name, e]) => (
            <li key={name} className="px-3 py-2"><span className="font-medium">{name}</span><span className="ml-3 text-muted">{e.stages.length ? e.stages.join(", ") : "nothing"}</span>
              {e.notes.map((n) => <p key={n} className="mt-0.5 text-[0.76rem] text-muted">{n}</p>)}</li>
          ))}
        </ul>
      </div>
      <div>
        <h3 className="mb-1 text-[0.84rem] font-medium">What it needs of this computer</h3>
        <ResourceTable needs={verdict.needs} free={verdict.free} running={resources?.campaigns.map((c) => c.campaign_id)} />
      </div>
    </div>
  );
}
