import type { CampaignEvent } from "../../lib/api";
import { stageWord } from "./campaignFormat";

// The story of a campaign in sentences. A kind the screen does not know is shown by its name, so a newer backend never shows a blank line.

const where = (d: Record<string, any>) => (d.stage ? `${stageWord(d.stage)}${d.target && d.target !== "*" ? ` (${d.target})` : ""}` : "");

export type EventLook = { text: string; tone: "plain" | "problem" | "good" };

export function eventLook(e: CampaignEvent): EventLook {
  const d = e.data ?? {};
  switch (e.kind) {
    case "created": return { text: `Campaign created for ${(d.ecosystems as string[] | undefined)?.join(", ") ?? "its ecosystems"}.`, tone: "plain" };
    case "started": return { text: `A process started to work through ${d.steps ?? "its"} steps${d.resumes ? ` (resumed ${d.resumes} time${d.resumes === 1 ? "" : "s"} before)` : ""}.`, tone: "plain" };
    case "stage_started": return { text: `Started: ${where(d)}, step ${d.index} of ${d.of}.`, tone: "plain" };
    case "stage_done": return { text: `Finished: ${where(d)}${d.status === "quota" ? ", stopped at the quota" : d.status === "skipped" ? ", skipped" : ""}${d.seconds != null ? ` in ${d.seconds} s` : ""}.`, tone: "good" };
    case "stage_error": return { text: `Failed: ${where(d) || "the campaign"}${d.cause ? `, ${String(d.cause).replace(/_/g, " ")}` : ""}${d.message ? `: ${d.message}` : ""}`, tone: "problem" };
    case "pause_requested": return { text: "Asked to pause. It stops at the next safe point.", tone: "plain" };
    case "paused": return { text: `Paused${d.stage ? ` during ${where(d)}` : ""}. What it had done is saved.`, tone: "plain" };
    case "resumed": return { text: "Resumed from where it stopped.", tone: "plain" };
    case "interrupted": return { text: `Interrupted${d.why ? `: ${d.why}` : ""}. Resume continues from the last saved step.`, tone: "problem" };
    case "forced_kill": return { text: `The process did not stop in time${d.after_seconds != null ? ` (${d.after_seconds} s)` : ""} and was ended. Only the step in progress is done again.`, tone: "problem" };
    case "finished": return { text: `The campaign finished: ${d.state ?? "done"}.`, tone: d.state === "done" ? "good" : "problem" };
    case "rate_limited": return { text: "Waiting for the GitHub rate limit to reset.", tone: "plain" };
    case "warning": return { text: String(d.text ?? "A warning."), tone: "problem" };
    default: return { text: e.kind.replace(/_/g, " "), tone: "plain" };
  }
}

/** "2026-10-10 10:04 UTC" from an ISO time (the same in every time zone, so a screenshot and a test agree). */
export const stamp = (iso: string | null | undefined): string => (iso ? `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC` : "—");

/** A step's result in a few words: the numbers it reported, or why it failed. The reason a step was skipped is shown whole (it is the answer), then a few of its numbers. */
export function detailSummary(detail: Record<string, any> | null | undefined, max = 4): string {
  const d = detail ?? {};
  const reason = typeof d.skipped === "string" ? d.skipped : "";
  const pairs = Object.entries(d).filter(([k, v]) => !(k === "skipped" && reason) && (typeof v === "number" || (typeof v === "string" && v.length < 60) || typeof v === "boolean"));
  const numbers = pairs.slice(0, max).map(([k, v]) => `${k.replace(/_/g, " ")} ${typeof v === "number" ? v.toLocaleString("en-US") : String(v)}`).join(", ");
  return reason ? `skipped: ${reason}${numbers ? ` (${numbers})` : ""}` : numbers;
}
