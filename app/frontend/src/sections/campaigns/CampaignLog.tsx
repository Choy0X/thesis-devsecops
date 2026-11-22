import { useCallback, useEffect, useRef, useState } from "react";
import { JobTerminal } from "../../components/JobTerminal";
import { ProgressStrip } from "../../components/ProgressStrip";
import { ErrorState, Panel } from "../../components/ui";
import { useWhileActive } from "../../components/ActivePage";
import { api, type CampaignEvent, type CampaignRow } from "../../lib/api";
import { useJob } from "../../lib/useJob";
import { eventLook, stamp } from "./eventText";

// The second view of a campaign's life: what happened, in sentences (its events), and what the process wrote while it worked (the output of its job).

function Story({ id, busy }: { id: string; busy: boolean }) {
  const [events, setEvents] = useState<CampaignEvent[]>([]);
  const [error, setError] = useState<unknown>(null);
  const last = useRef(0);
  const load = useCallback(() => api.campaignEvents(id, last.current, 500).then((r) => {
    setError(null);
    if (r.events.length) { last.current = r.last; setEvents((old) => [...old, ...r.events]); }
  }).catch(setError), [id]);
  useEffect(() => { last.current = 0; setEvents([]); load(); }, [load]);
  useWhileActive(load, busy ? 3000 : 20000);
  return (
    <Panel title="What happened" desc="The story of the campaign, oldest first.">
      {error != null && !events.length ? <ErrorState error={error} title="Could not read the story" onRetry={load} /> : (
        <ol aria-label="Events" className="space-y-1.5 text-[0.82rem]">
          {events.map((e) => {
            const look = eventLook(e);
            return (
              <li key={e.id} data-kind={e.kind} className="flex gap-3">
                <time dateTime={e.at} className="tnum w-36 shrink-0 text-[0.74rem] text-faint">{stamp(e.at)}</time>
                <span className={look.tone === "problem" ? "text-crit-soft" : look.tone === "good" ? "text-fg" : "text-muted"}>{look.tone === "problem" && <span className="mr-1.5 text-[0.7rem] font-semibold uppercase tracking-wider">Problem</span>}{look.text}</span>
              </li>
            );
          })}
          {events.length === 0 && <li className="text-muted">Nothing has happened yet.</li>}
        </ol>
      )}
    </Panel>
  );
}

function Output({ campaign }: { campaign: CampaignRow }) {
  const job = useJob();
  const running = job.status === "running";
  const jobId = campaign.job_id;
  useEffect(() => { if (jobId && job.id !== jobId) job.attach(jobId); }, [jobId]);                  // eslint-disable-line react-hooks/exhaustive-deps
  if (!jobId) return <Panel title="Output"><p className="text-[0.82rem] text-muted">The campaign has no process yet.</p></Panel>;
  return (
    <Panel title="Output" desc="What the process wrote while it worked. Each run of the campaign (a start, a resume) has its own output.">
      {job.progress && Object.keys(job.progress).length > 0 && <ProgressStrip p={job.progress} live={running} />}
      <JobTerminal title="the campaign process" rows={job.logs} earlier={job.earlier} onLoadEarlier={job.showEarlier} status={job.status} progress={job.progress} copyText={job.text}
        empty="waiting for output..." none="No output is kept for this run." ariaLabel="Output of the process" maxHeight="max-h-80" />
    </Panel>
  );
}

export function CampaignLog({ campaign }: { campaign: CampaignRow }) {
  const busy = campaign.state === "running" || campaign.state === "pausing";
  return <div className="space-y-5"><Story id={campaign.campaign_id} busy={busy} /><Output campaign={campaign} /></div>;
}
