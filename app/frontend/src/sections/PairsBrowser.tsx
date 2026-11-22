import { useWorkspaceState } from "../lib/workspaceState";
import { Checkbox } from '../components/kit';
import { useEffect, useState } from "react";
import { Button, EmptyState, ErrorState, Input, Panel, Skeleton } from "../components/ui";
import { Select } from "../components/kit";
import { useToast } from "../components/Toast";
import { DataTable, type Column } from "../components/hud/DataTable";
import { Dialog } from "../components/hud/Dialog";
import { DiffViewer } from "../components/hud/DiffViewer";
import { CodeView } from "../components/hud/CodeView";
import { Tabs } from "../components/hud/Tabs";
import { Dev } from "../lib/devMode";
import { api, type CodePair } from "../lib/api";
import { useDataVersion } from "../lib/dataSync";
import { num } from "../lib/format";
import { commitUrl } from "../lib/links";
import { ContextExchangePanel } from "./ContextExchangePanel";

// The mined code pairs: a table paged on the server (newest first) and a window that shows one pair as a change (the diff), as the vulnerable code and as
// the fixed code, next to the context it was mined with. The commit and the internal ids appear in Developer mode only.

const PAGE = 100;
const SEL = "w-auto min-w-[9rem]";

export function PairStatus({ r }: { r: CodePair }) {
  const chip = (t: string, c: string) => <span className={`rounded-full border px-2 py-0.5 text-[0.7rem] ${c}`}>{t}</span>;
  if (!r.code_available) return chip(r.skip_reason || "unavailable", "border-line text-faint");
  if (!r.tangled && !r.is_test) return chip("usable", "border-ok-line text-ok");
  return (
    <span className="inline-flex gap-1">
      {!!r.tangled && chip("tangled", "border-warn-line text-warn")}
      {!!r.is_test && chip("test file", "border-line text-muted")}
    </span>
  );
}

const COLUMNS: Column<CodePair>[] = [
  { key: "cve", label: "CVE", width: "9.5rem", render: (p) => <span className="font-mono text-[0.76rem]">{p.cve_id || p.ghsa_id || "—"}</span> },
  { key: "package", label: "Package", width: "10rem", render: (p) => p.package || "—" },
  { key: "where", label: "File and function", render: (p) => <span className="font-mono text-[0.74rem]">{p.file_path}{p.function_name ? ` :: ${p.function_name}` : ""}</span> },
  { key: "language", label: "Language", width: "6.5rem", render: (p) => p.language || "—" },
  { key: "level", label: "Level", width: "5.5rem", render: (p) => p.granularity },
  { key: "status", label: "Status", width: "9rem", render: (p) => <PairStatus r={p} /> },
];

