import { Workflow } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button, Panel } from "../../components/ui";
import { isBusy, useCampaigns } from "../../lib/campaignsStore";

// The way from the Collections page to the campaigns: a short note, how many are working, and the button that opens the start dialog on the Campaigns page.

export function StartCampaignEntry() {
  const go = useNavigate();
  const { campaigns, loaded } = useCampaigns();
  const running = campaigns.filter(isBusy).length;
  const interrupted = campaigns.filter((c) => c.state === "interrupted" && !c.legacy).length;
  const status = !loaded ? "" : [running ? `${running} running` : "", interrupted ? `${interrupted} interrupted` : ""].filter(Boolean).join(", ") || (campaigns.length ? "none running" : "none yet");
  return (
    <Panel title="Campaigns" desc="A campaign collects, mines and checks the advisories of the ecosystems you choose, with its own progress. Several can run side by side, and any can be paused."
      right={<div className="flex gap-2">
        <Button variant="ghost" onClick={() => go("/campaigns")}>Open the campaigns</Button>
        <Button variant="primary" icon={<Workflow size={14} aria-hidden />} onClick={() => go("/campaigns?start=")}>Start a campaign</Button>
      </div>}>
      <p className="text-[0.82rem] text-muted" aria-live="polite">{status ? `Campaigns: ${status}.` : "Reading the campaigns…"}</p>
    </Panel>
  );
}
