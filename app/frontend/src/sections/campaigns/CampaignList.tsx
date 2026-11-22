import { Pause, Play, Trash2 } from "lucide-react";
import { Badge } from "../../components/hud/Badge";
import { DataTable, type Column } from "../../components/hud/DataTable";
import { Progress } from "../../components/hud/Progress";
import { Button } from "../../components/ui";
import type { CampaignRow } from "../../lib/api";
import { canDelete, canPause, canResume, elapsed, percentOf, resumeWord, whereItIs } from "./campaignFormat";
import { StateChip } from "./StateChip";
import { useCampaignActions } from "./useCampaignActions";
import { Hint } from "../../components/kit";

// Every campaign in one table: its id, what it works on, its state, where it is, and the buttons that fit its state. Clicking a row opens the campaign.

export function CampaignList({ campaigns, onOpen, onChanged }: { campaigns: CampaignRow[]; onOpen: (id: string) => void; onChanged?: () => void }) {
  const { pause, resume, remove, busy, dialog } = useCampaignActions(onChanged);
  const columns: Column<CampaignRow>[] = [
    { key: "id", label: "Campaign", width: "11.5rem", sortValue: (c) => c.campaign_id, render: (c) => (
      <span className="flex items-center gap-2"><span className="font-mono text-[0.74rem]">{c.campaign_id}</span>{c.legacy && <Hint label="Made by the earlier kind of campaign: it keeps its steps and nothing more" className="no-underline"><Badge>earlier kind</Badge></Hint>}</span>) },
    { key: "name", label: "Name", sortValue: (c) => c.name ?? "", render: (c) => c.name || <span className="text-faint">no name</span> },
    { key: "ecosystems", label: "Ecosystems", width: "11rem", render: (c) => (
      <span className="flex gap-1 overflow-hidden" title={c.ecosystems.join(", ")}>
        {c.ecosystems.slice(0, 2).map((e) => <Badge key={e}>{e}</Badge>)}{c.ecosystems.length > 2 && <Badge>+{c.ecosystems.length - 2}</Badge>}
      </span>) },
    { key: "state", label: "State", width: "9rem", sortValue: (c) => c.state, render: (c) => <StateChip state={c.state} /> },
    { key: "progress", label: "Progress", width: "14rem", render: (c) => c.legacy ? <span className="text-faint">not recorded</span> : (
      <span className="flex items-center gap-2" title={whereItIs(c)}>
        <Progress value={percentOf(c)} segments={12} label={`${c.campaign_id}: ${whereItIs(c)}`} className="w-28" />
        <span className="tnum w-10 text-right text-[0.76rem] text-muted">{Math.round(percentOf(c))}%</span>
      </span>) },
    { key: "time", label: "Time", width: "5.5rem", align: "right", render: (c) => <span className="tnum text-muted">{elapsed(c) ?? "—"}</span> },
    { key: "actions", label: "", width: "10rem", align: "right", render: (c) => (
      <span className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
        {canPause(c) && <Button variant="ghost" className="h-7 px-2 text-[0.78rem]" icon={<Pause size={13} aria-hidden />} loading={busy === `${c.campaign_id}:pause`} onClick={() => pause(c)} aria-label={`Pause ${c.campaign_id}`}>Pause</Button>}
        {canResume(c) && <Button variant="ghost" className="h-7 px-2 text-[0.78rem]" icon={<Play size={13} aria-hidden />} loading={busy === `${c.campaign_id}:resume`} onClick={() => resume(c)} aria-label={`${resumeWord(c)} ${c.campaign_id}`}>{c.state === "partial" || c.state === "failed" ? "Retry" : "Resume"}</Button>}
        {canDelete(c) && <Button variant="ghost" className="h-7 px-2 text-[0.78rem] text-crit-soft" icon={<Trash2 size={13} aria-hidden />} loading={busy === `${c.campaign_id}:delete`} onClick={() => remove(c)} aria-label={`Delete ${c.campaign_id}`}>Delete</Button>}
      </span>) },
  ];
  return (
    <>
      <DataTable caption="Campaigns" columns={columns} rows={campaigns} rowKey={(c) => c.campaign_id} onRowClick={(c) => onOpen(c.campaign_id)} height={520} empty="No campaigns." />
      {dialog}
    </>
  );
}
