import { useEffect, useState } from "react";
import { api, type DbStatus } from "../lib/api";
import { Button } from "./ui";

// Local storage failures stay visible until a later check succeeds.
export function DatabaseBanner({ pollMs = 10_000 }: { pollMs?: number }) {
  const [st, setSt] = useState<DbStatus | null>(null);
  const load = () => api.dbStatus().then(setSt).catch(() => undefined);   // ignored: a failed poll keeps the last answer
  useEffect(() => {
    load();
    const timer = window.setInterval(load, pollMs);
    return () => window.clearInterval(timer);
  }, [pollMs]);
  if (!st || st.reachable) return null;

  return (
    <div role="alert" className="border-b border-crit-line bg-crit-tint px-6 py-3 text-[0.82rem]">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1">
          <b className="font-medium text-crit">The local database cannot be used.</b>{" "}
          <span className="text-muted">
            {st.reason || "Check the database folder's permissions and available disk space on the Database page."}
            {st.path && <span className="block break-all font-mono text-[0.74rem] text-faint">{st.path}</span>}
          </span>
        </div>
        <Button variant="ghost" onClick={load}>Check again</Button>
      </div>
    </div>
  );
}
