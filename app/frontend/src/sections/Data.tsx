import { useEffect, useState } from "react";
import { api, type DataRecord } from "../lib/api";
import { Panel, SevBar, Skeleton, Button, Input, Field, Textarea, EmptyState } from "../components/ui";
import { FileInput, Select } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { Icon } from "../components/Icon";
import { useToast } from "../components/Toast";
import { num } from "../lib/format";
import { RecordPairs, RecordFixCommits, RecordDependencies } from "./RecordPairs";

// ── The form of one record (used by the Records page) ────────────────────────
const BLANK: Partial<DataRecord> = {
  cve_id: "", osv_id: "", package: "", summary: "", published: "",
  cvss_vector: "", cvss_score: null, osv_cwe: "",
};

export function RecordForm({ record, onSaved, onCancel, onPairsChange }:
  { record: DataRecord | null; onSaved: (saved: DataRecord, created: boolean) => void; onCancel: () => void;
    onPairsChange?: () => void }) {
  const toast = useToast();
  const [f, setF] = useState<Partial<DataRecord>>(record ? { ...record } : { ...BLANK });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k: keyof DataRecord, v: any) => setF((cur) => ({ ...cur, [k]: v }));

  const save = async () => {
    setBusy(true); setErr("");
    try {
      const body: any = { ...f };
      body.cvss_score = f.cvss_score === null || f.cvss_score === undefined || (f.cvss_score as any) === ""
        ? null : Number(f.cvss_score);
      const saved = record ? await api.updateRecord(record.id, body) : await api.addRecord(body);
      toast.success(record ? `Record #${record.id} updated.`
        : "Record added. You can now add its code pairs below.");
      onSaved(saved, !record);
    } catch (e: any) { setErr(String(e?.message || e)); toast.error(`Save failed: ${e?.message || e}`); }
    finally { setBusy(false); }
  };

  return (
    <div className="mb-4 rounded-lg border border-brand/40 bg-brand-tint p-4">
      <div className="mb-3 text-[0.82rem] font-medium">{record ? `Edit record #${record.id}` : "Add record"}</div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="CVE id"><Input value={f.cve_id ?? ""} onChange={(e) => set("cve_id", e.target.value)} placeholder="CVE-2025-1234" /></Field>
        <Field label="OSV id"><Input value={f.osv_id ?? ""} onChange={(e) => set("osv_id", e.target.value)} placeholder="GHSA-xxxx (optional)" /></Field>
        <Field label="Package"><Input value={f.package ?? ""} onChange={(e) => set("package", e.target.value)} /></Field>
        <Field label="Published"><Input type="date" value={(f.published ?? "").slice(0, 10)} onChange={(e) => set("published", e.target.value)} /></Field>
        <Field label="CVSS vector" hint="Score is computed from the vector at build time if left blank.">
          <Input value={f.cvss_vector ?? ""} onChange={(e) => set("cvss_vector", e.target.value)} placeholder="CVSS:3.1/AV:N/…" /></Field>
        <Field label="CVSS score (manual)"><Input type="number" step="0.1" min="0" max="10"
          value={f.cvss_score ?? ""} onChange={(e) => set("cvss_score", e.target.value)} /></Field>
        <Field label="CWE"><Input value={f.osv_cwe ?? ""} onChange={(e) => set("osv_cwe", e.target.value)} placeholder="CWE-79" /></Field>
        <Field label="Language of the vulnerable code"
          hint={record?.language_source === "manual" ? "Set by hand; clear it to detect from git again."
            : "Detected from git when the fix commit is mined; type a language to override (its commits are mined again)."}>
          <Input value={f.language ?? ""} onChange={(e) => set("language", e.target.value)} placeholder="detected from git" /></Field>
      </div>
      <Field label="Summary"><Textarea value={f.summary ?? ""} onChange={(e) => set("summary", e.target.value)} /></Field>
      {err && <p className="mt-2 text-[0.8rem] text-crit-soft">{err}</p>}
      <div className="mt-3 flex gap-2">
        <Button variant="primary" onClick={save} loading={busy}>{record ? "Save changes" : "Add"}</Button>
        <Button variant="ghost" onClick={onCancel}>{record ? "Close" : "Cancel"}</Button>
      </div>
      {record && <RecordDependencies record={record} />}
      {record && <RecordFixCommits record={record} />}
      {record ? <RecordPairs record={record} onChange={onPairsChange} /> : (
        <p className="mt-3 text-[0.76rem] text-muted">After adding the record you can attach its vulnerable → fixed code pairs (needs a CVE or OSV id).</p>
      )}
    </div>
  );
}

