import { useEffect, useState } from "react";
import { api, isAbort, type BuildProfile, type CodeSummary, type DatasetFilters, type DatasetOptions, type DatasetPreview } from "../lib/api";
import { Button, Input, Field, Skeleton, ErrorState } from "../components/ui";
import { Checkbox, Radio, SelectField, Hint } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { Icon } from "../components/Icon";
import { useToast } from "../components/Toast";
import { num } from "../lib/format";
import { Group, Check, Chips, fmt, take, pick, SELECT } from "../components/filters";
import { useDataVersion } from "../lib/dataSync";
import { useConfirm } from "../components/hud/AlertDialog";

// Step 4: the training dataset = the Source records whose code is mined, joined to
// their vulnerable/fixed pairs; train + a small validation slice (pipeline/split.py).
// A profile (build_code_dataset.PROFILES) sets the quality settings; the scope
// (languages, ecosystems) is chosen separately; everything else is under Advanced.
const SCOPE_KEYS = ["languages", "ecosystems", "severities", "cwes", "date_from", "date_to"] as const;
const PROFILE_KEY = "aivf.buildProfile";
const savedProfile = () => { try { return localStorage.getItem(PROFILE_KEY); } catch { return null; } };
const saveProfile = (id: string) => { try { localStorage.setItem(PROFILE_KEY, id); } catch { /* ignored: private mode; the profile is simply not remembered */ } };

function applyProfile(opt: DatasetOptions, cur: DatasetFilters | null, p: BuildProfile): DatasetFilters {
  const scope: Partial<DatasetFilters> = {};
  if (cur) for (const k of SCOPE_KEYS) (scope as any)[k] = cur[k];
  return { ...opt.defaults, ...scope, ...p.filters, profile: p.id } as DatasetFilters;
}

