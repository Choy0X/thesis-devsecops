import { useCallback, useEffect, useRef, useState } from "react";
import { Check, X } from "lucide-react";
import { api, ApiError, errorText, type ResetCategory, type ResetInventory, type ResetStatus } from "../lib/api";
import { bytes as size, num } from "../lib/format";
import { useJobs } from "../lib/jobsList";
import { stageLabel } from "../lib/stageLabels";
import { Button, Skeleton } from "./ui";
import { Checkbox, SettingRow, SettingsCard, TextField } from "./kit";
import { Dialog } from "./hud/Dialog";

// Hard reset: delete what the app has stored, with the exact sizes shown first. The page only asks; every rule (what may be deleted, the token of the list, the typed word, no job
// running, links not followed, only the app's own databases) is checked by the server (pipeline/hard_reset.py). The categories that cost the most to get back (models, tools, settings and
// keys) start unticked, each with its own warning. "Delete everything" ticks all of them. When it is done the app is restarted, because its stores are empty.

const POLL_MS = 500;
type Phase = "list" | "running" | "done";

export const selectedSize = (rows: ResetCategory[], picked: Set<string>) => rows.filter((r) => picked.has(r.id)).reduce((n, r) => n + r.bytes, 0);
const available = (r: ResetCategory) => r.present && !r.blocked;

function Row({ row, picked, toggle }: { row: ResetCategory; picked: boolean; toggle: (id: string) => void }) {
  const off = !available(row);
  return (
    <li className={`border ${picked ? "border-crit-line bg-crit-tint" : "border-line bg-surface-2"} px-3 py-2.5`}>
      <div className="flex items-start justify-between gap-3">
        <Checkbox checked={picked && !off} disabled={off} onCheckedChange={() => toggle(row.id)} label={row.label}
          description={row.blocked ?? (row.present ? row.note : `Nothing is stored here. ${row.note}`)} />
        <span className="tnum shrink-0 pt-0.5 font-mono text-[0.8rem]" aria-label={`${row.label}: size`}>{off ? "" : `${size(row.bytes)}${row.databases.length ? "" : ` · ${num(row.files)} files`}`}</span>
      </div>
      {row.databases.length > 0 && (
        <ul className="ml-7 mt-1.5 space-y-0.5 text-[0.76rem] text-muted" aria-label="Databases">
          {row.databases.map((d) => <li key={d.name} className="flex justify-between gap-3"><span className="font-mono">{d.name}</span><span className="tnum">{size(d.bytes)} · {num(d.documents)} documents</span></li>)}
        </ul>)}
      {row.warning && !off && <p className={`ml-7 mt-1.5 text-[0.76rem] ${picked ? "text-crit-soft" : "text-warn"}`}>{row.warning}</p>}
    </li>
  );
}