export function PairsBrowser({ languages }: { languages: string[] }) {
  const [selected, setSelected] = useWorkspaceState<Set<number>>("pairs.selected", new Set());
  const [page, setPage] = useState<{ total: number; records: CodePair[] } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [search, setSearch] = useWorkspaceState("pairs.search", "");
  const [query, setQuery] = useWorkspaceState("pairs.query", "");
  const [language, setLanguage] = useWorkspaceState("pairs.language", "");
  const [granularity, setGranularity] = useWorkspaceState("pairs.granularity", "");
  const [available, setAvailable] = useWorkspaceState("pairs.available", "");
  const [source, setSource] = useWorkspaceState("pairs.source", "");
  const [offset, setOffset] = useWorkspaceState("pairs.offset", 0);
  const [openId, setOpenId] = useWorkspaceState<number | null>("pairs.openId", null);

  const load = () => api.codePairs({ search: query, language, granularity, available, source }, PAGE, offset)
    .then((p) => { setPage(p); setError(null); }).catch(setError);
  useEffect(() => { load(); }, [query, language, granularity, available, source, offset]);      // eslint-disable-line react-hooks/exhaustive-deps
  const v = useDataVersion(5000);
  useEffect(() => { if (v) load(); }, [v]);                                                      // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const t = window.setTimeout(() => { setOffset(0); setQuery(search); }, 300); return () => window.clearTimeout(t); }, [search]);

  const total = page?.total ?? 0;
  const from = total ? offset + 1 : 0;
  const to = Math.min(total, offset + PAGE);
  const pick = (set: (v: string) => void) => (v: string) => { setOffset(0); set(v); };
  const nothingYet = total === 0 && !query && !language && !granularity && !available && !source;

  return (
    <Panel title="Code pairs" desc="Before and after code associated with advisory-linked commits. Open a pair to inspect its evidence. Dataset builds assess admission automatically.">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="min-w-[14rem] flex-1"><Input placeholder="Search CVE, file, function, package, repository…" aria-label="Search code pairs" value={search} onChange={(e) => setSearch(e.target.value)} /></div>
        <Select aria-label="Language" value={language} onValueChange={pick(setLanguage)} className={SEL} options={[{ value: "", label: "all languages" }, ...languages.map((l) => ({ value: l, label: l }))]} />
        <Select aria-label="Level" value={granularity} onValueChange={pick(setGranularity)} className={SEL}
          options={[{ value: "", label: "any level" }, { value: "function", label: "function" }, { value: "hunk", label: "hunk" }, { value: "file", label: "file" }]} />
        <Select aria-label="Usability" value={available} onValueChange={pick(setAvailable)} className={SEL}
          options={[{ value: "", label: "any status" }, { value: "usable", label: "usable for training" }, { value: "1", label: "code stored" }, { value: "0", label: "code not available" }]} />
        <Select aria-label="Source" value={source} onValueChange={pick(setSource)} className={SEL}
          options={[{ value: "", label: "any source" }, { value: "mined", label: "mined" }, { value: "manual", label: "made by hand" }]} />
      </div>
      {error != null && !page ? <ErrorState error={error} title="Could not read the code pairs" onRetry={load} /> : (
        <DataTable caption="Code pairs" columns={[{ key: 'select', label: 'Select', width: '4rem', render: (p) => <Checkbox aria-label={`Select pair ${p.id}`} checked={selected.has(p.id)} onClick={(e) => e.stopPropagation()} onChange={(e) => setSelected((previous) => { const next = new Set(previous); e.target.checked ? next.add(p.id) : next.delete(p.id); return next; })} /> }, ...COLUMNS]} rows={page?.records ?? []} rowKey={(p) => p.id} height={520} onRowClick={(p) => setOpenId(p.id)}
          empty={nothingYet
            ? <EmptyState title="No code pairs yet" hint="Use a campaign to collect code pairs, then review their source evidence here." />
            : "No code pair matches these filters."} />
      )}
      <div className="mt-3 flex items-center justify-between text-[0.78rem] text-muted">
        <span className="tnum">{total ? `${num(from)} to ${num(to)} of ${num(total)} pairs` : "0 pairs"}</span>
        <span className="flex gap-2">
          <Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</Button>
          <Button variant="ghost" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</Button>
        </span>
      </div>
      {openId !== null && <PairWindow id={openId} onClose={() => setOpenId(null)} onOther={setOpenId} />}
      {selected.size > 0 && <div className="mt-5"><ContextExchangePanel ids={[...selected]} onDone={load} /></div>}
    </Panel>
  );
}

