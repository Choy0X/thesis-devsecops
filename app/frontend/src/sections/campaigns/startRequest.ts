import type { CampaignOptions, CampaignRequest } from "../../lib/api";

// What the owner chooses in the start dialog, kept as plain data, and the request that is sent from it. The same request is sent to be judged
// and to start, so what was judged is what starts.

export type Draft = {
  miningVersion?: 2 | 4;
  computeMode?: 'cpu' | 'cuda';
  name: string;
  ecosystems: string[];
  stages: Record<string, boolean>;
  mineWorkers: string; quota: string; pauseSeconds: string; recoverLimit: string; recoverWorkers: string; depsWorkers: string; refresh: boolean;
  buildProfile: string;
  contextProfile: string; contextWorkers: string; contextCap: string;
};

export const STAGE_ABOUT: Record<string, string> = {
  ingest: "Download the dump if it is not here, and read it. For an ecosystem with an adapter, its advisories go into the dataset.",
  queue: "Find the fix commits the records point to and queue them for mining.",
  recover: "Look for a fix commit for the records that name none (needs a GitHub token).",
  mine: "Fetch each queued commit and cut the vulnerable and the fixed code into pairs.",
  verify: "Check that each mined commit really is the fix, against the release dates of the package.",
  resplit: "Split the stored file pairs into function and hunk pairs.",
  dep_versions: "Work out the first fixed and the latest safe version of each record's package.",
  enrich: "Have the model chosen in Settings write a root cause, a secure pattern and an explanation for the pairs.",
  build: "Build a draft code-pair dataset of these ecosystems with a profile. It is a preview, never a frozen version.",
};

export function stagesOfPreset(options: CampaignOptions, preset: string): Record<string, boolean> {
  const on = new Set(options.presets.find((p) => p.id === preset)?.stages ?? []);
  return Object.fromEntries(options.stages.map((s) => [s.id, on.has(s.id)]));
}

/** The preset whose stages are exactly the ones that are on, or null (the plan is custom). */
export function presetOf(options: CampaignOptions, stages: Record<string, boolean>): string | null {
  const now = options.stages.filter((s) => stages[s.id]).map((s) => s.id).join(",");
  return options.presets.find((p) => options.stages.filter((s) => p.stages.includes(s.id)).map((s) => s.id).join(",") === now)?.id ?? null;
}

export function initialDraft(options: CampaignOptions, ecosystems: string[] = []): Draft {
  return {
    name: "", ecosystems, stages: stagesOfPreset(options, options.default_preset),
    mineWorkers: String(options.knobs.mine_workers ?? ""), quota: "", pauseSeconds: String(options.knobs.pause_seconds ?? ""), recoverLimit: "",
    recoverWorkers: String(options.knobs.recover_workers ?? ""), depsWorkers: String(options.knobs.deps_workers ?? ""), refresh: false,
    buildProfile: options.build_profiles[0]?.id ?? "", contextProfile: options.context_defaults.profile, contextWorkers: String(options.context_defaults.workers), contextCap: "",
  };
}

const number = (text: string): number | null => (text.trim() !== "" && Number.isFinite(Number(text)) ? Number(text) : null);

/** The request for a draft: only the knobs of the stages that are on are sent, empty ones as nothing (the stage's own default). */
export function requestOf(draft: Draft, options: CampaignOptions): CampaignRequest {
  const on = (s: string) => !!draft.stages[s];
  const knobs: Record<string, number | boolean | null> = {};
  if (on("mine")) Object.assign(knobs, { mine_workers: number(draft.mineWorkers), quota: number(draft.quota), pause_seconds: number(draft.pauseSeconds) });
  if (on("recover")) Object.assign(knobs, { recover_limit: number(draft.recoverLimit), recover_workers: number(draft.recoverWorkers) });
  if (on("dep_versions")) knobs.deps_workers = number(draft.depsWorkers);
  if (on("ingest")) knobs.refresh = draft.refresh;
  const req: CampaignRequest = { name: draft.name.trim() || null, ecosystems: draft.ecosystems, preset: presetOf(options, draft.stages), stages: draft.stages, knobs };
  if (draft.miningVersion === 4) {
    req.mining = {version: 4, parser_workers: 5};
    if (on("mine")) knobs.pause_seconds = 0;
  }
  if (draft.computeMode) req.compute = {mode: draft.computeMode};
  if (on("build")) req.build_profile = draft.buildProfile || null;
  if (on("enrich")) req.context = { profile: draft.contextProfile, workers: number(draft.contextWorkers) ?? options.context_defaults.workers, cap: number(draft.contextCap) };
  return req;
}

/** Why the draft cannot be sent yet, in words (the server judges the rest). */
export function draftProblem(draft: Draft): string | null {
  if (!draft.ecosystems.length) return "Choose at least one ecosystem.";
  if (!Object.values(draft.stages).some(Boolean)) return "Switch at least one stage on.";
  return null;
}
