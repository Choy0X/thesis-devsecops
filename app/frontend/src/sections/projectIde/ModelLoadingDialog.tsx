import { useEffect, useRef, useState } from "react";
import { Check, Cpu, LoaderCircle } from "lucide-react";
import { Dialog } from "../../components/hud/Dialog";
import { clockSpan } from "../../lib/format";
import { get, write, type Model } from "./client";
import { friendlyRunError } from "./runError";
import type { PreparationProgress } from "./runtimePreparation";

export type Preparation = { id: string; state: "loading" | "ready" | "failed"; phase?: string; message?: string; model: Model; elapsed_seconds?: number; progress?: { phase?: string; completed?: number; total?: number; unit?: string; percent?: number; cached?: boolean }; error?: { code?: string; message?: string; action?: unknown } };

export function ModelLoadingDialog({ model, allowShorter = true, purpose = "start", onClose, onReady }: {
  model: Model; allowShorter?: boolean; purpose?: "start" | "send" | "switch"; onClose: () => void; onReady: (job: Preparation, report: (progress: PreparationProgress) => void, signal: AbortSignal) => Promise<void>;
}) {
  const [context, setContext] = useState(model.context || 8192);
  const [attempt, setAttempt] = useState(0);
  const [job, setJob] = useState<Preparation>();
  const [error, setError] = useState("");
  const [continuing, setContinuing] = useState(false);
  const [continuation, setContinuation] = useState<PreparationProgress>();
  const [elapsed, setElapsed] = useState(0);
  const started = useRef(Date.now());
  const ready = useRef(onReady); ready.current = onReady;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let delivered = false;
    setJob(undefined); setError(""); setContinuation(undefined); started.current = Date.now(); setElapsed(0);
    const observe = async (next: Preparation) => {
      if (controller.signal.aborted) return;
      setJob(next);
      if (next.state === "failed") { setError(next.error?.message || next.message || "The model could not load. Try again after freeing memory."); return; }
      if (next.state === "ready") {
        if (delivered) return;
        delivered = true; setContinuing(true);
        try { await ready.current(next, progress => { if (!controller.signal.aborted) setContinuation(progress); }, controller.signal); }
        catch (reason) { if (!controller.signal.aborted) setError(friendlyRunError(reason)); }
        finally { if (!controller.signal.aborted) setContinuing(false); }
        return;
      }
      timer = setTimeout(() => { void get<Preparation>(`/api/ide/models/prepare/${encodeURIComponent(next.id)}`, controller.signal).then(observe).catch(fail); }, 650);
    };
    const fail = (reason: unknown) => { if (!controller.signal.aborted) setError(friendlyRunError(reason)); };
    void write<Preparation>("/api/ide/models/prepare", { model, context }).then(observe).catch(fail);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [model.id, context, attempt]);
  useEffect(() => { const clock = setInterval(() => setElapsed(Math.floor((Date.now() - started.current) / 1000)), 1000); return () => clearInterval(clock); }, []);
  const failed = !!error;
  const measured = job?.progress;
  const verification = job?.phase === "verifying";
  const percent = verification && typeof measured?.completed === "number" && typeof measured.total === "number" && measured.total > 0 ? Math.min(100, Math.max(0, Math.round(100 * measured.completed / measured.total))) : null;
  const phaseText = continuing ? continuation?.message || "Preparing the coding agent..." : job?.message || "Checking the selected model and available memory...";
  const step = continuing || job?.state === "ready" ? 2 : job && !verification ? 1 : 0;
  const canClose = !continuing || !!continuation?.cancellable;
  const seconds = Math.max(elapsed, Math.floor(job?.elapsed_seconds || 0));
  return <Dialog open onOpenChange={open => { if (!open && canClose) onClose(); }} title={failed ? job?.error?.code === "tool_check_failed" ? "Coding tool check failed" : continuation ? "Coding agent could not start" : purpose === "switch" ? "Model switch failed" : "Model could not load" : purpose === "switch" ? "Switching model" : "Loading selected model"} description={purpose === "switch" ? continuing ? "The selected model is loaded. Preparing the coding agent for this chat." : "Your current model stays selected until the new model is ready." : "The model and coding agent must be ready before you send a message."} dismissible={canClose} className="w-[min(36rem,94vw)]">
    <div className="chat-model-loading">
      <div className="chat-load-identity"><Cpu size={22} /><div><strong>{model.name}</strong><span>{context.toLocaleString()} token context</span></div></div>
      <ol className="chat-load-steps" aria-label="Model preparation steps">{["Verify file", "Load model", "Prepare agent"].map((label, index) => <li key={label} data-state={failed ? "waiting" : index < step ? "done" : index === step ? "active" : "waiting"}><span>{index < step && !failed ? <Check size={12} /> : index + 1}</span>{label}</li>)}</ol>
      {failed ? <p className="ide-error" role="alert">{error}</p> : <><div className={`chat-loading-track ${percent !== null ? "is-measured" : ""}`} role="progressbar" aria-label={continuing ? "Agent preparation" : percent !== null ? "File verification" : "Model loading"} aria-valuemin={percent !== null ? 0 : undefined} aria-valuemax={percent !== null ? 100 : undefined} aria-valuenow={percent ?? undefined} aria-valuetext={percent !== null ? `File verification ${percent}%` : phaseText}>{percent !== null && <span style={{ width: `${percent}%` }} />}</div><p role="status"><LoaderCircle size={15} className="chat-spin" />{phaseText}</p><div className="chat-load-metrics">{percent !== null ? <span>{percent}% of file verified</span> : <span>{(continuation?.phase || job?.phase)?.replace(/_/g, " ") || "Preparing"}</span>}<span>{clockSpan(seconds)} elapsed</span></div>{verification && <p className="chat-load-explanation">Reading the GGUF file to verify its identity. This check is reused while the file stays unchanged.</p>}</>}
      {failed && !continuation && job?.error?.code === "does_not_fit" && allowShorter && context > 2048 && <div className="chat-memory-choice"><p>Keep this model and retry with a shorter context. This reduces how much conversation and code it can consider at once.</p><button onClick={() => { setContext(context > 4096 ? 4096 : 2048); setAttempt(value => value + 1); }}>Retry with {context > 4096 ? "4,096" : "2,048"} tokens</button></div>}
      {failed && job?.error?.code === "tool_check_failed" && typeof job.error.action === "string" && <p className="chat-load-explanation">{job.error.action}</p>}
      {!failed && <p className="chat-load-cancel-note">No message is sent until preparation finishes. Shared preparation may finish in the background.</p>}
      <div className="ide-create-actions"><button disabled={!canClose} onClick={onClose}>{failed ? "Close" : continuing && purpose !== "send" ? "Close preparation" : purpose === "switch" ? "Cancel switch" : purpose === "start" ? "Cancel chat" : "Cancel message"}</button>{failed && <button className="ide-primary" onClick={() => setAttempt(value => value + 1)}>Retry loading</button>}</div>
    </div>
  </Dialog>;
}
