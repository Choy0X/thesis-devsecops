import { useState } from "react";
import { Dialog } from "../hud/Dialog";
import { Checkbox } from "../kit/Toggles";
import { Button } from "../ui";

// The question the agent puts to the owner before it scans the project's dependencies (the pending call ``rescan_dependencies``): a dialog with what the scan does and does not do, Confirm, Cancel and a box
// "Do not ask me again". Confirm runs it; Cancel (or closing the dialog) says no and the agent answers without it; with the box ticked, Confirm also saves a rule of the owner's, for every project, that
// allows it without asking (Settings, Permissions lists it and can take it away). The agent only gets as far as this question when the owner's own words were about dependencies.

export function DependencyScanDialog({ busy, onConfirm, onCancel }: { busy: boolean; onConfirm: (dontAskAgain: boolean) => void; onCancel: () => void }) {
  const [dontAsk, setDontAsk] = useState(false);
  return (
    <>
      <p role="status" className="border border-warn-line bg-warn-tint p-3 text-[0.8rem] text-muted">Waiting for your answer about the dependency scan.</p>
      <Dialog open onOpenChange={(open) => { if (!open && !busy) onCancel(); }} dismissible={!busy} title="Scan the dependencies?"
        description="The agent wants to check the dependencies of this project because you asked about them.">
        <div className="space-y-3 text-[0.84rem]">
          <ul className="list-disc space-y-1.5 pl-5 text-muted">
            <li>It reads the manifests and lock files (package.json, requirements.txt, a lock file) and matches the versions in them against published security advisories.</li>
            <li>It never reads the code of a dependency, installs nothing and changes nothing.</li>
            <li>It does not look up newer releases: the assistant says from its own knowledge whether a version looks old, and tells you that this is its judgement and not a finding of the scan.</li>
            <li>Without a lock file the exact versions are not known, and the result says so.</li>
          </ul>
          <Checkbox label="Do not ask me again" description="Scan when I ask about dependencies, without this question. You can turn it back on in Settings, under Permissions." checked={dontAsk} disabled={busy}
            onCheckedChange={(on) => setDontAsk(on)} />
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
            <Button variant="primary" disabled={busy} onClick={() => onConfirm(dontAsk)}>Confirm</Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}
