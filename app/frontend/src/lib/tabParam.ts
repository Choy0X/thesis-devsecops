import { useCallback, useEffect } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useWorkspaceState } from "./workspaceState";

/** The tab of a page lives in the address (`?tab=review`), so a deep link, the back button and a reload all return to the same view. A
 *  value that is not one of the page's tabs falls back to the first. */
export function useTabParam(tabs: readonly string[]): [string, (tab: string) => void] {
  const [params, setParams] = useSearchParams();
  const { pathname } = useLocation();
  const [remembered, setRemembered] = useWorkspaceState(`tabs.${pathname}`, tabs[0]);
  const asked = params.get("tab") ?? "";
  const tab = tabs.includes(asked) ? asked : asked ? tabs[0] : tabs.includes(remembered) ? remembered : tabs[0];
  useEffect(() => { if (tab !== remembered) setRemembered(tab); }, [tab, remembered]); // eslint-disable-line react-hooks/exhaustive-deps
  const setTab = useCallback((next: string) => {
    setParams((current) => {
      const copy = new URLSearchParams(current);
      // An explicit first-tab choice must remain distinguishable from visiting
      // the page without a tab, which restores the remembered selection.
      copy.set("tab", next);
      return copy;
    }, { replace: false });
  }, [setParams, tabs]);
  return [tab, setTab];
}
