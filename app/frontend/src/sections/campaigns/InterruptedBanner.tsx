import { Play, TriangleAlert } from "lucide-react";
import { Button } from "../../components/ui";
import type { CampaignRow } from "../../lib/api";
import { percentOf, whereItIs } from "./campaignFormat";
import { useCampaignActions } from "./useCampaignActions";

// Campaigns that were running when the app closed or the computer lost power are shown here with one button each. Nothing restarts by itself: the
// owner decides, because a resumed campaign uses the processor, the memory and the GitHub quota again.

export function InterruptedBanner({ campaigns, onOpen }: { campaigns: CampaignRow[]; onOpen: (id: string) => void }) {
  const { resume, busy, dialog } = useCampaignActions();
  const hurt = campaigns.filter((c) => c.state === "interrupted" && !c.legacy);
  if (!hurt.length) return null;
  return (
    <section role="alert" aria-label="Interrupted campaigns" className="border border-warn-line bg-warn-tint px-5 py-4">
      <p className="flex items-center gap-2 text-[0.88rem] font-medium text-warn"><TriangleAlert size={16} aria-hidden />
        {hurt.length === 1 ? "A campaign was interrupted" : `${hurt.length} campaigns were interrupted`}</p>
      <p className="mt-1 text-[0.8rem] text-muted">The app closed, or the computer lost power, while {hurt.length === 1 ? "it" : "they"} ran. Nothing was lost: each one saved its last step, and nothing restarted by itself.</p>
      <ul className="mt-3 space-y-2">
        {hurt.map((c) => (
          <li key={c.campaign_id} className="flex flex-wrap items-center gap-3 text-[0.82rem]">
            <button type="button" onClick={() => onOpen(c.campaign_id)} className="font-mono text-[0.76rem] underline-offset-2 hover:underline">{c.campaign_id}</button>
            <span className="text-muted">{c.name ? `${c.name}: ` : ""}{c.ecosystems.join(", ")}</span>
            <span className="text-faint">{whereItIs(c)}, {Math.round(percentOf(c))}%</span>
            <Button variant="subtle" className="ml-auto h-8" icon={<Play size={13} aria-hidden />} loading={busy === `${c.campaign_id}:resume`} onClick={() => resume(c)}
              aria-label={`Resume ${c.campaign_id}`}>Resume</Button>
          </li>
        ))}
      </ul>
      {dialog}
    </section>
  );
}
