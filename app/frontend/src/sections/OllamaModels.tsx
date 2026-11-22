import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Plus } from "lucide-react";
import { Dialog } from "../components/hud/Dialog";
import { RadioGroup, SelectField } from "../components/kit";
import { ProgressStrip } from "../components/ProgressStrip";
import { Button, EmptyState, ErrorState, Field, Input, Panel, Skeleton, StatusPill } from "../components/ui";
import { api, type InstalledOllamaModel, type OllamaInstall, type OllamaInventory, type RunAdapter, type ServingFile } from "../lib/api";
import { useOnChanged } from "../lib/changes";
import { gigabytes } from "../lib/fitWords";
import { refreshJobs, useRunningJobId } from "../lib/jobsList";
import { useJob } from "../lib/useJob";

const INSTALL_STAGE = "install_ollama_model";
const SOURCE_OPTIONS = [
  { value: "pull", label: "Ollama library" },
  { value: "huggingface", label: "Hugging Face GGUF" },
  { value: "gguf", label: "Local GGUF" },
  { value: "adapter", label: "Training run" },
];
const CAPABILITY_NAMES: Record<string, string> = {
  completion: "Text generation", tools: "Tool calling", embedding: "Embeddings", vision: "Vision", thinking: "Reasoning",
};

function InventoryTable({ label, models }: { label: string; models: InstalledOllamaModel[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[0.8rem]" aria-label={label}>
        <thead className="border-b border-line font-mono text-[0.72rem] uppercase tracking-wide text-faint">
          <tr><th scope="col" className="py-2 pr-4 font-medium">Model</th><th scope="col" className="py-2 pr-4 font-medium">Capabilities</th><th scope="col" className="py-2 text-right font-medium">Size</th></tr>
        </thead>
        <tbody>
          {models.map((model) => (
            <tr key={model.tag} className="border-b border-line/60 align-top">
              <td className="py-2 pr-4">
                {model.name !== model.tag && <span className="block font-medium">{model.name}</span>}
                <code className="break-all text-[0.76rem] text-muted">{model.tag}</code>
              </td>
              <td className="py-2 pr-4">
                {model.capabilities.length ? model.capabilities.map((kind) => CAPABILITY_NAMES[kind] ?? kind).join(", ") : "Unknown"}
                {!model.available && <span className="block text-warn">Unavailable</span>}
              </td>
              <td className="py-2 text-right tabular-nums text-muted">{model.location === "cloud" ? "Remote" : model.size > 0 ? gigabytes(model.size) : "Unknown"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function OllamaModels({ embedding = false }: { embedding?: boolean }) {
  const [inventory, setInventory] = useState<OllamaInventory | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const revision = useRef(0);
  const refresh = useCallback(async () => {
    const request = ++revision.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.ollamaModels();
      if (revision.current === request) setInventory(result);
    } catch (failure) {
      if (revision.current === request) setError(failure);
    } finally {
      if (revision.current === request) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => { revision.current++; };
  }, [refresh]);
  useOnChanged(["models"], refresh);

  const models = inventory?.models ?? [];
  const local = models.filter((model) => model.location === "local");
  const cloud = models.filter((model) => model.location === "cloud");
  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (!next) requestAnimationFrame(() => trigger.current?.focus());
  };

  return (
    <Panel title="Ollama models" desc="Registered tags and their reported capabilities." className="[&>header]:flex-wrap [&>header]:gap-3"
      right={<div className="flex flex-wrap gap-2">
        <Button variant="ghost" aria-label="Refresh Ollama models" loading={loading} onClick={() => void refresh()}>Refresh</Button>
        <Button ref={trigger} variant="primary" icon={<Plus size={15} aria-hidden />} onClick={() => onOpenChange(true)}>{embedding ? "Add embedding model" : "Add model to Ollama"}</Button>
      </div>}>
      {error != null
        ? <ErrorState error={error} onRetry={() => void refresh()} />
        : !inventory
          ? <Skeleton className="h-16 w-full" />
          : inventory.status === "unreachable"
            ? <p role="status" className="text-[0.8rem] text-warn">Ollama is unreachable. Refresh when Ollama is running.</p>
            : <>
              {local.length ? <InventoryTable label="Ollama local models" models={local} /> : <p className="text-[0.8rem] text-muted">No local Ollama models</p>}
              <section aria-label="Ollama Cloud" className="mt-3 border-t border-line pt-3">
                <h3 className="font-mono text-[0.78rem] font-semibold text-fg">Ollama Cloud</h3>
                <p className="mt-1 text-[0.78rem] text-muted">Cloud inference runs remotely and requires opt-in before sending project content.</p>
                {cloud.length > 0 && <InventoryTable label="Ollama Cloud models" models={cloud} />}
              </section>
            </>}
      {open && <InstallModel embedding={embedding} onOpenChange={onOpenChange} />}
    </Panel>
  );
}

export function InstallModel({ embedding, onOpenChange }: { embedding: boolean; onOpenChange: (next: boolean) => void }) {
  const [kind, setKind] = useState<OllamaInstall["kind"]>("pull");
  const [tag, setTag] = useState("");
  const [customTag, setCustomTag] = useState("");
  const [repository, setRepository] = useState("");
  const [revision, setRevision] = useState("main");
  const [filename, setFilename] = useState("");
  const [path, setPath] = useState("");
  const [run, setRun] = useState("");
  const [base, setBase] = useState("");
  const [adapters, setAdapters] = useState<{ runs: RunAdapter[]; files: ServingFile[] } | null>(null);
  const [adapterError, setAdapterError] = useState<unknown>(null);
  const job = useJob();
  const hasTrackedJob = useRef(false);
  const runningId = useRunningJobId(INSTALL_STAGE);
  const running = job.status === "running";
  const done = job.status === "done";
  const attach = job.attach;
  useEffect(() => {
    if (!runningId || hasTrackedJob.current) return;
    hasTrackedJob.current = true;
    attach(runningId);
  }, [runningId, attach]);
  useEffect(() => {
    if (job.status !== "idle" && job.status !== "running") refreshJobs();
  }, [job.status]);
  useEffect(() => {
    if (kind !== "adapter" || adapters) return;
    let active = true;
    setAdapterError(null);
    void Promise.all([api.convertAdapterRuns(), api.servingModels()]).then(([runs, models]) => {
      if (active) setAdapters({ runs: runs.adapters, files: models.files.filter((file) => file.info && !file.error && file.role !== "embedding") });
    }).catch((failure) => { if (active) setAdapterError(failure); });
    return () => { active = false; };
  }, [kind, adapters]);

  const optionalTag = customTag.trim() ? { tag: customTag.trim() } : {};
  const sourceTag = tag.trim();
  const request: OllamaInstall = kind === "pull" ? { kind, tag: sourceTag }
    : kind === "huggingface" ? { kind, repo_id: repository.trim(), revision: revision.trim() || "main", filename: filename.trim(), ...optionalTag }
    : kind === "gguf" ? { kind, path: path.trim(), ...optionalTag }
    : { kind, run_name: run, base_path: base, ...optionalTag };
  const valid = request.kind === "pull" ? Boolean(request.tag)
    : request.kind === "huggingface" ? request.repo_id.includes("/") && /\.gguf$/i.test(request.filename)
    : request.kind === "gguf" ? /\.gguf$/i.test(request.path)
    : Boolean(request.run_name && request.base_path);
  const start = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!valid || running || done) return;
    hasTrackedJob.current = true;
    void job.run(INSTALL_STAGE, request, () => api.installOllamaModel(request));
  };

  return (
    <Dialog open onOpenChange={onOpenChange} title={embedding ? "Add embedding model" : "Add model to Ollama"}
      description={embedding ? "Choose a source for an embedding-capable model." : "Choose a source for this Ollama installation."}>
      <form onSubmit={start} className="space-y-4">
        <fieldset disabled={running || done} className="min-w-0 space-y-4">
          <RadioGroup legend="Model source" value={kind} onValueChange={(value) => setKind(value as OllamaInstall["kind"])} options={SOURCE_OPTIONS} disabled={running || done} />
          {kind === "pull" && <Field label="Ollama tag" hint="Enter an exact local tag from the Ollama library."><Input autoComplete="off" value={tag} onChange={(event) => setTag(event.target.value)} placeholder={embedding ? undefined : "qwen2.5-coder:7b-instruct"} /></Field>}
          {kind === "huggingface" && <>
            <Field label="Repository"><Input autoComplete="off" value={repository} onChange={(event) => setRepository(event.target.value)} placeholder="owner/model-gguf" /></Field>
            <Field label="Revision" hint="Use the full commit ID to reproduce an import. The job resolves branch and tag names to a commit."><Input autoComplete="off" value={revision} onChange={(event) => setRevision(event.target.value)} placeholder="main" /></Field>
            <Field label="GGUF filename" hint="Use the exact filename and folder shown in the repository."><Input autoComplete="off" value={filename} onChange={(event) => setFilename(event.target.value)} placeholder="model-Q4_K_M.gguf" /></Field>
          </>}
          {kind === "gguf" && <Field label="GGUF file path" hint="Enter the full path to a GGUF file on this computer."><Input autoComplete="off" value={path} onChange={(event) => setPath(event.target.value)} placeholder="C:/models/model-Q4_K_M.gguf" /></Field>}
          {kind === "adapter" && (
            adapterError != null ? <ErrorState error={adapterError} />
              : !adapters ? <Skeleton className="h-24 w-full" />
                : !adapters.runs.length || !adapters.files.length ? <EmptyState title="A training run and base GGUF are required" hint="Import completed training outputs and add the GGUF for the original base model." />
                  : <>
                    <SelectField label="Training run" aria-label="Training run" value={run} onValueChange={(value) => { setRun(value); setBase(""); }} disabled={running || done}
                      options={[{ value: "", label: "Choose a training run" }, ...adapters.runs.map((item) => ({ value: item.run, label: item.name, hint: item.base ?? undefined }))]} />
                    <SelectField label="Base GGUF" aria-label="Base GGUF" value={base} onValueChange={setBase} disabled={!run || running || done}
                      hint="Select the GGUF for the run's original base. Ollama checks compatibility before registration."
                      options={[{ value: "", label: "Choose a base file" }, ...adapters.files.map((file) => ({ value: file.path, label: file.label ?? file.name, hint: file.info?.quantisation ?? undefined }))]} />
                  </>
          )}
          {kind !== "pull" && <Field label="New Ollama tag (optional)" hint="Leave empty for an automatically assigned tag."><Input autoComplete="off" value={customTag} onChange={(event) => setCustomTag(event.target.value)} placeholder="aivf/my-reviewer" /></Field>}
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          {!done && <Button type="submit" variant="primary" loading={running} disabled={!valid || running}>{kind === "adapter" ? "Deploy adapter" : "Install model"}</Button>}
          {running && <Button variant="ghost" disabled={!job.id} onClick={() => void job.cancel()}>Stop installation</Button>}
          {job.status !== "idle" && <StatusPill status={job.status} />}
        </div>
        {job.progress && <ProgressStrip p={job.progress} live={running} />}
        {job.status === "error" && <ErrorState error={job.failure ?? (job.error || "Ollama installation failed.")} />}
        {done && <p role="status" className="text-[0.8rem] text-ok">The model is registered in Ollama.</p>}
        {job.status === "cancelled" && <p role="status" className="text-[0.8rem] text-muted">The installation was stopped.</p>}
        {job.status === "interrupted" && <p role="status" className="text-[0.8rem] text-warn">The installation was interrupted. You can start it again.</p>}
      </form>
    </Dialog>
  );
}
