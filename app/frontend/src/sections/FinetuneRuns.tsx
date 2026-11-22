import { TrainWizard } from "./TrainWizard";
import { TrainingReadiness } from '../components/TrainingReadiness';
import { FullModelExport } from './FullModelExport';
import { LocalTrain } from "./LocalTrain";
import { TrialReport } from './TrialReport';
import { RunCurvesView } from "../components/RunCurvesView";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, ErrorState, Field, Input, Panel, Skeleton } from "../components/ui";
import { Checkbox, SelectField } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { GuidedImport } from "../components/GuidedImport";
import { Icon } from "../components/Icon";
import {
  api, type FinetuneRun, type FinetuneRunDetail,
} from "../lib/api";
import { num } from "../lib/format";
import { useOnActivate } from "../components/ActivePage";
import { refreshJobs } from "../lib/jobsList";
import { useConfirm } from "../components/hud/AlertDialog";
import { useWorkspaceState } from "../lib/workspaceState";
import { EvalRuns } from "./EvalRuns";
import type { EvalShared } from "../lib/api";

// The state of a run is read from evidence on disk (a verified bundle, a CPU check of that bundle, imported GPU outputs, an
// evaluation run). An exported notebook that has not been run is only "configured": it never shows as trained.
const LADDER = ["configured", "cpu_validated", "gpu_smoke_tested", "trained", "evaluated"];
const LADDER_NOTE: Record<string, string> = {
  configured: "a verified bundle exists for the resolved run",
  cpu_validated: "the bundle ran two steps on a tiny random model in a fresh process (a software check, not a result)",
  gpu_smoke_tested: "a short GPU run of this bundle was imported",
  trained: "a finished GPU run of this bundle was imported",
  evaluated: "an evaluation run of the trained adapter has metrics",
};

const pill = (tone: "ok" | "warn" | "muted" | "bad") =>
  `rounded-full border px-2 py-0.5 text-[0.7rem] font-normal ${
    tone === "ok" ? "border-ok-line text-ok" : tone === "warn" ? "border-warn-line text-warn"
      : tone === "bad" ? "border-crit-line text-crit" : "border-line text-muted"}`;

const short = (s?: string | null, n = 12) => (s ? s.slice(0, n) : "—");
const mb = (b: number) => `${(b / 1e6).toFixed(1)} MB`;

