import { useEffect, useMemo, useState } from "react";
import { api, type AffectedPackage, type CodePair, type DataRecord, type RecordFixCommit } from "../lib/api";
import { Button, Input, Field, Textarea, Skeleton } from "../components/ui";
import { Checkbox, SelectField, TextareaField, Hint } from "../components/kit";
import { useToast } from "../components/Toast";
import { PairStatus, PairWindow } from "./PairsBrowser";
import { DiffViewer } from "../components/hud/DiffViewer";
import { useDataVersion } from "../lib/dataSync";
import { commitUrl, webUrl } from "../lib/links";
import { useConfirm } from "../components/hud/AlertDialog";

// The languages offered by the pair form, learned once (the same for every record).
let knownLanguages: string[] | null = null;

// The vulnerable -> fixed code pairs of one advisory record, editable by hand.
// Mined pairs can be corrected (they are then kept when the commit is re-mined);
// pairs can also be added manually (source = manual).
const FALLBACK_LANGS = ["python", "javascript", "typescript", "java", "go", "php", "ruby", "c", "cpp", "csharp", "rust"];

export function RecordPairs({ record, onChange }: { record: DataRecord; onChange?: () => void }) {
  const toast = useToast();
  const [pairs, setPairs] = useState<CodePair[] | null>(null);
  const [editing, setEditing] = useState<CodePair | "new" | null>(null);
  const [viewing, setViewing] = useState<number | null>(null);
  const [langs, setLangs] = useState<string[]>(FALLBACK_LANGS);

  const load = () => api.recordPairs(record.id).then(setPairs).catch(() => setPairs([]));
  const v = useDataVersion(3000);
  useEffect(() => { load(); }, [record.id, v]);
  useEffect(() => {                                // the language list is the same for every record: asked once, not per opened record
    if (knownLanguages) { setLangs(knownLanguages); return; }
    api.codeSummary().then((s) => { if (s.languages?.length) { knownLanguages = s.languages; setLangs(s.languages); } })
      .catch(() => undefined);   // ignored: the language filter is optional
  }, []);

  const confirm = useConfirm();
  const del = async (p: CodePair) => {
    const target = `${p.file_path}${p.function_name ? ` :: ${p.function_name}` : ""}`;
    if (!(await confirm({ title: "Delete this code pair?", description: target, confirmLabel: "Delete the pair", tone: "critical" }))) return;
    try { await api.deletePair(p.id); toast.success("Code pair deleted."); load(); onChange?.(); }
    catch (e: any) { toast.error(`Delete failed: ${e?.message || e}`); }
  };
  const [enriching, setEnriching] = useState(false);
  const enrichAll = async () => {
    setEnriching(true);
    try {
      const r = await api.enrichRecord(record.id);
      if (r.failed) toast.error(`Enriched ${r.enriched}, failed ${r.failed}: ${r.errors[0] || ""}`);
      else toast.success(`Enriched ${r.enriched} pair(s) with ${r.model} (synthetic fields).`);
      load();
    } catch (e: any) { toast.error(`LLM enrichment failed: ${String(e?.message || e).replace(/^\d+ /, "")}`); }
    finally { setEnriching(false); }
  };
  const edit = async (p: CodePair) => {
    try { setEditing(await api.codePair(p.id)); } catch (e: any) { toast.error(String(e?.message || e)); }
  };

  return (
    <div className="mt-4 rounded-lg border border-line bg-surface/60 p-3.5">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div>
          <div className="text-[0.82rem] font-medium">Code pairs (vulnerable → fixed)</div>
          <div className="text-[0.74rem] text-muted">Mined from the fix commit, or added by hand. Each pair feeds the code-pair dataset.</div>
        </div>
        {editing === null && <div className="flex gap-2">
          {!!pairs?.some((p) => p.code_available && !p.is_test) &&
            <Button variant="ghost" onClick={enrichAll} loading={enriching}
              title="Draft root cause / secure pattern / explanation for this record's pairs with the LLM">Enrich with LLM</Button>}
          <Button variant="subtle" onClick={() => setEditing("new")}>Add code pair</Button>
        </div>}
      </div>

      {editing !== null && (
        <PairEditor record={record} pair={editing === "new" ? null : editing} languages={langs}
          onCancel={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); onChange?.(); }} />
      )}

      {!pairs ? <Skeleton className="h-16 w-full" /> : pairs.length === 0 ? (
        <p className="py-2 text-[0.78rem] text-faint">No code pairs for this advisory yet. Mine its fix commit or add one by hand.</p>
      ) : (
        <div className="overflow-x-auto rounded-md border border-line">
          <table className="w-full text-[0.78rem]">
            <thead className="bg-surface-2 text-[0.7rem] text-muted"><tr>
              {["Location", "Language", "Level", "Status", "Context", "Source", ""].map((h) => <th key={h} className="px-2.5 py-1.5 text-left font-medium">{h}</th>)}
            </tr></thead>
            <tbody>
              {pairs.map((p) => (
                <tr key={p.id} className="border-t border-line">
                  <td className="max-w-[22rem] truncate px-2.5 py-1.5 font-mono text-[0.72rem]" title={p.file_path}>
                    {p.file_path}{p.function_name && <span className="text-brand-strong"> :: {p.function_name}</span>}
                    {!p.function_name && p.span && <span className="text-faint"> :: lines {p.span}</span>}
                    {p.fix_check === "suspect" && <Hint label="the fix commit fails the release-date check" className="ml-1.5 text-[0.7rem] text-crit-soft">suspect fix</Hint>}</td>
                  <td className="px-2.5 py-1.5">{p.language || "—"}</td>
                  <td className="px-2.5 py-1.5 text-muted">{p.granularity}</td>
                  <td className="px-2.5 py-1.5"><PairStatus r={p} /></td>
                  <td className="whitespace-nowrap px-2.5 py-1.5 text-[0.7rem]"><Hint label="advisory text · fix commit message · LLM draft" className="no-underline">
                    {[["advisory", p.has_advisory], ["msg", p.has_msg], ["LLM", p.has_llm]].map(([t, on]) => (
                      <span key={t as string} className={`mr-1.5 ${on ? "text-ok" : "text-faint"}`}>{on ? "✓" : "·"} {t}</span>
                    ))}</Hint></td>
                  <td className="px-2.5 py-1.5 text-muted">{p.source}{p.edited_at && p.source !== "manual" ? " · edited" : ""}</td>
                  <td className="whitespace-nowrap px-2.5 py-1.5 text-right">
                    <button type="button" className="mr-2.5 text-brand-strong hover:underline" onClick={() => setViewing(p.id)}>view</button>
                    <button type="button" className="mr-2.5 text-brand-strong hover:underline" onClick={() => edit(p)}>edit</button>
                    <button type="button" className="text-crit-soft hover:underline" onClick={() => del(p)}>delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {viewing != null && <PairWindow id={viewing} onOther={setViewing} onClose={() => setViewing(null)} />}
    </div>
  );
}

type Draft = {
  language: string; file_path: string; function_name: string; granularity: string; cwe: string;
  repo_url: string; fix_commit: string; parent_commit: string; is_test: boolean;
  explanation: string; code_before: string; code_after: string;
};

function PairEditor({ record, pair, languages, onSaved, onCancel }:
  { record: DataRecord; pair: CodePair | null; languages: string[]; onSaved: () => void; onCancel: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [d, setD] = useState<Draft>(() => ({
    language: pair?.language || "python", file_path: pair?.file_path || "",
    function_name: pair?.function_name || "", granularity: pair?.granularity || "function",
    cwe: pair?.cwe || record.osv_cwe || record.cwe || "", repo_url: pair?.repo_url || "",
    fix_commit: pair?.fix_commit || "", parent_commit: pair?.parent_commit || "",
    is_test: !!pair?.is_test, explanation: pair?.explanation ?? (record.summary || ""),
    code_before: pair?.code_before || "", code_after: pair?.code_after || "",
  }));
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((cur) => ({ ...cur, [k]: v }));
  const preview = useMemo(() => lineDiff(d.code_before, d.code_after), [d.code_before, d.code_after]);

  const save = async () => {
    if (!d.code_before.trim() || !d.code_after.trim()) {
      toast.error("Both the vulnerable and the fixed code are needed."); return;
    }
    setBusy(true);
    try {
      const body = { ...d, is_test: d.is_test ? 1 : 0 };
      if (pair) await api.updatePair(pair.id, body);
      else await api.createPair({ ...body, cve_id: record.cve_id, osv_id: record.osv_id,
        package: record.package, published: record.published, cvss_vector: record.cvss_vector });
      toast.success(pair ? "Code pair updated." : "Code pair added.");
      onSaved();
    } catch (e: any) { toast.error(`Save failed: ${e?.message || e}`); }
    finally { setBusy(false); }
  };

  const code = "font-mono text-[0.74rem] leading-[1.5]";
  return (
    <div className="mb-3 rounded-lg border border-brand/40 bg-brand-tint p-3.5">
      <div className="mb-2.5 text-[0.8rem] font-medium">{pair ? "Edit code pair" : "Add code pair"}
        {pair && pair.source !== "manual" && <span className="ml-2 text-[0.72rem] font-normal text-muted">
          Mined pair: your edit is kept if its commit is mined again.</span>}</div>
      <div className="grid gap-2.5 sm:grid-cols-4">
        <SelectField label="Language" value={d.language} onValueChange={(v) => set("language", v)} options={languages.map((l) => ({ value: l, label: l }))} />
        <Field label="File path"><Input value={d.file_path} onChange={(e) => set("file_path", e.target.value)} placeholder="pkg/module.py" /></Field>
        <Field label="Function"><Input value={d.function_name} onChange={(e) => set("function_name", e.target.value)} placeholder="Class.method (optional)" /></Field>
        <SelectField label="Level" value={d.granularity} onValueChange={(v) => set("granularity", v)}
          options={[{ value: "function", label: "function" }, { value: "hunk", label: "hunk" }, { value: "file", label: "file" }]} />
        <Field label="CWE"><Input value={d.cwe} onChange={(e) => set("cwe", e.target.value)} placeholder="CWE-22" /></Field>
        <Field label="Repository URL"><Input value={d.repo_url} onChange={(e) => set("repo_url", e.target.value)} placeholder="https://github.com/org/repo" /></Field>
        <Field label="Fix commit"><Input value={d.fix_commit} onChange={(e) => set("fix_commit", e.target.value)} placeholder="sha (optional)" /></Field>
        <Field label="Parent commit"><Input value={d.parent_commit} onChange={(e) => set("parent_commit", e.target.value)} placeholder="sha (optional)" /></Field>
      </div>
      <Checkbox className="mt-2" checked={d.is_test} onCheckedChange={(v) => set("is_test", v)} label="Test file (excluded from training by default)" />
      <Field label="Explanation (advisory text)"><Textarea value={d.explanation} onChange={(e) => set("explanation", e.target.value)} /></Field>
      <div className="mt-2 grid gap-2.5 lg:grid-cols-2">
        <TextareaField label={<span className="text-diff-del">Vulnerable code (before the fix)</span>} value={d.code_before} onChange={(e) => set("code_before", e.target.value)} rows={12} spellCheck={false} inputClassName={code} />
        <TextareaField label={<span className="text-diff-add">Fixed code (after the fix)</span>} value={d.code_after} onChange={(e) => set("code_after", e.target.value)} rows={12} spellCheck={false} inputClassName={code} />
      </div>
      {(d.code_before || d.code_after) && (
        <div className="mt-2.5">
          <div className="mb-1 text-[0.74rem] text-muted">Diff preview (stored diff is recomputed on save)</div>
          {preview === null
            ? <p className="text-[0.8rem] text-muted">Too large to preview here; the diff is computed when you save.</p>
            : <div className="max-h-56 overflow-auto"><DiffViewer diff={preview} language={d.language} height={224} label="Preview of the change" /></div>}
        </div>
      )}
      <div className="mt-3 flex gap-2">
        <Button variant="primary" onClick={save} loading={busy}>{pair ? "Save pair" : "Add pair"}</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

/** A minimal line diff (LCS) for the live preview, as one unified hunk so the viewer can number and colour the lines; null when the texts are too large to compare here. */
export function lineDiff(before: string, after: string): string | null {
  const a = before ? before.split("\n") : [], b = after ? after.split("\n") : [];
  if (a.length * b.length > 4_000_000) return null;
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: string[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(" " + a[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push("-" + a[i++]);
    else out.push("+" + b[j++]);
  }
  while (i < n) out.push("-" + a[i++]);
  while (j < m) out.push("+" + b[j++]);
  return [`@@ -${n ? 1 : 0},${n} +${m ? 1 : 0},${m} @@`, ...out].join("\n");
}

// ── code status chip (records table) ─────────────────────────────────────────
export function CodeStatusChip({ status, pairs }: { status?: string | null; pairs?: number | null }) {
  const chip = (t: string, c: string) =>
    <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[0.7rem] ${c}`}>{t}</span>;
  switch (status) {
    case "mined": return chip(`${pairs ?? 0} pair${pairs === 1 ? "" : "s"}`, "border-ok-line text-ok hover:bg-ok-tint-strong");
    case "pending": return chip("pending", "border-warn-line text-warn");
    case "no_code": return chip("no code", "border-line text-muted");
    case "failed": return chip("failed", "border-crit-line text-crit-soft");
    case "no_fix": return chip("no fix commit", "border-line text-faint");
    case "suspect": return chip("suspect fix", "border-crit-line text-crit-soft");
    default: return <span className="text-faint">—</span>;
  }
}

export function FixCheckChip({ check, reason }: { check?: string | null; reason?: string | null }) {
  if (!check) return null;
  const cls = check === "verified" ? "border-ok-line text-ok"
    : check === "suspect" ? "border-crit-line text-crit-soft" : "border-line text-faint";
  // The check compares release dates only; it does not prove the commit fixes the vulnerability.
  const label = check === "verified" ? "release-date plausible" : check === "suspect" ? "release-date suspect" : "not checked";
  return <Hint label={reason || "Compares the commit date with the affected and fixed releases; it does not prove the fix."} className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[0.7rem] no-underline ${cls}`}>{label}</Hint>;
}

// ── the record's fix commits and what mining found in each ───────────────────
export function RecordFixCommits({ record }: { record: DataRecord }) {
  const [rows, setRows] = useState<RecordFixCommit[] | null>(null);
  const v = useDataVersion(3000);
  useEffect(() => { api.recordFixCommits(record.id).then(setRows).catch(() => setRows([])); }, [record.id, v]);
  const link = (c: RecordFixCommit) => commitUrl(c.repo_url, c.sha) ?? webUrl(c.repo_url) ?? undefined;
  return (
    <div className="mt-4 rounded-lg border border-line bg-surface/60 p-3.5">
      <div className="text-[0.82rem] font-medium">Fix commits</div>
      <div className="mb-2 text-[0.74rem] text-muted">From the advisory (OSV) and its CVE's NVD references. Each is mined in the CVE's language, detected from git, and checked against the package's release dates: a fix is committed after the last affected release and by the fixed one.</div>
      {!rows ? <Skeleton className="h-10 w-full" /> : rows.length === 0 ? (
        <p className="text-[0.78rem] text-faint">No fix commit known for this advisory. Run "Extract fix commits", or add a code pair by hand.</p>
      ) : (
        <ul className="space-y-1.5">
          {rows.map((c) => {
            const langs = c.detected_langs ? JSON.parse(c.detected_langs).join(", ") : null;
            return (
              <li key={c.commit_key} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.78rem]">
                <a href={link(c)} target="_blank" rel="noreferrer" className="font-mono text-[0.74rem] text-brand-strong hover:underline">
                  {c.owner}/{c.repo}@{(c.sha || "").slice(0, 10)}</a>
                <span className="text-muted">{c.status}{c.pairs ? ` · ${c.pairs} pairs` : ""}</span>
                <FixCheckChip check={c.fix_check} reason={c.fix_check_reason} />
                {langs && <span className="text-faint">language: {langs}</span>}
                {c.error && c.status !== "mined" && <span className="truncate text-faint" title={c.error}>{c.error}</span>}
                <Hint label={c.link === "declared" ? "the advisory declares this commit as the fix" : "the advisory only links this commit"} className="text-faint">
                  {c.source || c.via}{c.link ? ` · ${c.link}` : ""}</Hint>
                {c.fix_check === "suspect" && c.fix_check_reason &&
                  <div className="basis-full pl-1 text-[0.74rem] text-crit-soft">{c.fix_check_reason} - its pairs are not used.</div>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ── dependency versions (records table + record form) ────────────────────────
export function DepStatusChip({ status, safe, fixed }: { status?: string | null; safe?: string | null; fixed?: string | null }) {
  const chip = (t: string, c: string, title: string) =>
    <Hint label={title} className={`whitespace-nowrap rounded-full border px-2 py-0.5 font-mono text-[0.7rem] no-underline ${c}`}>{t}</Hint>;
  switch (status) {
    case "fixed": return chip(safe || fixed || "fixed", "border-ok-line text-ok",
      `fixed release exists${fixed ? ` (first fixed ${fixed})` : ""}; latest safe ${safe || "—"}`);
    case "safe_newer": return chip(safe || "newer", "border-brand-line text-brand-strong", "no fix event, but a newer release is not affected");
    case "unsafe": return chip("unsafe", "border-crit-line text-crit-soft", "no fixed release and no unaffected newer release");
    case "unknown": return chip("unknown", "border-line text-faint", "no usable version data");
    default: return <span className="text-faint">—</span>;
  }
}

export function describeRanges(e: AffectedPackage): string {
  const parts = (e.ranges || []).map((r) => {
    const lo = !r.introduced || r.introduced === "0" ? "" : `>=${r.introduced}, `;
    if (r.fixed) return `${lo}<${r.fixed}`;
    if (r.last_affected) return `${lo}<=${r.last_affected}`;
    return `${lo ? lo.slice(0, -2) : ">=0"} (no fix)`;
  });
  if (!parts.length && e.versions_total) parts.push(`${e.versions_total} listed version${e.versions_total === 1 ? "" : "s"}`);
  return parts.join(" · ") || "—";
}

export function RecordDependencies({ record }: { record: DataRecord }) {
  const toast = useToast();
  const [rec, setRec] = useState<DataRecord>(record);
  const [busy, setBusy] = useState(false);
  useEffect(() => setRec(record), [record]);
  let pkgs: AffectedPackage[] = [];
  try { pkgs = JSON.parse(rec.affected || "[]"); } catch { pkgs = []; }
  const refresh = async () => {
    setBusy(true);
    try { setRec(await api.refreshDepVersions(record.id)); toast.success("Dependency versions updated."); }
    catch (e: any) { toast.error(`Version check failed: ${String(e?.message || e).replace(/^\d+ /, "")}`); }
    finally { setBusy(false); }
  };
  return (
    <div className="mt-4 rounded-lg border border-line bg-surface/60 p-3.5">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div>
          <div className="text-[0.82rem] font-medium">Dependency versions</div>
          <div className="text-[0.74rem] text-muted">Affected ranges from the advisory; the safe version is the newest published release outside every range (package registry).
            {rec.deps_checked_at && <> Checked {new Date(rec.deps_checked_at).toLocaleString()}.</>}</div>
        </div>
        <Button variant="ghost" onClick={refresh} loading={busy}>Refresh versions</Button>
      </div>
      {pkgs.length === 0 ? <p className="text-[0.78rem] text-faint">No version data yet. Mine runs this check, or click Refresh versions.</p> : (
        <div className="overflow-x-auto rounded-md border border-line">
          <table className="w-full text-[0.76rem]">
            <thead className="bg-surface-2 text-[0.7rem] text-muted"><tr>
              {["Package", "Affected", "First fixed", "Safe version", "Latest release", "Status"].map((h) => <th key={h} className="px-2.5 py-1.5 text-left font-medium">{h}</th>)}
            </tr></thead>
            <tbody>
              {pkgs.map((p, i) => (
                <tr key={i} className="border-t border-line">
                  <td className="px-2.5 py-1.5"><span className="text-faint">{p.ecosystem} · </span><span className="font-mono">{p.package}</span></td>
                  <td className="px-2.5 py-1.5 font-mono text-[0.72rem]">{describeRanges(p)}</td>
                  <td className="px-2.5 py-1.5 font-mono">{p.first_fixed || "—"}</td>
                  <td className="px-2.5 py-1.5 font-mono">{p.safe_version || "—"}{p.safe_is_prerelease && <span className="text-faint"> (pre-release)</span>}</td>
                  <td className="px-2.5 py-1.5 font-mono text-muted">{p.latest_version || (p.registry === false ? "registry n/a" : "—")}</td>
                  <td className="px-2.5 py-1.5"><DepStatusChip status={p.status} safe={p.safe_version} fixed={p.first_fixed} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
