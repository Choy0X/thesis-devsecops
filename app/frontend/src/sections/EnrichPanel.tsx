import { useEffect, useMemo, useState } from "react";
import { api, isAbort, type DatasetOptions, type EnrichCandidates, type EnrichFilters } from "../lib/api";
import { Field, Input, Skeleton } from "../components/ui";
import { Checkbox, Radio, SelectField } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { Group, Check, Chips, pick, take } from "../components/filters";
import { num } from "../lib/format";
import { PairWindow } from "./PairsBrowser";
import { useDataVersion } from "../lib/dataSync";
import { ContextExchangePanel } from "./ContextExchangePanel";
import { CampaignContextItems } from './CampaignContextItems';

// Step 3: LLM enrichment aimed at the pairs whose *real* context is thin. The list is
// the rows a dataset build would contain, rated weak / partial / strong from the
// advisory, the commit message, CWE, severity and whether the location is named.
const DEFAULTS: EnrichFilters = {
  levels: ["weak"], missing: [], min_advisory: 200, splits: ["train", "val"], status: "missing",
  val_share: 0.05, train_all: false,
  order: "weakest", languages: [], severities: [], cwes: [], ecosystems: [], granularity: "",
  prefer_function: true, max_lines: 300, per_cve_cap: 5, date_from: null, date_to: null,
  include_tests: false, include_tangled: false, exclude_suspect: true,
};
const PAGE = 50;
const LEVEL_CLS: Record<string, string> = {
  weak: "border-crit-line text-crit-soft", partial: "border-warn-line text-warn", strong: "border-ok-line text-ok",
};

