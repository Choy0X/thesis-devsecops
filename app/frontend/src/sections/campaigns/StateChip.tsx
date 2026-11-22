import { Activity, CircleAlert, CircleCheck, CircleDashed, Hourglass, OctagonX, Pause, type LucideIcon } from "lucide-react";
import type { CampaignState } from "../../lib/api";
import { cn } from "../../lib/cn";
import { STATE_MEANS, STATE_WORDS } from "./campaignFormat";
import { Hint } from "../../components/kit";

// The state of a campaign: its word and a shape of its own, never a colour alone.
const LOOK: Record<CampaignState, { Icon: LucideIcon; cls: string }> = {
  running: { Icon: Activity, cls: "border-brand-line bg-brand-tint text-brand-strong" },
  pausing: { Icon: Hourglass, cls: "border-warn-line text-warn" },
  paused: { Icon: Pause, cls: "border-line-strong text-muted" },
  interrupted: { Icon: CircleAlert, cls: "border-warn-line bg-warn-tint text-warn" },
  done: { Icon: CircleCheck, cls: "border-ok-line text-ok" },
  partial: { Icon: CircleDashed, cls: "border-warn-line text-warn" },
  failed: { Icon: OctagonX, cls: "border-crit-line text-crit" },
};

export function StateChip({ state, className }: { state: CampaignState; className?: string }) {
  const { Icon, cls } = LOOK[state] ?? LOOK.paused;
  return (
    <Hint label={STATE_MEANS[state]} className={cn("inline-flex items-center gap-1.5 border px-2 py-0.5 text-[0.72rem] font-medium uppercase tracking-wider no-underline", cls, className)} data-state={state}>
      <Icon size={12} aria-hidden />{STATE_WORDS[state] ?? state}
    </Hint>
  );
}
