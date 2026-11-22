import type { CampaignAnalytics, CampaignOptions, CampaignRow, CampaignVerdict } from "../../lib/api";

// A campaign as the API gives it, for the tests of the campaign screens.
export const campaign = (over: Partial<CampaignRow> = {}): CampaignRow => ({
  campaign_id: "cmp-20261010-aaaa", name: "npm first", state: "running", created_at: "2026-10-10T10:00:00Z", started_at: "2026-10-10T10:00:00Z", finished_at: null,
  heartbeat_ts: 1, control: "run", job_id: "job1", pid: 4242, resumes: 0, error: null, ecosystems: ["npm"],
  progress: { target: "npm", stage: "mine", step: 3, steps: 4, percent: 62.5, label: "Mine the code pairs" },
  summary: {}, declared: { cpu: 2, ram_gb: 1.5 }, legacy: false,
  plan: { name: "npm first", ecosystems: ["npm"], preset: "collect", stages: { ingest: true, queue: true, recover: true, mine: true }, knobs: { mine_workers: 4 }, build_profile: null,
          context: { profile: "weak", workers: 2, cap: null } },
  ...over,
});

const ORDER = ["ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions", "enrich", "build"];
export const campaignOptions = (over: Partial<CampaignOptions> = {}): CampaignOptions => ({
  targets: [
    { name: "npm", downloaded: true, ingested: true, dump_bytes: 400_000_000, snapshot: null, mineable: true, reason: null, owner: null, stopped: null },
    { name: "PyPI", downloaded: true, ingested: true, dump_bytes: 120_000_000, snapshot: null, mineable: true, reason: null, owner: null, stopped: null },
    { name: "Ubuntu", downloaded: true, ingested: true, dump_bytes: 770_000_000, snapshot: null, mineable: false, reason: "This ecosystem has no mining adapter, so a campaign only reads its dump.", owner: null, stopped: null },
    { name: "Go", downloaded: true, ingested: false, dump_bytes: 50_000_000, snapshot: null, mineable: true, reason: null, owner: "cmp-20261010-zzzz", stopped: "read" },
  ],
  presets: [
    { id: "ingest_only", name: "Ingest only", stages: ["ingest"] }, { id: "collect", name: "Collect", stages: ["ingest", "queue", "recover", "mine"] },
    { id: "collect_verify", name: "Collect and verify", stages: ["ingest", "queue", "recover", "mine", "verify", "resplit"] },
    { id: "full", name: "Full", stages: ["ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions"] },
    { id: "full_context", name: "Full with model-written context", stages: ["ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions", "enrich"] },
  ],
  stages: ORDER.map((id) => ({ id, label: id === "enrich" ? "Write the context with the model" : id === "build" ? "Build a draft dataset" : id.replace("_", " "), scope: id === "build" ? "campaign" as const : "ecosystem" as const })),
  knobs: { mine_workers: 4, pause_seconds: 10, deps_workers: 24, recover_workers: 8, recover_limit: null, quota: null, refresh: false },
  knob_limits: { mine_workers: [1, 16], deps_workers: [1, 64], recover_workers: [1, 32] },
  build_profiles: [{ id: "training", name: "Training (recommended)", description: "Every usable pair, at most 5 per CVE." }, { id: "final", name: "Final model", description: "Validation folded in." }],
  context_profiles: [{ id: "weak", name: "Only weak context", description: "Pairs whose real text is thin." }, { id: "all", name: "All pairs", description: "Every pair without a draft." }],
  context_defaults: { profile: "weak", workers: 2, cap: null }, context_workers: [1, 4], default_preset: "collect",
  ...over,
});

export const verdict = (over: Partial<CampaignVerdict> = {}): CampaignVerdict => ({
  ok: true, plan: campaign().plan!, errors: [], warnings: [],
  per_ecosystem: { npm: { stages: ["ingest", "queue", "recover", "mine"], notes: [] } },
  needs: { cpu: 2, ram_gb: 1.4, github_per_hour: 2800, disk_gb: 15, context: false, build: false },
  free: { cpu: 5, ram_gb: 9.2, github_per_hour: 4500, disk_gb: 180, jobs: 4 },
  ...over,
});