export function HardResetDialog({ open, onClose, pollMs = POLL_MS }: { open: boolean; onClose: () => void; pollMs?: number }) {
  const jobs = useJobs();
  const running = jobs.filter((j) => j.status === "running");
  const [phase, setPhase] = useState<Phase>("list");
  const [inv, setInv] = useState<ResetInventory | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [typed, setTyped] = useState("");
  const [backup, setBackup] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [status, setStatus] = useState<ResetStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const measure = useCallback(async (refresh: boolean) => {
    setError(""); setInv(null);
    try {
      let got = await api.resetInventory(refresh);
      while (alive.current && !got.done && !got.error) {
        setInv(got);
        await new Promise((r) => setTimeout(r, pollMs));
        got = await api.resetInventory(false);
      }
      if (!alive.current) return;
      if (got.error) { setError(`The list could not be made: ${got.error}`); return; }
      setInv(got);
      setPicked(new Set(got.categories.filter((c) => c.default && available(c)).map((c) => c.id)));
    } catch (e) { if (alive.current) setError(errorText(e)); }
  }, [pollMs]);
  useEffect(() => {
    if (!open) return;
    setPhase("list"); setTyped(""); setBackup(false); setStatus(null); setNote("");
    void measure(true);
  }, [open, measure]);

  const rows = inv?.done ? inv.categories : [];
  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const everything = () => setPicked(new Set(rows.filter(available).map((r) => r.id)));
  const defaults = () => setPicked(new Set(rows.filter((r) => r.default && available(r)).map((r) => r.id)));
  const word = inv?.confirm_word ?? "RESET";
  const chosen = rows.filter((r) => picked.has(r.id) && available(r));
  const ready = !!inv?.done && inv.token !== null && chosen.length > 0 && typed.trim() === word && running.length === 0 && !starting;

  const go = async () => {
    if (!inv?.token) return;
    setStarting(true); setError("");
    try {
      let s = await api.resetStart({ token: inv.token, categories: chosen.map((r) => r.id), confirm: typed.trim(), backup: backup && picked.has("mongo") });
      setPhase("running"); setStatus(s);
      while (alive.current && !s.done) {
        await new Promise((r) => setTimeout(r, pollMs));
        s = await api.resetStatus();
        setStatus(s);
      }
      if (alive.current) setPhase("done");
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.code === "stale") { setNote("The list on the page was out of date. It has been measured again; choose again."); setTyped(""); void measure(true); }       // the list changed under the page
    } finally { if (alive.current) setStarting(false); }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o && phase !== "running") onClose(); }} className="w-[min(46rem,94vw)]"
      title="Hard reset" description="Delete what the app has stored. Nothing is deleted until you have typed the word below.">
      {phase === "list" && (
        <div className="space-y-4">
          {error && <p role="alert" className="text-[0.82rem] text-crit-soft">{error}</p>}
          {note && <p role="status" className="text-[0.82rem] text-warn">{note}</p>}
          {!inv?.done && !error && (
            <div role="status" className="space-y-2 text-[0.82rem] text-muted">
              <p>Measuring what is stored... {inv?.progress.category ? `${inv.progress.category}: ${num(inv.progress.files)} files, ${size(inv.progress.bytes)}` : ""}</p>
              <Skeleton className="h-24 w-full" />
            </div>)}
          {inv?.done && (
            <>
              <ul className="space-y-2" aria-label="What can be deleted">{rows.map((r) => <Row key={r.id} row={r} picked={picked.has(r.id)} toggle={toggle} />)}</ul>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-[0.82rem]" role="status">Chosen: <b className="font-mono">{size(selectedSize(chosen, picked))}</b> in {chosen.length} {chosen.length === 1 ? "category" : "categories"}.</p>
                <div className="flex gap-2">
                  <Button variant="ghost" size="sm" onClick={defaults}>Default choice</Button>
                  <Button variant="subtle" size="sm" onClick={everything}>Delete everything</Button>
                </div>
              </div>
              <p className="border border-line bg-surface-2 px-3 py-2 text-[0.78rem] text-muted">{inv.docker_note} The code, the tests, the notes and the Python environments are never deleted.</p>
              {picked.has("mongo") && <Checkbox checked={backup} onCheckedChange={setBackup} label="Back up the databases first"
                description="Dumps each database with mongodump into the folder of backups and checks the file can be read. If that fails, nothing is deleted. The new archive is kept even if the backups are deleted too." />}
              {running.length > 0 && <p role="alert" className="text-[0.82rem] text-warn">Jobs are running ({[...new Set(running.map((j) => stageLabel(j.stage)))].join(", ")}). A reset waits until they are done or stopped.</p>}
              <TextField label={`Type ${word} to continue`} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} inputClassName="font-mono"
                hint="It is checked again by the app itself before anything is deleted." />
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={onClose}>Cancel</Button>
                <Button variant="critical" disabled={!ready} loading={starting} onClick={go}>Delete what is chosen</Button>
              </div>
            </>)}
        </div>
      )}
      {phase !== "list" && (
        <div className="space-y-3" aria-live="polite">
          {error && <p role="alert" className="text-[0.82rem] text-crit-soft">{error}</p>}
          <ul className="space-y-1.5 text-[0.82rem]" aria-label="Steps of the reset">
            {(status?.steps ?? []).map((s, i) => (
              <li key={`${s.id}-${i}`} className="flex items-start gap-2">
                {s.ok ? <Check size={14} className="mt-0.5 text-ok" aria-hidden /> : <X size={14} className="mt-0.5 text-crit-soft" aria-hidden />}
                <span><span className="font-medium">{s.label}</span><span className="text-muted"> · {s.detail}</span></span>
              </li>))}
            {phase === "running" && <li className="text-faint">Working...</li>}
          </ul>
          {phase === "done" && (
            <>
              <p role="status" className={`text-[0.84rem] ${status?.ok ? "text-ok" : "text-warn"}`}>
                {status?.ok ? "Done. The app is empty now." : "Done, with problems (above)."}
                {status && status.pending > 0 ? ` ${status.pending} item${status.pending === 1 ? " is" : "s are"} in use and will be deleted at the next start.` : ""} Restart the app so that it starts from the empty state.
              </p>
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={onClose}>Close</Button>
                <Button variant="primary" onClick={() => { onClose(); window.dispatchEvent(new CustomEvent("app-reboot-request")); }}>Restart the app</Button>
              </div>
            </>)}
        </div>
      )}
    </Dialog>
  );
}

export function HardResetPanel() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <SettingsCard title="Hard reset" desc="Delete what the app has stored: the databases, the downloaded sources, the built datasets, the logs, the training runs and more. You see every size first and choose what goes.">
        <SettingRow label="Start over" hint="Models, installed tools and your saved keys are offered too, but never ticked for you. The code and the Python environments are never touched.">
          <Button variant="danger" onClick={() => setOpen(true)}>Hard reset...</Button>
        </SettingRow>
      </SettingsCard>
      {open && <HardResetDialog open onClose={() => setOpen(false)} />}
    </>
  );
}
