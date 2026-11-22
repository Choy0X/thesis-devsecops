import { Button } from "../../components/ui";
import { Dialog } from "../../components/hud/Dialog";
import type { ApiReason } from "../../lib/apiError";

// Why a campaign could not start or resume: every reason in words, with the numbers behind it. Nothing was started and nothing was queued.

export function RefusalDialog({ title, reasons, hint, onClose }: { title: string; reasons: ApiReason[]; hint?: string | null; onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }} title={title} description="Nothing was started.">
      <ul className="space-y-3 text-[0.84rem]">
        {reasons.map((r, i) => (
          <li key={`${r.code}-${i}`} className="border-l-2 border-warn-line pl-3">
            <p className="leading-relaxed">{r.text}</p>
            {r.numbers && Object.keys(r.numbers).length > 0 && (
              <p className="mt-1 font-mono text-[0.72rem] text-faint">{Object.entries(r.numbers).map(([k, v]) => `${k.replace(/_/g, " ")} ${String(v)}`).join(" · ")}</p>
            )}
          </li>
        ))}
      </ul>
      {hint && <p className="mt-4 text-[0.8rem] text-muted">{hint}</p>}
      <div className="mt-5 flex justify-end"><Button variant="subtle" onClick={onClose}>Close</Button></div>
    </Dialog>
  );
}
