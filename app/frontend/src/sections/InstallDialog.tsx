import { useEffect, useState } from "react";
import { Button } from "../components/ui";
import { Dialog } from "../components/hud/Dialog";
import { useConfirm } from "../components/hud/AlertDialog";
import { RunConsole } from "../components/RunConsole";
import { useToast } from "../components/Toast";
import { api, errorText, type EcosystemUpdate } from "../lib/api";
import { refreshJobs } from "../lib/jobsList";

// Installing one OSV dump from a dialog: download it, read it, update it or just ask OSV whether its copy is the newest. The two steps (download, then read) show their own numbers; the
// working store is not touched. A download or a read that was cut keeps its progress: the dialog says where it stopped, Continue picks up from there, and Start over (after a confirmation)
// throws the saved work away. Used by the Ecosystems table and by Find an advisory (for a dump that was downloaded but not read yet).

export type Mode = "download" | "read" | "update" | "check";
export const VERB: Record<Mode, string> = { download: "Download", read: "Read", update: "Update", check: "Check" };
/** What was left unfinished by a cut: which step, and how far it got (percent is null when the size is not known). */
export type Stopped = { kind: "read" | "download"; percent: number | null };

const TITLE: Record<Mode, (n: string) => string> = {
  download: (n) => `Download the ${n} dump`, read: (n) => `Read the ${n} dump`, update: (n) => `Update the ${n} dump`, check: (n) => `Check the ${n} dump`,
};
const STOPPED_TITLE = { read: (n: string) => `Continue reading the ${n} dump`, download: (n: string) => `Continue the ${n} download` };
const DESCRIPTION: Record<Mode, string> = {
  download: "Fetches OSV's copy of this ecosystem, then reads it into the table that Find an advisory searches. Some dumps are hundreds of megabytes, so it needs the internet and disk space. Your data store is not changed.",
  read: "Reads the dump that is already on this computer into the table that Find an advisory searches. Nothing is downloaded and your data store is not changed.",
  update: "Fetches OSV's newer copy of this ecosystem, then reads it again. Some dumps are hundreds of megabytes, so it needs the internet and disk space.",
  check: "Asks OSV whether its copy changed. If it did, Update fetches it and reads it again; if not, nothing is downloaded.",
};

export function InstallDialog({ name, mode, nonce, onClose, onDone, stopped }: { name: string; mode: Mode; nonce: number; onClose: () => void; onDone: () => void; stopped?: Stopped }) {
  const confirm = useConfirm();
  const toast = useToast();
  const params = mode === "download" ? { ecosystem: name, refresh: false } : mode === "read" ? { ecosystem: name, skip_download: true } : { ecosystem: name, refresh: true };
  const hint = stopped ? "Continues from where it stopped." : mode === "read" ? "Nothing is fetched." : mode === "download" ? "Download, then read: two steps, each with its own numbers." : "Asks OSV whether its copy changed; nothing is fetched if it did not.";
  const where = stopped ? (stopped.percent == null ? "It stopped part of the way." : `It stopped at ${stopped.percent}%.`) : "";
  const startOver = async () => {
    const what = stopped?.kind === "download" ? "the part of the download already on the disk" : "the part of the dump already read";
    if (!(await confirm({ title: "Start over?", description: `This throws away ${what} (${where.replace(/^It stopped /, "stopped ")}) and does the step again from the beginning.`, confirmLabel: "Start over" }))) return;
    try {
      await api.startJob("osv_install", { ...params, fresh: true });
      refreshJobs();                                                       // the console below follows the job that is now running
    } catch (e) { toast.error(`Could not start over: ${errorText(e)}`); }
  };
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={stopped ? STOPPED_TITLE[stopped.kind](name) : TITLE[mode](name)} description={DESCRIPTION[mode]}>
      {stopped && (
        <div role="status" className="mb-3 flex flex-wrap items-center gap-3 border border-warn-line bg-warn-tint px-3 py-2 text-[0.8rem]">
          <span className="text-warn">{where} Continue picks up from there.</span>
          <Button variant="ghost" onClick={startOver}>Start over</Button>
        </div>
      )}
      {(mode === "update" || mode === "check") && !stopped && <UpdateCheck name={name} nonce={nonce} />}
      <RunConsole stage="osv_install" label={stopped ? "Continue" : VERB[mode === "check" ? "update" : mode]} params={params} onDone={onDone} hint={hint} match={(j) => j.params?.ecosystem === name} oneShot />
    </Dialog>
  );
}

const UPDATE_WORDS: Record<EcosystemUpdate["state"], string> = {
  current: "The copy on this computer is the newest one.",
  update_available: "OSV has a newer copy. Update to fetch it.",
  unreachable: "OSV could not be reached, so nothing is known.",
  unknown: "This could not be decided.",
  not_downloaded: "There is no copy on this computer yet.",
  not_listed: "OSV does not publish this ecosystem.",
};

/** Asks OSV, with one small request and no download, whether the dump on this computer is the newest; it asks when the dialog opens and again whenever ``nonce`` changes (after a job). */
function UpdateCheck({ name, nonce }: { name: string; nonce: number }) {
  const [res, setRes] = useState<EcosystemUpdate | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const ask = () => { setBusy(true); setProblem(""); api.ecosystemUpdate(name).then(setRes).catch((e) => setProblem(errorText(e))).finally(() => setBusy(false)); };
  useEffect(ask, [name, nonce]);   // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="mb-3 flex flex-wrap items-center gap-3 text-[0.8rem]">
      <Button variant="ghost" loading={busy} onClick={ask}>Check for a newer copy</Button>
      {res && <span role="status" className={res.state === "update_available" ? "text-warn" : "text-muted"}>{UPDATE_WORDS[res.state]}{res.detail && res.state !== "current" ? ` ${res.detail}` : ""}</span>}
      {problem && <span role="alert" className="text-crit-soft">{problem}</span>}
    </div>
  );
}
