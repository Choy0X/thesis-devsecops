import { useSyncExternalStore } from "react";
import { api, type Job } from "./api";
import { notifyChanged, topicsOfFinished } from "./changes";

// The list of jobs, asked for once for the whole app instead of once by each console. It is polled only while somebody is watching
// and the window is visible (every few seconds while a job runs, slowly otherwise), and listeners hear about a change only when the
// set of running jobs changed, not on every poll.

const FAST = 3000;
const IDLE = 15000;

let jobs: Job[] = [];
let signature = "";
let trayKey = "";                                    // what the Jobs tray shows: every job's state and progress, not only which ones run
let finishedSeen: Set<string> | null = null;         // the jobs that change what the app lists (a download, an install ...) that are over and already told about (lib/changes.ts)
let timer: number | null = null;
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();
const trayListeners = new Set<() => void>();

const runningOf = (list: Job[]) => list.filter((j) => j.status === "running");

async function load() {
  if (loading) return loading;
  loading = (async () => {
    try {
      jobs = await api.listJobs();
      const finished = topicsOfFinished(jobs, finishedSeen);                    // a download, install or conversion that ended changes what the lists and the status say
      finishedSeen = finished.seen;
      if (finished.topics.length) notifyChanged(...finished.topics);
      const next = runningOf(jobs).map((j) => `${j.id}:${j.stage}`).join("|");
      const tray = jobs.map((j) => `${j.id}:${j.status}:${j.progress?.processed ?? ""}:${j.progress?.label ?? ""}`).join("|");
      if (next !== signature) { signature = next; listeners.forEach((l) => l()); }
      if (tray !== trayKey) { trayKey = tray; trayListeners.forEach((l) => l()); }
    } catch { /* ignored: the next poll asks again; a console just does not re-attach meanwhile */ }
    finally { loading = null; }
  })();
  return loading;
}

function schedule() {
  if (timer != null || listeners.size === 0) return;
  timer = window.setTimeout(async () => {
    timer = null;
    if (!document.hidden) await load();
    schedule();
  }, runningOf(jobs).length ? FAST : IDLE);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) { load().then(schedule); }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer != null) { clearTimeout(timer); timer = null; }
  };
}

/** Ask now (a job was just started). */
export function refreshJobs() { load().then(schedule); }

/** The id of the running job of a stage, or null; with ``match``, the first one it accepts (two downloads of different ecosystems are both "osv_install"). Changes only when a job of any stage starts or ends. */
export function runningJobId(stage: string, match?: (job: Job) => boolean): string | null {
  return runningOf(jobs).find((j) => j.stage === stage && (!match || match(j)))?.id ?? null;
}

export function useRunningJobId(stage: string, match?: (job: Job) => boolean): string | null {
  return useSyncExternalStore(subscribe, () => runningJobId(stage, match));
}

/** Every job the backend knows (newest first), refreshed while a component follows it; the Jobs tray reads this. */
export function useJobs(): Job[] {
  return useSyncExternalStore((listener) => {
    trayListeners.add(listener);
    const stop = subscribe(() => undefined);                 // the shared poll runs while the tray is mounted
    return () => { trayListeners.delete(listener); stop(); };
  }, () => jobs);
}

/** Forget everything (tests start each case from nothing). */
export function resetJobsList() {
  jobs = [];
  signature = "";
  trayKey = "";
  finishedSeen = null;
  trayListeners.clear();
  loading = null;
  if (timer != null) { clearTimeout(timer); timer = null; }
  listeners.clear();
}
