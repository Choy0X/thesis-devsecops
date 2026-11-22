import type { EngineRemediation, EngineRun, EngineStep, EngineTask, EngineValidation } from "../../lib/api";

// The words the Task tab uses for what the engine recorded. Each reason is a sentence about the work, not an identifier: the page never shows a code where a person has to read one.

const STOP: Record<string, string> = {
  answered: "Finished with an answer",
  max_rounds: "Ran out of rounds",
  max_seconds: "Ran out of time",
  max_tool_calls: "Ran out of tool calls",
  max_tokens: "Ran out of tokens",
  no_progress: "Stopped: the same failure kept repeating",
  loop: "Stopped: it was going round in circles",
  cancelled: "Stopped by you",
  gate_unmet: "Stopped: the answer could not be supported",
  refused: "Stopped: it kept asking for what this mode cannot do",
};

export function stopWords(reason: string | null | undefined): string {
  if (!reason) return "";
  if (STOP[reason]) return STOP[reason];
  if (reason.startsWith("error:")) return `Stopped by an error (${reason.slice(6).replace(/_/g, " ")})`;
  return reason.replace(/_/g, " ");
}

export const RUN_STATE: Record<string, string> = {
  running: "Running", awaiting_approval: "Waiting for you", completed: "Finished", failed: "Failed", cancelled: "Stopped", exhausted: "Ran out of budget", interrupted: "Interrupted",
};

export const COMPLETION: Record<EngineTask["completion"]["status"], { label: string; tone: "ok" | "warn" | "muted" }> = {
  open: { label: "In progress", tone: "muted" },
  complete: { label: "Complete", tone: "ok" },
  unverified: { label: "Unverified", tone: "warn" },
  partial: { label: "Partial", tone: "warn" },
};

export const STEP_STATE: Record<EngineStep["state"], string> = { pending: "To do", active: "Doing", done: "Done", blocked: "Blocked", skipped: "Skipped" };

const REMEDIATION: Record<string, string> = {
  verified: "The test failed on the original code and passes on the fix, and the project's own tests still pass.",
  no_code_change: "Nothing but the test was changed, so there is no fix to check.",
  test_not_new: "That file was already part of the project: a regression test has to be a new file.",
  test_does_not_use_project: "The test does not use any of the project's own code.",
  test_passes_before_patch: "The test already passes on the original code, so it does not show the problem.",
  test_not_runnable: "The test could not run on the original code (an import error, or no test was found).",
  fix_does_not_pass_the_test: "The test still fails on the fixed code.",
  patch_does_not_apply: "The patch could not be applied to the original project.",
  build_failed: "The code does not compile after the patch.",
  project_tests_failed: "The project's own tests fail after the patch.",
  integrity: "The patch touches an existing test or a protected file.",
  unsupported: "This kind of project or change cannot be verified here.",
  env_error: "The check could not run here, so nothing was verified.",
};

export function remediationWords(r: Pick<EngineRemediation, "status" | "reason">): string {
  return REMEDIATION[r.status === "verified" ? "verified" : r.reason] ?? r.reason.replace(/_/g, " ");
}

export const STEP_NAMES = ["apply", "build", "security_before", "security_after", "functionality", "integrity"] as const;
export const STEP_LABEL: Record<(typeof STEP_NAMES)[number], string> = {
  apply: "Patch applies", build: "Builds", security_before: "Test fails before", security_after: "Test passes after", functionality: "Project tests", integrity: "Nothing protected touched",
};

/** "The test failed before the fix", as a step reads when it did what it should: security_before is the one step where failing is the right result. */
export function stepGood(name: (typeof STEP_NAMES)[number], status: string | undefined): boolean {
  return name === "security_before" ? status === "failed" : status === "passed";
}

export function checkWords(v: EngineValidation): string {
  const what = v.cmd.startsWith("verify_fix") ? "Fix verification" : `${v.kind[0].toUpperCase()}${v.kind.slice(1)} run`;
  const who = v.by === "engine" ? " (run by the engine)" : "";
  if (v.class === "passed") return `${what} passed${who}`;
  if (v.class === "validation_env") return `${what} could not run here${who}${v.env_reason ? `: ${v.env_reason}` : ""}`;
  return `${what} failed${who}${v.readable ? `: ${v.readable}` : ""}`;
}

/** "3 of 20 rounds, 12 of 150 tool calls, 1 min 5 s of 30 min": what a run has used of what it was given; only the limits it has. */
export function budgetWords(run: EngineRun | null | undefined): string {
  if (!run) return "";
  const parts = [`${run.spent.rounds} of ${run.budget.rounds} rounds`];
  if (run.budget.tool_calls) parts.push(`${run.spent.tool_calls} of ${run.budget.tool_calls} tool calls`);
  if (run.budget.seconds) parts.push(`${duration(run.spent.active_seconds)} of ${duration(run.budget.seconds)}`);
  if (run.budget.tokens) parts.push(`${run.spent.tokens.toLocaleString("en-US")} of ${run.budget.tokens.toLocaleString("en-US")} tokens`);
  return parts.join(", ");
}

export function duration(seconds: number): string {
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
  return `${Math.round(seconds)} s`;
}
