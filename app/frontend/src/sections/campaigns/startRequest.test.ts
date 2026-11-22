import { describe, expect, it } from "vitest";
import { draftProblem, initialDraft, presetOf, requestOf, stagesOfPreset } from "./startRequest";
import { campaignOptions } from "./fixtures";

const options = campaignOptions();

describe("the draft of a campaign and the request made from it", () => {
  it("starts from the default preset, with the settings the server offers", () => {
    const d = initialDraft(options, ["npm"]);
    expect(d.ecosystems).toEqual(["npm"]);
    expect(Object.entries(d.stages).filter(([, on]) => on).map(([s]) => s)).toEqual(["ingest", "queue", "recover", "mine"]);
    expect(d.mineWorkers).toBe("4");
    expect(d.recoverWorkers).toBe("8");
    expect(d.pauseSeconds).toBe("10");
    expect(d.contextProfile).toBe("weak");
    expect(d.buildProfile).toBe("training");
  });

  it("knows which preset the stages are, and says custom for any other set", () => {
    expect(presetOf(options, stagesOfPreset(options, "full"))).toBe("full");
    expect(presetOf(options, stagesOfPreset(options, "ingest_only"))).toBe("ingest_only");
    const custom = { ...stagesOfPreset(options, "collect"), verify: true };
    expect(presetOf(options, { ...custom, resplit: true, dep_versions: false })).toBe("collect_verify");
    expect(presetOf(options, { ...custom, build: true })).toBeNull();
  });

  it("sends only the settings of the stages that are on, and empty ones as nothing", () => {
    const d = { ...initialDraft(options, ["npm", "PyPI"]), name: "  First  ", quota: "50", pauseSeconds: "", refresh: true };
    const req = requestOf(d, options);
    expect(req).toEqual({ name: "First", ecosystems: ["npm", "PyPI"], preset: "collect", stages: d.stages,
      knobs: { mine_workers: 4, quota: 50, pause_seconds: null, recover_limit: null, recover_workers: 8, refresh: true } });
    const ingestOnly = requestOf({ ...d, stages: stagesOfPreset(options, "ingest_only") }, options);
    expect(ingestOnly.knobs).toEqual({ refresh: true });
    expect(ingestOnly.preset).toBe("ingest_only");
  });

  it("sends the build profile and the context depth only when those stages are on", () => {
    const d = initialDraft(options, ["npm"]);
    const full = requestOf({ ...d, stages: { ...stagesOfPreset(options, "full_context"), build: true }, contextProfile: "all", contextWorkers: "3", contextCap: "100", buildProfile: "final" }, options);
    expect(full.build_profile).toBe("final");
    expect(full.context).toEqual({ profile: "all", workers: 3, cap: 100 });
    expect(full.preset).toBeNull();
    const plain = requestOf(d, options);
    expect(plain.build_profile).toBeUndefined();
    expect(plain.context).toBeUndefined();
  });

  it("says in words why a draft cannot be sent", () => {
    const d = initialDraft(options, []);
    expect(draftProblem(d)).toBe("Choose at least one ecosystem.");
    expect(draftProblem({ ...d, ecosystems: ["npm"] })).toBeNull();
    expect(draftProblem({ ...d, ecosystems: ["npm"], stages: stagesOfPreset(options, "none") })).toBe("Switch at least one stage on.");
  });
});

it("sends an explicitly selected adaptive mining policy for a new campaign", () => {
  const draft={...initialDraft(options,["npm"]),miningVersion:4 as const};
  const request=requestOf(draft,options);
  expect(request.mining).toEqual({version:4,parser_workers:5});
  expect(request.knobs?.pause_seconds).toBe(0);
});
