import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { Tabs } from "../components/hud/Tabs";
import { Button, EmptyState, ErrorState, Field, Input, Panel, Skeleton } from "../components/ui";
import { api, errorText, type BuildList, type SecurityBuildProfile, type ProfileBuild } from "../lib/api";
import { useDataVersion } from "../lib/dataSync";
import { useTabParam } from "../lib/tabParam";
import { useWorkspaceState } from "../lib/workspaceState";
import { AdmissionCounts, BuildCard } from "../sections/DatasetBuild";
import { BuildEvidenceActions } from '../sections/BuildEvidenceActions';
import { TrainingReadiness } from '../components/TrainingReadiness';

const TABS = ["wizard", "versions"] as const;
const PAGE = 100;
const PROFILE_LABELS: Record<string, string> = { js_ts_security: "JS/TS security", all_language_security: "All-language security" };
const STATE_LABELS: Record<string, string> = { queued: "Waiting to start", building: "Building", blocked: "Needs attention", failed: "Build failed", interrupted: "Interrupted", ready: "Ready", historical: "Historical", frozen: "Frozen", unfrozen: "Unfrozen", external: "Imported", preview: "Preview" };
const BLOCKER_LABELS: Record<string, string> = {
  no_automatically_eligible_security_units: "No examples passed automatic evidence checks. Open build details for the reasons.",
  no_supported_source_units: "No supported source units are available for this profile.",
  historical_build_requires_explicit_readiness_validation: "This historical version needs a readiness check before training.",
  source_snapshot_failed: "The source data could not be captured. Retry the build.",
  job_start_failed: "The build could not start. Retry the build.",
  job_finished_without_dataset_publication: "The build ended without saving a dataset. Retry the build.",
  build_interrupted_requires_explicit_retry: "The build was interrupted. Retry to create a new version.",
  security_readiness_missing: "This dataset has not passed the security readiness checks.",
  build_failed: "The build failed. Open build details for the diagnostic report.",
};
const readable = (value: string) => { const words = value.replace(/_/g, " "); return words.charAt(0).toUpperCase() + words.slice(1); };
const stateLabel = (value: string) => STATE_LABELS[value] ?? readable(value);
const blockerCode = (value: unknown) => typeof value === "string" ? value : value && typeof value === "object" && "code" in value ? String(value.code) : "";
function BuildBlockers({ blockers }: { blockers: unknown[] }) {
  if (!blockers.length) return null;
  const messages = [...new Set(blockers.map(value => BLOCKER_LABELS[blockerCode(value)] ?? "This build needs attention. Open build details for the diagnostic report."))];
  return <div className="mt-3 space-y-2 text-[0.8rem]">
    <ul className="space-y-1 text-warn">{messages.map(message => <li key={message}>{message}</li>)}</ul>
    <details className="text-muted"><summary className="cursor-pointer">Build details</summary><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[0.76rem]">{JSON.stringify(blockers, null, 2)}</pre></details>
  </div>;
}
export function BuildPage() {
  const [tab, setTab] = useTabParam(TABS);
  const [params] = useSearchParams();
  const [selected, setSelected] = useWorkspaceState<ProfileBuild | null>("build.selected", null);
  const [page, setPage] = useWorkspaceState("build.page", 0);
  const [search, setSearch] = useWorkspaceState("build.search", "");
  if (params.get("step") === "review") return <Navigate to="/pairs?tab=context" replace />;
  const created = (build: ProfileBuild) => { setSelected(build); setPage(0); setSearch(''); setTab("versions"); };
  return <Tabs value={tab} onValueChange={setTab} items={[
    { id: "wizard", label: "Build dataset", content: <ProfileBuilder onCreated={created} /> },
    { id: "versions", label: "Dataset versions", content: <Versions selected={selected} onSelect={setSelected} onCreated={created} page={page} setPage={setPage} search={search} setSearch={setSearch} /> },
  ]} />;
}
function ProfileBuilder({ onCreated }: { onCreated: (build: ProfileBuild) => void }) {
  const [profiles, setProfiles] = useState<SecurityBuildProfile[] | null>(null);
  const [profile, setProfile] = useWorkspaceState("build.profile", "js_ts_security");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.buildProfiles().then(r => { setProfiles(r.profiles); setError(null); }).catch(setError);
  useEffect(() => { load(); }, []);
  if (!profiles) return error ? <ErrorState error={error} title="Could not read build profiles" onRetry={load} /> : <Skeleton className="h-40" />;
  const chosen = profiles.find(p => p.id === profile) ?? profiles[0];
  if (!chosen) return <Panel title="Dataset profiles"><EmptyState title="No dataset profiles available"
    action={<Button variant="subtle" onClick={load}>Refresh profiles</Button>} /></Panel>;
  const build = async () => {
    setBusy(true); setError(null);
    try { onCreated((await api.createBuild(chosen.id)).build); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <div className="max-w-3xl space-y-5"><Panel title="Dataset profile" desc="Choose the source coverage for the next build.">
    <div role="radiogroup" aria-label="Dataset profile" className="divide-y divide-line border border-line">
      {profiles.map(p => <button key={p.id} type="button" role="radio" aria-checked={chosen.id === p.id} onClick={() => setProfile(p.id)}
        className={`flex w-full items-start gap-3 px-3 py-4 text-left text-[0.85rem] ${chosen.id === p.id ? "bg-brand-tint" : "hover:bg-surface-2"}`}>
        <span aria-hidden className={`mt-1 h-3 w-3 shrink-0 rounded-full border ${chosen.id === p.id ? "border-brand-strong bg-brand" : "border-line-strong"}`} />
        <span><span className="block font-medium">{p.label}</span><span className="mt-1 block text-[0.78rem] text-muted">{p.id === "js_ts_security" ? "JavaScript and TypeScript from npm." : "Security data across supported languages."}</span></span>
      </button>)}
    </div>
    <p className="mt-4 text-[0.78rem] text-muted">Automatic evidence checks. Injection share 10%.</p>
    {chosen.counts && <div className="mt-3 space-y-2 text-[0.78rem] text-muted">
      <dl className="space-y-1">{([['Automatically eligible', chosen.counts.automatically_eligible], ['Insufficient context', chosen.counts.insufficient_context], ['Excluded', chosen.counts.excluded]] as const).map(([label, count]) => <div key={label} className="flex flex-wrap justify-between gap-x-4"><dt>{label}</dt><dd className="tabular-nums">{count == null ? 'Calculated during build' : count.toLocaleString()}</dd></div>)}</dl>
      <p>Final admission is checked against the captured source during the build.</p>
      <details><summary className="cursor-pointer">Source counts</summary><p className="mt-1">{chosen.counts.source_pairs?.toLocaleString() ?? '-'} source pairs, {chosen.counts.source_units ?? chosen.counts.units ?? '-'} units{chosen.counts_estimated ? ' (estimate)' : ''}.</p></details>
    </div>}
    <BuildBlockers blockers={chosen.blockers ?? []} />
    {error != null && <p role="alert" className="mt-3 text-[0.8rem] text-crit-soft">{errorText(error)}</p>}
    <div className="mt-4 flex flex-wrap items-center gap-4">
      <Button variant="primary" loading={busy} onClick={() => void build()}>Build dataset</Button>
      <Link className="text-[0.78rem] text-brand-strong hover:underline" to="/pairs?tab=context">Optional context improvements</Link>
    </div>
  </Panel></div>;
}
function Versions({ selected, onSelect, onCreated, page, setPage, search, setSearch }: { selected: ProfileBuild | null; onSelect: (b: ProfileBuild) => void; onCreated: (b: ProfileBuild) => void; page: number; setPage: (page: number) => void; search: string; setSearch: (search: string) => void }) {
  const [list, setList] = useState<BuildList | null>(null);
  const request = useRef(0);
  const selectedTag = useRef(selected?.tag);
  selectedTag.current = selected?.tag;
  const [error, setError] = useState<unknown>(null);
  const v = useDataVersion(3000);
  const load = async () => {
    const identity = ++request.current;
    try {
      const result = await api.builds({ limit: PAGE, offset: page * PAGE, search });
      if (identity !== request.current) return;
      setList(result); setError(null);
      const wanted = selectedTag.current;
      if (!wanted) return;
      const fresh = result.builds.find(build => build.tag === wanted);
      if (fresh) onSelect(fresh);
      const detail = await api.buildDetail(wanted);
      if (identity === request.current && selectedTag.current === wanted && detail?.build) onSelect(detail.build);
    } catch (failure) {if (identity === request.current) setError(failure);}
  };
  useEffect(() => { void load(); return () => {request.current++;}; }, [v, page, search]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!selected || !["queued", "building"].includes(selected.state ?? "")) return;
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [selected?.tag, selected?.state, page, search]); // eslint-disable-line react-hooks/exhaustive-deps
  const total = list?.total ?? list?.builds.length ?? 0;
  const active = selected;
  useEffect(() => {
    if (!selected?.tag) return;
    let active = true;
    void api.buildDetail(selected.tag).then(detail => {if (active && detail?.build) onSelect(detail.build);}).catch(setError);
    return () => {active = false;};
  }, [selected?.tag]); // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="space-y-5">
    <Field label="Search dataset versions"><Input value={search} onChange={e => { setSearch(e.target.value); setPage(0); }} placeholder="Version or profile" /></Field>
    {error != null && <ErrorState error={error} title="Could not read dataset versions" onRetry={load} />}
    {!list && !error && <Skeleton className="h-40" />}
    {list && <>
      <div className="overflow-x-auto"><table className="w-full text-left text-[0.82rem]" aria-label="Dataset versions">
        <thead className="text-muted"><tr><th className="py-3">Version</th><th>Profile</th><th>State</th><th>Created</th></tr></thead>
        <tbody>{list.builds.map(b => <tr key={b.tag} className={`border-t border-line ${active?.tag === b.tag ? "bg-brand-tint" : ""}`}>
          <td className="py-3"><button className="text-brand-strong hover:underline" onClick={() => onSelect(b)}>dataset_{b.tag}</button></td><td>{b.profile ? PROFILE_LABELS[b.profile] ?? readable(b.profile) : "Historical"}</td><td>{b.full_training && !b.full_training.allowed ? 'Built, insufficient evidence for full training' : stateLabel(b.state ?? b.status)}</td><td>{b.created_at?.slice(0, 16).replace("T", " ") ?? "-"}</td>
        </tr>)}</tbody>
      </table></div>
      {!total && <p className="text-[0.82rem] text-muted">No dataset versions yet. Choose a profile on Build dataset.</p>}
      <div className="flex items-center gap-3 text-[0.78rem] text-muted"><Button variant="ghost" disabled={!page} onClick={() => setPage(page - 1)}>Previous</Button><span>{total ? `${page * PAGE + 1}-${Math.min((page + 1) * PAGE, total)} of ${total}` : "0 versions"}</span><Button variant="ghost" disabled={(page + 1) * PAGE >= total} onClick={() => setPage(page + 1)}>Next</Button></div>
    </>}
    {active && <Panel title={`dataset_${active.tag}`}>
      <p role="status" className="text-[0.82rem]">{stateLabel(active.state ?? active.status)}{active.progress?.label ? `: ${active.progress.label}` : ""}</p>
      {active.progress?.total != null && <p className="mt-2 text-[0.78rem] text-muted">{active.progress.processed ?? 0} of {active.progress.total}</p>}
      <AdmissionCounts counts={active.counts ?? {}} />
      {(active.readiness || active.full_training) && <div className="mt-3"><TrainingReadiness readiness={active.readiness} assessment={active.full_training} /></div>}
      <BuildBlockers blockers={active.blockers ?? []} />
      <BuildEvidenceActions key={active.tag} build={active} onChanged={() => void load()} onCreated={onCreated} />
    </Panel>}
    {active && (!active.state || ["ready", "historical"].includes(active.state)) && active.tier_mix && <BuildCard b={active} />}
  </div>;
}
