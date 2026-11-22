import { useWorkspaceState } from "../lib/workspaceState";
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Boxes, Trash2 } from "lucide-react";
import { Button, EmptyState, ErrorState, Field, Input, Panel, Skeleton } from "../components/ui";
import { IconButton, RadioGroup, Select, SelectField } from "../components/kit";
import { DevicePanel } from "../components/DevicePanel";
import { DataTable, type Column } from "../components/hud/DataTable";
import { Dialog } from "../components/hud/Dialog";
import { Dev } from "../lib/devMode";
import { notifyChanged, useOnChanged } from "../lib/changes";
import { api, errorText, type DownloadPlan, type FitResult, type InstallPlan, type HubModel, type InstalledRemovalPlan, type LocalModel, type LocalModels, type NotReadyModel, type PartialDownload, type ShelfModel } from "../lib/api";
import { useConfirm } from "../components/hud/AlertDialog";
import { useToast } from "../components/Toast";
import { ModelDownload } from "../components/ModelDownload";
import { InstallPlanView, installQuestion } from "../components/InstallPlanView";
import { FinishInstall } from "../components/FinishInstall";
import { RoleChoice } from "../components/RoleChoice";
import { gigabytes, headline, PART_LABEL } from "../lib/fitWords";

// The models that are already on this computer, and whether one fits: the estimate of the memory to run it or to fine-tune it, set against what this
// computer has free. The Catalog tab searches the Hub and opens the same check, and from there a model can be downloaded.

const formats = (m: LocalModel) => [m.safetensors ? "safetensors" : null, m.gguf.length ? `GGUF (${m.gguf.length})` : null, !m.safetensors && !m.gguf.length && m.weights ? "other weights" : null]
  .filter(Boolean).join(", ") || "no weights found";

// The widths are measured, not guessed: every cell here is one line in a row of one fixed height (a cell that wraps is drawn over the rows around it), so each column is as wide as its longest
// content ("Hugging Face cache", "safetensors, GGUF (12)", the words "Not ready" and a small button) and the model's name, which is cut with the full name on hover, takes the rest. The layout
// spec (e2e/layout.spec.ts) measures all of it at 1024 and 1400 px, so a change of a width or of a cell is checked there.
const COLUMNS: Column<LocalModel>[] = [
  { key: "id", label: "Model", render: (m) => <span className="font-mono text-[0.78rem]" title={m.id}>{m.id}</span>, sortValue: (m) => m.id },
  { key: "size", label: "Size", width: "5rem", align: "right", render: (m) => gigabytes(m.bytes), sortValue: (m) => m.bytes },
  { key: "formats", label: "Formats", width: "11rem", render: formats },
  { key: "where", label: "Found in", width: "9.5rem", render: (m) => (m.source === "hub_cache" ? "Hugging Face cache" : m.source === "converted" ? "Converted here" : m.source === "ollama" ? "Ollama's files" : "Your folder") },
];

