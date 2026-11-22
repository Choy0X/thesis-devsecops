import { useEffect, useState } from "react";
import { api, type Job } from "../lib/api";
import { useWhileActive } from "../components/ActivePage";
import { Panel, StatusPill, EmptyState, ErrorState } from "../components/ui";
import { Icon } from "../components/Icon";

export function Logs() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = () => api.listJobs().then((j) => { setJobs(j); setError(null); }).catch(setError);
  useEffect(() => { load(); }, []);
  useWhileActive(load, 2000);                       // the list refreshes only while this page is on screen
  if (!jobs && error) return <Panel title="Activity log"><ErrorState error={error} title="Could not read the activity log" onRetry={load} /></Panel>;
  if (!jobs) return <Panel title="Activity log"><div className="h-24" /></Panel>;
  return (
    <Panel title="Activity log" desc="Every stage run this session, with status and timing.">
      {error != null && <div className="mb-3"><ErrorState error={error} title="The list could not be refreshed" onRetry={load} /></div>}
      {jobs.length === 0 ? <EmptyState icon={<Icon.logs />} title="No activity yet" hint="Runs you launch appear here with their status." /> : (
        <table className="w-full text-[0.82rem]">
          <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint">
            <th className="py-2 pr-4 font-medium">Stage</th><th className="py-2 pr-4 font-medium">Status</th>
            <th className="py-2 pr-4 font-medium">Started</th><th className="py-2 font-medium">Lines</th></tr></thead>
          <tbody className="tnum">
            {jobs.map((j) => (
              <tr key={j.id} className="border-b border-line/60">
                <td className="py-2 pr-4 font-mono text-muted">{j.stage}</td>
                <td className="py-2 pr-4"><StatusPill status={j.status} /></td>
                <td className="py-2 pr-4 text-faint">{j.started?.slice(11, 19) || "—"}</td>
                <td className="py-2 text-faint">{j.log_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