export function DatasetBuilder({ sum, onDone }: { sum: CodeSummary | null; onDone: () => void }) {
  const toast = useToast();
  const [opt, setOpt] = useState<DatasetOptions | null>(null);
  const [f, setF] = useState<DatasetFilters | null>(null);
  const [pv, setPv] = useState<DatasetPreview | null>(null);
  const [pvError, setPvError] = useState<unknown>(null);
  const [nextVersion, setNextVersion] = useState("");   // the next unused code_pairs version, from the server
  useEffect(() => { api.builds().then((b) => setNextVersion(b.next_versions.code_pairs)).catch(() => undefined); }, []);   // ignored: the button then says 'Freeze' without a number
  const [busy, setBusy] = useState(false);
  const [funnel, setFunnel] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  const [optError, setOptError] = useState<unknown>(null);
  const loadOptions = () => api.datasetOptions().then((o) => {
    setOptError(null);
    setOpt(o);
    setF((cur) => {
      if (cur) return cur;
      const p = o.profiles?.find((x) => x.id === savedProfile());
      return p ? applyProfile(o, null, p) : { ...o.defaults };
    });
  }).catch(setOptError);                          // keep the last options, show why they are stale
  const v = useDataVersion(30000);                 // heavy: at most every 30 s while data changes
  useEffect(() => { loadOptions(); }, [v]);

  // live preview, debounced; a newer choice cancels the request of the older one, so a slow old answer can never overwrite a new one
  useEffect(() => {
    if (!f) return;
    setBusy(true);
    const cancel = new AbortController();
    const timer = window.setTimeout(() => {
      api.datasetPreview(f, cancel.signal)
        .then((p) => { if (!cancel.signal.aborted) { setPv(p); setPvError(null); } })
        .catch((e) => { if (!isAbort(e)) setPvError(e); })
        .finally(() => { if (!cancel.signal.aborted) setBusy(false); });
    }, 350);
    return () => { clearTimeout(timer); cancel.abort(); };
  }, [f, v]);

  // an advanced setting changed by hand turns the profile into "custom"; scope does not
  const scoped = (k: keyof DatasetFilters) => (SCOPE_KEYS as readonly string[]).includes(k as string);
  const set = <K extends keyof DatasetFilters>(k: K, v: DatasetFilters[K]) =>
    setF((cur) => cur && { ...cur, [k]: v, ...(scoped(k) ? {} : { profile: "custom" }) });
  const toggle = (k: "languages" | "severities" | "cwes" | "ecosystems", v: string) =>
    setF((cur) => cur && { ...cur, [k]: cur[k].includes(v) ? cur[k].filter((x) => x !== v) : [...cur[k], v] });
  const choose = (p: BuildProfile | "custom") => {
    if (!opt) return;
    if (p === "custom") { setAdvanced(true); setF((cur) => cur && { ...cur, profile: "custom" }); saveProfile("custom"); return; }
    setF((cur) => applyProfile(opt, cur, p));
    saveProfile(p.id);
  };

  const confirm = useConfirm();
  const freeze = async () => {
    const name = `code_pairs_${nextVersion || "<next version>"}`;
    if (!f || !(await confirm({ title: `Freeze ${name}?`, confirmLabel: `Freeze ${name}`,
      description: "Freezing writes the dataset with these filters and records the hashes of its files; a later change becomes the next version." }))) return;
    try { await api.startJob("build_code_dataset", { filters: f, freeze: true }); toast.info(`Freezing ${name}… (see the log above)`); }
    catch (e: any) { toast.error(`Freeze failed: ${e?.message || e}`); }
  };

  const done = () => { onDone(); loadOptions(); };
  if ((!opt || !f) && optError != null) return <ErrorState error={optError} title="Could not read the dataset options" onRetry={loadOptions} />;
  if (!opt || !f) return <Skeleton className="h-40 w-full" />;
  const empty = opt.records_mined === 0;

  return (
    <div className="mt-4 rounded-lg border border-line bg-surface-2/40 p-4">
      <div className="mb-1 flex items-center gap-2">
        <span className="grid h-5 w-5 place-items-center rounded-full border border-line-strong text-[0.66rem] tnum text-muted">4</span>
        <span className="text-[0.86rem] font-medium">Build dataset</span>
      </div>
      <p className="mb-3 text-[0.76rem] leading-snug text-muted">
        The training dataset is the Source records whose code is mined ({num(opt.records_mined)} now), each joined to its
        vulnerable / fixed code. Every row carries the CVE, CWE, CVSS severity, language, advisory text, commits and code.
      </p>

      {empty ? (
        <p className="text-[0.8rem] text-muted">No record has mined code yet: run Extract and Mine first.</p>
      ) : (
        <>
          <div className="mb-1.5 text-[0.76rem] font-medium text-muted">Profile</div>
          <div className="mb-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            {(opt.profiles || []).map((p) => (
              <ProfileCard key={p.id} on={f.profile === p.id} name={p.name} desc={p.description} onClick={() => choose(p)} />
            ))}
            <ProfileCard on={f.profile === "custom"} name="Custom" desc="Your own settings: opens the advanced options."
              onClick={() => choose("custom")} />
          </div>

          <div className="mb-3 rounded-md border border-line bg-surface/60 p-3">
            <div className="mb-1 text-[0.76rem] font-medium text-muted">Scope <span className="font-normal text-faint">- what to train on (none picked = all)</span></div>
            <Chips label="Languages (detected from git)" all="all" items={opt.languages} picked={f.languages} onToggle={(v) => toggle("languages", v)} />
            <Chips label="Ecosystems" all="all" items={opt.ecosystems} picked={f.ecosystems} onToggle={(v) => toggle("ecosystems", v)} />
            <p className="mt-1 text-[0.72rem] text-faint">Collection window: CVEs published {opt.window.since || opt.window.earliest || "—"} → today
              {opt.window.since ? " (NVD “since” date of the ingest)" : " (earliest mined record)"}. Dates, severities and CWEs are under Advanced.</p>
          </div>

          <button type="button" onClick={() => setAdvanced(!advanced)}
            className="mb-3 flex items-center gap-2 text-[0.78rem] text-brand-strong hover:underline">
            {advanced ? "Hide advanced options" : "Advanced options"}
            <span className="text-faint no-underline">(split, code, fix commits, context, rows, detection set)</span>
          </button>
          {advanced && (
            <div className="mb-1 grid gap-4 lg:grid-cols-2">
              <Group title="Data (scope)">
                <div className="grid grid-cols-2 gap-2">
                  <Field label="Published from"><Input type="date" value={f.date_from || ""} onChange={(e) => set("date_from", e.target.value || null)} /></Field>
                  <Field label="Published to"><Input type="date" value={f.date_to || ""} onChange={(e) => set("date_to", e.target.value || null)} /></Field>
                </div>
                <Chips label="Severity (from CVSS)" all="all" items={pick(opt.severities, ["Critical", "Warning", "Recommendation"])} picked={f.severities} onToggle={(v) => toggle("severities", v)} />
                <Check label="Only CVEs with a CVSS score" v={f.require_cvss} on={(v) => set("require_cvss", v)} />
                <Chips label="CWE" all="any" items={take(opt.cwes, 18)} picked={f.cwes} onToggle={(v) => toggle("cwes", v)} />
                <Check label="Only CVEs with a CWE" v={f.require_cwe} on={(v) => set("require_cwe", v)} />
              </Group>
              <div className="space-y-4">
                <Group title="Split">
                  <p className="mb-2 text-[0.74rem] text-faint">Every row trains, except a validation slice of whole packages (stable hash, the
                    same on every build) that the notebooks use for early stopping and the decision threshold. No test split.</p>
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[0.78rem]">
                    <label className="flex items-center gap-2">Validation share
                      <Input type="number" min={1} max={30} disabled={f.train_all} value={Math.round(f.val_share * 100)}
                        onChange={(e) => set("val_share", Math.min(30, Math.max(1, Number(e.target.value) || 5)) / 100)} className="w-24 min-h-8" inputClassName="h-8 tnum" suffix="%" /></label>
                    <Checkbox checked={f.train_all} onCheckedChange={(v) => set("train_all", v)} label="Train on everything (no validation)" title="For the final model, once its settings were chosen on validation" />
                  </div>
                </Group>
                <Group title="Code">
                  <div className="grid grid-cols-3 gap-2">
                    <SelectField label="Level" value={f.granularity} onValueChange={(v) => set("granularity", v)}
                      options={[{ value: "", label: "any level" }, { value: "function", label: "function only" }, { value: "hunk", label: "hunk only" }, { value: "file", label: "file only" }]} />
                    <Field label="Max lines (vulnerable)"><Input type="number" min={0} value={f.max_lines} onChange={(e) => set("max_lines", Number(e.target.value) || 0)} /></Field>
                    <Field label="Max changed lines"><Input type="number" min={0} value={f.max_changed} placeholder="0 = any" onChange={(e) => set("max_changed", Number(e.target.value) || 0)} /></Field>
                  </div>
                  {!f.granularity && <Check label="Prefer function / hunk level (drop a file pair when its functions or hunks are paired)" v={f.prefer_function} on={(v) => set("prefer_function", v)} />}
                  <Check label="Include test-file pairs" v={f.include_tests} on={(v) => set("include_tests", v)} />
                  <Check label="Include tangled commits (fix bundled with refactoring)" v={f.include_tangled} on={(v) => set("include_tangled", v)} />
                </Group>
                <Group title="Fix commits">
                  <Check label="Exclude fix commits that fail the release-date check (committed before the last affected release or after the fixed one)"
                    v={f.exclude_suspect} on={(v) => set("exclude_suspect", v)} />
                  <Check label="Only fix commits the release dates confirm (plus hand-made pairs)" v={f.only_verified} on={(v) => set("only_verified", v)} />
                  {opt.fix_checks && Object.keys(opt.fix_checks).length > 0 && (
                    <div className="text-[0.72rem] text-faint tnum">mined commits: {fmt(opt.fix_checks)}
                      {opt.fix_checks.unchecked ? " - the unchecked ones are checked when Build runs" : ""}</div>
                  )}
                </Group>
                <Group title="Context">
                  <Check label="Only rows with advisory text" v={f.require_advisory} on={(v) => set("require_advisory", v)} />
                  <Check label="Only rows with a fix commit message" v={f.require_commit_msg} on={(v) => set("require_commit_msg", v)} />
                  <Check label="Only LLM-enriched rows" v={f.only_enriched} on={(v) => set("only_enriched", v)} />
                  <SelectField label="Weak-context rows (no full advisory, no informative commit message)" value={f.weak_context} onValueChange={(v) => set("weak_context", v as DatasetFilters["weak_context"])}
                    options={[{ value: "keep", label: "keep" }, { value: "enriched_only", label: "keep only when LLM-enriched (step 3)" }, { value: "drop", label: "drop" }]} />
                  <Check label="Include synthetic LLM fields (flagged in synthetic_fields)" v={f.include_synthetic} on={(v) => set("include_synthetic", v)} />
                </Group>
                <Group title="Which unit is the vulnerable one">
                  <SelectField label="Keep rows whose unit is named" aria-label="Which unit is the vulnerable one" value={f.attribution} onValueChange={(v) => set("attribution", v as DatasetFilters["attribution"])}
                    hint="A fix often changes several functions. Named: the advisory or the fix points at the function (or at the file when it is the only function changed there)."
                    options={[{ value: "", label: "all rows (the unit is not checked)" }, { value: "supporting", label: "named, or its file is named" }, { value: "decision", label: "named only" }]} />
                  <Check label="One row for a fix that was cherry-picked to several release branches" v={f.collapse_backports} on={(v) => set("collapse_backports", v)} />
                </Group>
                <Group title="Rows">
                  <div className="flex items-center gap-2 text-[0.78rem]">
                    <Radio name="unit" checked={f.unit === "pairs"} onChange={() => set("unit", "pairs")} label="Every usable pair, at most" />
                    <Input type="number" min={0} value={f.per_cve_cap} aria-label="Pairs per CVE" onChange={(e) => set("per_cve_cap", Number(e.target.value) || 0)} className="w-24 min-h-8" inputClassName="h-8 tnum" />
                    <span>per CVE <span className="text-faint">(0 = no cap)</span></span>
                  </div>
                  <Radio className="mt-1.5" name="unit" checked={f.unit === "best"} onChange={() => set("unit", "best")} label="One row per record (its best pair)" />
                </Group>
                <Group title="Detection set">
                  <p className="mb-2 text-[0.74rem] text-faint">The detection JSONL holds each pair's vulnerable code (1) and its fixed twin (0), a candidate patch rather than code proven secure.
                    A function the fix did not touch is no evidence of secure code, so such functions are left out unless you ask for them below.</p>
                  <Check label="Add functions the fix did not touch as weak safe samples (unverified)" v={f.include_untouched} on={(v) => set("include_untouched", v)} />
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    <Field label="Safe samples per vulnerable" hint={f.include_untouched ? "1 = only fixed twins; 2 = one untouched function more" : "Used only with the untouched functions above"}>
                      <Input type="number" min={1} max={5} step={0.5} value={f.safe_ratio} disabled={!f.include_untouched}
                        onChange={(e) => set("safe_ratio", Math.min(5, Math.max(1, Number(e.target.value) || 2)))} /></Field>
                    <Field label="Longest code (characters)" hint="Longer pairs are skipped, never cut">
                      <Input type="number" min={500} step={500} value={f.max_chars}
                        onChange={(e) => set("max_chars", Math.max(500, Number(e.target.value) || 8000))} /></Field>
                  </div>
                </Group>
              </div>
            </div>
          )}
        </>
      )}

      {!empty && (
        <div className={`mt-4 rounded-md border border-line bg-surface/60 p-3 transition-opacity ${busy ? "opacity-60" : ""}`}>
          {busy && <p role="status" className="mb-2 text-[0.74rem] text-muted">Computing the preview...</p>}
          {pvError != null && !busy && (
            <div className="mb-3"><ErrorState error={pvError} title="The preview could not be computed" onRetry={() => setF((c) => c && { ...c })} /></div>
          )}
          {!pv ? (pvError == null ? <Skeleton className="h-10 w-full" /> : null) : (
            <>
              {(pv.warnings || []).map((w, i) => (
                <div key={i} className="mb-2 rounded-md border border-warn-line bg-warn-tint px-3 py-2 text-[0.76rem] text-warn">{w}</div>
              ))}
              <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-[0.8rem]">
                <span><b className="tnum text-[1rem]">{num(pv.rows)}</b> <span className="text-muted">rows</span></span>
                <span className="tnum text-muted">{num(pv.records)} records · {num(pv.cves)} CVEs · {num(pv.commits)} commits</span>
                {["train", "val"].map((s) => (
                  <span key={s} className="tnum"><span className="text-muted">{s}</span> {num(pv.split_counts[s] || 0)}
                    <span className="text-faint"> ({num(pv.split_cves[s] || 0)} CVEs)</span></span>
                ))}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[0.74rem] text-muted tnum">
                <span>severity: {fmt(pv.by_severity)}</span>
                {pv.by_severity_source && <Hint label="record CVSS score / v3 / v4 vector, else the NVD score of the CVE; osv_label: only GitHub's LOW / HIGH / CRITICAL label, no score" className="no-underline">severity from: {fmt(pv.by_severity_source)}</Hint>}
                <span>level: {fmt(pv.by_granularity)}</span>
                {pv.by_context && <Hint label="real context of each row (advisory, commit message, CWE)" className="no-underline">context: {fmt(pv.by_context)}</Hint>}
                {pv.by_fix_check && <span>fix check: {fmt(pv.by_fix_check)}</span>}
                <span>languages: {fmt(pv.by_language)}</span>
                {pv.llm_enriched > 0 && <span>LLM-enriched: {num(pv.llm_enriched)}</span>}
                {pv.dependencies && pv.dependencies.rows > 0 && (
                  <Hint label="dependencies_*.jsonl: one row per vulnerable package of the records whose dependency versions were checked (the mined ones)" className="no-underline">
                    dependency rows: {num(pv.dependencies.rows)} ({fmt(pv.dependencies.by_status || {})})</Hint>
                )}
                <button type="button" className="text-brand-strong hover:underline" onClick={() => setFunnel(!funnel)}>
                  {funnel ? "hide" : "what the filters removed"}</button>
              </div>
              {funnel && (
                <ol className="mt-2 space-y-0.5 text-[0.74rem] tnum">
                  {pv.funnel.map((s, i) => (
                    <li key={i} className="flex gap-3"><span className="w-14 text-right">{num(s.rows)}</span><span className="text-muted">{s.step}</span></li>
                  ))}
                  <li className="flex gap-3"><span className="w-14 text-right">{num(pv.rows)}</span>
                    <span className="text-muted">after de-duplication ({pv.duplicates_removed ?? 0}) and the leak guard ({pv.leak_guard_removed ?? 0})</span></li>
                </ol>
              )}
            </>
          )}
        </div>
      )}

      <div className="mt-4">
        <RunConsole stage="build_code_dataset" label="Build dataset" params={{ filters: f }} onDone={done}
          hint="Writes the preview dataset (parquet, JSONL samples, detection JSONL, dependencies JSONL) and its stats." />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button onClick={freeze} disabled={empty}>{nextVersion ? `Freeze as code_pairs_${nextVersion}` : "Freeze"}</Button>
          {sum?.dataset && <>
            <a href="/api/code/export?fmt=jsonl" download><Button icon={<Icon.data />}>Samples JSONL</Button></a>
            <a href="/api/code/export?fmt=detection" download><Button icon={<Icon.data />}>Detection JSONL</Button></a>
            <a href="/api/code/export?fmt=parquet" download><Button icon={<Icon.data />}>Parquet</Button></a>
            <a href="/api/code/export?fmt=dependencies" download><Button icon={<Icon.data />}>Dependencies JSONL</Button></a>
          </>}
        </div>
      </div>
    </div>
  );
}

function ProfileCard({ on, name, desc, onClick }: { on: boolean; name: string; desc: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={on}
      className={`rounded-lg border p-3 text-left transition-colors ${on
        ? "border-brand bg-brand/10" : "border-line bg-surface-2/50 hover:border-line-strong"}`}>
      <div className="flex items-center gap-2 text-[0.82rem] font-medium">
        <span className={`h-3 w-3 shrink-0 rounded-full border ${on ? "border-brand bg-brand" : "border-line-strong"}`} />{name}</div>
      <div className="mt-1 text-[0.72rem] leading-snug text-muted">{desc}</div>
    </button>
  );
}
