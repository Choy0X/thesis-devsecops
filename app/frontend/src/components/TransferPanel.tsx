import type { JobProgress } from "../lib/api";
import { bytes, clockSpan, num } from "../lib/format";
import { ErrorState } from "./ui";
import { ProgressStrip } from "./ProgressStrip";

// Where a model download or install is, in words and numbers: the model, a state (planning, downloading, finished, stopped, failed), and for each state what a person needs. While it runs: the bar,
// the percentage, the size done of the size planned, the speed, the time left, the files done and the file being fetched; an install says which of its steps it is on (fetching the converter
// scripts, downloading, converting). Finished: what arrived and how fast, or that the model is ready to run. Stopped: that what was fetched is kept, and that the button above continues it. Failed:
// the reason, in words. The terminal that goes with it is drawn by the caller, below this panel.

export type TransferState = "planning" | "downloading" | "finished" | "stopped" | "failed";

/** The state of a download from the status of its job and the last progress report it sent. A job that has not reported yet is still planning. */
export function transferState(status: string, p: JobProgress | null | undefined): TransferState {
  if (status === "done") return "finished";
  if (status === "cancelled") return "stopped";
  if (status === "error" || status === "interrupted") return "failed";
  return p && (p.state === "downloading" || (p.total != null && p.total > 0)) ? "downloading" : "planning";
}

const WORD: Record<TransferState, [string, string]> = {
  planning: ["Planning", "text-muted"], downloading: ["Downloading", "text-brand-strong"], finished: ["Finished", "text-ok"], stopped: ["Stopped", "text-warn"], failed: ["Failed", "text-crit-soft"],
};
const STEP_WORD: Record<string, string> = { scripts: "Fetching scripts", download: "Downloading", convert: "Converting" };

/** The word of the header: the state of a download, and for an install the step it is on while it runs and "Ready to run" at the end. */
export function stateWord(state: TransferState, p: JobProgress | null | undefined, install: boolean): [string, string] {
  if (install && state === "finished") return ["Ready to run", "text-ok"];
  if (install && state === "downloading") return [STEP_WORD[p?.phase ?? ""] ?? "Installing", "text-brand-strong"];
  return WORD[state];
}

/** The sentence of a job's error: its last line. A job's error is the end of what it printed (progress bars and all); the terminal under the panel has the rest. */
export function errorSentence(error: string | undefined): string {
  const lines = (error ?? "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  return lines[lines.length - 1] ?? "";
}

/** What a finished download says: the size, the files, the time and the speed it had. */
export function finishedWords(p: JobProgress | null | undefined): string {
  if (!p) return "The download is finished.";
  const size = p.total ?? p.processed;
  const what = [size != null ? bytes(size) : "", p.files_total ? `${num(p.files_total)} ${p.files_total === 1 ? "file" : "files"}` : ""].filter(Boolean).join(" in ") || "The model";
  if (p.seconds == null) return `${what} arrived.`;
  const took = p.seconds < 60 ? `${p.seconds.toFixed(1)} s` : clockSpan(p.seconds);
  return `${what}, took ${took}${size && p.seconds >= 1 ? ` (${bytes(size / p.seconds)}/s)` : ""}.`;
}

export function TransferPanel({ model, status, progress, error, failure, install = false, role = null, ollama = false }: {
  model: string;
  /** The status of the download's job (idle, running, done, cancelled, error, interrupted). */
  status: string;
  progress?: JobProgress | null;
  /** Why it failed: the job's own words, and the error the start was refused with when there was no job. */
  error?: string;
  failure?: unknown;
  /** The job installs the model (download, scripts, conversion), not only downloads it. */
  install?: boolean;
  /** What the model will be used as, for the words of the end of an install. */
  role?: "chat" | "embedding" | null;
  ollama?: boolean;
}) {
  const state = transferState(status, progress);
  const [word, tone] = stateWord(state, progress, install);
  const noun = install ? "install" : "download";
  const sentence = errorSentence(error);
  return (
    <section aria-label={`${install ? "Install" : "Download"} of ${model}`} data-state={state} className="hud-frame relative mt-3 border border-line bg-surface-2 px-4 py-3">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="break-all font-mono text-[0.82rem] font-medium">{model}</h3>
        <span className={`caps text-[0.7rem] ${tone}`}>{ollama && state === "finished" ? "Installed in Ollama" : word}</span>
      </header>
      <div className="mt-2">
        {state === "planning" && <p className="text-[0.8rem] text-muted">{(ollama && progress?.label) || (install ? "Checking what the install needs and getting ready." : "Reading the file list and getting ready to fetch it.")}</p>}
        {state === "downloading" && progress && <ProgressStrip p={progress} live bare />}
        {state === "finished" && ollama && <p role="status" className="text-[0.8rem]">{progress?.model_tag ? `${progress.model_tag} is available in Ollama.` : "The model is available in Ollama."}</p>}
        {state === "finished" && install && !ollama && (
          <div className="space-y-2">
            <p className="text-[0.8rem]">{model} is converted and ready to run{role ? ` as ${role === "chat" ? "a language" : "an embedding"} model` : ""}. Settings lists it by its name and format.</p>
            <p className="text-[0.76rem] text-muted">The downloaded files are kept: fine-tuning needs them, and the converted file can be made again from them.</p>
          </div>
        )}
        {state === "finished" && !install && (
          <div className="space-y-2">
            <p className="text-[0.8rem]">{finishedWords(progress)}</p>
            <p className="text-[0.76rem] text-muted">The model is in the cache of this computer and is listed under Installed models.</p>
          </div>
        )}
        {state === "stopped" && <p className="text-[0.8rem]">Stopped. What was fetched is kept; the button above, Continue the {noun}, picks up from there.</p>}
        {state === "failed" && (sentence || failure != null) && <ErrorState error={failure ?? sentence} title={failure ? undefined : `The ${noun} failed: ${sentence}`} />}
        {state === "failed" && !sentence && failure == null && <p className="text-[0.8rem] text-crit-soft">The {noun} stopped with an error.</p>}
      </div>
    </section>
  );
}