export function FinetuneRuns() {
  const [runs, setRuns] = useState<FinetuneRun[] | null>(null);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useWorkspaceState("runs.search", "");
  const [state, setState] = useWorkspaceState("runs.state", "");
  const [page, setPage] = useWorkspaceState("runs.page", 0);
  const [selected, setSelected] = useWorkspaceState<string[]>("runs.selection", []);
  const [all, setAll] = useWorkspaceState("runs.all", false);
  const [active, setActive] = useWorkspaceState("runs.active", "");
  const [prepare, setPrepare] = useWorkspaceState("runs.prepare", false);
  const [deletion, setDeletion] = useWorkspaceState("runs.deletion", "");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const confirm = useConfirm();
  const sequence = useRef(0);
  const load = () => { const id = ++sequence.current; return api.finetuneRuns({ limit: 100, offset: page * 100, search: q, state }).then(r => { if (id !== sequence.current) return; setRuns(r.runs); setTotal(r.total ?? r.runs.length); setErr(""); }).catch(e => { if (id === sequence.current) setErr(String(e?.message || e)); }); };
  useEffect(() => { load(); }, [q, state, page]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!deletion) return;
    const timer = window.setInterval(() => { api.getJob(deletion).then(job => {
      if (["done", "error", "failed", "cancelled", "interrupted"].includes(job.status)) { setDeletion(""); if (job.error) setErr(job.error); else { setSelected([]); setAll(false); setActive(""); } load(); }
    }).catch(e => { setErr(String(e?.message || e)); setDeletion(""); }); }, 1500);
    return () => window.clearInterval(timer);
  }, [deletion]); // eslint-disable-line react-hooks/exhaustive-deps
  const filters = (nextQ: string, nextState: string) => { setQ(nextQ); setState(nextState); setPage(0); setSelected([]); setAll(false); setRuns(null); };
  const remove = async () => {
    setBusy(true); setErr("");
    try {
      const preview = await api.previewRunDeletion(all ? { select_all: true, search: q, state } : { names: selected });
      const blocked = preview.blocked;
      const description = <div className="space-y-2"><p>{preview.total} runs, {(preview.bytes / 1e6).toFixed(1)} MB.</p><p>Selected runs: {preview.names.slice(0, 10).join(", ")}{preview.names.length > 10 ? ` and ${preview.names.length - 10} more` : ""}.</p><p>Run bundles, notebooks, imports and run artifacts are removed.</p><details><summary>Artifacts and dependencies</summary><pre className="max-h-48 overflow-auto whitespace-pre-wrap text-[0.75rem]">{JSON.stringify({ names: preview.names, artifacts: preview.artifacts, dependencies: preview.dependencies, blocked: preview.blocked }, null, 2)}</pre></details>{blocked && <p>Some runs cannot be deleted. Resolve the listed dependencies first.</p>}</div>;
      if (blocked) { setErr(`Deletion blocked. Resolve these dependencies first: ${JSON.stringify(preview.dependencies)}`); return; }
      if (!await confirm({ title: `Delete ${preview.total} runs?`, description, confirmLabel: "Delete runs", tone: "critical" })) return;
      const result = await api.deleteRuns(preview.preview_token);
      setDeletion(result.job_id); refreshJobs();
    } catch (e) { setErr(String((e as Error)?.message || e)); } finally { setBusy(false); }
  };
  const activeRun = runs?.find(r => r.name === active);
  const pageSelected = !!runs?.length && runs.every(r => selected.includes(r.name));
  return <div className="space-y-5">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-lg font-semibold">Training runs</h2><p className="mt-1 text-[0.8rem] text-muted">Prepare a QLoRA trial, import results, or inspect evaluation evidence.</p></div>
      <Button variant="primary" aria-expanded={prepare} onClick={() => setPrepare(!prepare)}>{prepare ? "Close setup" : "Prepare portable trial"}</Button>
    </div>
    {prepare && <TrainWizard initialMode="trial" onDone={() => { load(); setPrepare(false); }} />}
    <div className="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_13rem_auto]">
      <Field label="Search runs"><Input value={q} onChange={e => filters(e.target.value, state)} placeholder="Name, model or dataset" /></Field>
      <SelectField label="State" value={state} onValueChange={value => filters(q, value)} options={[{ value: "", label: "All states" }, ...LADDER.map(value => ({ value, label: value.replace(/_/g, " ") }))]} />
      <Button variant="ghost" disabled={(!all && !selected.length) || busy || !!deletion} loading={busy} onClick={() => void remove()}>Delete selected{all ? ` (${total})` : selected.length ? ` (${selected.length})` : ""}</Button>
    </div>
    {deletion && <p role="status" className="text-[0.82rem]">Deleting runs. Progress is available in Jobs.</p>}
    {err && <p role="alert" className="text-[0.82rem] text-crit-soft">{err}</p>}
    {!runs && !err && <Skeleton className="h-32" />}
    {runs && <>
      <div className="flex flex-wrap gap-4 text-[0.8rem]"><Checkbox label="Select this page" checked={pageSelected && !all} disabled={all} onChange={e => setSelected(current => e.target.checked ? [...new Set([...current, ...runs.map(r => r.name)])] : current.filter(name => !runs.some(r => r.name === name)))} /><Button variant="ghost" disabled={!total} aria-pressed={all} onClick={() => { setAll(!all); setSelected([]); }}>{all ? "Clear selection" : `Select all ${total} matching runs`}</Button></div>
      <div className="overflow-x-auto"><table className="w-full text-left text-[0.82rem]" aria-label="Training runs"><thead className="text-muted"><tr><th className="w-10"><span className="sr-only">Select</span></th><th className="py-3">Run</th><th>Model</th><th>Dataset</th><th>State</th></tr></thead><tbody>{runs.map(r => <tr key={r.name} className={`border-t border-line ${active === r.name ? "bg-brand-tint" : ""}`}><td><Checkbox aria-label={`Select ${r.name}`} checked={all || selected.includes(r.name)} disabled={all} onChange={e => setSelected(current => e.target.checked ? [...current, r.name] : current.filter(name => name !== r.name))} /></td><td className="py-3"><button className="text-brand-strong hover:underline" onClick={() => setActive(active === r.name ? "" : r.name)}>{r.name}</button></td><td>{r.model?.split("/").pop() || "-"}</td><td>dataset_{r.dataset.tag}</td><td>{r.state.replace(/_/g, " ")}</td></tr>)}</tbody></table></div>
      {!total && <p className="text-[0.82rem] text-muted">No training runs match. Prepare a run to get started.</p>}
      <div className="flex items-center gap-3 text-[0.78rem] text-muted"><Button variant="ghost" disabled={!page} onClick={() => setPage(page - 1)}>Previous</Button><span>{total ? `${page * 100 + 1}-${Math.min((page + 1) * 100, total)} of ${total}` : "0 runs"}</span><Button variant="ghost" disabled={(page + 1) * 100 >= total} onClick={() => setPage(page + 1)}>Next</Button></div>
      {activeRun && <RunCard key={activeRun.name} run={activeRun} reload={load} taken={runs.map(r => r.name)} initiallyOpen />}
    </>}
  </div>;
}