/** One pair in a window: Change, Vulnerable, Fixed and Context, with the other pairs of the same advisory listed below. */
export function PairWindow({ id, onClose, onOther }: { id: number; onClose: () => void; onOther: (id: number) => void }) {
  const toast = useToast();
  const [p, setP] = useState<CodePair | null>(null);
  const [tab, setTab] = useState("diff");
  const [siblings, setSiblings] = useState<CodePair[]>([]);
  useEffect(() => {
    setP(null); setTab("diff");
    api.codePair(id).then(setP).catch((e: any) => { toast.error(`Could not open the pair: ${e?.message || e}`); onClose(); });
  }, [id]);                                                                                     // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setSiblings([]);
    if (!p?.cve_id) return;
    api.codePairs({ search: p.cve_id }, 12, 0).then((r) => setSiblings(r.records.filter((s) => s.id !== id && s.cve_id === p.cve_id))).catch(() => setSiblings([]));   // ignored: the list of other pairs is a convenience
  }, [p?.cve_id, id]);

  const link = p ? commitUrl(p.repo_url, p.fix_commit) : null;
  const synthetic = (t: string, v?: string | null) => v ? (
    <div className="mb-4"><div className="mb-1 flex items-center gap-2 text-[0.76rem] font-medium">{t}
      <span className="border border-warn-line px-1.5 text-[0.66rem] text-warn">written by a model{p?.llm_model ? ` (${p.llm_model})` : ""}</span></div>
      <p className="whitespace-pre-wrap text-[0.8rem] leading-relaxed text-muted">{v}</p></div>) : null;
  const real = (t: string, v?: string | null) => v ? (
    <div className="mb-4"><div className="mb-1 text-[0.76rem] font-medium">{t}</div><p className="whitespace-pre-wrap text-[0.8rem] leading-relaxed text-muted">{v}</p></div>) : null;

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={p ? (p.cve_id || p.ghsa_id || "Code pair") : "Code pair"}
      description={p ? `${p.language ?? "unknown language"} · ${p.granularity}${p.package ? ` · ${p.package}` : ""}` : undefined} className="w-[min(64rem,96vw)] max-h-[92vh]">
      {!p ? <Skeleton className="h-64 w-full" /> : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-[0.78rem]">
            <PairStatus r={p} />
            <span className="font-mono text-[0.74rem] text-muted">{p.file_path}{p.function_name ? ` :: ${p.function_name}` : ""}</span>
            {link && <a href={link} target="_blank" rel="noreferrer" className="text-brand-strong hover:underline">Open the fix commit</a>}
          </div>
          <Dev><p className="break-all font-mono text-[0.7rem] text-faint">pair {p.id} · fix {p.fix_commit} · parent {p.parent_commit} · {p.pair_key}</p></Dev>
          <Tabs value={tab} onValueChange={setTab} items={[
            { id: "diff", label: "Change", content: <DiffViewer diff={p.diff || ""} language={p.language} label="Change from vulnerable to fixed" /> },
            { id: "before", label: "Vulnerable", content: <CodeView text={p.code_before} language={p.language} label="Vulnerable code" /> },
            { id: "after", label: "Fixed", content: <CodeView text={p.code_after} language={p.language} label="Fixed code" /> },
            { id: "context", label: "Context", content: (
              <div>
                {real("Advisory text", p.explanation)}
                {real("Message of the fix commit", p.commit_message)}
                {synthetic("Root cause", p.root_cause_llm)}
                {synthetic("Secure pattern", p.secure_pattern_llm)}
                {synthetic("Explanation draft", p.explanation_llm)}
                {!p.explanation && !p.commit_message && !p.root_cause_llm && <p className="text-[0.8rem] text-muted">No context is stored for this pair.</p>}
              </div>) },
          ]} />
          {siblings.length > 0 && (
            <div className="border-t border-line pt-3">
              <div className="mb-1.5 text-[0.76rem] font-medium">Other pairs of {p.cve_id}</div>
              <ul className="flex flex-wrap gap-1.5">
                {siblings.map((s) => (
                  <li key={s.id}><button type="button" onClick={() => onOther(s.id)} className="border border-line bg-surface-2 px-2 py-1 font-mono text-[0.72rem] text-muted hover:border-brand hover:text-fg">
                    {s.file_path.split("/").pop()}{s.function_name ? ` :: ${s.function_name}` : ""}</button></li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}
