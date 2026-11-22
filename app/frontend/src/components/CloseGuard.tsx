import { useEffect, useRef, useState } from "react";
import { Check, X } from "lucide-react";
import { api, errorText, type ShutdownStatus } from "../lib/api";
import { useJobs } from "../lib/jobsList";
import { useUnsavedFiles } from "../lib/fileDrafts";
import { stageLabel } from "../lib/stageLabels";
import { useConfirm } from "./hud/AlertDialog";
import { Progress } from "./hud/Progress";
import { Button } from "./kit";

// Closing the window asks the page first (app/window_close.py sends `app-close-request`), and Settings can ask the same ("Quit app") or ask for a restart (`app-reboot-request`).
// With jobs running the person always decides; with none, the question "Quit the app?" is asked unless the person ticked "Do not ask me again" (the setting confirm_quit). Then the
// shutdown runs (jobs stopped and recorded, the model gateway and the databases closed) while this overlay shows each step, and only when it has finished does the window close, or,
// for a restart, does the page ask the app to start again and wait for it to answer. A second close while this is going on changes nothing. Force quit closes at once, and the
// launcher's own shutdown still runs after the window is gone.

const POLL_MS = 300;
const BACK_POLL_MS = 1000;
const BACK_LIMIT_MS = 120_000;

type Mode = "quit" | "reboot";

// A default that is a function made inside the component would be a new value on every render, the effect below would start over each time and stop the close in flight (found by trying a
// real restart: the shutdown started and the page never followed it). So the default is made once, here.
const reloadThePage = () => window.location.reload();

