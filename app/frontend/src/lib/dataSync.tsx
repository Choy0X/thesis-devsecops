import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { api } from "./api";
import { invalidateRequestResource } from "./request";
import { Hint } from "../components/kit";

// Live data for the Data page. The backend's /api/data/stamp changes whenever a job
// or an edit writes records, fix commits, code pairs or a dataset build; this
// provider polls it (fast while a job runs) and bumps `version` when it changes.
// Panels reload when their (throttled) version changes; Refresh forces it at once.
//
// The context holds only what panels react to (version, forced) and a stable refresh function, so a poll that finds nothing new
// renders nothing. What the header button shows (running stages, last check, "refreshing") lives in a small store of its own.

type Sync = { version: number; forced: number; refresh: () => void };
const Ctx = createContext<Sync>({ version: 0, forced: 0, refresh: () => undefined });

type Status = { running: string[]; syncing: boolean; lastSync: number | null };
const INITIAL: Status = { running: [], syncing: false, lastSync: null };
let status: Status = INITIAL;
const listeners = new Set<() => void>();
const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

function setStatus(patch: Partial<Status>) {
  const next = { ...status, ...patch };
  if (sameList(next.running, status.running) && next.syncing === status.syncing && next.lastSync === status.lastSync) return;
  status = sameList(next.running, status.running) ? { ...next, running: status.running } : next;
  listeners.forEach((l) => l());
}
/** Forget the last status (tests start from nothing). */
export function resetSyncStatus() { status = INITIAL; listeners.forEach((l) => l()); }
const useStatus = () => useSyncExternalStore((cb) => { listeners.add(cb); return () => { listeners.delete(cb); }; }, () => status);

const FAST = 3000;    // while a job is running
const IDLE = 15000;

export function DataSyncProvider({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const [counts, setCounts] = useState({ version: 0, forced: 0 });      // one state: one render per real change
  const stamp = useRef<string | null>(null);
  const timer = useRef<number | null>(null);
  const runningRef = useRef<string[]>([]);

  // `bump` is false when the caller is already reloading everything (a manual refresh), so one change is never reloaded twice.
  const poll = useCallback(async (bump = true) => {
    try {
      const s = await api.dataStamp();
      runningRef.current = s.running ?? [];
      const changed = stamp.current !== null && s.stamp !== stamp.current;
      stamp.current = s.stamp;
      setStatus({ running: s.running ?? [], lastSync: Date.now() });
      if (changed && bump) { invalidateRequestResource('/api/data'); setCounts((c) => ({ ...c, version: c.version + 1 })); }
    } catch { /* ignored: backend busy or restarting; the next tick asks again */ }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const loop = async () => {
      if (!alive) return;
      if (!document.hidden) await poll();
      if (alive) timer.current = window.setTimeout(loop, runningRef.current.length ? FAST : IDLE);
    };
    loop();
    const onVisible = () => { if (!document.hidden) poll(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, poll]);

  const refresh = useCallback(() => {
    invalidateRequestResource('/api/data');
    setStatus({ syncing: true });
    setCounts((c) => ({ version: c.version + 1, forced: c.forced + 1 }));
    poll(false).finally(() => window.setTimeout(() => setStatus({ syncing: false }), 600));
  }, [poll]);

  const value = useMemo(() => ({ ...counts, refresh }), [counts, refresh]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useDataSync = () => useContext(Ctx);

/** The data version for one panel, throttled to at most one reload per
 *  `minIntervalMs` (heavy panels use a long interval). A manual Refresh is never
 *  throttled. Use it as an effect dependency to reload. */
export function useDataVersion(minIntervalMs = 3000): number {
  const { version, forced } = useContext(Ctx);
  const [out, setOut] = useState(0);
  const last = useRef(0);
  const lastForced = useRef(forced);
  const pending = useRef<number | null>(null);
  useEffect(() => {
    if (version === 0) return;
    const now = Date.now();
    const isForced = forced !== lastForced.current;
    lastForced.current = forced;
    const wait = isForced ? 0 : Math.max(0, last.current + minIntervalMs - now);
    if (pending.current) clearTimeout(pending.current);
    pending.current = window.setTimeout(() => {
      last.current = Date.now();
      pending.current = null;
      setOut((o) => o + 1);
    }, wait);
  }, [version, forced, minIntervalMs]);
  useEffect(() => () => { if (pending.current) clearTimeout(pending.current); }, []);
  return out;
}

const STAGE_LABEL: Record<string, string> = {
  ingest: "ingest", extract_fixes: "extract", mine_code: "mining", dep_versions: "dependency versions",
  enrich_pairs: "LLM enrichment", build_code_dataset: "building", dataset: "building",
  verify_fixes: "verifying", resplit_pairs: "re-splitting",
};

export function SyncButton() {
  const { refresh } = useDataSync();
  const { lastSync, running, syncing } = useStatus();
  const live = running.length > 0;
  return (
    <div className="ml-auto flex items-center gap-3 text-[0.76rem]">
      {live ? (
        <Hint label="Updating automatically every few seconds" className="flex items-center gap-1.5 text-ok no-underline">
          <span className="h-2 w-2 animate-pulse rounded-full bg-ok" />
          live · {running.map((s) => STAGE_LABEL[s] || s).join(", ")}
        </Hint>
      ) : (
        <Hint label="Checked for new data every 15 s" className="text-faint no-underline">
          {lastSync ? `synced ${new Date(lastSync).toLocaleTimeString()}` : "syncing…"}
        </Hint>
      )}
      <button type="button" onClick={refresh} disabled={syncing}
        className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-line bg-surface px-3 text-[0.8rem] hover:border-line-strong disabled:opacity-60">
        <svg viewBox="0 0 24 24" className={`h-3.5 w-3.5 ${syncing ? "animate-spin" : ""}`} fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M21 12a9 9 0 1 1-2.64-6.36" strokeLinecap="round" /><path d="M21 4v5h-5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Refresh
      </button>
    </div>
  );
}