export function FetchBox({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const [mode, setMode] = useState<"package" | "cve_id">("package");
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState(false);

  const go = async () => {
    if (!val.trim()) return;
    setBusy(true);
    try {
      const r = await api.fetchOsv({ [mode]: val.trim() } as any);
      if (r.added) toast.success(`Fetched ${r.added} record(s) from OSV.`);
      else toast.info("No matching advisories found on OSV.");
      setVal(""); onDone();
    } catch (e: any) { toast.error(`OSV fetch failed: ${e?.message || e}`); }
    finally { setBusy(false); }
  };

  return (
    <div className="rounded-lg border border-line bg-surface-2 p-3">
      <div className="mb-2 text-[0.78rem] font-medium">Fetch from OSV (live)</div>
      <div className="flex gap-2">
        <Select aria-label="Fetch by" value={mode} onValueChange={(v) => setMode(v as any)} className="w-32 shrink-0" options={[{ value: "package", label: "package" }, { value: "cve_id", label: "CVE id" }]} />
        <Input value={val} onChange={(e) => setVal(e.target.value)}
          placeholder={mode === "package" ? "requests" : "CVE-2023-32681"}
          onKeyDown={(e) => { if (e.key === "Enter") go(); }} />
        <Button variant="subtle" onClick={go} loading={busy}>Fetch</Button>
      </div>
    </div>
  );
}

export function ImportBox({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const onFile = async (file: File) => {
    setBusy(true);
    try {
      const content = await file.text();
      const format = file.name.toLowerCase().endsWith(".csv") ? "csv" : "json";
      const r = await api.importRecords(format, content);
      toast.success(`Imported ${r.imported} record(s). Store total: ${r.total}.`);
      onDone();
    } catch (e: any) { toast.error(`Import failed: ${e?.message || e}`); }
    finally { setBusy(false); }
  };

  return (
    <div className="rounded-lg border border-line bg-surface-2 p-3">
      <div className="mb-2 text-[0.78rem] font-medium">Import records (CSV / JSON)</div>
      <FileInput accept=".csv,.json" aria-label="Records file (CSV or JSON)" disabled={busy} buttonLabel={busy ? "Importing..." : "Choose file"}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.currentTarget.value = ""; }} />
    </div>
  );
}

