import { useSyncExternalStore } from "react";
import { api, type CampaignListing, type CampaignRow } from "./api";

// The list of campaigns, asked for once for the whole app instead of once by each component that shows it. It is polled only while somebody is
// watching and the window is visible: every few seconds while a campaign runs or is pausing, slowly otherwise. Listeners hear about a change only
// when what the list shows changed.

const FAST = 3000;
const IDLE = 15000;

export type CampaignsSnapshot = { loaded: boolean; error: unknown; campaigns: CampaignRow[]; interrupted: string[]; owners: Record<string, string> };
const EMPTY: CampaignsSnapshot = { loaded: false, error: null, campaigns: [], interrupted: [], owners: {} };

let snap: CampaignsSnapshot = EMPTY;
let key = "";
let timer: number | null = null;
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();

export const isBusy = (c: Pick<CampaignRow, "state">) => c.state === "running" || c.state === "pausing";

async function load() {
  if (loading) return loading;
  loading = (async () => {
    try {
      const data: CampaignListing = await api.campaigns();
      const next = JSON.stringify(data);
      if (next !== key || snap.error) {
        key = next;
        snap = { loaded: true, error: null, campaigns: data.campaigns, interrupted: data.interrupted, owners: data.owners };
        listeners.forEach((l) => l());
      }
    } catch (e) {
      snap = { ...snap, loaded: true, error: e };                    // kept visible until the next poll succeeds; the rows already read stay
      key = "";
      listeners.forEach((l) => l());
    } finally { loading = null; }
  })();
  return loading;
}

function schedule() {
  if (timer != null || listeners.size === 0) return;
  timer = window.setTimeout(async () => {
    timer = null;
    if (!document.hidden) await load();
    schedule();
  }, snap.campaigns.some(isBusy) ? FAST : IDLE);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) { load().then(schedule); }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer != null) { clearTimeout(timer); timer = null; }
  };
}

/** Ask now (a campaign was just started, paused, resumed or deleted). */
export function refreshCampaigns(): Promise<void> { return load().then(schedule); }

/** The campaigns, newest first; polled while a component uses this. */
export function useCampaigns(): CampaignsSnapshot {
  return useSyncExternalStore(subscribe, () => snap);
}

/** Forget everything (tests start each case from nothing). */
export function resetCampaigns() {
  snap = EMPTY;
  key = "";
  loading = null;
  if (timer != null) { clearTimeout(timer); timer = null; }
  listeners.clear();
}
