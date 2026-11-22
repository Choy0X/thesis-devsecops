import { useEffect, useState } from "react";
import { CheckCircle2 } from "lucide-react";
import { api, errorText, type InstallPlan } from "../lib/api";
import { Dialog } from "./hud/Dialog";
import { RoleChoice } from "./RoleChoice";
import { InstallPlanView, installQuestion } from "./InstallPlanView";
import { ModelDownload } from "./ModelDownload";
import { Skeleton } from "./ui";

// Finishing the install of a model that is already on this computer but that Ollama cannot run as it is (it came as safetensors): the plan, said in words, and the job that fetches the
// converter scripts when they are missing and converts it. The files it was downloaded as are kept.

export function FinishInstall({ model, onClose }: { model: { id: string; source: string }; onClose: () => void }) {
  const [plan, setPlan] = useState<InstallPlan | null>(null);
  const [problem, setProblem] = useState("");
  const [role, setRole] = useState<"" | "chat" | "embedding">("");           // "" = what the name and the files say
  const [active, setActive] = useState(false);
  const [done, setDone] = useState(false);
  useEffect(() => {
    setPlan(null); setProblem("");
    let live = true;
    api.finishPlan(model.id, model.source, role).then((p) => { if (live) setPlan(p); }).catch((e) => { if (live) setProblem(errorText(e)); });
    return () => { live = false; };
  }, [model.id, model.source, role]);
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={`Finish installing ${model.id.split("/").pop()}`}
      description="Converts the model to GGUF so that Ollama can run it. The files it was downloaded as are kept." className="max-h-[92vh]">
      <div className="space-y-4">
        {problem ? <p role="alert" className="border border-crit-line bg-crit-tint px-3 py-2 text-[0.8rem] text-crit-soft">{problem}</p> : !plan ? <Skeleton className="h-24 w-full" /> : (
          <>
            <p className="break-all font-mono text-[0.78rem] text-muted">{model.id}</p>
            {done ? (
              // The conversion is over: the role choice and the plan it was made from are gone, not left stale beside a control that cannot be pressed again (trying to convert the
              // same model a second time fails outright, since the converted file is already there and is never overwritten).
              <p className="flex items-center gap-1.5 text-[0.82rem] text-ok"><CheckCircle2 size={16} aria-hidden />Converted.</p>
            ) : (
              <>
                <RoleChoice role={plan.role} detected={plan.role_detected} onChange={setRole} disabled={active} />
                <InstallPlanView plan={plan} />
              </>
            )}
            <ModelDownload stage="install_model" repoId={model.id} quant={plan.format ?? ""} extra={{ source: model.source, skip_download: true, role: plan.role }} role={plan.role}
              label="Convert it" disabled={!plan.ready} confirmText={installQuestion(plan)}
              hint={plan.ready ? "A stopped conversion leaves nothing behind that blocks the next try." : "This cannot be done yet: see above."} onActive={setActive} onDone={() => setDone(true)} />
          </>
        )}
      </div>
    </Dialog>
  );
}
