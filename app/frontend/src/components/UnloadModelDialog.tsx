import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { api, errorText } from "../lib/api";
import { Dialog } from "./hud/Dialog";
import { Button } from "./ui";

// Freeing the configured language model, watched instead of happening silently behind a page that has already moved
// on: it stays open and cannot be dismissed (no Close button, no Esc, no click outside) while the unload is in flight. Unlike loading a model, unloading it is a single request the
// gateway does not answer until it is actually done (it drains what the model was still answering first), so there is no status to poll here, only the one request to wait on.

export function UnloadModelDialog({ onDone }: { onDone: () => void }) {
  const [problem, setProblem] = useState("");
  useEffect(() => {
    api.servingUnload().then(onDone).catch((e) => setProblem(errorText(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onDone(); }} dismissible={!!problem} title="Unloading the model"
      description={problem ? undefined : "This frees the graphics card before you leave; it stays open until it is done."}>
      <div className="space-y-3 text-[0.84rem]">
        {problem ? (
          <p role="alert" className="border border-crit-line bg-crit-tint px-3 py-2 text-crit-soft">{problem}</p>
        ) : (
          <p role="status" className="flex items-center gap-2 text-muted"><Loader2 size={15} className="animate-spin" aria-hidden />Unloading...</p>
        )}
        {problem && <div className="flex justify-end gap-2"><Button variant="ghost" onClick={onDone}>Leave anyway</Button></div>}
      </div>
    </Dialog>
  );
}
