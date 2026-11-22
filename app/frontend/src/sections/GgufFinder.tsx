import { useEffect, useState } from "react";
import { api, errorText, type GgufVersions } from "../lib/api";
import { Button, EmptyState, Field, Input, Panel } from "../components/ui";
import { gigabytes } from "../lib/fitWords";
import { num } from "../lib/format";
import { HubDownload } from "./ModelHub";

// A base model comes as safetensors, which is what fine-tuning reads, and the local server needs a quantised GGUF copy of it. Others have converted many models
// already: this page looks for them on the Hub (metadata only) and shows, before anything is downloaded, what each quantisation costs on the disk next to the
// copy that is already there or would be needed for training.

export type Row = { repo: string; quant: string; bytes: number; withTraining: number | null; room: "yes" | "no" | "unknown" };

/** Every quantisation of every candidate as one row, the smaller first, with its size next to the training copy and whether the disk has room for it. */
export function rows(v: GgufVersions): Row[] {
  const out: Row[] = [];
  for (const c of v.candidates) {
    for (const [quant, bytes] of Object.entries(c.quants)) {
      const withTraining = v.training_bytes == null ? null : bytes + v.training_bytes;
      out.push({ repo: c.id, quant, bytes, withTraining, room: v.free_bytes == null ? "unknown" : bytes * 1.1 <= v.free_bytes ? "yes" : "no" });
    }
  }
  return out.sort((a, b) => a.repo.localeCompare(b.repo) || a.bytes - b.bytes);
}

export function GgufFinder() {
  const [base, setBase] = useState("");
  const [found, setFound] = useState<GgufVersions | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState("");
  useEffect(() => {
    api.trainConfig().then((c) => {                                  // the base model of this project, read from its own configuration
      const id = c.sections.flatMap((s) => s.fields).find((f) => f.key === "model.id")?.value;
      if (typeof id === "string") setBase((b) => b || id);
    }).catch(() => undefined);                                       // ignored: the field stays empty and can be typed
  }, []);
  const look = async () => {
    setBusy(true); setError(""); setFound(null);
    try { setFound(await api.ggufFor(base.trim())); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const all = found ? rows(found) : [];
  return (
    <Panel title="Get a GGUF copy of a model" desc="The local server needs a quantised GGUF file. Look for copies that others have already converted; this sends the model name to huggingface.co and downloads nothing.">
      <form className="mb-3 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); look(); }}>
        <div className="min-w-[16rem] flex-1"><Field label="Base model"><Input aria-label="Base model" value={base} placeholder="org/model" onChange={(e) => setBase(e.target.value)} /></Field></div>
        <Button type="submit" variant="primary" loading={busy} disabled={!base.includes("/")}>Look for GGUF versions</Button>
      </form>
      {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
      {found && found.candidates.length === 0 && <EmptyState title="No GGUF version found" hint="Nobody has published one under this name, or the Hub did not answer for them. You can convert the model on this computer with the panel above, if it is downloaded as safetensors." />}
      {found && found.candidates.length > 0 && (
        <div className="space-y-3">
          <p className="text-[0.78rem] text-muted">
            {found.how === "converted from this model" ? "Repositories the Hub lists as converted from this model" : "Repositories found by the model's name, which may be of a different model"}, the most downloaded first.
            {found.training_bytes != null ? ` The training copy (safetensors) is ${gigabytes(found.training_bytes)}.` : " The training copy's size is not stated."}
            {found.free_bytes != null ? ` ${gigabytes(found.free_bytes)} is free on the disk that holds the model cache.` : ""}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-[0.82rem]">
              <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint">
                <th className="py-2 pr-4 font-medium">Repository</th><th className="py-2 pr-4 font-medium">Quantisation</th><th className="py-2 pr-4 font-medium">GGUF copy</th>
                <th className="py-2 pr-4 font-medium">With the training copy</th><th className="py-2 pr-4 font-medium">Room</th><th className="py-2 font-medium" /></tr></thead>
              <tbody className="tnum text-muted">
                {all.map((r) => (
                  <tr key={`${r.repo}:${r.quant}`} className="border-b border-line/60">
                    <td className="py-2 pr-4 font-mono text-[0.76rem] text-fg">{r.repo}</td><td className="py-2 pr-4">{r.quant}</td><td className="py-2 pr-4">{gigabytes(r.bytes)}</td>
                    <td className="py-2 pr-4">{r.withTraining == null ? "not known" : gigabytes(r.withTraining)}</td>
                    <td className={`py-2 pr-4 ${r.room === "no" ? "text-crit-soft" : r.room === "yes" ? "text-ok" : ""}`}>{r.room === "yes" ? "enough" : r.room === "no" ? "not enough" : "not known"}</td>
                    <td className="py-2"><Button variant="ghost" onClick={() => setOpen(open === r.repo ? "" : r.repo)}>{open === r.repo ? "Close" : "Download"}</Button></td>
                  </tr>))}
              </tbody>
            </table>
          </div>
          <p className="text-[0.74rem] text-faint">{num(found.candidates.reduce((n, c) => n + (c.downloads ?? 0), 0))} downloads across these repositories. Check a repository's licence and who made it before using it.</p>
          {open && <HubDownload key={open} repoId={open} wontFit={false} />}
        </div>)}
    </Panel>
  );
}
