import { useCallback, useEffect, useState } from "react";
import { api, errorText, type ServingFile, type ServingStatus } from "../lib/api";
import { useOnChanged } from "../lib/changes";
import { gigabytes } from "../lib/fitWords";
import { Button, EmptyState, Panel, Skeleton } from "../components/ui";
import { useConfirm } from "../components/hud/AlertDialog";
import { useToast } from "../components/Toast";
import { DerivedCachePanel } from "../components/DerivedCachePanel";
import { GgufFinder } from "./GgufFinder";
import { OllamaModels } from "./OllamaModels";

export const POLL_MS = 1500;
const SLOW_MS = 6000;

export function Serving() {
  const confirm = useConfirm();
  const toast = useToast();
  const [status, setStatus] = useState<ServingStatus | null>(null);
  const [files, setFiles] = useState<ServingFile[] | null>(null);
  const [unloading, setUnloading] = useState<string | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(() => api.servingStatus().then((s) => {
    if (!s?.ollama) throw new Error("Ollama status is unavailable. Reload the app to refresh the runtime connection.");
    setStatus(s); setError("");
  }).catch((e) => setError(errorText(e))), []);
  const loadFiles = useCallback(() => api.servingModels().then((r) => setFiles(r.files ?? [])).catch((e) => setError(errorText(e))), []);
  useEffect(() => { void refresh(); void loadFiles(); }, [refresh, loadFiles]);
  useOnChanged(["models"], () => { void refresh(); void loadFiles(); });
  const starting = status?.ollama.running === false;
  useEffect(() => {
    const timer = window.setInterval(refresh, starting ? POLL_MS : SLOW_MS);
    return () => window.clearInterval(timer);
  }, [refresh, starting]);

  const unload = async (model: string) => {
    const accepted = await confirm({ title: `Unload ${model}?`, confirmLabel: "Unload", cancelLabel: "Keep loaded",
      description: "Ollama frees the model's memory after its active requests finish. The next request loads it again." });
    if (!accepted) return;
    setUnloading(model);
    try {
      await api.servingUnload({ model });
      toast.success(`${model} was unloaded.`);
      await refresh();
    } catch (e) { setError(errorText(e)); }
    finally { setUnloading(null); }
  };

  if (!status && !error) return <Skeleton className="h-40 w-full" />;
  return (
    <div className="space-y-5">
      {error && <p role="alert" className="text-[0.82rem] text-crit-soft">{error}</p>}
      {status?.exclusive && (
        <p role="status" className="border border-warn-line bg-warn-tint px-3 py-2 text-[0.82rem] text-warn">
          The graphics card is held by {status.exclusive.owner}.
        </p>)}
      {status && (
        <Panel title={status.ollama.version ? `Ollama ${status.ollama.version}` : "Ollama"} desc="Running models">
          {status.ollama.running
            ? status.running_models.length > 0
              ? <ul className="divide-y divide-line">
                  {status.running_models.map((model) => (
                    <li key={model.name} className="flex flex-wrap items-center justify-between gap-3 py-2">
                      <div className="min-w-0">
                        <p className="break-all font-medium">{model.name}</p>
                        <p className="text-[0.8rem] text-muted">
                          {model.context_length ? `${model.context_length.toLocaleString()} context tokens. ` : ""}
                          {model.size_vram ? `${gigabytes(model.size_vram)} of GPU memory.` : "No GPU memory in use."}
                        </p>
                      </div>
                      <Button variant="ghost" loading={unloading === model.name} disabled={unloading !== null}
                        aria-label={`Unload ${model.name}`} onClick={() => void unload(model.name)}>Unload</Button>
                    </li>
                  ))}
                </ul>
              : <EmptyState title="No models are running" hint="Ollama loads a model when you use it." />
            : <p role="status" className="text-[0.82rem] text-warn">{status.ollama.error ?? "Ollama is not running. It starts with the app."}</p>}
        </Panel>)}

      <OllamaModels />

      <Panel title="Models on this computer" desc="GGUF files detected on disk. Use Add model to Ollama to register one for Projects.">
        {files === null ? <Skeleton className="h-16 w-full" />
          : files.length === 0 ? <EmptyState title="No GGUF model is installed" hint="This list shows importable files. Registered Ollama models appear above." />
          : (
            <div className="overflow-x-auto">
              <table className="w-full text-[0.8rem]" aria-label="Models on this computer">
                <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-3 font-medium">Model</th><th className="py-2 pr-3 font-medium">Quantisation</th>
                  <th className="py-2 pr-3 font-medium">Layers</th><th className="py-2 pr-3 font-medium">Size</th></tr></thead>
                <tbody>
                  {files.map((f) => (
                    <tr key={f.path} className="border-b border-line/60 align-top">
                      <td className="py-2 pr-3"><span className="font-medium">{f.label ?? f.name}</span><span className="block text-[0.72rem] text-muted">{f.label && f.label !== f.name ? f.name : f.id}</span></td>
                      <td className="py-2 pr-3">{f.info?.quantisation ?? <span className="text-warn">{f.error ? "cannot be read" : "unknown"}</span>}</td>
                      <td className="tnum py-2 pr-3">{f.info?.layers ?? ""}</td><td className="tnum py-2 pr-3">{gigabytes(f.bytes)}</td>
                    </tr>))}
                </tbody>
              </table>
            </div>)}
      </Panel>
      <DerivedCachePanel />
      <GgufFinder />
    </div>
  );
}
