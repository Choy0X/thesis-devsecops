import { useEffect, useRef, useState } from "react";
import { api, type Job } from "./api";
import { stageLabel } from "./stageLabels";
import { useJobs } from "./jobsList";
import { useToast } from "../components/Toast";

// A job that was running and has now ended tells the person once, on whatever page they are on: finished, stopped with an error, or cut off when the app
// closed. Jobs that were already over when the page opened say nothing (they are in the Jobs tray). The words carry no ids, hashes or paths.

export type JobAlert = { tone: "success" | "error"; text: string; key: string };

/** What to tell, and the statuses to remember for the next time. `previous` is null before the first list has been seen. */
export function alertsFor(previous: Map<string, string> | null, jobs: Job[]): { alerts: JobAlert[]; next: Map<string, string> } {
  const next = new Map(jobs.map((j) => [j.id, j.status] as const));
  const alerts: JobAlert[] = [];
  if (previous) {
    for (const j of jobs) {
      if (previous.get(j.id) !== "running" || j.status === "running") continue;
      if (j.stage === "qualify_npm_dataset" && j.status === "done" && j.progress?.state === "waiting") continue;
      const name = stageLabel(j.stage);
      if (j.status === "done") alerts.push({ tone: "success", text: `${name} finished.`, key: j.id });
      else if (j.status === "error") alerts.push({ tone: "error", text: `${name} stopped with an error. Open the Jobs list to see why.`, key: j.id });
      else if (j.status === "interrupted") alerts.push({ tone: "error", text: `${name} was cut off when the app closed.`, key: j.id });
    }
  }
  return { alerts, next };
}

/** The window is minimised, behind another one or on another desktop: a toast inside it would not be seen. */
export const windowInBackground = (): boolean => document.hidden || !document.hasFocus();

export const APP_NAME = "AI Vulnerability Framework";

// The switch is kept in settings.json (desktop_notify), not in the page's own storage, which is lost whenever the address of the app changes. It is on until the settings say
// otherwise; the page reads it once and every holder of the switch (the Jobs tray, Settings) follows the same value.
let enabled = true;
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();
const tell = () => listeners.forEach((l) => l());

export const alertsEnabled = (): boolean => enabled;

/** Reads the switch from the settings, once per page. A failed read leaves the alerts on and is tried again the next time. */
export function loadAlertsSetting(): Promise<void> {
  loading ??= api.getSettings().then((s) => { enabled = s.desktop_notify !== "off"; tell(); }).catch(() => { loading = null; });         // ignored: the alerts stay on
  return loading;
}

/** Turns the alerts on or off at once and saves the choice; when it cannot be saved the switch goes back and the error is thrown. */
export async function setAlertsEnabled(on: boolean): Promise<void> {
  const before = enabled;
  enabled = on; tell();
  try { await api.putSettings({ desktop_notify: on ? "" : "off" }); }
  catch (e) { enabled = before; tell(); throw e; }
}

/** For tests: back to the state before the settings were read. */
export function resetAlerts(): void { enabled = true; loading = null; }

/** Shows the alerts of the shared jobs list as toasts. Mount it once. */
export function useJobAlerts(): void {
  const jobs = useJobs();
  const toast = useToast();
  useEffect(() => { loadAlertsSetting(); }, []);
  const seen = useRef<Map<string, string> | null>(null);
  const told = useRef(new Set<string>());
  useEffect(() => {
    if (jobs.length === 0 && seen.current === null) return;            // the first answer has not come yet
    const { alerts, next } = alertsFor(seen.current, jobs);
    seen.current = next;
    if (!alertsEnabled()) return;
    for (const a of alerts) {
      if (told.current.has(a.key)) continue;
      told.current.add(a.key);
      if (a.tone === "success") toast.success(a.text); else toast.error(a.text);
      if (windowInBackground()) api.shellNotify(APP_NAME, a.text).catch(() => undefined);      // ignored: a missed native notification leaves the toast and the Jobs list
    }
  }, [jobs, toast]);
}

/** The switch, for the Jobs tray and Settings: its value and a function that sets and saves it. */
export function useAlertsSwitch(): [boolean, (on: boolean) => Promise<void>] {
  const [on, setOn] = useState(alertsEnabled);
  useEffect(() => {
    const follow = () => setOn(alertsEnabled());
    listeners.add(follow);
    loadAlertsSetting();
    follow();
    return () => { listeners.delete(follow); };
  }, []);
  return [on, setAlertsEnabled];
}