export function CloseGuard({ pollMs = POLL_MS, backPollMs = BACK_POLL_MS, backLimitMs = BACK_LIMIT_MS, reload = reloadThePage }:
  { pollMs?: number; backPollMs?: number; backLimitMs?: number; reload?: () => void }) {
  const confirm = useConfirm();
  const jobs = useJobs();
  const running = jobs.filter((j) => j.status === "running");
  const latest = useRef(running);
  latest.current = running;
  const unsaved = useUnsavedFiles();
  const latestUnsaved = useRef(unsaved);
  latestUnsaved.current = unsaved;
  const busy = useRef(false);
  const [mode, setMode] = useState<Mode>("quit");
  const [status, setStatus] = useState<ShutdownStatus | null>(null);
  const [started, setStarted] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [starting, setStarting] = useState(false);                   // a restart: the new copy is being waited for
  const [failure, setFailure] = useState("");                        // a restart that did not happen: the app stays open and says why

  useEffect(() => {
    let gone = false;                                                // the guard was unmounted: a close in flight stops following the shutdown
    const handle = async (m: Mode) => {
      if (busy.current) return;                                      // a second click on close while this runs changes nothing
      busy.current = true;
      const open = latest.current;
      const verb = m === "quit" ? "quit" : "restart";
      const drafts = latestUnsaved.current;
      if (drafts) {                                                  // editor drafts exist only in this page: closing it loses them
        const sure = await confirm({
          title: drafts === 1 ? "A file has unsaved edits" : `${drafts} files have unsaved edits`, tone: "critical",
          description: `Projects has workspace file edits that were not saved. ${m === "quit" ? "Closing" : "Restarting"} the app discards them. Keep the app open to save them first.`,
          confirmLabel: `Discard the edits and ${verb}`, cancelLabel: "Keep the app open" });
        if (!sure) { busy.current = false; return; }
      }
      if (open.length) {
        const names = [...new Set(open.map((j) => stageLabel(j.stage)))].join(", ");
        const sure = await confirm({
          title: open.length === 1 ? "A job is still running" : `${open.length} jobs are still running`, tone: "critical",
          description: `${names}. ${m === "quit" ? "Closing" : "Restarting"} the app stops ${open.length === 1 ? "it" : "them"}; the work done so far is kept, and it can be started again.`,
          confirmLabel: `Stop the jobs and ${verb}`, cancelLabel: "Keep the app open" });
        if (!sure) { busy.current = false; return; }
      } else if (m === "reboot") {
        const sure = await confirm({ title: "Restart the app?", description: "It stops what is running, saves its state and starts again with the saved settings. It takes a few seconds.",
          confirmLabel: "Restart the app", cancelLabel: "Keep it as it is" });
        if (!sure) { busy.current = false; return; }
      } else {
        const settings = await api.getSettings().catch(() => null);          // ignored: without the setting the question is asked
        if (settings?.confirm_quit !== "off") {
          const sure = await confirm({ title: "Quit the app?", description: "It stops what is running, saves its state and closes. Nothing is running now.",
            confirmLabel: "Quit the app", cancelLabel: "Keep it open", rememberLabel: "Do not ask me again",
            onRemember: (checked) => { if (checked) api.putSettings({ confirm_quit: "off" }).catch(() => undefined); } });         // ignored: the question is then asked again next time
          if (!sure) { busy.current = false; return; }
        }
      }
      setMode(m); setFailure(""); setStarting(false); setStarted(Date.now());
      try {
        let s = await api.shutdown();
        setStatus(s);
        while (!s.done && !gone) {
          await new Promise((r) => setTimeout(r, pollMs));
          if (gone) return;
          s = await api.shutdownStatus();
          setStatus(s);
        }
      } catch { /* ignored: if the shutdown cannot be followed, the window still closes below */ }
      if (gone) return;
      if (m === "quit") {
        await api.shellQuit().catch(() => undefined);                // ignored: with no window of its own there is nothing to close
        return;
      }
      try {
        const r = await api.shellReboot();
        if (!r.ok) { setFailure(r.detail || "The app could not start again."); busy.current = false; return; }
      } catch (e) { setFailure(errorText(e)); busy.current = false; return; }
      setStarting(true);                                             // the window goes with the old copy; a browser tab waits for the new one and reloads
      const begun = Date.now();
      let wentDown = false;
      while (!gone && Date.now() - begun < backLimitMs) {
        await new Promise((r) => setTimeout(r, backPollMs));
        const up = await fetch("/api/health", { cache: "no-store" }).then((r) => r.ok).catch(() => false);
        if (!up) wentDown = true;
        else if (wentDown) { reload(); return; }
      }
      if (!gone) setFailure("The app did not answer again. Start it from the launcher.");
    };
    const onClose = () => {
      api.shellCloseAck().catch(() => undefined);                    // ignored: without the acknowledgement the window closes on its own after a few seconds
      void handle("quit");
    };
    const onReboot = () => { void handle("reboot"); };
    window.addEventListener("app-close-request", onClose);
    window.addEventListener("app-reboot-request", onReboot);
    return () => { gone = true; window.removeEventListener("app-close-request", onClose); window.removeEventListener("app-reboot-request", onReboot); };
  }, [confirm, pollMs, backPollMs, backLimitMs, reload]);

  useEffect(() => {
    if (!started) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [started]);

  if (!started) return null;
  const restarting = mode === "reboot";
  const title = restarting ? "Restarting the app" : "Closing the app";
  const deadline = status?.deadline_seconds ?? 25;
  const left = Math.max(0, Math.ceil(deadline - (now - started) / 1000));
  return (
    <div role="dialog" aria-modal="true" aria-label={title} className="fixed inset-0 z-dialog grid place-items-center bg-bg">
      <div className="hud-brackets chamfer-r w-[min(30rem,92vw)] border border-line-strong bg-surface p-6 shadow-pop [--ch-line:var(--border-strong)]">
        <h2 className="text-[1rem] font-semibold">{title}</h2>
        {failure ? <p role="alert" className="mt-2 text-[0.84rem] text-crit-soft">{failure}</p>
          : starting ? <p role="status" className="mt-1 text-[0.82rem] text-muted">The app is starting again. This page opens again by itself when it answers.</p>
          : <p className="mt-1 text-[0.82rem] text-muted">Stopping what is running and saving the state. {restarting ? "It starts again" : "It closes"} by itself in at most {left} s.</p>}
        <div className="mt-4"><Progress value={Math.min(status?.steps.length ?? 0, 4)} max={4} segments={16} label={restarting ? "Restart progress" : "Closing progress"} /></div>
        <ul className="mt-4 space-y-1.5 text-[0.82rem]" aria-label={restarting ? "Restart steps" : "Closing steps"}>
          {(status?.steps ?? []).map((s) => (
            <li key={s.name} className="flex items-start gap-2">
              {s.ok ? <Check size={14} className="mt-0.5 text-ok" aria-hidden /> : <X size={14} className="mt-0.5 text-crit-soft" aria-hidden />}
              <span><span className="font-medium">{s.name}</span>{s.detail ? <span className="text-muted"> · {s.detail}</span> : null}</span>
            </li>
          ))}
          {!status?.done && !failure && <li className="text-faint">Working...</li>}
        </ul>
        {status?.done && !status.ok && !failure && <p className="mt-3 text-[0.8rem] text-crit-soft">A step did not finish cleanly (listed above). The app {restarting ? "restarts" : "closes"} anyway.</p>}
        <div className="mt-5 flex justify-end gap-2">
          {failure && restarting && <Button variant="subtle" onClick={() => { setStarted(0); setFailure(""); busy.current = false; }}>Keep using the app</Button>}
          {!restarting && <Button variant="subtle" onClick={() => api.shellQuit().catch(() => undefined) /* ignored: with no window of its own there is nothing to close */}>Force quit</Button>}
        </div>
      </div>
    </div>
  );
}
