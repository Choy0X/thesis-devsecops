import { useState, type ReactNode } from "react";
import { useConfirm } from "../../components/hud/AlertDialog";
import { useToast } from "../../components/Toast";
import { api, type CampaignRow } from "../../lib/api";
import { ApiError, errorText, type ApiReason } from "../../lib/apiError";
import { refreshCampaigns } from "../../lib/campaignsStore";
import { RefusalDialog } from "./RefusalDialog";

// Pause, resume and delete a campaign, with the same words wherever the buttons are (the list, the banner, the detail page). A refusal that comes with
// reasons (the machine has no room, an ecosystem is held) opens a dialog that lists them; any other failure is a toast.

export type CampaignAction = "pause" | "resume" | "delete";

export function useCampaignActions(onDone?: (action: CampaignAction, id: string) => void): {
  pause: (c: CampaignRow) => Promise<void>; resume: (c: CampaignRow) => Promise<void>; remove: (c: CampaignRow) => Promise<void>; busy: string | null; dialog: ReactNode;
} {
  const toast = useToast();
  const confirm = useConfirm();
  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ title: string; reasons: ApiReason[]; hint: string | null } | null>(null);

  const run = async (c: CampaignRow, action: CampaignAction, work: () => Promise<void>, failure: string) => {
    setBusy(`${c.campaign_id}:${action}`);
    try {
      await work();
      await refreshCampaigns();
      onDone?.(action, c.campaign_id);
    } catch (e) {
      if (e instanceof ApiError && e.reasons.length) setRefusal({ title: e.title, reasons: e.reasons, hint: e.hint });
      else toast.error(`${failure}: ${errorText(e)}`, e instanceof ApiError ? e.technical : undefined);
    } finally { setBusy(null); }
  };

  return {
    busy,
    dialog: refusal ? <RefusalDialog title={refusal.title} reasons={refusal.reasons} hint={refusal.hint} onClose={() => setRefusal(null)} /> : null,
    pause: (c) => run(c, "pause", async () => { await api.campaignPauseOne(c.campaign_id); toast.info("Asked to pause. It stops at the next safe point, saves where it is and frees the processor and memory it used."); }, "Could not pause"),
    resume: (c) => run(c, "resume", async () => { await api.campaignResume(c.campaign_id); toast.success(c.state === "partial" || c.state === "failed" ? "Trying the failed steps again." : "Resumed from where it stopped."); }, "Could not resume"),
    remove: async (c) => {
      const sure = await confirm({
        title: `Delete campaign ${c.campaign_id}?`, tone: "critical", confirmLabel: "Delete the campaign",
        description: <>The campaign&apos;s own records go: its steps, its history and its charts. Everything it produced stays, because it is already in the dataset: the commits, the code pairs and the drafts.
          {c.state === "paused" || c.state === "interrupted" ? " The ecosystems it holds become free for another campaign." : ""} This cannot be undone.</>,
      });
      if (!sure) return;
      await run(c, "delete", async () => { await api.campaignDelete(c.campaign_id); toast.success("Campaign deleted. What it produced is still in the dataset."); }, "Could not delete");
    },
  };
}