const nums = (a: number, b: number, c: number, d: number, e: number) => ({ advisories: a, with_fix_pointer: b, mined: c, pairs: d, usable_pairs: e });
export const analytics = (over: Partial<CampaignAnalytics> = {}): CampaignAnalytics => ({
  campaign_id: "cmp-20261010-aaaa", name: "npm first", state: "running", legacy: false, ecosystems: ["npm"], created_at: "2026-10-10T10:00:00Z", started_at: "2026-10-10T10:00:00Z", finished_at: null,
  generated_at: "2026-10-10T11:00:00Z", note: null,
  timeline: { steps: [
    { target: "npm", stage: "ingest", label: "Read the advisories", status: "done", attempts: 1, started_at: "2026-10-10T10:00:00Z", finished_at: "2026-10-10T10:10:00Z", seconds: 600, error: null, eta_seconds: null, fraction: null },
    { target: "npm", stage: "mine", label: "Mine the code pairs", status: "running", attempts: 1, started_at: "2026-10-10T10:10:00Z", finished_at: null, seconds: 3000, error: null, eta_seconds: 1000, fraction: 0.75 },
    { target: "*", stage: "build", label: "Build a draft dataset", status: "pending", attempts: 0, started_at: null, finished_at: null, seconds: null, error: null, eta_seconds: null, fraction: null }],
    total: 3, done: 1, errors: 0, percent: 40, elapsed_seconds: 3600, active_seconds: 3600 },
  errors: { by_cause: [{ cause: "rate_limit", n: 2 }, { cause: "network", n: 1 }], latest: [{ at: "2026-10-10T10:30:00Z", target: "npm", stage: "mine", cause: "network", message: "down" }],
            commits: [{ status: "no_code", reason: "no_source_files", n: 5 }], forced_kills: 0, interruptions: 1 },
  funnel: { ecosystems: { npm: { before: nums(10, 4, 2, 5, 4), now: nums(110, 54, 22, 55, 44), gained: nums(100, 50, 20, 50, 40) } }, total: nums(100, 50, 20, 50, 40), has_baseline: true,
            flow: { stages: [{ id: "advisories", label: "Advisories read", unit: "advisories", count: 100, lost: [] }, { id: "with_fix_pointer", label: "With a fix commit", unit: "advisories", count: 50, lost: [{ id: "no_fix", label: "No fix commit found", count: 50 }] },
                             { id: "commits", label: "Fix commits worked on", unit: "commits", count: 20, lost: [{ id: "no_code:x", label: "no code: no_source_files", count: 5 }] }, { id: "usable_pairs", label: "Usable code pairs", unit: "pairs", count: 40, lost: [] }] } },
  throughput: { bucket_seconds: 600, per_hour: { commits: 120, pairs: 80 },
    buckets: [{ at: "2026-10-10T10:10:00Z", commits: 5, pairs: 3 }, { at: "2026-10-10T10:20:00Z", commits: 9, pairs: 6 }, { at: "2026-10-10T10:30:00Z", commits: 0, pairs: 0 }],
    samples: [{ at: "2026-10-10T10:10:00Z", rss_mb: 300, pending: 50, remaining: 4000, waiting: false }, { at: "2026-10-10T10:20:00Z", rss_mb: 340, pending: 36, remaining: 3800, waiting: true }],
    waits: [{ start: "2026-10-10T10:20:00Z", end: "2026-10-10T10:25:00Z" }] },
  mix: { pairs: 40, commits: 20, language: { python: 25, javascript: 15 }, granularity: { file: 20, function: 20 }, ecosystem: { npm: 40 }, cwe: { "CWE-79": 12, "CWE-22": 3 }, severity: { high: 22, critical: 8, medium: 10 } },
  context: null, outcomes: { commits: 25, by_status: { mined: 20, no_code: 5 }, reasons: [{ status: "no_code", reason: "no_source_files", n: 5 }] },
  ...over,
});
