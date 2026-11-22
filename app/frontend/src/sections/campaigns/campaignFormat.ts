import type { CampaignRow, CampaignState, CampaignStepStatus } from "../../lib/api";
import { clockSpan } from "../../lib/format";

// How the words of a campaign are written: its states, its stages and its time.

export const STATE_WORDS: Record<CampaignState, string> = {
  running: "Running", pausing: "Pausing", paused: "Paused", interrupted: "Interrupted", done: "Done", partial: "Partly done", failed: "Failed",
};

/** What each state means, said once, for a title or a tooltip. */
export const STATE_MEANS: Record<CampaignState, string> = {
  running: "A process is working through the stages now.",
  pausing: "It was asked to pause and is saving where it is.",
  paused: "Stopped where it was; Resume continues from there. It still holds its ecosystems.",
  interrupted: "The process is gone (the app closed or the computer lost power). Resume continues from the last saved step.",
  done: "Every step finished.",
  partial: "Some steps or source items remain unresolved. Completed collection can proceed to draft review; security approval remains separate.",
  failed: "No step finished. Resume tries again.",
};

export const STEP_WORDS: Record<CampaignStepStatus, string> = {
  pending: "Waiting", running: "Running", paused: "Paused", done: "Done", quota: "Stopped at the quota", error: "Failed", skipped: "Skipped",
};

export const STAGE_WORDS: Record<string, string> = {
  ingest: "Read the advisories", queue: "Queue the fix commits", recover: "Recover missing fix commits", mine: "Mine the code pairs", verify: "Check the fixes",
  resplit: "Split pairs into functions", dep_versions: "Check dependency versions", enrich: "Write the context with the model", build: "Build a draft dataset",
};
export const stageWord = (stage: string) => STAGE_WORDS[stage] ?? stage.replace(/_/g, " ");

export const canPause = (c: Pick<CampaignRow, "state" | "legacy">) => !c.legacy && c.state === "running";
export const canResume = (c: Pick<CampaignRow, "state" | "legacy">) => !c.legacy && ["paused", "interrupted", "partial", "failed"].includes(c.state);
export const canDelete = (c: Pick<CampaignRow, "state">) => c.state !== "running" && c.state !== "pausing";
export const resumeWord = (c: Pick<CampaignRow, "state">) => (c.state === "partial" || c.state === "failed" ? "Retry the failed steps" : "Resume");

/** "Mine the code pairs (npm), step 3 of 7", from what the campaign last reported. */
export function whereItIs(c: CampaignRow): string {
  const p = c.progress ?? {};
  if (c.state === "done" && c.summary?.skipped_sources) return `Collection finished; ${c.summary.skipped_sources} source commits explicitly skipped`;
  if (c.state === "done") return "Finished";
  if (c.state === "partial" && c.summary?.unresolved_items && c.summary?.steps?.error === 0)
    return `Collection finished; ${c.summary.unresolved_items} source commits unresolved`;
  if (c.state === "failed" || c.state === "partial") return c.state === "failed" ? "No step finished" : `${c.summary?.steps?.error ?? 0} step(s) failed`;
  if (!p.stage) return c.state === "running" ? "Starting" : "Not started";
  const target = p.target && p.target !== "*" ? ` (${p.target})` : "";
  return `${stageWord(p.stage)}${target}${p.step && p.steps ? `, step ${p.step} of ${p.steps}` : ""}`;
}

export const percentOf = (c: CampaignRow): number => Math.max(0, Math.min(100, c.state === "done" ? 100 : c.progress?.percent ?? 0));

export function elapsed(c: CampaignRow, now = Date.now()): string | null {
  if (!c.started_at) return null;
  const end = c.finished_at && !["running", "pausing"].includes(c.state) ? Date.parse(c.finished_at) : now;
  return clockSpan(Math.max(0, (end - Date.parse(c.started_at)) / 1000));
}

