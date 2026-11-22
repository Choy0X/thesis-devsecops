import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, Pause, Play, Trash2 } from "lucide-react";
import { Button, ErrorState, Panel, Skeleton } from "../../components/ui";
import { Tabs } from "../../components/hud/Tabs";
import { api, type CampaignDetail as Detail } from "../../lib/api";
import { useWhileActive } from "../../components/ActivePage";
import { Dev } from "../../lib/devMode";
import { useTabParam } from "../../lib/tabParam";
import { canDelete, canPause, canResume, elapsed, resumeWord, whereItIs } from "./campaignFormat";
import { CampaignAnalyticsTab } from "./CampaignAnalytics";
import { CampaignConfig } from "./CampaignConfig";
import { CampaignLog } from "./CampaignLog";
import { CampaignOverview } from "./CampaignOverview";
import { StateChip } from "./StateChip";
import { useCampaignActions } from "./useCampaignActions";
import { CampaignContextItems } from '../CampaignContextItems';
import { CampaignCompute } from './CampaignCompute';
import { CampaignSources } from './CampaignSources';

// One campaign: its header with the buttons that fit its state, and its tabs: the overview with every step, the analytics (charts of its progress, yield, speed, products and errors), the log with its story and its output, and its plan.

const TABS = ["overview", "analytics", "log", "context", "config"] as const;

export function CampaignDetail({ id, onBack, onDeleted }: { id: string; onBack: () => void; onDeleted: () => void }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tab, setTab] = useTabParam(TABS);
  const load = useCallback(() => api.campaignDetail(id).then((d) => { setDetail(d); setError(null); }).catch(setError), [id]);
  useEffect(() => { setDetail(null); load(); }, [load]);
  useWhileActive(load, detail && ["running", "pausing"].includes(detail.campaign.state) ? 3000 : 15000);
  const { pause, resume, remove, busy, dialog } = useCampaignActions((action) => { if (action === "delete") onDeleted(); else load(); });

  if (error != null && !detail) return <ErrorState error={error} title="Could not read the campaign" onRetry={load} />;
  if (!detail) return <div className="space-y-4"><Skeleton className="h-10 w-1/2" /><Skeleton className="h-40 w-full" /></div>;
  const c = detail.campaign;
  const reviewReady = detail.context_review?.ready ?? c.state === 'done';
  const unresolved = detail.context_review?.unresolved_sources ?? c.summary?.unresolved_items ?? 0;
  const onlySourceDecision = c.state === 'partial' && unresolved > 0 && reviewReady &&
    detail.steps.every(step => step.status === 'done' || step.status === 'skipped');
  return (
    <div className="space-y-5">
      <Button variant="ghost" icon={<ArrowLeft size={14} aria-hidden />} onClick={onBack}>All campaigns</Button>
      <Panel title={<span className="flex items-center gap-3">{c.name || "Campaign"}<StateChip state={c.state} /></span>}
        desc={<span className="font-mono text-[0.76rem]">{c.campaign_id}</span>}
        right={<div className="flex gap-2">
          {canPause(c) && <Button variant="subtle" icon={<Pause size={14} aria-hidden />} loading={busy === `${id}:pause`} onClick={() => pause(c)}>Pause</Button>}
          {canResume(c) && !onlySourceDecision && <Button variant="primary" icon={<Play size={14} aria-hidden />} loading={busy === `${id}:resume`} onClick={() => resume(c)}>{resumeWord(c)}</Button>}
          {canDelete(c) && <Button variant="ghost" className="text-crit-soft" icon={<Trash2 size={14} aria-hidden />} onClick={() => remove(c)}>Delete</Button>}
        </div>}>
        <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-1.5 text-[0.84rem]">
          <dt className="text-muted">Ecosystems</dt><dd>{c.ecosystems.join(", ")}</dd>
          <dt className="text-muted">Where it is</dt><dd>{whereItIs(c)}</dd>
          <dt className="text-muted">Time</dt><dd className="tnum">{elapsed(c) ?? "not started"}</dd>
        </dl>
        <Dev><p className="mt-3 break-all font-mono text-[0.7rem] text-faint">job {c.job_id ?? "none"} · process {c.pid ?? "none"} · control {c.control ?? "none"}</p></Dev>
      </Panel>
      {(unresolved > 0 || (c.summary?.skipped_sources ?? 0) > 0) && !['running', 'pausing'].includes(c.state) && <CampaignSources key={id} campaignId={id} onUpdated={load} />}
      <CampaignCompute campaignId={id} state={c.state} onUpdated={load} />
      <Tabs value={tab} onValueChange={setTab} items={[
        { id: "overview", label: "Overview", content: <CampaignOverview detail={detail} /> },
        { id: "analytics", label: "Analytics", content: <CampaignAnalyticsTab campaign={c} /> },
        { id: "log", label: "Log", content: <CampaignLog campaign={c} /> },
        { id: "context", label: "Model-written context", content: reviewReady ? <div className="space-y-3">
          {unresolved > 0 && <p role="status" className="text-sm text-warn">{unresolved} source commits remain unresolved and are excluded from these items. Context improvements for collected items are optional; dataset admission uses automatic evidence checks.</p>}
          <CampaignContextItems campaignId={id} />
        </div> : <Panel title="Model-written context"><p className="text-sm text-muted">{detail.context_review?.reason ?? 'Finish collection checks before context review.'}</p></Panel> },
        { id: "config", label: "Config", content: <CampaignConfig campaign={c} /> },
      ]} />
      {dialog}
    </div>
  );
}
