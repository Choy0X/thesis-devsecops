import { useWorkspaceState } from "../lib/workspaceState";
import { useEffect, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { Button, EmptyState, ErrorState, Input, Panel, Skeleton } from "../components/ui";
import { Select } from "../components/kit";
import { useToast } from "../components/Toast";
import { useConfirm } from "../components/hud/AlertDialog";
import { DataTable, type Column, type SortState } from "../components/hud/DataTable";
import { Dialog, Sheet } from "../components/hud/Dialog";
import { SeverityBadge, type Severity } from "../components/hud/Badge";
import { Dev } from "../lib/devMode";
import { api, type DataRecord, type RecordFacets, type RecordRow } from "../lib/api";
import { useDataVersion } from "../lib/dataSync";
import { num } from "../lib/format";
import { isAbort } from "../lib/request";
import { FetchBox, ImportBox, RecordForm } from "../sections/Data";
import { CodeStatusChip, RecordDependencies, RecordFixCommits } from "../sections/RecordPairs";
import { YearSeverityPanel } from "../components/charts/Panels";

// The dataset records: a searchable, filterable, sortable table paged on the server, a drawer with everything about one record, and create, edit and delete with
// a question that says what will happen. The table holds only a few short fields per row (the server sends no long text); the whole record is read when a row is
// opened, and the drawer opens at once from what the row already has, with a skeleton where the rest will be. Internal ids are shown in Developer mode only.

const PAGE = 100;
const SEL = "w-auto min-w-[9rem]";
const INTENT_MS = 150;                         // a pointer resting this long on a row starts reading the record it opens

/** The record's severity word: the advisory's own label when it has one, otherwise the band of its CVSS score. */
export function severityOf(r: Pick<DataRecord, "cvss_score"> & { osv_severity?: string | null }): Severity | null {
  const label = (r.osv_severity ?? "").toUpperCase();
  if (label === "CRITICAL") return "critical";
  if (label === "HIGH") return "high";
  if (label === "MODERATE" || label === "MEDIUM") return "medium";
  if (label === "LOW") return "low";
  const s = r.cvss_score;
  if (s == null) return null;
  return s >= 9 ? "critical" : s >= 7 ? "high" : s >= 4 ? "medium" : "low";
}

const SORT_OF: Record<string, string> = { cve: "cve_id", ecosystem: "ecosystem", published: "published" };      // table column -> the sort the server knows
const SEVERITIES: [string, string][] = [["critical", "Critical"], ["high", "High"], ["medium", "Medium"], ["low", "Low"], ["unrated", "No severity given"]];
const CODE_STATUSES: [string, string][] = [["mined", "mined"], ["pending", "pending"], ["no_fix", "no fix commit"], ["no_code", "no code"], ["none", "not tried"]];
const DEP_STATUSES: [string, string][] = [["fixed", "fixed"], ["unfixed", "no fix yet"], ["none", "not checked"]];

const COLUMNS: Column<RecordRow>[] = [
  { key: "cve", label: "CVE", width: "9.5rem", sortValue: (r) => r.cve_id, render: (r) => <span className="font-mono text-[0.76rem]">{r.cve_id || r.osv_id || "—"}</span> },
  { key: "package", label: "Package", render: (r) => r.package || "—" },
  { key: "ecosystem", label: "Ecosystem", width: "7rem", sortValue: (r) => r.ecosystem, render: (r) => r.ecosystem || "—" },
  { key: "language", label: "Language", width: "7rem", render: (r) => r.language || "—" },
  { key: "severity", label: "Severity", width: "7.5rem", render: (r) => { const s = severityOf(r); return s ? <SeverityBadge level={s} /> : <span className="text-faint">—</span>; } },
  { key: "published", label: "Published", width: "6.5rem", sortValue: (r) => r.published, render: (r) => (r.published ? r.published.slice(0, 10) : "—") },
  { key: "fix", label: "Fix commits", width: "6rem", align: "right", render: (r) => String(r.fix_commit_count ?? 0) },
  { key: "code", label: "Code", width: "8rem", render: (r) => <CodeStatusChip status={r.code_status} pairs={r.pair_count} /> },
  { key: "pairs", label: "Pairs", width: "4.5rem", align: "right", render: (r) => String(r.pair_count ?? 0) },
];

/** One filter: the values the server offered with how many records each would give under the other filters; the chosen value stays listed even when it has none. */
function FilterSelect({ label, any, value, onChange, options, counts }: {
  label: string; any: string; value: string; onChange: (v: string) => void; options: [string, string][]; counts?: Record<string, number>;
}) {
  const listed = options.some(([v]) => v === value) || !value ? options : [...options, [value, value] as [string, string]];
  return (
    <Select aria-label={label} value={value} onValueChange={onChange} className={SEL}
      options={[{ value: "", label: any }, ...listed.map(([v, text]) => ({ value: v, label: counts && counts[v] != null ? `${text} (${num(counts[v])})` : text }))]} />
  );
}

/** What the drawer shows while the whole record is still on its way. */
function RecordSkeleton() {
  return (
    <div role="status" aria-label="Loading the whole record" className="space-y-4">
      <div className="space-y-2"><Skeleton className="h-3 w-24" /><Skeleton className="h-3 w-full" /><Skeleton className="h-3 w-5/6" /></div>
      <div className="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-2"><Skeleton className="h-3 w-16" /><Skeleton className="h-3 w-40" /><Skeleton className="h-3 w-12" /><Skeleton className="h-3 w-full" /><Skeleton className="h-3 w-20" /><Skeleton className="h-3 w-28" /></div>
      <div className="space-y-2"><Skeleton className="h-3 w-32" /><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /></div>
    </div>
  );
}

type Opened = { row: RecordRow; full: DataRecord | null; error: unknown };

export function RecordsPage() {
  const toast = useToast();
  const confirm = useConfirm();
  const [facets, setFacets] = useState<RecordFacets | null>(null);
  const [page, setPage] = useState<{ total: number; exact: boolean; records: RecordRow[] } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [search, setSearch] = useWorkspaceState("records.search", "");
  const [query, setQuery] = useWorkspaceState("records.query", "");
  const [language, setLanguage] = useWorkspaceState("records.language", "");
  const [status, setStatus] = useWorkspaceState("records.status", "");
  const [dep, setDep] = useWorkspaceState("records.dep", "");
  const [severity, setSeverity] = useWorkspaceState("records.severity", "");
  const [ecosystem, setEcosystem] = useWorkspaceState("records.ecosystem", "");
  const [sort, setSort] = useWorkspaceState<SortState>("records.sort", null);
  const [offset, setOffset] = useWorkspaceState("records.offset", 0);
  const [open, setOpen] = useState<Opened | null>(null);
  const [form, setForm] = useWorkspaceState<DataRecord | "new" | null>("records.form", null);
  const [adding, setAdding] = useState(false);
  const [overTime, setOverTime] = useState(false);
  const listSeq = useRef(0);
  const facetSeq = useRef(0);
  const fullSeq = useRef(0);
  const fullAbort = useRef<AbortController | null>(null);
  const intent = useRef<number | undefined>(undefined);

  const filters = { language, code_status: status, dependency_status: dep, severity, ecosystem, ...(sort ? { sort: SORT_OF[sort.key] ?? "id", dir: sort.dir } : { sort: "id", dir: "desc" as const }) };
  // An answer that comes after a newer question was asked is dropped, so a slow earlier request can never overwrite the rows of the filter on screen.
  const load = () => {
    const mine = ++listSeq.current;
    return api.records(query, PAGE, offset, filters)
      .then((p) => { if (mine === listSeq.current) { setPage({ total: p.total, exact: p.total_exact !== false, records: p.records }); setError(null); } })
      .catch((e) => { if (mine === listSeq.current && !isAbort(e)) setError(e); });
  };
  const loadFacets = () => {
    const mine = ++facetSeq.current;
    return api.recordFacets(query, filters).then((f) => { if (mine === facetSeq.current) setFacets(f); })
      .catch(() => { if (mine === facetSeq.current) setFacets(null); });                            // ignored: the filters then offer no counts
  };
  useEffect(() => { load(); }, [query, offset, language, status, dep, severity, ecosystem, sort]);       // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadFacets(); }, [query, language, status, dep, severity, ecosystem]);               // eslint-disable-line react-hooks/exhaustive-deps
  const v = useDataVersion(3000);
  useEffect(() => { if (v) { load(); loadFacets(); } }, [v]);                                            // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (search === query) return; const t = window.setTimeout(() => { setOffset(0); setQuery(search); }, 300); return () => window.clearTimeout(t); }, [search, query]);
  useEffect(() => () => { window.clearTimeout(intent.current); fullAbort.current?.abort(); }, []);
  const reload = () => { load(); loadFacets(); };
  const filter = (set: (v: string) => void) => (value: string) => { setOffset(0); set(value); };

  // Opening a record: the drawer is there at once with the row's own fields; the whole record fills it in when it arrives.
  const show = (row: RecordRow) => {
    fullAbort.current?.abort();
    const ctl = new AbortController();
    fullAbort.current = ctl;
    const mine = ++fullSeq.current;
    setOpen({ row, full: null, error: null });
    api.getRecord(row.id, ctl.signal)
      .then((full) => { if (mine === fullSeq.current) setOpen({ row, full, error: null }); })
      .catch((e) => { if (mine === fullSeq.current && !isAbort(e)) setOpen({ row, full: null, error: e }); });
  };
  const close = () => { fullAbort.current?.abort(); fullSeq.current++; setOpen(null); };
  // Resting the pointer (or the focus) on a row for a moment reads its record early; the request cache hands it to the drawer.
  const onIntent = (row: RecordRow | null) => {
    window.clearTimeout(intent.current);
    if (row) intent.current = window.setTimeout(() => { api.getRecord(row.id).catch(() => undefined /* ignored: an early read is only a head start; opening the row reads it again and shows the error */); }, INTENT_MS);
  };
  const remove = async (r: Pick<RecordRow, "id" | "cve_id" | "osv_id">) => {
    const label = r.cve_id || r.osv_id || `record ${r.id}`;
    const sure = await confirm({ title: `Delete ${label}?`, tone: "critical", confirmLabel: "Delete the record",
      description: "The record is removed from the dataset, and so are the code pairs made by hand for it. Pairs mined from fix commits stay, because they belong to the commit. This cannot be undone." });
    if (!sure) return;
    try { await api.deleteRecord(r.id); close(); reload(); toast.success("Record deleted."); } catch (e: any) { toast.error(`Delete failed: ${e?.message || e}`); }
  };

  const total = page?.total ?? 0;
  const exact = page?.exact ?? true;
  const shown = page?.records.length ?? 0;
  const from = shown ? offset + 1 : 0;
  const to = offset + shown;
  const hasNext = shown === PAGE && (!exact || offset + PAGE < total);
  const ecosystems = Object.entries(facets?.ecosystem ?? {}).sort((a, b) => b[1] - a[1]).map(([e]) => [e, e] as [string, string]);
  const languages = Object.keys(facets?.language ?? {}).map((l) => [l, l] as [string, string]);
  const none = !query && !language && !status && !dep && !severity && !ecosystem;
  const view = open ? (open.full ?? open.row) : null;

  return (
    <div className="space-y-5">
      <Panel title="Advisory records" desc="Open a record to inspect affected versions and fix evidence."
        right={<div className="flex flex-wrap gap-2"><Button variant="subtle" icon={<Plus size={14} aria-hidden />} onClick={() => setForm("new")}>Add a record</Button>
          <Button variant="ghost" onClick={() => setAdding((a) => !a)}>{adding ? "Hide fetch and import" : "Fetch or import"}</Button></div>}>
        {adding && <div className="mb-4 grid gap-3 sm:grid-cols-2"><FetchBox onDone={reload} /><ImportBox onDone={reload} /></div>}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="w-full min-w-0 lg:min-w-56 lg:flex-1"><Input placeholder="Search CVE, OSV ID, package, or summary" aria-label="Search records" value={search} onChange={(e) => setSearch(e.target.value)} /></div>
          <FilterSelect label="Severity" any="any severity" value={severity} onChange={filter(setSeverity)} options={SEVERITIES} counts={facets?.severity} />
          <FilterSelect label="Ecosystem" any="all ecosystems" value={ecosystem} onChange={filter(setEcosystem)} options={ecosystems} counts={facets?.ecosystem} />
          <FilterSelect label="Language" any="all languages" value={language} onChange={filter(setLanguage)} options={languages} counts={facets?.language} />
          <FilterSelect label="Code status" any="any code status" value={status} onChange={filter(setStatus)} options={CODE_STATUSES} counts={facets?.code_status} />
          <FilterSelect label="Dependency status" any="any dependency status" value={dep} onChange={filter(setDep)} options={DEP_STATUSES} counts={facets?.dependency_status} />
        </div>
        {error != null && !page ? <ErrorState error={error} title="Could not read the records" onRetry={load} /> : (
          <DataTable caption="Source records" columns={COLUMNS} rows={page?.records ?? []} rowKey={(r) => r.id} height={560} onRowClick={show} onRowIntent={onIntent}
            sort={sort} onSortChange={(s) => { setOffset(0); setSort(s); }} rowCount={exact ? total : -1} rowOffset={offset}
            empty={total === 0 && none
              ? <EmptyState title="No records yet" hint="Collect advisories on the CVE Collections page, fetch one by its CVE id, or import a file." />
              : "No record matches these filters."} />
        )}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[0.78rem] text-muted">
          <span className="tnum">{shown ? `${num(from)} to ${num(to)} of ${num(total)}${exact ? "" : "+"} records` : "0 records"}</span>
          <span className="flex gap-2">
            <Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</Button>
            <Button variant="ghost" disabled={!hasNext} onClick={() => setOffset(offset + PAGE)}>Next</Button>
          </span>
        </div>
      </Panel>

      <Panel title="Over time and severity" desc="How many vulnerabilities were published each year, split by severity."
        right={<Button variant="ghost" aria-expanded={overTime} onClick={() => setOverTime((o) => !o)}>{overTime ? "Hide" : "Show"}</Button>}>
        {overTime ? <YearSeverityPanel /> : <p className="text-[0.8rem] text-muted">Shown on request, so the table stays quick.</p>}
      </Panel>

      <Sheet open={open !== null} onOpenChange={(o) => { if (!o) close(); }} className="w-[min(40rem,96vw)]"
        title={open ? (open.row.cve_id || open.row.osv_id || "Record") : "Record"}
        description={open?.row.package ? `${open.row.package}${open.row.ecosystem ? ` · ${open.row.ecosystem}` : ""}` : undefined}>
        {open && view && (
          <div className="space-y-5 text-[0.84rem]">
            <div className="flex flex-wrap items-center gap-2">
              {(() => { const s = severityOf(view); return s ? <SeverityBadge level={s} /> : null; })()}
              <CodeStatusChip status={view.code_status} pairs={view.pair_count} />
              {view.published && <span className="text-muted">published {view.published.slice(0, 10)}</span>}
            </div>
            {view.summary && <p className="leading-relaxed">{view.summary}</p>}
            {open.full ? (
              <>
                {open.full.details && <details><summary className="cursor-pointer text-muted">Advisory text</summary><p className="mt-2 whitespace-pre-wrap leading-relaxed text-muted">{open.full.details}</p></details>}
                <dl className="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-1.5">
                  <dt className="text-muted">Weakness</dt><dd>{open.full.osv_cwe || open.full.cwe || "—"}</dd>
                  <dt className="text-muted">CVSS</dt><dd className="break-all font-mono text-[0.74rem]">{open.full.cvss_vector || "—"}{open.full.cvss_score != null ? ` (${open.full.cvss_score})` : ""}</dd>
                  <dt className="text-muted">Languages</dt><dd>{open.full.language || "not detected yet"}</dd>
                </dl>
                <RecordDependencies record={open.full} />
                <RecordFixCommits record={open.full} />
              </>
            ) : open.error != null ? <ErrorState error={open.error} title="Could not read the whole record" onRetry={() => show(open.row)} /> : <RecordSkeleton />}
            <Dev><p className="break-all font-mono text-[0.7rem] text-faint">id {open.row.id} · {open.row.osv_id} · {open.row.cve_id}</p></Dev>
            <div className="flex gap-2 border-t border-line pt-4">
              <Button disabled={!open.full} onClick={() => { if (open.full) { setForm(open.full); close(); } }}>Edit</Button>
              <Button variant="ghost" className="text-crit-soft" onClick={() => remove(open.row)}>Delete</Button>
            </div>
          </div>
        )}
      </Sheet>

      <Dialog open={form !== null} onOpenChange={(o) => { if (!o) setForm(null); }} title={form === "new" ? "Add a record" : "Edit the record"} className="max-h-[90vh]">
        {form !== null && (
          <RecordForm key={form === "new" ? "new" : form.id} record={form === "new" ? null : form} onCancel={() => setForm(null)}
            onSaved={() => { setForm(null); reload(); }} onPairsChange={load} />
        )}
      </Dialog>
    </div>
  );
}