const files = (n: number) => `${n} file${n === 1 ? "" : "s"}`;
const joinWords = (xs: string[]) => (xs.length < 2 ? (xs[0] ?? "") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

/** What the question before deleting an installed model says: what goes and how much room it frees, what it means for this kind of model, the roles Settings gives it, and (for
 *  Ollama) that it has to be stopped first. Written from the plan the backend made, so the words are about this model and not about models in general. */
export function DeleteWords({ model, plan }: { model: LocalModel; plan: InstalledRemovalPlan }) {
  const what = plan.kind === "cache"
    ? `Deletes ${gigabytes(plan.bytes)} (${files(plan.files)}, ${plan.revisions} version${plan.revisions === 1 ? "" : "s"}) from the Hugging Face cache on this computer. It can be downloaded again. A run prepared on it needs it downloaded again before it can train or answer.`
    : plan.kind === "ollama"
      ? `Removes this model from Ollama the way Ollama itself does: its manifest and the files no other Ollama model uses (${files(plan.files)}, ${gigabytes(plan.bytes)}).${plan.kept_shared_files ? ` ${files(plan.kept_shared_files)} that another model uses stay.` : ""} It can be pulled again with Ollama.`
      : `Deletes the folder below and everything in it (${files(plan.files)}, ${gigabytes(plan.bytes)}). ${model.source === "converted"
        ? "A converted model can be made again by converting its original model again."
        : "The app cannot get it back: unless you have a copy or can download it again, it is gone."}`;
  return (
    <div className="space-y-2">
      {plan.ollama_running && (
        <p>Ollama is running ({joinWords(plan.ollama_processes)}). It keeps the files of its models open, so it has to be stopped before this model can be deleted. Stopping ends all of Ollama's programs,
          including any model it has loaded and its icon in the taskbar. The app does not start Ollama again afterwards.</p>)}
      <p>{what}</p>
      {plan.kind === "folder" && <p className="break-all font-mono text-[0.74rem] text-faint">{plan.path}</p>}
      {plan.settings_use.length > 0 && <p>Settings uses it as {joinWords(plan.settings_use)}. After the deletion that choice points to a file that is not there; choose another model in Settings.</p>}
    </div>);
}

export function InstalledModels() {
  const [offset, setOffset] = useWorkspaceState("models.offset", 0);
  const [search, setSearch] = useWorkspaceState("models.search", "");
  const [inv, setInv] = useState<LocalModels | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [fitting, setFitting] = useState<{ id: string; source: string } | null>(null);
  const [partial, setPartial] = useState<PartialDownload[]>([]);
  const [notReady, setNotReady] = useState<NotReadyModel[] | null>(null);          // installed but not runnable as they are; null until read (nothing is claimed before)
  const [finishing, setFinishing] = useState<{ id: string; source: string } | null>(null);
  const [problem, setProblem] = useState("");
  const [removing, setRemoving] = useState(false);
  const busy = useRef(false);                                  // one delete at a time; a button is not disabled while the question is open, because a disabled button drops the focus the question gives back
  const listRef = useRef<HTMLDivElement>(null);
  const confirm = useConfirm();
  const toast = useToast();
  const load = () => {
    api.localModels({ limit: 100, offset, search }).then((i) => { setInv(i); setErr(null); }).catch(setErr);
    api.partialDownloads().then((r) => setPartial(r.partial)).catch(() => setPartial([]));          // ignored: the list of stopped downloads is a help; the models still list without it
    api.servingModels().then((r) => setNotReady(r.not_ready ?? [])).catch(() => setNotReady(null));   // ignored: without it no model is said to be ready or not
  };
  useEffect(() => { load(); }, [offset, search]);
  useOnChanged(["models"], load);                                 // a download, a conversion or a delete ended, here or anywhere: the list reads again

  // Deleting asks the backend what would go, says it in words, and only then deletes. A refusal (the model is in use, or may not be deleted) is shown where the list is, as a sentence that
  // stays until it is dismissed. A running Ollama gets the same question with the other button: "Stop Ollama and delete".
  const deleteModel = async (m: LocalModel) => {
    if (busy.current) return;
    busy.current = true; setProblem("");
    try {
      const plan = await api.installedRemovalPlan(m);
      if (!plan.removable) { setProblem(`${m.id} cannot be deleted here. ${plan.refusal ?? ""}`.trim()); return; }
      if (plan.in_use) { setProblem(`${m.id} is in use. ${plan.in_use}`); return; }
      const ok = await confirm(plan.ollama_running
        ? { title: "Ollama is running", confirmLabel: "Stop Ollama and delete", tone: "critical", description: <DeleteWords model={m} plan={plan} /> }
        : { title: `Delete ${m.id}?`, confirmLabel: `Delete ${gigabytes(plan.bytes)}`, tone: "critical", description: <DeleteWords model={m} plan={plan} /> });
      if (!ok) return;
      setRemoving(true);
      try {
        const out = await api.removeInstalled(m, plan.ollama_running);
        toast.success(`${m.id} deleted; ${gigabytes(out.freed_bytes)} freed.${out.stopped.length ? " Ollama was stopped; start it again when you need it." : ""}`);
        if (out.left_behind.length) toast.error(`${files(out.left_behind.length)} of ${m.id} could not be deleted because another program holds them. No model uses them any more.`, out.left_behind.join(", "));
        notifyChanged("models");                  // this list reads again, and so do Settings and the Serving tab: a deleted model must not stay offered there
      } finally { setRemoving(false); listRef.current?.focus(); }          // the button that was pressed was disabled for a moment (and its row may be gone): the focus goes to the list
    } catch (e) { setProblem(`${m.id} was not deleted. ${errorText(e)}`); } finally { busy.current = false; }
  };
  const notReadyKeys = new Set((notReady ?? []).map((m) => `${m.source}:${m.id}`));
  const columns: Column<LocalModel>[] = [...COLUMNS, {
    key: "ready", label: "Ready to run", width: "13.5rem",
    render: (m) => notReady === null ? null : notReadyKeys.has(`${m.source}:${m.id}`) ? (
      <span className="flex items-center gap-2 whitespace-nowrap">
        <span className="text-warn">Not ready</span>
        <Button variant="ghost" size="sm" aria-label={`Finish the install of ${m.id}`} onClick={(e) => { e.stopPropagation(); setFinishing({ id: m.id, source: m.source }); }} onKeyDown={(e) => e.stopPropagation()}>Finish install</Button>
      </span>
    ) : m.gguf.length || m.safetensors ? <span className="text-ok">Ready</span> : null,
    sortValue: (m) => (notReadyKeys.has(`${m.source}:${m.id}`) ? 0 : 1),
  }, {
    key: "delete", label: "Delete", width: "5rem", align: "right",
    render: (m) => (
      <IconButton label={`Delete ${m.id}`} tip="Delete from this computer" tone="danger" size="sm" disabled={removing}
        onClick={(e) => { e.stopPropagation(); void deleteModel(m); }} onKeyDown={(e) => e.stopPropagation()}>
        <Trash2 size={14} aria-hidden />
      </IconButton>),
  }];
  return (
    <div className="space-y-5">
      <DevicePanel />
      <Panel title="Model files on disk" desc="Storage for training, conversions and imports. Open a model to estimate memory use or manage its files."
        right={<Button variant="ghost" onClick={load}>Look again</Button>}>
        <Field label="Search installed models"><Input value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></Field>
        {problem && (
          <div role="alert" className="mb-3 flex items-start justify-between gap-3 border border-crit-line bg-crit-tint px-3 py-2 text-[0.8rem] text-crit-soft">
            <span>{problem}</span>
            <Button variant="ghost" onClick={() => setProblem("")}>Dismiss</Button>
          </div>)}
        {!inv ? (err != null ? <ErrorState error={err} title="Could not read the models" onRetry={load} /> : <Skeleton className="h-32 w-full" />) : (
          <div ref={listRef} tabIndex={-1} role="group" aria-label="Installed models list" className="outline-none">
            <DataTable caption="Installed models" columns={columns} rows={inv.models} rowKey={(m) => `${m.source}:${m.path}`} height={320} onRowClick={setFitting}
              empty={<EmptyState icon={<Boxes size={20} />} title="No model found" hint={inv.hub_cache_exists
                ? "The models folder has no model yet. Models you download for fine-tuning or serving appear here."
                : "The models folder does not exist yet. It is made when the first model is downloaded."} />} />
            <div className="mt-3 flex items-center gap-3 text-[0.78rem] text-muted"><Button variant="ghost" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</Button><span>{(inv.total ?? inv.models.length) ? `${offset + 1}-${Math.min(offset + inv.models.length, inv.total ?? inv.models.length)} of ${inv.total ?? inv.models.length}` : "0 models"}</span><Button variant="ghost" disabled={offset + 100 >= (inv.total ?? inv.models.length)} onClick={() => setOffset(offset + 100)}>Next</Button><span>{gigabytes(inv.total_bytes)} in all</span></div>
            <Dev><p className="mt-1 break-all font-mono text-[0.7rem] text-faint">cache {inv.hub_cache}{inv.models_dir ? ` · folder ${inv.models_dir}` : ""}</p></Dev>
          </div>
        )}
      </Panel>
      {partial.length > 0 && <StoppedDownloads rows={partial} onOpen={(repo) => setFitting({ id: repo, source: "hub" })} onChanged={load} />}
      {fitting && <FitDialog model={fitting} onClose={() => setFitting(null)} onChanged={() => { setFitting(null); load(); }} />}
      {finishing && <FinishInstall model={finishing} onClose={() => setFinishing(null)} />}
    </div>
  );
}

const gb = (b: number) => (b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(b >= 10 * 1024 ** 3 ? 0 : 1)} GB` : `${Math.max(1, Math.round(b / 1024 ** 2))} MB`);

/** Downloads that stopped part-way: the part already fetched stays on disk, so downloading again continues from it. It can also be discarded; only the unfinished files of that model go. */
export function StoppedDownloads({ rows, onOpen, onChanged }: { rows: PartialDownload[]; onOpen: (repo: string) => void; onChanged: () => void }) {
  const confirm = useConfirm();
  const toast = useToast();
  const discard = async (r: PartialDownload) => {
    if (!(await confirm({ title: `Discard the part of ${r.repo_id} that was downloaded?`, confirmLabel: `Discard ${gigabytes(r.partial_bytes)}`,
      description: `Deletes the ${r.partial_files} unfinished file${r.partial_files === 1 ? "" : "s"} (${gigabytes(r.partial_bytes)}) of this model. Files of it that finished are kept. Downloading it again starts that part from the beginning.` }))) return;
    try { const out = await api.discardPartial(r.repo_id); toast.success(`${gigabytes(out.freed_bytes)} freed.`); onChanged(); } catch (e) { toast.error(errorText(e)); }
  };
  return (
    <Panel title="Downloads that stopped" desc="A download that was stopped or cut off keeps the part already fetched. Downloading again continues from there.">
      <ul className="space-y-2" aria-label="Downloads that stopped">
        {rows.map((r) => (
          <li key={r.repo_id} className="flex flex-wrap items-center justify-between gap-3 text-[0.82rem]">
            <span><span className="font-mono text-[0.78rem]">{r.repo_id}</span> <span className="text-muted">{gigabytes(r.partial_bytes)} fetched in {r.partial_files} unfinished file{r.partial_files === 1 ? "" : "s"}</span></span>
            <span className="flex gap-2">
              <Button variant="ghost" aria-label={`Continue downloading ${r.repo_id}`} onClick={() => onOpen(r.repo_id)}>Continue</Button>
              <Button variant="ghost" aria-label={`Discard the part of ${r.repo_id}`} onClick={() => void discard(r)}>Discard</Button>
            </span>
          </li>))}
      </ul>
    </Panel>);
}

/** Removing a downloaded model from the cache: says what is deleted and how much room it frees before anything is, and refuses (with the reason) while something uses it. */
export function RemoveModel({ repoId, onRemoved }: { repoId: string; onRemoved: () => void }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setProblem("");
    try {
      const plan = await api.removalPlan(repoId);
      if (plan.in_use) { setProblem(plan.in_use); return; }
      const ok = await confirm({ title: `Remove ${repoId}?`, confirmLabel: `Remove ${gigabytes(plan.bytes)}`, tone: "critical",
        description: `Deletes ${gigabytes(plan.bytes)} (${plan.revisions} version${plan.revisions === 1 ? "" : "s"}) from the model cache on this computer. It can be downloaded again. A run prepared on it needs it downloaded again before it can train or answer. Models in your own folders are never touched.` });
      if (!ok) return;
      const out = await api.removeModel(repoId);
      toast.success(`${repoId} removed; ${gigabytes(out.freed_bytes)} freed.`);
      notifyChanged("models");
      onRemoved();
    } catch (e) { setProblem(errorText(e)); } finally { setBusy(false); }
  };
  return (
    <div className="mt-4 space-y-2 border-t border-line pt-4">
      <p className="text-[0.82rem] font-medium">Remove this model</p>
      <p className="text-[0.78rem] text-muted">Frees its room in the Hugging Face cache. You are asked first, with the size.</p>
      {problem && <p role="alert" className="text-[0.78rem] text-crit-soft">{problem}</p>}
      <Button variant="ghost" loading={busy} onClick={() => void run()}>Remove from this computer</Button>
    </div>);
}

export function FitDialog({ model, onClose, onChanged }: { model: { id: string; source: string }; onClose: () => void; onChanged?: () => void }) {
  const [context, setContext] = useState("2048");
  const [estimate, setEstimate] = useState({ context: 2048 });
  const [ggufFile, setGgufFile] = useState<string | null>(model.source === "hub" ? null : "");
  const [result, setResult] = useState<FitResult | null>(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);                    // a download of this model exists: the dialog shows the download and folds the estimate into one line
  const [showEstimate, setShowEstimate] = useState(false);

  useEffect(() => {
    if (ggufFile === null) return;
    let current = true;
    setBusy(true); setProblem(""); setResult(null);
    void api.modelFit({ model_id: model.id, source: model.source, mode: "inference", context: estimate.context, micro_batch: 1, rank: 16,
      ...(ggufFile ? { gguf_file: ggufFile } : {}) })
      .then((fit) => { if (current) setResult(fit); })
      .catch((e) => { if (current) setProblem(errorText(e)); })
      .finally(() => { if (current) setBusy(false); });
    return () => { current = false; };
  }, [estimate, ggufFile, model.id, model.source]);

  const weightsOnly = result?.estimate_complete === false;
  const h = result && ggufFile !== null ? weightsOnly ? {
    title: "Runtime memory estimate unavailable",
    detail: result.notes[0],
    tone: "unknown" as const,
  } : headline(result.check, result.mode) : null;
  const TONE = { ok: "border-ok-line bg-ok-tint", warn: "border-warn-line bg-warn-tint", crit: "border-crit-line bg-crit-tint", unknown: "border-line bg-surface-2" } as const;
  const folded = downloading && !showEstimate;
  const oneLine = result && h ? weightsOnly ? `Model file size: ${gigabytes(result.total)}.`
    : `${h.title}: ${gigabytes(result.total)} estimated, ${result.check.gpu_free_bytes ? `${gigabytes(result.check.gpu_free_bytes)} of video memory free` : "no graphics card"}.`
    : "The estimate is folded away while the model downloads.";
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={`Does ${model.id.split("/").pop()} fit?`} description="An estimate of the memory needed, compared with what is free now." className="max-h-[92vh]">
      <div className="space-y-4">
        {downloading && (
          <div className="flex flex-wrap items-center justify-between gap-3 border border-line bg-surface-2 px-3 py-2">
            <p className="text-[0.8rem] text-muted">{oneLine}</p>
            <Button variant="ghost" onClick={() => setShowEstimate(!showEstimate)} aria-expanded={showEstimate}>{showEstimate ? "Hide estimate" : "Show estimate"}</Button>
          </div>
        )}
        {!folded && (
          <>
          {ggufFile !== null && !weightsOnly && (
            <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); setEstimate({ context: Number(context) || 2048 }); }}>
              <Field label="Context (tokens)"><Input type="number" min={128} max={1_000_000} value={context} onChange={(e) => setContext(e.target.value)} /></Field>
              <Button type="submit" loading={busy}>Estimate</Button>
            </form>
          )}
          {ggufFile !== null && problem && <p role="alert" className="border border-crit-line bg-crit-tint px-3 py-2 text-[0.8rem] text-crit-soft">{problem}</p>}
          {result && h && (
            <div className="space-y-3">
              <div role="status" className={`border px-4 py-3 ${TONE[h.tone]}`}>
                <p className="flex items-center gap-2 text-[0.9rem] font-semibold">{h.tone !== "ok" && <AlertTriangle size={15} aria-hidden />}{h.title}</p>
                <p className="mt-0.5 text-[0.78rem] text-muted">{h.detail}</p>
              </div>
              <table className="w-full text-[0.8rem]">
                <caption className="sr-only">{weightsOnly ? "Model file size and available memory" : "Estimated memory by part"}</caption>
                <tbody>
                  {Object.entries(result.parts).filter(([k, v]) => k !== "total" && v > 0).map(([k, v]) => (
                    <tr key={k} className="border-t border-line"><th scope="row" className="py-1 text-left font-normal text-muted">{weightsOnly ? "Model file size" : PART_LABEL[k] ?? k}</th><td className="tnum py-1 text-right">{gigabytes(v)}</td></tr>))}
                  {!weightsOnly && <tr className="border-t border-line-strong"><th scope="row" className="py-1.5 text-left font-medium">Estimated total</th><td className="tnum py-1.5 text-right font-medium">{gigabytes(result.total)}</td></tr>}
                  <tr><th scope="row" className="py-1 text-left font-normal text-muted">Free video memory</th><td className="tnum py-1 text-right">{result.check.gpu_free_bytes ? gigabytes(result.check.gpu_free_bytes) : "no graphics card"}</td></tr>
                  <tr><th scope="row" className="py-1 text-left font-normal text-muted">Free main memory</th><td className="tnum py-1 text-right">{result.check.ram_free_bytes ? gigabytes(result.check.ram_free_bytes) : "unknown"}</td></tr>
                </tbody>
              </table>
              {!weightsOnly && result.notes.length > 0 && <ul className="list-disc space-y-0.5 pl-5 text-[0.76rem] text-muted">{result.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
              {!weightsOnly && <p className="text-[0.74rem] text-faint">This is an estimate from the model's own dimensions, not a measurement. The real figure depends on the software that runs the model.</p>}
            </div>
          )}
          </>
        )}
        {model.source === "hub" && <HubDownload repoId={model.id} wontFit={h?.tone === "crit"} onActive={setDownloading} onEstimateFile={setGgufFile} />}
        {model.source === "hub_cache" && onChanged && <RemoveModel repoId={model.id} onRemoved={onChanged} />}
      </div>
    </Dialog>
  );
}

/** Downloading a Hub model into the cache: what it would fetch and whether the disk can hold it, shown before anything starts; a GGUF-only model needs one
 *  quantisation chosen. Starting asks first, and says when the estimate above found that the model will not fit this computer. */
export function HubDownload({ repoId, wontFit, onActive, onEstimateFile }: {
  repoId: string; wontFit: boolean; onActive?: (active: boolean) => void;
  onEstimateFile?: (file: string | null) => void;
}) {
  const [quant, setQuant] = useState("");
  const [active, setActive] = useState(false);                     // a download of this model exists (running or over): the choices above it are settled
  const [plan, setPlan] = useState<DownloadPlan | null>(null);
  const [problem, setProblem] = useState("");
  const [install, setInstall] = useState<InstallPlan | null>(null);          // what installing it ready to run would do (a model that comes as safetensors only)
  const [installProblem, setInstallProblem] = useState("");
  const [mode, setMode] = useState<"install" | "download">("install");
  const [fileName, setFileName] = useState("");
  const [role, setRole] = useState<"" | "chat" | "embedding">("");           // "" = what the name says
  const chosen = useRef(false);                                  // the person picked (or un-picked) a quantisation: the recommendation is not applied again
  const choose = (q: string) => { chosen.current = true; setQuant(q); };
  useEffect(() => {
    let current = true;
    setPlan(null); setProblem("");
    api.downloadPlan(repoId, quant).then((p) => {
      if (!current) return;
      setPlan(p);
      if (!quant && !chosen.current && p.needs_quant && p.recommended_quant) setQuant(p.recommended_quant.quant);      // starts on the recommended quantisation; it can be changed
    }).catch((e) => { if (current) setProblem(errorText(e)); });
    return () => { current = false; };
  }, [repoId, quant]);
  const ggufFiles = plan?.gguf_files ?? [];
  const file = ggufFiles.find((candidate) => candidate.name === fileName) ?? ggufFiles[0];
  const resultFile = plan && !plan.needs_quant ? file?.name ?? "" : null;
  useEffect(() => { onEstimateFile?.(resultFile); }, [onEstimateFile, resultFile]);
  const safetensors = !!plan?.patterns?.includes("*.safetensors");
  useEffect(() => {
    setInstall(null); setInstallProblem("");
    if (!safetensors) return;
    let live = true;
    api.installPlan(repoId, role).then((p) => { if (live) setInstall(p); }).catch((e) => { if (live) setInstallProblem(errorText(e)); });
    return () => { live = false; };
  }, [repoId, safetensors, role]);
  const installing = install?.kind === "convert" && mode === "install";
  const ask = plan
    ? `This downloads ${plan.files} file${plan.files === 1 ? "" : "s"}, ${gigabytes(plan.bytes)}, from huggingface.co into the model cache of this computer.`
      + (plan.partial_bytes ? ` ${gigabytes(plan.partial_bytes)} of it is already there and is not fetched again.` : "")
      + (plan.gated ? " The model is gated: a Hugging Face token must be set in Settings and its terms accepted on its page, or the download is refused." : "")
      + (wontFit ? " The estimate above says it will NOT fit this computer's free memory; it can still be downloaded, but not run here." : "")
    : "";
  return (
    <div className="mt-4 space-y-3 border-t border-line pt-4">
      <p className="text-[0.82rem] font-medium">{file ? "Model file" : "Download this model"}</p>
      {problem ? <p role="alert" className="text-[0.78rem] text-crit-soft">{problem}</p> : !plan ? <p className="text-[0.78rem] text-muted">Reading the file list...</p> : (
        <>
          {plan.quants.length > 0 && plan.needs_quant || quant ? (
            <SelectField label="Which quantisation" aria-label="Quantisation" value={quant} onValueChange={choose} disabled={active}
              hint={plan.recommended_quant && quant === plan.recommended_quant.quant ? plan.recommended_quant.reason
                : `Choose one copy of the weights.${plan.recommended_quant ? ` Recommended quantisation: ${plan.recommended_quant.quant}.` : ""}`}
              options={[{ value: "", label: "choose one" }, ...plan.quants.map((q) => {
                const size = plan.quant_bytes?.[q] ? gigabytes(plan.quant_bytes[q]) : "";
                const recommended = plan.recommended_quant?.quant === q;
                return { value: q, label: `${q}${size || recommended ? ` (${[size, recommended ? "recommended" : ""].filter(Boolean).join(", ")})` : ""}` };
              })]} />
          ) : null}
          {ggufFiles.length > 1 && file && (
            <SelectField label="GGUF file" value={file.name} onValueChange={setFileName} disabled={active}
              options={ggufFiles.map((candidate) => ({ value: candidate.name, label: candidate.name, hint: gigabytes(candidate.bytes) }))} />
          )}
          {file && ggufFiles.length === 1 && <p className="break-words text-[0.78rem] text-muted">{file.name}</p>}
          {!plan.needs_quant && !file && (
            <p className="text-[0.78rem] text-muted">{plan.files} file{plan.files === 1 ? "" : "s"}, {gigabytes(plan.bytes)}. {plan.free_bytes == null ? "The free disk space is unknown." : `${gigabytes(plan.free_bytes)} free on the disk that holds the cache.`}{" "}
              {plan.enough ? "That is enough." : "That is not enough: the disk needs a tenth more than the model to spare."}</p>
          )}
          {plan.gated && !plan.token_set && <p role="alert" className="text-[0.78rem] text-warn">This model is gated and no Hugging Face token is set. Accept its terms on its page, make a token there, and put it in Settings; the download is refused until then.</p>}
          {!!plan.partial_bytes && <p className="text-[0.78rem] text-muted">{gigabytes(plan.partial_bytes)} of it was fetched before the download stopped; it continues from there.</p>}
          {installProblem && <p className="text-[0.78rem] text-muted">Installing it ready to run could not be planned ({installProblem}); the files can still be downloaded.</p>}
          {install?.kind === "convert" && (
            <div className="space-y-3 border border-line bg-surface-2 px-3 py-3">
              <RadioGroup legend="What to do with it" value={mode} onValueChange={(v) => setMode(v as "install" | "download")} disabled={active}
                options={[{ value: "install", label: "Install it ready to run", description: "Downloads the model and converts it to GGUF. Add the converted file to Ollama from the Installed tab." },
                          { value: "download", label: "Only download the files", description: "For fine-tuning or other tools. Ollama cannot run safetensors files as they are." }]} />
              {mode === "install" && (
                <>
                  <RoleChoice role={install.role} detected={install.role_detected} onChange={setRole} disabled={active} />
                  <InstallPlanView plan={install} />
                </>
              )}
            </div>
          )}
          {file ? (
            plan.revision ? (
              <ModelDownload key={file.name} repoId={repoId} quant={quant} label="Install in Ollama"
                disabled={(plan.gated && !plan.token_set) || (plan.free_bytes !== null && plan.free_bytes < file.bytes * 1.1)}
                confirmText={`Download ${file.name} (${gigabytes(file.bytes)}) from Hugging Face and register it in Ollama.${wontFit ? " The model's estimated runtime memory exceeds available memory." : ""}`}
                hint={plan.free_bytes !== null && plan.free_bytes < file.bytes * 1.1 ? "Free up disk space to install this file." : undefined}
                ollama={{ kind: "huggingface", repo_id: repoId, revision: plan.revision, filename: file.name }}
                onActive={(on) => { setActive(on); onActive?.(on); }} />
            ) : <p role="alert" className="text-[0.78rem] text-warn">Hugging Face did not report a revision. Refresh the model details to try again.</p>
          ) : installing && install ? (
            <ModelDownload key="install" stage="install_model" repoId={repoId} quant={install.format ?? ""} extra={{ role: install.role }} role={install.role} label="Install" disabled={!install.ready}
              confirmText={installQuestion(install, (plan.gated ? " The model is gated: a Hugging Face token must be set in Settings and its terms accepted on its page, or the download is refused." : "")
                + (wontFit ? " The estimate above says it will NOT fit this computer's free memory; it can still be installed, but not run here." : ""))}
              hint={install.ready ? "Stop keeps what is already downloaded; Install again continues from it." : "This cannot be installed yet: see above."} onActive={(on) => { setActive(on); onActive?.(on); }} />
          ) : (
            <ModelDownload key="download" repoId={repoId} quant={quant} label={plan.partial_bytes ? "Continue the download" : "Download"} disabled={plan.needs_quant || !plan.enough || (plan.gated && !plan.token_set)} confirmText={ask}
              hint={plan.needs_quant ? "Choose a quantisation first." : "Stop keeps what is already downloaded; starting again continues from it."}
              onActive={(on) => { setActive(on); onActive?.(on); }} />
          )}
        </>
      )}
    </div>
  );
}

const compact = (n: number | null) => (n == null ? "" : n >= 1e9 ? `${(n / 1e9).toFixed(1)} B` : n >= 1e6 ? `${Math.round(n / 1e6)} M` : String(n));

/** The models this project itself uses, checked against the Hub when asked (nothing is typed in as fact), each with a way to see whether it fits. */
function ProjectShelf({ onOpen }: { onOpen: (m: HubModel) => void }) {
  const [rows, setRows] = useState<ShelfModel[] | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const run = () => { setBusy(true); setErr(null); api.modelShelf().then((r) => setRows(r.models)).catch(setErr).finally(() => setBusy(false)); };
  return (
    <Panel title="Used by this project" desc="The base model for fine-tuning, looked up on the Hub when you ask."
      right={<Button variant="ghost" loading={busy} onClick={run}>{rows ? "Check again" : "Check them now"}</Button>}>
      {err != null ? <ErrorState error={err} onRetry={run} /> : rows === null ? <p className="text-[0.8rem] text-muted">Not looked up yet. Checking sends the two model names to huggingface.co.</p> : (
        <ul className="space-y-2 text-[0.82rem]" aria-label="Project models">
          {rows.map((m) => (
            <li key={m.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 border border-line bg-surface-2 px-3 py-2">
              <span className="text-muted">{m.role}</span>
              <span className="font-mono text-[0.78rem]">{m.id}</span>
              {m.error ? <span className="text-crit-soft">Could not be checked: {m.error}</span> : (
                <>
                  <span className="tnum">{compact(m.parameters ?? null) || "size not stated"}{m.parameters ? " parameters" : ""}</span>
                  <span>{m.gated ? "Gated: accept its terms on the Hub first" : "Open"}</span>
                  <Button variant="ghost" className="ml-auto" onClick={() => onOpen(m as HubModel)}>Does it fit?</Button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

const CATALOG_COLUMNS: Column<HubModel>[] = [
  { key: "id", label: "Model", render: (m) => <span className="font-mono text-[0.78rem]">{m.id}</span>, sortValue: (m) => m.id },
  { key: "task", label: "For", width: "10rem", render: (m) => m.task?.replace(/-/g, " ") ?? "" },
  { key: "parameters", label: "Parameters", width: "7rem", align: "right", render: (m) => compact(m.parameters), sortValue: (m) => m.parameters ?? 0 },
  { key: "downloads", label: "Downloads", width: "7rem", align: "right", render: (m) => compact(m.downloads), sortValue: (m) => m.downloads ?? 0 },
  { key: "likes", label: "Likes", width: "5rem", align: "right", render: (m) => (m.likes ?? "") as string | number, sortValue: (m) => m.likes ?? 0 },
  { key: "gated", label: "Access", width: "7rem", render: (m) => (m.gated ? "Gated" : "Open") },
];

/** Search the Hugging Face Hub (metadata only: nothing is downloaded) and check whether a model would fit. The search starts when asked, so opening the tab
 *  sends nothing to the internet. */
export function Catalog() {
  const [q, setQ] = useState("");
  const [task, setTask] = useState("");
  const [rows, setRows] = useState<HubModel[] | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [fitting, setFitting] = useState<HubModel | null>(null);
  const run = () => { setBusy(true); setErr(null); api.searchModels(q.trim(), task).then((r) => setRows(r.models)).catch((e) => { setRows(null); setErr(e); }).finally(() => setBusy(false)); };
  return (
    <div className="space-y-5">
      <ProjectShelf onOpen={setFitting} />
      <Panel title="Catalog" desc="Search Hugging Face. The search sends your words to huggingface.co; nothing is downloaded.">
        <form className="mb-3 flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); run(); }}>
          <div className="min-w-[14rem] flex-1"><Input placeholder="Search models, for example qwen coder" aria-label="Search models" value={q} onChange={(e) => setQ(e.target.value)} /></div>
          <Select aria-label="What the model is for" value={task} onValueChange={setTask} className="w-56"
            options={[{ value: "", label: "any task" }, { value: "generation", label: "text generation" }, { value: "embedding", label: "embedding models" }, { value: "gguf", label: "GGUF files (for the local models)" }]} />
          <Button type="submit" variant="primary" loading={busy}>Search</Button>
        </form>
        {err != null ? <ErrorState error={err} onRetry={run} /> : rows === null ? (
          <EmptyState icon={<Boxes size={20} />} title="Nothing searched yet" hint="Type a name, or leave it empty to see the most downloaded models, then press Search. Open a result to see whether it fits this computer." />
        ) : (
          <DataTable caption="Models on the Hub" columns={CATALOG_COLUMNS} rows={rows} rowKey={(m) => m.id} height={360} onRowClick={setFitting}
            empty="No model matches this search." />
        )}
        {rows !== null && <p className="mt-3 text-[0.76rem] text-muted">{rows.length} model{rows.length === 1 ? "" : "s"}, the most downloaded first.</p>}
      </Panel>
      {fitting && <FitDialog model={{ id: fitting.id, source: "hub" }} onClose={() => setFitting(null)} />}
    </div>
  );
}
