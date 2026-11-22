import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2 } from "lucide-react";
import { api, errorText, type ServingStatus } from "../lib/api";
import { slotWords } from "../lib/gatewayWords";
import { clockSpan } from "../lib/format";
import { Dialog } from "./hud/Dialog";
import { Button } from "./ui";

export function LoadModelDialog({ role, label, onClose, onReady }: { role: "llm"; label: string; onClose: () => void; /** Fired once, as soon as the slot turns ready, instead of waiting for Done to be clicked. */ onReady?: () => void }) {
  const [status, setStatus] = useState<ServingStatus | null>(null);
  const [problem, setProblem] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const started = useRef(Date.now());

  const start = () => {
    setProblem(""); setStatus(null); started.current = Date.now(); setElapsed(0);
    api.servingLoad(role).then((r) => setStatus(r.status)).catch((e) => setProblem(errorText(e)));
  };
  useEffect(start, [role]);        // eslint-disable-line react-hooks/exhaustive-deps

  const slot = status?.slots[role];
  const loading = !problem && (!slot || slot.state === "loading" || slot.state === "absent");
  useEffect(() => {
    if (!loading) return;
    const poll = window.setInterval(() => {
      if (!status) return;
      api.servingStatusLive().then(setStatus).catch((e) => setProblem(errorText(e)));
    }, 1500);
    const clock = window.setInterval(() => setElapsed(Math.round((Date.now() - started.current) / 1000)), 1000);
    return () => { window.clearInterval(poll); window.clearInterval(clock); };
  }, [loading, Boolean(status)]);

  const ready = slot?.state === "ready";
  const failed = !problem && slot?.state === "failed";
  const firedReady = useRef(false);
  useEffect(() => {
    if (ready && !firedReady.current) { firedReady.current = true; onReady?.(); }
  }, [ready]);        // eslint-disable-line react-hooks/exhaustive-deps
  const settled = ready || failed || !!problem;
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} dismissible={settled} title={`Loading the ${label.toLowerCase()}`}
      description={settled ? undefined : "This stays open until the model is ready or the load fails."}>
      <div className="space-y-3 text-[0.84rem]">
        {problem ? (
          <p role="alert" className="border border-crit-line bg-crit-tint px-3 py-2 text-crit-soft">{problem}</p>
        ) : failed ? (
          <p role="alert" className="border border-crit-line bg-crit-tint px-3 py-2 text-crit-soft">{slotWords(slot)}</p>
        ) : ready ? (
          <p role="status" className="flex items-center gap-1.5 text-ok"><CheckCircle2 size={16} aria-hidden />{slotWords(slot)}</p>
        ) : (
          <p role="status" className="flex items-center gap-2 text-muted">
            <Loader2 size={15} className="animate-spin" aria-hidden />
            {status ? slotWords(slot) : "Waiting for Ollama..."}
            {elapsed > 0 && <span className="tnum text-faint">({clockSpan(elapsed)})</span>}
          </p>
        )}
        <div className="flex justify-end gap-2">
          {(problem || failed) && <Button variant="primary" onClick={start}>Retry</Button>}
          {settled && <Button variant={ready ? "primary" : "ghost"} onClick={onClose}>{ready ? "Done" : "Dismiss"}</Button>}
        </div>
      </div>
    </Dialog>
  );
}
