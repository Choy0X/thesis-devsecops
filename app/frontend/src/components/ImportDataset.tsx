import { useRef, useState } from "react";
import { api, errorText, type ExternalCheck } from "../lib/api";
import { Button } from "./ui";
import { FileInput, Input } from "./kit";
import { Dialog } from "./hud/Dialog";
import { RunConsole } from "./RunConsole";
import { HubDatasets } from "./HubDatasets";
import { num } from "../lib/format";

// Bringing the person's own training examples in as a dataset of its own. The file is sent to the app on this computer, read and checked row by row in words, and only a file without a
// problem can be imported. The result is labelled external: it has no review tier, it is never merged into a frozen version, and a run on it says so.

const ACCEPT = ".jsonl,.ndjson,.parquet,.csv";

/** A name for the dataset made from the file's name: lower-case letters, digits and underscores, starting with a letter or a digit. */
export const nameFrom = (filename: string): string => {
  const base = filename.replace(/\.[^.]*$/, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
  return /^[a-z0-9]/.test(base) ? base : `data_${base}`.replace(/_+$/, "").slice(0, 40);
};

export function ImportDataset({ onImported }: { onImported: (tag: string) => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [file, setFile] = useState<{ upload_id: string; filename: string; size_bytes: number } | null>(null);
  const [report, setReport] = useState<ExternalCheck | null>(null);
  const [name, setName] = useState("");
  const [hub, setHub] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const reset = () => { setFile(null); setReport(null); setError(""); setName(""); setHub(false); if (input.current) input.current.value = ""; };
  const close = () => {
    if (file) api.externalDiscard(file.upload_id).catch(() => undefined);              // ignored: a file left behind is only a file in the app's own uploads folder
    reset();
    setOpen(false);
  };
  const choose = async (f: File) => {
    reset();
    setBusy(true);
    try {
      const up = await api.externalUpload(f);
      setFile(up);
      setName(nameFrom(up.filename));
      setReport(await api.externalCheck(up.upload_id));
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };
  // A file fetched from the Hub is an upload like any other: it is checked in words, and only a file without a problem can be imported.
  const fetched = async (up: { upload_id: string; filename: string; size_bytes: number }) => {
    setHub(false);
    setBusy(true);
    try {
      setFile({ ...up, size_bytes: up.size_bytes });
      setName(nameFrom(up.filename));
      setReport(await api.externalCheck(up.upload_id));
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };
  const nameOk = /^[a-z0-9][a-z0-9_]{0,39}$/.test(name);

  return (
    <>
      <Button variant="ghost" onClick={() => setOpen(true)}>Import a file</Button>
      <Dialog open={open} onOpenChange={(o) => { if (!o) close(); }} title="Import training data"
        description="Your own examples, as JSONL, Parquet or CSV. They become a dataset of their own, labelled external.">
        <div className="space-y-4 text-[0.82rem]">
          <p className="text-muted">Each row is one example: a <b className="font-medium">task</b>, the <b className="font-medium">context_text</b> the model reads and the <b className="font-medium">response</b> it should write
            (as a JSON object, or a JSON string in a CSV). Optional: split, val_role, family_id, record_id, repository, pool, weight, language, ecosystem. Every answer is checked against the
            response contract the app's own data meets.</p>
          <div>
            <label className="mb-1 block text-[0.74rem] font-medium text-muted" htmlFor="import-file">File</label>
            <FileInput id="import-file" ref={input} accept={ACCEPT} disabled={busy} onChange={(e) => { const f = e.target.files?.[0]; if (f) void choose(f); }} />
            <p className="mt-1 text-[0.74rem] text-faint">It is read on this computer and only to make the dataset; nothing is sent anywhere.</p>
          </div>
          {!file && (
            <div>
              <Button variant="ghost" aria-expanded={hub} onClick={() => setHub(!hub)}>{hub ? "Hide Hugging Face" : "Find a dataset on Hugging Face"}</Button>
              {hub && <div className="mt-2"><HubDatasets onFetched={(up) => void fetched(up)} /></div>}
            </div>)}
          {busy && <p role="status" className="text-muted">Reading and checking the file...</p>}
          {error && <p role="alert" className="text-crit-soft">{error}</p>}

          {report && file && (
            <div className="space-y-3">
              <p>
                <b className="font-medium">{file.filename}</b> ({(file.size_bytes / 1048576).toFixed(1)} MB): {num(report.rows)} rows, {num(report.valid_rows)} usable{report.rows_with_problems ? `, ${num(report.rows_with_problems)} with a problem` : ""}.
                {report.valid_rows > 0 && <> {Object.entries(report.tasks).map(([t, n]) => `${t} ${num(n)}`).join(", ")}; {Object.entries(report.splits).map(([s, n]) => `${s} ${num(n)}`).join(", ")}; {num(report.families)} famil{report.families === 1 ? "y" : "ies"}.</>}
              </p>
              {report.problems.length > 0 && (
                <div role="alert">
                  <p className="font-medium text-crit-soft">{num(report.problems_total)} problem{report.problems_total === 1 ? "" : "s"} found, so nothing can be imported from this file{report.problems_total > report.problems.length ? ` (the first ${report.problems.length} are listed)` : ""}:</p>
                  <ul aria-label="Problems" className="mt-1 max-h-56 list-disc space-y-0.5 overflow-y-auto pl-5 text-muted">{report.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
                  <p className="mt-2 text-muted">Correct the file and choose it again.</p>
                </div>)}
              {report.warnings.length > 0 && <ul aria-label="Warnings" className="list-disc space-y-0.5 pl-5 text-warn">{report.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
              {report.ok && (
                <div className="space-y-3 border-t border-line pt-3">
                  <div>
                    <label className="mb-1 block text-[0.74rem] font-medium text-muted" htmlFor="import-name">Name of the dataset</label>
                    <Input id="import-name" aria-label="Name of the dataset" value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} inputClassName="font-mono" />
                    <p className="mt-1 text-[0.74rem] text-faint">It will be called dataset_ext_{name || "..."}. Lower-case letters, digits and underscores.</p>
                  </div>
                  <p className="text-muted">It is kept with the file's name, size and hash, the time and who imported it. It has no review tier, so it is never called reviewed or verified, and a run on it is not
                    comparable with a run on a frozen version.</p>
                  <RunConsole stage="import_external" label="Import" disabled={!nameOk} params={{ upload: file.upload_id, name }}
                    launch={() => api.externalImport(file.upload_id, name)} onDone={() => { setFile(null); onImported(`ext_${name}`); reset(); setOpen(false); }} />
                </div>)}
            </div>)}
        </div>
      </Dialog>
    </>
  );
}