const PROFILE_WORDS: Record<string, string> = { contract: "as in the contract", smoke: "smoke test", balanced: "balanced", higher_quality: "higher quality", custom: "changed by hand" };
export const profileWords = (r: Pick<FinetuneRun, "profile" | "overrides">) => (r.profile ? PROFILE_WORDS[r.profile] || r.profile : "as in the contract")
  + (r.overrides ? ` (${r.overrides} setting${r.overrides === 1 ? "" : "s"} changed)` : "");


function Ladder({ state, blocked }: { state: string; blocked: { state: string; reason: string }[] }) {
  const at = LADDER.indexOf(state);
  const reason = (s: string) => blocked.find((b) => b.state === s)?.reason;
  return (
    <ul className="space-y-1.5 text-[0.8rem]">
      {LADDER.map((s, i) => {
        const reached = i <= at && !reason(s);
        return (
          <li key={s} className="flex items-start gap-2">
            <span className={`mt-0.5 ${reached ? "text-ok" : "text-faint"}`}>{reached ? "●" : "○"}</span>
            <span>
              <span className={reached ? "font-medium" : "text-muted"}>{s.replace(/_/g, " ")}</span>
              <span className="text-faint"> {reached ? LADDER_NOTE[s] : ""}</span>
              {!reached && <span className="block text-[0.76rem] text-warn">Blocked: {reason(s) || LADDER_NOTE[s]}</span>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function RunCard({ run, reload, taken = [], initiallyOpen = false }: { run: FinetuneRun; reload: () => void; taken?: string[]; initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  const [again, setAgain] = useWorkspaceState(`train.cloneName.${run.name}`, "");
  const [cloneRequest, setCloneRequest] = useWorkspaceState(`train.cloneRequest.${run.name}`, {fingerprint: '', requestId: ''});
  const [detail, setDetail] = useState<FinetuneRunDetail | null>(null);
  const [detailError, setDetailError] = useState<unknown>(null);
  const refresh = () => { api.finetuneRun(run.name).then(setDetail).catch(() => undefined); reload(); };   // ignored: the detail keeps its last value
  useEffect(() => { if (open && !detail) api.finetuneRun(run.name).then(d => { setDetail(d); setDetailError(null); }).catch(setDetailError); }, [open]);   // ignored: the card stays collapsed without detail
  const frozen = run.dataset.status === "frozen";
  const state = detail?.state || { state: run.state, blocked: run.blocked, evidence: {} };
  const isTrial = run.mode === 'trial' || detail?.prepared.mode === 'trial';
  const trialImports = detail?.trial_imports ?? [];
  const latestTrial = trialImports[trialImports.length - 1];
  const evaluation = detail?.evaluation_availability ?? run.evaluation_availability;
  return (
    <Panel title={<span>{run.name} <span className={`ml-2 ${pill(run.state === "trained" || run.state === "evaluated" ? "ok" : "muted")}`}>{run.state.replace(/_/g, " ")}</span>
      <span className={`ml-2 ${pill(frozen ? "ok" : "warn")}`}>dataset_{run.dataset.tag} {frozen ? "frozen" : "unfrozen"}</span></span>}
      right={<Button variant="ghost" onClick={() => setOpen(!open)}>{open ? "Hide" : "Details"}</Button>}>
      <div className="flex flex-wrap gap-x-8 gap-y-2 text-[0.82rem]">
        {run.model && <span><span className="text-muted">Model </span><b className="font-medium">{run.model.split("/").pop()}</b></span>}
        <span><span className="text-muted">Stage </span><b className="font-medium tnum">{run.stage}</b></span>
        <span><span className="text-muted">Settings </span><b className="font-medium">{profileWords(run)}</b></span>
        {run.purpose && <span><span className="text-muted">Purpose </span><b className="font-medium">{run.purpose}</b></span>}
        <span><span className="text-muted">Mode </span><b className="font-medium">{run.mode === 'trial' ? `Experimental trial (${run.total_budget_minutes ?? 30} minutes)` : 'Full training'}</b></span>
        <span><span className="text-muted">Bundle </span>{mb(run.bundle.bytes)}</span>
      </div>
      {(run.bundle.warnings ?? []).map(w => <p key={w.code} className="mt-2 text-[0.76rem] text-warn">{w.detail}</p>)}
      {open && <div className="mt-4 space-y-4">
          {detailError != null && <ErrorState error={detailError} title="Could not read run details" onRetry={() => api.finetuneRun(run.name).then(d => { setDetail(d); setDetailError(null); }).catch(setDetailError)} />}
        {isTrial ? <ol aria-label="Trial progress" className="flex flex-wrap gap-x-4 gap-y-2 text-[0.78rem]">
          <li>Bundle prepared</li><li>Remote execution and resource checks</li>
          <li>{latestTrial ? `Imported experimental evidence: ${latestTrial.state}` : 'Waiting for trial_outputs.zip'}</li>
        </ol> : <ol aria-label="Run progress" className="flex flex-wrap gap-x-4 gap-y-2 text-[0.78rem]">
          {LADDER.map((value, index) => <li key={value} aria-current={state.state === value ? "step" : undefined} className={index <= LADDER.indexOf(state.state) ? "font-medium text-fg" : "text-faint"}>{value.replace(/_/g, " ")}</li>)}
        </ol>}
          {!isTrial && <RunCurvesView name={run.name} />}
          {isTrial && <div className="space-y-3">
            <p className="text-[0.8rem] text-muted">Download the bundle and notebook below, run them on a remote runtime, then import trial_outputs.zip. Resource measurements come from that runtime.</p>
            <GuidedImport run={run.name} kind="trial" onDone={refresh} />
            {detail?.trial_imports?.map(trial => <TrialReport key={trial.import_id} trial={trial} />)}
            <details className="text-[0.78rem]"><summary className="cursor-pointer">Derived sample coverage</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(detail?.prepared.trial_data?.coverage ?? run.trial_coverage ?? {}, null, 2)}</pre></details>
          </div>}
          {evaluation?.state === 'blocked' && <p role="status" className="text-[0.8rem] text-warn">
            Security evaluation unavailable: {evaluation.detail} {evaluation.remediation}
          </p>}
          {!isTrial && detail && <RunEvaluations detail={detail} />}
        <div>
          {detail?.local_train && <RunSection label="Historical local training">
          <div>
            <LocalTrain run={run.name} last={detail?.local_train} onDone={refresh} />
          </div>
          </RunSection>}
          <RunSection label="Import outputs and evaluations">
          <div>
            <div className="mb-2 text-[0.76rem] font-medium  text-faint">Import the outputs of a GPU run</div>
            <GuidedImport run={run.name} kind="outputs" onDone={refresh} />
          </div>
          <div>
            <div className="mb-2 text-[0.76rem] font-medium  text-faint">Import an evaluation (answers on the validation views)</div>
            <GuidedImport run={run.name} kind="eval" onDone={refresh} />
            {detail && detail.eval_imports && detail.eval_imports.length > 0 && (
              <table className="mt-3 w-full text-[0.8rem] tnum">
                <thead><tr className="text-left text-faint"><th className="py-1">Id</th><th>Control</th><th>Attempts</th><th>Excluded</th><th>Counts</th><th>Run</th></tr></thead>
                <tbody>
                  {detail.eval_imports.map((e) => (
                    <tr key={e.import_id} className="border-t border-line">
                      <td className="py-1 font-mono">{short(e.import_id, 8)}</td><td>{e.baseline}</td><td>{num(e.attempts)}</td><td>{num(e.excluded)}</td>
                      <td className={e.counts_as_evaluation ? "text-ok" : "text-warn"}>{e.counts_as_evaluation ? "yes" : e.smoke_test ? "no (software)" : e.partial ? "no (partial)" : "no (excluded)"}</td>
                      <td className="font-mono">{e.tag}</td>
                    </tr>))}
                </tbody>
              </table>)}
          </div>
          {detail && detail.imports.length > 0 && (
            <div>
              <div className="mb-2 text-[0.76rem] font-medium  text-faint">Imports</div>
              <table className="w-full text-[0.8rem] tnum">
                <thead><tr className="text-left text-faint"><th className="py-1">Id</th><th>Type</th><th>State</th><th>Stage</th><th>Loss</th><th>Adapter</th><th>Calibration</th></tr></thead>
                <tbody>
                  {detail.imports.map((i) => (
                    <tr key={i.import_id} className="border-t border-line">
                      <td className="py-1 font-mono">{short(i.import_id, 8)}</td><td>{String(i.type).replace(/_/g, " ")}</td><td>{i.run?.state}</td>
                      <td>{i.run?.stage}</td><td className="font-mono">{i.run?.loss_mode}</td>
                      <td className="font-mono">{short(i.adapter?.sha256, 8)}</td><td>{i.calibration?.status || "—"}</td>
                    </tr>))}
                </tbody>
              </table>
              <p className="mt-1 text-[0.74rem] text-faint">An import proves the files are the ones the run wrote and match this run; the claims in its run config come from whoever ran it.</p>
            </div>
          )}
          </RunSection>
          {!isTrial && <RunSection label="Diagnostics">
          <div>
            <div className="mb-2 text-[0.76rem] font-medium  text-faint">How far this run has got</div>
            <Ladder state={state.state} blocked={state.blocked} />
          </div>
          <div>
            <div className="mb-2 text-[0.76rem] font-medium  text-faint">CPU check</div>
            <RunConsole stage="finetune_validate" label="Run the CPU check" params={{ run: run.name }} onDone={refresh}
              hint="Two steps on a tiny random model in a fresh process. A software check: not a training result and not a GPU test." />
            {detail?.cpu_validation && (
              <p className={`mt-2 text-[0.78rem] ${detail.cpu_validation.passed ? "text-ok" : "text-crit"}`}>
                Last check {detail.cpu_validation.passed ? "passed" : "failed"} ({detail.cpu_validation.seconds} s); the app was {detail.cpu_validation.app_importable ? "importable (the check is not isolated)" : "not importable in that process"}.
              </p>)}
          </div>
          <div>
            <div className="mb-2 text-[0.76rem] font-medium  text-faint">Local GPU check (small model)</div>
            <div className="flex flex-wrap gap-4">
              <RunConsole stage="gpu_eval" label="Answer a few views" params={{ run: run.name }} onDone={refresh}
                disabled={evaluation?.state === 'blocked'}
                hint="The untouched small model answers a few validation views through the evaluation harness." />
            </div>
            {detail?.local_gpu && detail.local_gpu.kind !== "train" && (
              <p className={`mt-2 text-[0.78rem] ${detail.local_gpu.passed ? "text-ok" : "text-crit"}`}>
                Last local {detail.local_gpu.device === "cpu" ? "processor" : "GPU"} {detail.local_gpu.kind} {detail.local_gpu.passed ? "passed" : "failed"} on {detail.local_gpu.device === "cpu" ? "the processor" : detail.local_gpu.gpu?.name} ({detail.local_gpu.model}, {detail.local_gpu.seconds} s
                {detail.local_gpu.peak_vram_mb ? `, peak ${detail.local_gpu.peak_vram_mb} MB, ${detail.local_gpu.tokens_per_second} tokens/s` : ""}).
                A software check on a dev-only model: not a training result, and the run's state is unchanged.
              </p>)}
          </div>
          </RunSection>}
          <RunSection label="Run provenance and prepare again">
            {!isTrial && detail?.full_training && <TrainingReadiness assessment={detail.full_training} purpose={String(detail.reexport?.purpose ?? run.purpose ?? 'train')} />}
            <div className="flex flex-wrap gap-x-8 gap-y-2 text-[0.8rem]">
              {run.prepared_at && <span><span className="text-muted">Prepared </span>{run.prepared_at.slice(0, 16).replace("T", " ")}</span>}
              <span><span className="text-muted">Base commit </span><span className="font-mono">{short(run.model_revision)}</span></span>
              <span><span className="text-muted">Bundle </span>{mb(run.bundle.bytes)} <span className="font-mono text-faint">{short(run.bundle.sha256)}</span></span>
              <span><span className="text-muted">Files </span>{num(run.bundle.files)}</span>
            </div>
          <div>
            <div className="mb-2 text-[0.76rem] font-medium  text-faint">Prepare again</div>
            <Field label="Name of the new run" hint="The same dataset version, settings and pinned commits under a new name. This run and its imports are not touched.">
              <Input aria-label="Name of the new run" value={again} onChange={(e) => setAgain(e.target.value)} placeholder="Automatic (runN)" />
            </Field>
            {again.trim() && taken.includes(again.trim()) && <p className="mt-1 text-[0.76rem] text-warn">A run with that name exists already.</p>}
            <div className="mt-2">
              <RunConsole key={again.trim()} stage="export_training_bundle" label="Prepare again" params={{ ...(again.trim() ? {name: again.trim()} : {}), ...(detail?.reexport || {}) }}
                disabled={taken.includes(again.trim()) || !detail?.reexport || (detail?.reexport?.mode !== 'trial' && detail?.reexport?.purpose !== 'smoke' && detail?.full_training?.allowed === false)} onDone={refresh} oneShot
                launch={async params => {
                  const {refresh: _refresh, run: _oldName, parent_run_id: _oldId, creation_request_id: _oldRequest, ...copy} = params;
                  const clean = {...copy, overrides: {...copy.overrides, ...(run.model ? {'model.id': run.model} : {})}};
                  const fingerprint = JSON.stringify(clean);
                  const requestId = cloneRequest.fingerprint === fingerprint ? cloneRequest.requestId : crypto.randomUUID();
                  setCloneRequest({fingerprint, requestId});
                  const created = await api.createTrainingRun({...clean, request_id: requestId});
                  return {id: created.job_id};
                }}
                match={job => !!cloneRequest.requestId && job.params?.creation_request_id === cloneRequest.requestId}
                hint="Writes a new bundle and notebook; nothing is trained." />
            </div>
          </div>
          </RunSection>
          <RunSection label="Downloads and exports">
      <div className="mt-3 flex flex-wrap gap-2.5">
        <a href={`/api/finetune/runs/${encodeURIComponent(run.name)}/bundle`} download><Button icon={<Icon.data />}>Download bundle</Button></a>
        <a href={`/api/finetune/runs/${encodeURIComponent(run.name)}/notebook`} download><Button icon={<Icon.tune />}>Download notebook</Button></a>
        <a href="https://colab.research.google.com/" target="_blank" rel="noreferrer"><Button icon={<Icon.external />}>Open Colab</Button></a>
      </div>
          {detail && <FullModelExport run={run.name} imports={detail.imports} />}
          </RunSection>
        </div>
      </div>}
    </Panel>
  );
}

function RunSection({ label, children }: { label: string; children: ReactNode }) {
  const [visited, setVisited] = useState(false);
  return <details className="border-t border-line" onToggle={event => { if (event.currentTarget.open) setVisited(true); }}>
    <summary className="cursor-pointer py-3 text-[0.82rem] font-medium">{label}</summary>
    {visited && <div className="space-y-4 pb-4">{children}</div>}
  </details>;
}

function RunEvaluations({ detail }: { detail: FinetuneRunDetail }) {
  const [runs, setRuns] = useState<EvalShared[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const tags = [...new Set([...(detail.state.evidence.evaluations ?? []), ...detail.eval_imports.map(x => x.tag).filter(Boolean)])];
  useEffect(() => {
    if (!tags.length) { setRuns([]); return; }
    api.experiments().then(rows => setRuns(rows.filter((row): row is EvalShared => (row as EvalShared).schema === "eval-shared/1" && tags.includes(row.tag)))).catch(setError);
  }, [tags.join("|")]);
  if (error) return <ErrorState error={error} title="Could not read run evaluations" />;
  return runs?.length ? <EvalRuns runs={runs} /> : <p className="text-[0.8rem] text-muted">No evaluation metrics imported for this run.</p>;
}