// ── Severity-classification dataset (one row per CVE, label from CVSS) ───────
export function SeverityDatasetPanel({ onDone }: { onDone: () => void }) {
  const [next, setNext] = useState("");           // the next unused dataset version, from the server
  useEffect(() => { api.builds().then((b) => setNext(b.next_versions.dataset)).catch(() => undefined); }, []);   // ignored: the button then says 'Freeze' without a number
  const [profile, setProfile] = useState<"training" | "final">("training");
  const [valPct, setValPct] = useState(5);
  const [advanced, setAdvanced] = useState(false);
  const trainAll = profile === "final";
  const params = { val_share: valPct / 100, train_all: trainAll };
  const card = (id: "training" | "final", name: string, desc: string) => (
    <button type="button" onClick={() => setProfile(id)} aria-pressed={profile === id}
      className={`rounded-lg border p-3 text-left transition-colors ${profile === id
        ? "border-brand bg-brand/10" : "border-line bg-surface-2/50 hover:border-line-strong"}`}>
      <div className="flex items-center gap-2 text-[0.82rem] font-medium">
        <span className={`h-3 w-3 shrink-0 rounded-full border ${profile === id ? "border-brand bg-brand" : "border-line-strong"}`} />{name}</div>
      <div className="mt-1 text-[0.72rem] leading-snug text-muted">{desc}</div>
    </button>
  );
  return (
    <Panel title="Severity-classification dataset"
      desc="One row per CVE (its GHSA / PYSEC advisories merged), labelled Recommendation / Warning / Critical from CVSS. Writes data/processed/dataset_preview.parquet (freeze: the next unused dataset_v<N>.parquet).">
      <div className="mb-2 grid gap-2 sm:grid-cols-2">
        {card("training", "Training (recommended)", `Every row trains except a ${valPct}% validation slice of whole packages, used for early stopping.`)}
        {card("final", "Final model", "Validation folded into training. For the last run only, with the run_config.json of a Training run (FINAL_FROM).")}
      </div>
      <button type="button" onClick={() => setAdvanced(!advanced)} className="mb-2 text-[0.76rem] text-brand-strong hover:underline">
        {advanced ? "Hide advanced options" : "Advanced options"}</button>
      {advanced && (
        <label className="mb-3 flex items-center gap-2 text-[0.8rem]">Validation share
          <Input type="number" min={1} max={30} disabled={trainAll} value={valPct} onChange={(e) => setValPct(Math.min(30, Math.max(1, Number(e.target.value) || 5)))}
            className="w-24 min-h-8" inputClassName="h-8 tnum" suffix="%" /></label>
      )}
      <div className="mt-2 grid gap-4 lg:grid-cols-2">
        <RunConsole stage="dataset" label="Build preview" params={params} onDone={onDone}
          hint="Writes dataset_preview.parquet; rebuild any time." />
        <RunConsole stage="dataset" label={next ? `Freeze as dataset_${next}` : "Freeze"} params={{ ...params, freeze: true }} onDone={onDone}
          hint={`Writes dataset_${next || "<next version>"}.parquet and records its hashes.`}
          confirmText={`Freeze dataset_${next || "<next version>"} with these settings? Freezing records the hashes of its files; a later change becomes the next version.`} />
      </div>
    </Panel>
  );
}

// ── Built-dataset snapshot (real stats; empty until a build runs) ────────────
export function SnapshotPanel({ stats }: { stats: any }) {
  const splits = ["train", "val"];
  const dist = stats?.class_distribution_per_split || {};
  return (
    <Panel title="Dataset snapshot" desc="Computed from the last build (preview or frozen v1).">
      {!stats ? <Skeleton className="h-28 w-full" /> : !stats.records_final ? (
        <EmptyState icon={<Icon.data />} title="No dataset built yet"
          hint="Add or ingest records, then build the dataset to compute real statistics." />
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-4">
            {[["records", stats.records_final], ["with CWE", stats.records_with_cwe],
              ["advisories merged", stats.duplicate_advisories_merged ?? stats.dedup_removed], ["with code", stats.records_with_code]].map(([k, v]) => (
              <div key={k as string} className="bg-surface px-4 py-3">
                <div className="text-[0.72rem] text-muted">{k}</div>
                <div className="tnum mt-0.5 text-[1.1rem] font-semibold">{num(v as number)}</div>
              </div>
            ))}
          </div>
          <div className="space-y-2.5">
            {splits.map((sp) => {
              const d = dist[sp] || {};
              const totalN = (d.Recommendation || 0) + (d.Warning || 0) + (d.Critical || 0);
              return (
                <div key={sp}>
                  <div className="mb-1 flex items-center justify-between text-[0.78rem]">
                    <span className="font-medium capitalize">{sp}</span>
                    <span className="tnum text-faint">{num(totalN)}</span>
                  </div>
                  <SevBar rec={d.Recommendation || 0} warn={d.Warning || 0} crit={d.Critical || 0} />
                </div>
              );
            })}
          </div>
          <Legend />
        </div>
      )}
    </Panel>
  );
}

function Legend() {
  return (
    <div className="flex gap-4 text-[0.74rem] text-muted">
      {[["Recommendation", "bg-info"], ["Warning", "bg-warn"], ["Critical", "bg-crit"]].map(([l, c]) => (
        <span key={l} className="inline-flex items-center gap-1.5"><span className={`h-2 w-2 rounded-full ${c}`} />{l}</span>
      ))}
    </div>
  );
}
