import { createContext, useCallback, useContext, useEffect, useRef, type ReactNode } from "react";
import { getRequestScope, setRequestScope } from "../lib/request";

const ScopeContext = createContext<AbortController | null>(null);

/** Capture the owner when a parent and its tab content have different lifetimes. */
export function usePageRequest() {
  const owner = useContext(ScopeContext);
  return useCallback(<T,>(request: () => T): T => {
    const previous = getRequestScope();
    setRequestScope(owner);
    try { return request(); } finally { setRequestScope(previous); }
  }, [owner]);
}

/** Everything inside belongs to one page: the GET requests it starts are aborted when the page is left, so they do not queue behind
 *  the few connections a browser allows per host. Writes are not affected.
 *
 *  The scope is set while rendering so the page's own effects see it. The abort is deferred by one tick, so React's development
 *  re-mount (unmount and mount again in a row) does not cancel the requests of a page that stays. */
export function PageScope({ children }: { children: ReactNode }) {
  const scope = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  if (!scope.current || scope.current.signal.aborted) scope.current = new AbortController();
  setRequestScope(scope.current);

  useEffect(() => {
    mounted.current = true;
    const mine = scope.current!;
    return () => {
      mounted.current = false;
      setTimeout(() => {
        if (mounted.current) return;
        mine.abort();
        if (getRequestScope() === mine) setRequestScope(null);
      }, 0);
    };
  }, []);

  return <ScopeContext.Provider value={scope.current}>{children}</ScopeContext.Provider>;
}
