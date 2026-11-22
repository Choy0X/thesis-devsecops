import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { requestEpoch } from "../lib/request";

// A page that was visited stays mounted while another is shown (hidden, with its state, filters and scroll position intact), so
// coming back costs no requests. The price is that a hidden page must not keep working: it pauses its timers, and when it is brought
// back it refreshes what it shows only if that may have changed: something was written or a job ended while it was away (the
// request layer's epoch moved), or it was away for a long time.
type Ctx = { active: boolean; activations: number; refreshes: number };
const PageContext = createContext<Ctx>({ active: true, activations: 0, refreshes: 0 });

/** A page left alone this long is refreshed when it comes back, whatever happened meanwhile. */
export const STALE_AFTER_MS = 60_000;

/** Tells the page inside whether it is on screen, how many times it has been brought back since it was first shown, and how many of
 *  those times what it shows may be out of date. */
export function ActivePage({ active, children }: { active: boolean; children: ReactNode }) {
  const [activations, setActivations] = useState(0);
  const [refreshes, setRefreshes] = useState(0);
  const [was, setWas] = useState(active);
  const left = useRef<{ epoch: number; at: number } | null>(null);
  if (active !== was) {                               // derived while rendering, so `active` and the counters change in one commit
    setWas(active);
    if (active) {
      setActivations((n) => n + 1);
      const away = left.current;
      if (!away || away.epoch !== requestEpoch() || Date.now() - away.at > STALE_AFTER_MS) setRefreshes((n) => n + 1);
    }
  }
  useEffect(() => { if (!active) left.current = { epoch: requestEpoch(), at: Date.now() }; }, [active]);
  const value = useMemo(() => ({ active, activations, refreshes }), [active, activations, refreshes]);
  return <PageContext.Provider value={value}>{children}</PageContext.Provider>;
}

export const useActive = () => useContext(PageContext).active;

/** Calls `tick` every `ms` while the page is on screen, and once right away each time the page is brought back. The first call after
 *  mounting is the page's own load, so it is not made here. */
export function useWhileActive(tick: () => void, ms: number) {
  const { active, activations } = useContext(PageContext);
  const latest = useRef(tick);
  latest.current = tick;
  useEffect(() => {
    if (!active) return;
    if (activations > 0) latest.current();
    const timer = window.setInterval(() => latest.current(), ms);
    return () => window.clearInterval(timer);
  }, [active, activations, ms]);
}

/** Calls `refresh` when the page is brought back after having been hidden (not on the first mount), but only if what it shows may be
 *  out of date; with `always` on every return. */
export function useOnActivate(refresh: () => void, options: { always?: boolean } = {}) {
  const { active, activations, refreshes } = useContext(PageContext);
  const latest = useRef(refresh);
  latest.current = refresh;
  const count = options.always ? activations : refreshes;
  const handled = useRef(0);
  useEffect(() => {
    if (active && count > handled.current) { handled.current = count; latest.current(); }   // once per return that asked for it
  }, [active, count]);
}
