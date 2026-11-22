import { type LocalGpuReport } from "../lib/api";

export const short = (model: string | null | undefined) => (model || "").split("/").pop() || "";

/** What the last local training of the run says, in words; the checkpoint it would resume from is named when it stopped early. */
export function reportLine(r: LocalGpuReport): { tone: string; text: string } {
  const at = `step ${r.steps_done ?? 0}${r.steps ? ` of ${r.steps}` : ""}`;
  if (r.state === "finished" || r.state === "early_stopped") {
    return { tone: "text-ok", text: `Finished at ${at} (${short(r.model)}, ${r.seconds ?? 0} s in the last call). ${r.pinned_base ? "" : "A software check on a dev-only model: not a result."}`.trim() };
  }
  if (r.state === "interrupted") {
    return { tone: "text-warn", text: `Stopped at ${at}${r.interrupted_by === "session_budget" ? " on its session budget" : ""}. A checkpoint was written: start it again to continue from there.` };
  }
  if (r.state === "stopped") {
    return { tone: "text-warn", text: `Stopped at ${at} (cancelled, or the app was closed). It continues from its last checkpoint when started again; steps after that checkpoint are done again.` };
  }
  if (r.state === "running") return { tone: "text-muted", text: `Running, ${at}.` };
  return { tone: "text-crit", text: `Failed at ${at}. The output of the runner is in the job log.` };
}

export function LocalTrain({ last }: { run: string; last: LocalGpuReport | null | undefined; onDone: () => void }) {
  const line = last ? reportLine(last) : null;
  return <div className="space-y-2 text-[0.8rem]">
    <p>Desktop training is retired. Prepare a portable trial or full bundle and run its notebook on a remote runtime. Import trial_outputs.zip or outputs.zip when it finishes.</p>
    {line && <p className={line.tone}>Historical local run: {line.text}</p>}
  </div>;
}
