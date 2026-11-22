import { useEffect, useRef, useState } from "react";
import { CheckCircle2 } from "lucide-react";
import { useJob } from "../lib/useJob";
import type { Job } from "../lib/api";
import { refreshJobs, useRunningJobId } from "../lib/jobsList";
import { useOnActivate } from "./ActivePage";
import { Button, StatusPill, EmptyState, ErrorState } from "./ui";
import { useConfirm } from "./hud/AlertDialog";
import { Icon } from "./Icon";
import { JobTerminal } from "./JobTerminal";
import { ProgressStrip } from "./ProgressStrip";
import { stageLabel } from "../lib/stageLabels";
import { api, type ComputeChoices } from "../lib/api";
import { DEVICE_STAGES, rememberedOf, rememberedText, type Device } from "../lib/compute";
import { ComputeDialog } from "./ComputeDialog";

// A stage launcher with a live log terminal. Reused across sections.
// Jobs run in their own process on the backend, so leaving the tab does not stop
// them; on mount the console re-attaches to a running job of the same stage.
export function RunConsole({ stage, params, label, hint, onDone, disabled, confirmText, launch, match, oneShot }:
  { stage: string; params?: Record<string, any>; label: string; hint?: string; onDone?: () => void;
    disabled?: boolean; confirmText?: string; launch?: (params: Record<string, any>) => Promise<{ id: string }>;
    /** Which running job of the stage to follow, when several of one stage can run (two ecosystems downloading at once). */
    match?: (job: Job) => boolean;
    /** For a run that is not meant to be repeated once it finishes (pressing it again would redo work already done, or fail outright): the button is replaced by a plain "Done"
     *  once the job is done, instead of staying clickable. Off by default: most consoles run something that is fine to run again. */
    oneShot?: boolean }) {
  const job = useJob();
  const confirm = useConfirm();
  const wasRunning = useRef(false);
  const startedHere = useRef(false);               // this console launched the job (otherwise it only follows one that was already running)
  const [showOutput, setShowOutput] = useState(false);
  const [asking, setAsking] = useState<{ data: ComputeChoices; initial: Device | null } | null>(null);        // the Compute dialog, for a job that holds a model on a device
  const answer = useRef<((r: { device: Device; remember: boolean } | null) => void) | null>(null);

  // A job of this stage that is already running (started before this page was opened, or by another console of the stage) is
  // followed; the list comes from one shared poll and the output from one shared connection per job.
  const runningId = useRunningJobId(stage, match);
  useEffect(() => { if (runningId && job.id !== runningId) job.attach(runningId); }, [runningId]);   // eslint-disable-line react-hooks/exhaustive-deps
  useOnActivate(refreshJobs, { always: true });                      // coming back to a page that stayed mounted: look for jobs that started meanwhile (one shared request)

  useEffect(() => {
    if (wasRunning.current && job.status === "done") onDone?.();
    wasRunning.current = job.status === "running";
  }, [job.status]);

  // Before a job that holds a model on a device: where should it run? A choice the person asked to remember is used without asking; if the choices cannot be read the job starts as it
  // always did, with the device its stage defaults to.
  const chooseDevice = async (): Promise<Device | "cancel" | null> => {
    let data: ComputeChoices, remembered: Device | undefined;
    try {
      const [d, settings] = await Promise.all([api.computeChoices(stage, params?.run, params?.model), api.getSettings()]);
      data = d;
      remembered = rememberedOf(settings.compute_remember)[stage];
    } catch {
      return null;                                                  // ignored: without the choices the job starts as before, on the device its stage defaults to
    }
    if (remembered && data.choices.some((c) => c.device === remembered && c.available)) return remembered;
    const available = data.choices.filter((c) => c.available);
    const result = await new Promise<{ device: Device; remember: boolean } | null>((resolve) => {
      answer.current = resolve;
      setAsking({ data, initial: available.length === 1 ? available[0].device : null });
    });
    setAsking(null);
    if (!result) return "cancel";
    if (result.remember) {
      try {
        const current = rememberedOf((await api.getSettings()).compute_remember);
        await api.putSettings({ compute_remember: rememberedText({ ...current, [stage]: result.device }) });
      } catch { /* ignored: the choice is used for this job even when it could not be remembered */ }
    }
    return result.device;
  };

  const running = job.status === "running";
  const done = oneShot && job.status === "done";
  // Several consoles of one stage follow the same job when it was started elsewhere; drawing the same output in each of them is
  // wasted work, so only the console that started it (or one the reader opens) draws it.
  const follower = !startedHere.current && !showOutput && job.id != null;
  return (
    <div>
      {asking && <ComputeDialog data={asking.data} initial={asking.initial} onStart={(device, remember) => answer.current?.({ device, remember })} onCancel={() => answer.current?.(null)} />}
      <div className="flex items-center gap-3">
        <span className="shrink-0 whitespace-nowrap">
          {done ? (
            <span className="inline-flex h-9 items-center gap-1.5 px-1 text-[0.74rem] font-semibold uppercase tracking-[0.07em] text-ok">
              <CheckCircle2 size={16} aria-hidden />Done
            </span>
          ) : (
            <Button variant="primary" icon={<Icon.play />} loading={running} disabled={disabled && !running}
              onClick={async () => {
                if (confirmText && !(await confirm({ title: "Please confirm", description: confirmText, confirmLabel: label }))) return;
                let sent = params;
                if (DEVICE_STAGES.has(stage) && !(params && "device" in params)) {
                  const chosen = await chooseDevice();
                  if (chosen === "cancel") return;
                  if (chosen) sent = { ...params, device: chosen };
                }
                startedHere.current = true; setShowOutput(false); job.run(stage, sent, launch);
              }}>{label}</Button>
          )}
        </span>
        {running && <Button variant="ghost" onClick={job.cancel}>Stop</Button>}
        {job.status !== "idle" && <StatusPill status={job.status} />}
        {hint && job.status === "idle" && <span className="text-[0.78rem] text-muted">{hint}</span>}
      </div>

      {job.status !== "idle" && job.progress && Object.keys(job.progress).length > 0 && (
        <ProgressStrip p={job.progress} live={running} />
      )}

      {job.status === "error" && (job.failure || job.error) && (
        <div className="mt-3"><ErrorState error={job.failure ?? job.error} title={job.failure ? undefined : `The ${stage} run failed: ${job.error}`} /></div>
      )}

      {follower && job.status !== "idle" && (
        <p className="mt-2 text-[0.76rem] text-muted">
          This run was started elsewhere; its output is not drawn here.{" "}
          <button type="button" onClick={() => setShowOutput(true)} className="text-brand-strong hover:underline">Show output</button>
        </p>
      )}

      {!follower && job.status !== "idle" && (job.logs.length > 0 || job.failure == null) && (
        <JobTerminal title={stageLabel(stage)} rows={job.logs} earlier={job.earlier} onLoadEarlier={job.showEarlier} status={job.status} progress={job.progress} copyText={job.text} />
      )}
      {job.status === "idle" && !hint && (
        <div className="mt-3"><EmptyState icon={<Icon.engine />} title="Not run yet"
          hint="Launch the stage above; live output streams here." /></div>
      )}
    </div>
  );
}
