import { Button } from "../ui";
import { Composer } from "./Composer";

// Shown in place of the composer once Plan mode's own propose_plan tool pauses the session (pending_call.name === "propose_plan"): the plan itself, Accept (switches this same
// session's mode to Manual or Auto in place and continues it, keeping everything already investigated), or say no and tell it what to do differently with the real, shared
// Composer -- typing feedback here sends it back as a denial of the pending call, carrying the owner's own words instead of a generic refusal, and the same Plan session continues.

export function PlanReviewCard({ plan, busy, onAccept, onRequestChanges, onStop }: {
  plan: string;
  busy: boolean;
  onAccept: (mode: "manual" | "auto") => void;
  onRequestChanges: (feedback: string) => Promise<boolean | void> | boolean | void;
  onStop: () => void;
}) {
  return (
    <div role="alertdialog" aria-label="Implement this plan?" className="space-y-3 border border-warn-line bg-warn-tint p-3">
      <p className="text-[0.84rem] font-semibold">Implement this plan?</p>
      <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap break-words text-[0.8rem]">{plan}</pre>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy} onClick={() => onAccept("manual")}>Accept - Manual</Button>
        <Button variant="primary" disabled={busy} onClick={() => onAccept("auto")}>Accept - Auto</Button>
      </div>
      <Composer busy={busy} placeholder="No -- what should it do differently?" hint="Sent back to the model as feedback on the plan above; it stays in this same session." commands={false}
        onSend={onRequestChanges} onStop={onStop} />
    </div>
  );
}