export function EnrichPanel({ onDone, campaignId }: { onDone: () => void; campaignId?: string }) {
  const [opt, setOpt] = useState<DatasetOptions | null>(null);
  const [f, setF] = useState<EnrichFilters>(DEFAULTS);
  const [data, setData] = useState<EnrichCandidates | null>(null);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [mode, setMode] = useState<"selected" | "matching">("matching");
  const [limit, setLimit] = useState("50");
  const [workers, setWorkers] = useState("2");
  const [more, setMore] = useState(false);
  const [viewing, setViewing] = useState<number | null>(null);
  const [tick, setTick] = useState(0);

  const v = useDataVersion(30000);                 // heavy: at most every 30 s while data changes
  useEffect(() => { api.datasetOptions().then(setOpt).catch(() => undefined); }, [v]);   // ignored: the options keep their last value
  useEffect(() => { setOffset(0); }, [f]);
  useEffect(() => {                                // debounced; a newer choice cancels the older request and its answer is ignored
    setBusy(true);
    const cancel = new AbortController();
    const timer = window.setTimeout(() => {
      api.enrichCandidates(f, PAGE, offset, cancel.signal)
        .then((d) => { if (!cancel.signal.aborted) { setData(d); setErr(null); } })
        .catch((e) => { if (!isAbort(e)) setErr(String(e?.message || e).replace(/^\d+ /, "")); })
        .finally(() => { if (!cancel.signal.aborted) setBusy(false); });
    }, 350);
    return () => { clearTimeout(timer); cancel.abort(); };
  }, [f, offset, tick, v]);
  useEffect(() => { if (mode === "selected" && sel.size === 0) setMode("matching"); }, [sel, mode]);

  const set = <K extends keyof EnrichFilters>(k: K, v: EnrichFilters[K]) => setF((c) => ({ ...c, [k]: v }));
  const toggle = (k: "levels" | "missing" | "splits" | "languages" | "severities" | "ecosystems" | "cwes", v: string) =>
    setF((c) => ({ ...c, [k]: c[k].includes(v) ? c[k].filter((x) => x !== v) : [...c[k], v] }));
  const flip = (id: number) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const rows = data?.rows || [];
  const pageIds = rows.map((r) => r.pair_id);
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => sel.has(id));
  const labels = data?.missing_labels || {};
  const total = data?.total || 0;
  const done = () => { setSel(new Set()); setTick((t) => t + 1); onDone(); };

  const params = useMemo(() => mode === "selected"
    ? { ids: [...sel], campaign_id: campaignId, workers: Number(workers) || 2 }
    : { filters: f, campaign_id: campaignId, limit: Number(limit) || 50, workers: Number(workers) || 2 }, [mode, sel, f, limit, workers, campaignId]);
  const runLabel = mode === "selected" ? `Enrich ${num(sel.size)} selected` : `Enrich ${num(Math.min(total, Number(limit) || 50))} matching`;

  return (
    <div className="mt-4 rounded-lg border border-line bg-surface-2/40 p-4">
      <div className="mb-1 flex items-center gap-2">
        <span className="grid h-5 w-5 place-items-center rounded-full border border-line-strong text-[0.66rem] tnum text-muted">3</span>
        <span className="text-[0.86rem] font-medium">Enrich with the LLM (optional)</span>
      </div>
      <p className="mb-3 text-[0.76rem] leading-snug text-muted">
        Spend the LLM where the real context is thin. The list holds the rows a dataset build would contain, rated from real
        text only: <b className="font-medium text-crit-soft">weak</b> = no full advisory and no informative commit message;
        <b className="font-medium text-ok"> strong</b> = full advisory, an informative message or the location named, and a CWE.
        Drafts (root cause, secure pattern, explanation) are stored separately and flagged synthetic; the real text is never replaced.
      </p>

      {data && (
        <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[0.76rem] tnum text-muted">
          <span>{num(data.dataset_rows)} dataset rows:</span>
          {(["weak", "partial", "strong"] as const).map((l) => (
            <span key={l}><span className={LEVEL_CLS[l].split(" ")[1]}>{l}</span> {num(data.all_levels?.[l] || 0)}</span>
          ))}
          <span>· LLM-enriched {num(data.enriched)}</span>
          {data.older_prompt > 0 && <span>· {num(data.older_prompt)} with an older prompt than {data.prompt_version}</span>}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Group title="Context">
          <Chips label="Context level" all="all" items={pick(data?.levels || {}, ["weak", "partial", "strong"])}
            picked={f.levels} onToggle={(v) => toggle("levels", v)} />
          <Chips label="Missing (a pair must lack every one selected)" all="any" items={data?.missing || {}}
            labels={labels} picked={f.missing} onToggle={(v) => toggle("missing", v)} />
          <div className="grid grid-cols-2 gap-2">
            <Field label="Full advisory from (characters)">
              <Input type="number" min={0} value={f.min_advisory} onChange={(e) => set("min_advisory", Number(e.target.value) || 0)} /></Field>
            <SelectField label="LLM drafts" value={f.status} onValueChange={(v) => set("status", v as EnrichFilters["status"])}
              options={[{ value: "missing", label: "not enriched yet" }, { value: "older", label: "enriched with an older prompt" }, { value: "any", label: "any (re-enrich)" }]} />
          </div>
        </Group>
        <Group title="Data">
          <Chips label="Split" all="all" items={pick(data?.splits || {}, ["train", "val"])} picked={f.splits} onToggle={(v) => toggle("splits", v)} />
          {opt && <Chips label="Languages" all="all" items={opt.languages} picked={f.languages} onToggle={(v) => toggle("languages", v)} />}
          {opt && <Chips label="Severity" all="all" items={pick(opt.severities, ["Critical", "Warning", "Recommendation"])}
            picked={f.severities} onToggle={(v) => toggle("severities", v)} />}
          <div className="grid grid-cols-2 gap-2">
            <SelectField label="Level" value={f.granularity} onValueChange={(v) => set("granularity", v)}
              options={[{ value: "", label: "any level" }, { value: "function", label: "function" }, { value: "hunk", label: "hunk" }, { value: "file", label: "file" }]} />
            <SelectField label="Order" value={f.order} onValueChange={(v) => set("order", v as EnrichFilters["order"])}
              options={[{ value: "weakest", label: "weakest context first" }, { value: "newest", label: "newest first" }]} />
          </div>
          <button type="button" className="text-[0.76rem] text-brand-strong hover:underline" onClick={() => setMore(!more)}>
            {more ? "fewer filters" : "more filters (ecosystem, CWE, dates, build rules)"}</button>
          {more && opt && (
            <div className="space-y-2">
              <Chips label="Ecosystems" all="all" items={opt.ecosystems} picked={f.ecosystems} onToggle={(v) => toggle("ecosystems", v)} />
              <Chips label="CWE" all="any" items={take(opt.cwes, 18)} picked={f.cwes} onToggle={(v) => toggle("cwes", v)} />
              <div className="grid grid-cols-2 gap-2">
                <Field label="Published from"><Input type="date" value={f.date_from || ""} onChange={(e) => set("date_from", e.target.value || null)} /></Field>
                <Field label="Published to"><Input type="date" value={f.date_to || ""} onChange={(e) => set("date_to", e.target.value || null)} /></Field>
                <Field label="Max lines (vulnerable)"><Input type="number" min={0} value={f.max_lines} onChange={(e) => set("max_lines", Number(e.target.value) || 0)} /></Field>
                <Field label="Pairs per CVE (0 = all)"><Input type="number" min={0} value={f.per_cve_cap} onChange={(e) => set("per_cve_cap", Number(e.target.value) || 0)} /></Field>
              </div>
              <Check label="Prefer function / hunk level (skip a file pair when its functions are paired)" v={f.prefer_function} on={(v) => set("prefer_function", v)} />
              <Check label="Exclude suspect fix commits" v={f.exclude_suspect} on={(v) => set("exclude_suspect", v)} />
              <Check label="Include test files" v={f.include_tests} on={(v) => set("include_tests", v)} />
              <Check label="Include tangled commits" v={f.include_tangled} on={(v) => set("include_tangled", v)} />
              <p className="text-[0.72rem] text-faint">Keep these equal to the Build settings so only rows that end up in the dataset are enriched.</p>
            </div>
          )}
        </Group>
      </div>

      <div className={`mt-4 rounded-md border border-line bg-surface/60 transition-opacity ${busy ? "opacity-60" : ""}`}>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-3 py-2 text-[0.78rem]">
          <span><b className="tnum">{num(total)}</b> <span className="text-muted">matching pairs</span></span>
          {sel.size > 0 && <span className="tnum text-brand-strong">{num(sel.size)} selected</span>}
          {sel.size > 0 && <button type="button" className="text-muted hover:text-fg" onClick={() => setSel(new Set())}>clear selection</button>}
          <span className="ml-auto flex items-center gap-2 text-muted">
            {total > PAGE && <>
              <button type="button" disabled={offset === 0} className="disabled:opacity-40 hover:text-fg" onClick={() => setOffset(Math.max(0, offset - PAGE))}>Prev</button>
              <span className="tnum">{num(offset + 1)}–{num(Math.min(offset + PAGE, total))}</span>
              <button type="button" disabled={offset + PAGE >= total} className="disabled:opacity-40 hover:text-fg" onClick={() => setOffset(offset + PAGE)}>Next</button>
            </>}
          </span>
        </div>
        {err ? <p className="p-3 text-[0.78rem] text-crit-soft">{err}</p> : !data ? <Skeleton className="m-3 h-24" /> : rows.length === 0 ? (
          <p className="p-3 text-[0.78rem] text-muted">
            {data.dataset_rows === 0 ? "No dataset rows yet: mine code first." : "No pair matches these filters - the selected pairs already have enough context, or loosen the filters."}</p>
        ) : (
          <div className="max-h-[28rem] overflow-auto">
            <table className="w-full text-[0.76rem]">
              <thead className="sticky top-0 bg-surface text-left text-faint">
                <tr>
                  <th className="w-8 px-3 py-1.5"><Checkbox checked={allOnPage} aria-label="Select every pair on this page"
                    onCheckedChange={() => setSel((s) => { const n = new Set(s); pageIds.forEach((id) => allOnPage ? n.delete(id) : n.add(id)); return n; })} /></th>
                  <th className="px-2 py-1.5 font-medium">Advisory / location</th>
                  <th className="px-2 py-1.5 font-medium">Context</th>
                  <th className="px-2 py-1.5 font-medium">Missing</th>
                  <th className="px-2 py-1.5 font-medium">What it has</th>
                  <th className="px-2 py-1.5 font-medium">LLM</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.pair_id} className={`border-t border-line align-top ${sel.has(r.pair_id) ? "bg-brand/5" : ""}`}>
                    <td className="px-3 py-1.5"><Checkbox checked={sel.has(r.pair_id)} onCheckedChange={() => flip(r.pair_id)} aria-label={`Select the pair of ${r.cve || r.file}`} /></td>
                    <td className="max-w-[18rem] px-2 py-1.5">
                      <button type="button" onClick={() => setViewing(r.pair_id)} className="text-left hover:underline">
                        <span className="font-mono text-[0.74rem]">{r.cve || "—"}</span>
                        <span className="ml-1.5 text-faint">{r.language} · {r.granularity} · {r.split}</span>
                        <div className="truncate font-mono text-[0.72rem] text-muted" title={r.file}>
                          {r.file}{r.function ? ` :: ${r.function}` : r.span ? ` :: lines ${r.span}` : ""}</div>
                      </button>
                    </td>
                    <td className="px-2 py-1.5">
                      <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[0.7rem] ${LEVEL_CLS[r.level]}`}>{r.level}</span>
                    </td>
                    <td className="px-2 py-1.5">
                      <div className="flex max-w-[14rem] flex-wrap gap-1">
                        {r.missing.map((m) => <span key={m} className="rounded bg-surface-2 px-1.5 py-0.5 text-[0.68rem] text-muted">{labels[m] || m}</span>)}
                      </div>
                    </td>
                    <td className="max-w-[20rem] px-2 py-1.5 text-muted">
                      <div className="truncate" title={r.advisory}>
                        <span className="text-faint">advisory ({num(r.advisory_len)}): </span>{r.advisory || "—"}</div>
                      <div className="truncate" title={r.commit_message}>
                        <span className="text-faint">commit ({r.commit_state}): </span>{r.commit_message || "—"}</div>
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-faint">{r.enriched ? (r.llm_prompt_version || "yes") : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-x-5 gap-y-2">
        <div className="flex items-center gap-2 text-[0.78rem]">
          <Radio name="enrich-mode" checked={mode === "matching"} onChange={() => setMode("matching")} label="All matching pairs, at most" />
          <Input type="number" min={1} value={limit} aria-label="At most this many matching pairs" onChange={(e) => setLimit(e.target.value)} className="w-24 min-h-8" inputClassName="h-8 tnum" />
          <span className="text-faint">(in the order above)</span>
        </div>
        <Radio name="enrich-mode" checked={mode === "selected"} disabled={!sel.size} onChange={() => setMode("selected")} label={`Only the ${num(sel.size)} selected pairs`} />
        <label className="flex items-center gap-2 text-[0.78rem] text-muted">Parallel requests
          <Input type="number" min={1} max={8} value={workers} onChange={(e) => setWorkers(e.target.value)} className="w-20 min-h-8" inputClassName="h-8 tnum" />
        </label>
      </div>
      <div className="mt-3">
        <RunConsole stage="enrich_pairs" label={runLabel} params={params} onDone={done}
          disabled={mode === "matching" ? total === 0 : sel.size === 0}
          hint={`Uses the LLM engine from Settings (prompt ${data?.prompt_version || "enrich-v3"}). Click a pair to inspect it or enrich it alone.`} />
      </div>
      <div className="mt-5"><ContextExchangePanel ids={[...sel]} campaignId={campaignId} onDone={() => { setTick((t) => t + 1); onDone(); }} /></div>
      {campaignId && <CampaignContextItems campaignId={campaignId} />}
      {viewing != null && <PairWindow id={viewing} onOther={setViewing} onClose={() => { setViewing(null); setTick((t) => t + 1); }} />}
    </div>
  );
}
