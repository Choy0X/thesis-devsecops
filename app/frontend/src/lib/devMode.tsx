import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

// Developer mode shows what the default views hide: hashes, job ids, run folders, config digests, file paths, raw logs and the technical
// details of an error. It is a per-computer preference kept in the browser's storage, and a badge in the header says when it is on.

const KEY = "aivf.developer";

const read = (): boolean => {
  try { return window.localStorage.getItem(KEY) === "1"; } catch { return false; }       // ignored: storage may be blocked; the mode is then off
};

type Ctx = { on: boolean; set: (on: boolean) => void };
const DevContext = createContext<Ctx>({ on: false, set: () => undefined });

export function DevModeProvider({ children, initial }: { children: ReactNode; initial?: boolean }) {
  const [on, setOn] = useState<boolean>(() => initial ?? read());
  const set = useCallback((next: boolean) => {
    setOn(next);
    try { window.localStorage.setItem(KEY, next ? "1" : "0"); } catch { /* ignored: the choice then lasts until the window closes */ }
  }, []);
  const value = useMemo(() => ({ on, set }), [on, set]);
  return <DevContext.Provider value={value}>{children}</DevContext.Provider>;
}

export const useDevMode = (): [boolean, (on: boolean) => void] => {
  const { on, set } = useContext(DevContext);
  return [on, set];
};

/** Renders its children only while Developer mode is on. */
export function Dev({ children }: { children: ReactNode }) {
  const [on] = useDevMode();
  return on ? <>{children}</> : null;
}
