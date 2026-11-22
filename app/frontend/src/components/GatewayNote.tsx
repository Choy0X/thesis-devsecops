import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, errorText, type ServingStatus } from "../lib/api";
import { useOnChanged } from "../lib/changes";
import { slotWords } from "../lib/gatewayWords";
import { useOnActivate } from "./ActivePage";
import { useConfirm } from "./hud/AlertDialog";
import { Button } from "./ui";
import { SettingRow } from "./kit";
import { useToast } from "./Toast";
import { LoadModelDialog } from "./LoadModelDialog";

const LOADING_POLL_MS = 1500;      // a load can be started from elsewhere (the project chat, another Settings tab); this is how the row keeps up with it live, the same pace LoadModelDialog polls at

export function useGatewayStatus(): { status: ServingStatus | null; failed: boolean; reload: () => void } {
  const [status, setStatus] = useState<ServingStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const reload = useCallback(() => { api.servingStatus().then((s) => { setStatus(s); setFailed(false); }).catch(() => setFailed(true)); }, []);
  useEffect(reload, [reload]);
  useOnChanged(["models"], reload);
  useOnActivate(reload);                                    // Settings stays mounted: coming back to it reads again when something was written or finished meanwhile
  const loading = Object.values(status?.slots ?? {}).some((s) => s.state === "loading");
  useEffect(() => {
    if (!loading) return;
    const refreshLive = () => api.servingStatusLive().then((s) => { setStatus(s); setFailed(false); }).catch(() => setFailed(true));
    const id = window.setInterval(refreshLive, LOADING_POLL_MS);
    return () => window.clearInterval(id);
  }, [loading]);
  return { status, failed, reload };
}

export function GatewayNote({ status, failed, role, onChanged }: { status: ServingStatus | null; failed?: boolean; role: "llm"; onChanged?: () => void }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [loading, setLoading] = useState(false);
  const [unloading, setUnloading] = useState(false);
  const box = "flex flex-wrap items-center justify-between gap-2 border border-warn-line bg-warn-tint px-3 py-2 text-[0.8rem] text-warn";
  const row = (body: ReactNode) => <SettingRow label="Model state" hint="What Ollama says about this model.">{body}</SettingRow>;
  if (!status) return failed ? row(<p role="status" className={box}>Ollama status could not be read.</p>) : null;
  if (!status.ollama.running) {
    return row(<p role="status" className={box}>{status.ollama.error ?? "Ollama is not running. It starts with the app."}</p>);
  }
  const slot = status.slots[role];
  const choice = status.choices[role];
  const tone = !slot || slot.state === "absent" ? "text-muted" : slot.state === "ready" ? "text-ok" : slot.state === "failed" ? "text-crit-soft" : "text-warn";
  const label = "Language model";
  // A load can be started on purpose only for a local model that is actually chosen, and only while nothing is already happening to it (a load already running, or a model already
  // loaded, is watched or unloaded, not loaded again).
  const canLoad = choice.engine === "local" && !!choice.model && (!slot || slot.state === "absent" || slot.state === "failed");
  const canUnload = choice.engine === "local" && slot?.state === "ready";
  const unload = async () => {
    const ok = await confirm({ title: `Unload the ${label.toLowerCase()}?`, confirmLabel: "Unload it", cancelLabel: "Keep it loaded",
      description: "It is unloaded once it has finished what it is answering, however long that takes, and the graphics card is freed. It loads again the next time it is used, which takes a few seconds." });
    if (!ok) return;
    setUnloading(true);
    try { await api.servingUnload({ role }); toast.success(`The ${label.toLowerCase()} was unloaded.`); onChanged?.(); }
    catch (e) { toast.error(errorText(e)); }
    finally { setUnloading(false); }
  };
  return (
    <>
      {row(
        <span className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <span role="status" className={`text-[0.8rem] ${tone}`}>{slotWords(slot)}</span>
          {canLoad && <Button variant="ghost" size="sm" onClick={() => setLoading(true)}>Load</Button>}
          {canUnload && <Button variant="ghost" size="sm" loading={unloading} onClick={() => void unload()}>Unload</Button>}
        </span>)}
      {loading && <LoadModelDialog role={role} label={label} onClose={() => { setLoading(false); onChanged?.(); }} />}
    </>
  );
}
