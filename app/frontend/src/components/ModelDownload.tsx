import { useCallback, useEffect, useRef } from "react";
import { CheckCircle2 } from "lucide-react";
import { useJob } from "../lib/useJob";
import { api, type Job, type OllamaInstall } from "../lib/api";
import { refreshJobs, useRunningJobId } from "../lib/jobsList";
import { stageLabel } from "../lib/stageLabels";
import { useOnActivate } from "./ActivePage";
import { useConfirm } from "./hud/AlertDialog";
import { Icon } from "./Icon";
import { JobTerminal } from "./JobTerminal";
import { TransferPanel } from "./TransferPanel";
import { Button } from "./ui";

// The download of one model, or its install (a download followed by the conversion that makes it ready to run): the button that starts it (after a question), Stop, the panel that says where it is,
// and the terminal under it. It follows a download or an install of this model that is already running (started before, from another page or window), and only of this model: another model
// downloading at the same time is not this one. The job runs in its own process, so closing the dialog that holds this does not stop it, and opening the model again finds it.

export function ModelDownload({ repoId, quant, label, disabled, confirmText, hint, onActive, onDone, stage = "hf_download", extra, role, ollama }: {
  repoId: string;
  quant: string;
  /** The text of the start button ("Download", "Continue the download", "Install"). */
  label: string;
  disabled?: boolean;
  /** The question asked before anything is fetched; none asks nothing. */
  confirmText?: string;
  /** Said beside the button while nothing has run. */
  hint?: string;
  /** Called with true once a download of this model exists (running or over) and false while there is none: the dialog that holds this shows the download then. */
  onActive?: (active: boolean) => void;
  /** Called once, the moment the job finishes successfully (not on a stop or a failure): a dialog that shows the plan and the choices above this can swap them for a short summary,
   *  instead of leaving stale controls beside a control that is already done. */
  onDone?: () => void;
  /** What is started: a plain download, or an install (download, converter scripts and conversion). */
  stage?: "hf_download" | "install_model";
  /** More parameters of the job (an install takes the role, and for a model that is already here ``source`` and ``skip_download``). */
  extra?: Record<string, unknown>;
  /** What the model will be used as, for the words of the end of an install. */
  role?: "chat" | "embedding" | null;
  ollama?: Extract<OllamaInstall, { kind: "huggingface" }>;
}) {
  const job = useJob();
  const confirm = useConfirm();
  const match = useCallback((j: Job) => j.params?.repo_id === repoId, [repoId]);
  const downloading = useRunningJobId("hf_download", match);
  const installing = useRunningJobId("install_model", match);
  const matchArtifact = useCallback((j: Job) => !!ollama && j.params?.repo_id === ollama.repo_id
    && j.params?.filename === ollama.filename && j.params?.revision === ollama.revision, [ollama]);
  const registering = useRunningJobId("install_ollama_model", matchArtifact);
  const runningId = ollama ? registering : installing ?? downloading;
  useEffect(() => { if (runningId && job.id !== runningId) job.attach(runningId); }, [runningId]);       // eslint-disable-line react-hooks/exhaustive-deps
  useOnActivate(refreshJobs, { always: true });                                                           // coming back to a page that stayed mounted: look for downloads that started meanwhile
  const idle = job.status === "idle";
  const running = job.status === "running";
  const wasRunning = useRef(false);
  useEffect(() => { onActive?.(!idle); }, [idle]);                                                         // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!running && !idle) refreshJobs(); }, [running]);                                  // eslint-disable-line react-hooks/exhaustive-deps; the job ended: the lists behind the dialog hear of it now, not at the next poll
  useEffect(() => {
    if (wasRunning.current && job.status === "done") onDone?.();
    wasRunning.current = running;
  }, [job.status]);                                                                                       // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    if (confirmText && !(await confirm({ title: "Please confirm", description: confirmText, confirmLabel: label }))) return;
    if (ollama) void job.run("install_ollama_model", ollama, () => api.installOllamaModel(ollama));
    else void job.run(stage, { repo_id: repoId, quant, ...extra });
  };
  const stopped = job.status === "cancelled";
  const done = job.status === "done";
  const install = !!ollama || stage === "install_model" || job.progress?.phase != null;
  return (
    <div>
      <div className="flex items-center gap-3">
        <span className="shrink-0 whitespace-nowrap">
          {done ? (
            // A done job is over, for good: starting it again would repeat work already finished (and for an install, fail outright: the converted file is already there and is never
            // overwritten). The button is replaced, not just disabled, so nothing here invites a second try; what happened is said below, by TransferPanel.
            <span className="inline-flex h-9 items-center gap-1.5 px-1 text-[0.74rem] font-semibold uppercase tracking-[0.07em] text-ok">
              <CheckCircle2 size={16} aria-hidden />Done
            </span>
          ) : (
            <Button variant="primary" icon={<Icon.play />} loading={running} disabled={disabled && !running} onClick={start}>{stopped ? (install ? "Continue the install" : "Continue the download") : label}</Button>
          )}
        </span>
        {running && <Button variant="ghost" onClick={job.cancel}>Stop</Button>}
        {idle && hint && <span className="text-[0.78rem] text-muted">{hint}</span>}
      </div>
      {!idle && (
        <>
          <TransferPanel model={repoId} status={job.status} progress={job.progress} error={job.error} failure={job.failure} install={install} role={role} ollama={!!ollama} />
          {(job.logs.length > 0 || running) && (
            <JobTerminal title={stageLabel(ollama ? "install_ollama_model" : install ? "install_model" : "hf_download")} rows={job.logs} earlier={job.earlier} onLoadEarlier={job.showEarlier} status={job.status} progress={job.progress}
              copyText={job.text} maxHeight="max-h-48" />
          )}
        </>
      )}
    </div>
  );
}
