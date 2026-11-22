import type { RunningOllamaModel, ServingStatus } from "../src/lib/api";

export const servingFixture = (running_models: RunningOllamaModel[] = []): ServingStatus => ({
  ollama: { running: true, version: "0.35.0", error: null },
  choices: { llm: { engine: "local", model: "" }, embed: { engine: "local", model: "" } },
  slots: {},
  exclusive: null,
  running_models,
});

// Answers for the API calls the pages make when they open. A path with no fixture gets an empty object and is reported by the test, so a missing
// answer is visible rather than silently wrong. The shapes follow lib/api.ts.

const record = {
  id: 1, osv_id: "GHSA-aaaa-bbbb-cccc", cve_id: "CVE-2024-1001", package: "left-pad", summary: "Prototype pollution", published: "2024-03-01T00:00:00Z",
  ecosystem: "npm", language: "javascript", fix_commit_count: 1, code_status: "mined", pair_count: 2, osv_severity: "HIGH", cvss_score: null, source: "osv",
};
const pair = {
  id: 1, pair_key: "k1", commit_key: "github.com/o/r@abc1234", cve_id: "CVE-2024-1001", ghsa_id: null, ecosystem: "npm", package: "left-pad", cwe: "CWE-1321",
  file_path: "src/index.js", language: "javascript", function_name: "merge", granularity: "function", is_test: 0, tangled: 0, code_available: 1, skip_reason: null, source: "mined", llm_model: null,
};
const codeSummary = {
  fix_commits: { total: 3, by_status: { mined: 2, pending: 1 }, cves_with_fix: 2 },
  pairs: { total: 2, usable: 2, tangled: 0, tests: 0, functions: 2, cves: 1, commits: 1, enriched: 0, by_language: { javascript: 2 }, by_granularity: { function: 2 }, by_source: { mined: 2 }, by_skip_reason: {}, top_cwe: [] },
  dataset: null, languages: ["javascript"],
};
const storeSummary = { total: 1, by_source: { osv: 1 }, with_cve: 1, with_code: 0, with_vector: 0, by_code_status: { mined: 1 }, by_dependency_status: {}, by_language: { javascript: 1 }, by_ecosystem: { npm: { advisories: 1, malware: 0, withdrawn: 0, informational: 0, with_fix_pointer: 1, mined: 1, pairs: 2, usable_pairs: 2 } } };
const run = {
  name: "r1", state: "configured", dataset: { tag: "v1", manifest_sha256: "a".repeat(64), status: "frozen" }, model_revision: "b".repeat(40), stage: 1, blocked: [],
  bundle: { path: "training/runs/r1/bundle.zip", sha256: "c".repeat(64), bytes: 42_000_000, files: 120, warnings: [] },
};
const build = { tag: "v1", status: "frozen", path: "data/processed/dataset_v1", counts: { records: 10, groups: 4, views: 30, duplicates: 1 }, tier_mix: { unreviewed: 1, auto_checked: 8, llm_adjudicated: 0, human_reviewed: 1 }, versions: {}, blocked: {}, files: {} };
const settings = {
  llm_base_url: "http://localhost:11434/v1", llm_api_key: "", llm_api_key_set: false, llm_model: "", nvd_api_key: "", github_token: "", hf_token: "",
  smtp_host: "", smtp_port: "587", smtp_user: "", smtp_password: "", smtp_from: "", smtp_to: "", cpu_workers: "auto", interim_dir: "", gpu_python: "", models_dir: "", llm_engine: "", llm_local_model: "", llm_local_adapter: "", llm_local_context: "", embed_engine: "", embed_local_model: "", embed_device: "", embed_base_url: "", embed_model: "", embed_api_key_set: false, embed_api_key_hint: "", model_idle_minutes: "", quality_policy: "", compute_gpu: "", ui_gpu: "", max_jobs: "", notify_email: "", desktop_notify: "", confirm_quit: "",
  database_dir: "", database_storage: { path: "data/interim/database", in_effect: "data/interim/database", restart_required: false, exists: true, free_gb: 100, problems: [] }, window_frame: "",
  storage: { path: "data/interim", in_effect: "data/interim", restart_required: false, exists: true, free_gb: 100, problems: [] },
};
const device = { cpu: { physical_cores: 6, logical_cores: 12, workers: 5 }, ram: { total_gb: 16, free_gb: 8 }, disks: [{ drive: "C:", fstype: "NTFS", total_gb: 500, free_gb: 120 }], gpu: { available: false, reason: "no card", cards: [] } };
const mix = { scope: "store", total: 1, language: { javascript: 1 }, ecosystem: { npm: 1 }, severity: { critical: 0, high: 1, medium: 0, low: 0, none: 0 } };

const options = {
  records_mined: 1, profiles: [], split: { splits: ["train", "val"], val_share: 0.05, train_all: false, rule: "stable hash per group" },
  window: { since: null, collected_at: null, earliest: "2024-01-01", latest: "2024-12-31" }, languages: { javascript: 2 }, ecosystems: { npm: 1 }, cwes: { "CWE-1321": 1 },
  severities: { Critical: 0, High: 1, Medium: 0, Low: 0 }, defaults: {"date_from": null, "date_to": null, "languages": [], "severities": [], "require_cvss": false, "cwes": [], "require_cwe": false, "ecosystems": [], "exclude_suspect": true, "only_verified": false, "granularity": "", "prefer_function": true, "max_lines": 300, "min_lines": 1, "max_changed": 0, "require_advisory": false, "require_commit_msg": false, "only_enriched": false, "include_synthetic": true, "weak_context": "keep", "val_share": 0.05, "train_all": false, "include_tests": false, "include_tangled": false, "sibling_languages": true, "include_quarantined": false, "unit": "pairs", "per_cve_cap": 5, "safe_ratio": 2.0, "max_chars": 8000, "profile": "training"}, fix_checks: {}, records_suspect: 0,
};

type Hit = unknown;
const GET: [RegExp, Hit][] = [
  [/^\/api\/ide\/projects$/, { projects: [] }],
  [/^\/api\/ide\/models$/, { models: [] }],
  [/^\/api\/health$/, { ok: true }],
  [/^\/api\/db\/status$/, { engine: "sqlite", embedded: true, path: "data/interim/database/aivf.sqlite3", uri: "data/interim/database/aivf.sqlite3", database: "aivf", reachable: true, container: { name: "", state: "not_required" }, version: "3.53.1", journal_mode: "wal" }],
  [/^\/api\/db\/overview$/, { collections: [], database: "aivf", documents: 0, bytes: 0 }],
  [/^\/api\/db\/backups$/, { backups: [], folder: "data/interim/backups" }],
  [/^\/api\/device$/, device],
  [/^\/api\/compute$/, { physical_cores: 6, logical_cores: 12, ram_gb: 16, ram_free_gb: 8, cpu_workers: 5, auto_cpu_workers: 5, max_cpu_workers: 16, gpu: { available: false } }],
  [/^\/api\/lifecycle$/, { clean: true, note: "", at: null }],
  [/^\/api\/jobs$/, []],
  [/^\/api\/models\/partial$/, { partial: [] }],
  [/^\/api\/serving\/ollama\/models$/, { status: "ready", models: [] }],
  [/^\/api\/collections\/cves$/, { query: "", ecosystem: "", state: "", total: 0, offset: 0, limit: 50, ecosystems_searched: [], downloaded_ecosystems: 0, unread: [], results: [] }],
  [/^\/api\/settings$/, settings],
  [/^\/api\/settings\/storage$/, settings.storage],
  [/^\/api\/data\/stamp$/, { stamp: "1" }],
  [/^\/api\/dataset\/summary$/, storeSummary],
  [/^\/api\/dataset\/records$/, { total: 1, total_exact: true, limit: 50, offset: 0, records: [record] }],
  [/^\/api\/dataset\/facets$/, { total: 1, severity: { low: 0, medium: 0, high: 1, critical: 0, unrated: 0 }, ecosystem: { npm: 1 }, language: { javascript: 1 }, code_status: { mined: 1 }, dependency_status: { none: 1 } }],
  [/^\/api\/dataset\/records\/\d+$/, record],
  [/^\/api\/dataset\/records\/\d+\/(fix-commits|pairs)$/, []],
  [/^\/api\/dataset\/lineage$/, { stages: [{ id: "advisories", label: "Vulnerability advisories", unit: "advisories", count: 1, lost: [] }], set_aside: {} }],
  [/^\/api\/dataset\/mix$/, mix],
  [/^\/api\/dataset\/by-year$/, { total: 1, dated: 1, undated: 0, unrated: 0, years: [{ year: 2024, unrated: 0, low: 0, medium: 0, high: 1, critical: 0, total: 1 }] }],
  [/^\/api\/results\/dataset-stats$/, {}],
  [/^\/api\/code\/summary$/, codeSummary],
  [/^\/api\/code\/pairs$/, { total: 1, records: [pair] }],
  [/^\/api\/code\/context-items$/, { items: [], total: 0, admission_policy: "automatic", context_improvement_optional: true }],
  [/^\/api\/code\/pairs\/\d+$/, { ...pair, diff: "@@ -1 +1 @@\n-a\n+b\n", code_before: "a\n", code_after: "b\n" }],
  [/^\/api\/code\/dataset\/options$/, options],
  [/^\/api\/code\/mining\/checkpoint$/, { interrupted: false, active: false, active_here: false, pending: 1, failed: 0 }],
  [/^\/api\/data\/builds$/, { builds: [build], next_version: "v2", next_versions: { dataset: "v2" } }],
  [/^\/api\/data\/build-profiles$/, { profiles: [{ id: "js_ts_security", label: "JS/TS security", counts: { source_pairs: 1, automatically_eligible: 1 }, blockers: [] }], default_profile: "js_ts_security" }],
  [/^\/api\/data\/split-check$/, { split_versions: [] }],
  [/^\/api\/data\/review\/summary$/, { rows: 10, candidates: 8, auto_checked: 8, tiers: {}, failed: {}, not_evaluated: [], decisions: 0, audit_id: null, rubric_version: "rubric_v1", audit_error_rate: "unestimated" }],
  [/^\/api\/experiments$/, []],
  [/^\/api\/finetune\/runs$/, { runs: [run], root: "training/runs" }],
  [/^\/api\/finetune\/runs\/r1$/, { name: "r1", prepared: {}, cpu_validation: null, state: { state: "configured", blocked: [], evidence: {} }, imports: [], eval_imports: [], local_gpu: null,
    local_train: { kind: "train", model: "Qwen/Qwen2.5-Coder-0.5B", passed: true, gpu: { name: "NVIDIA GeForce GTX 1660" }, folder: "f", state: "interrupted", complete: false, steps_done: 3, steps: 8, interrupted_by: "session_budget", pinned_base: false } }],
  [/^\/api\/finetune\/runs\/r1\/curves$/, { sources: [] }],
  [/^\/api\/finetune\/runs\/r1\/local-fit$/, { available: true, pinned_base: "Qwen/Qwen2.5-Coder-7B", default_model: "Qwen/Qwen2.5-Coder-0.5B", running: false, model: "Qwen/Qwen2.5-Coder-7B", is_pinned_base: true,
    gpu: { name: "NVIDIA GeForce GTX 1660", total_mb: 6144, free_mb: 5000 },
    gate: { verdict: "cannot_fit", refuse: true, model: "Qwen/Qwen2.5-Coder-7B", context: 4096, micro_batch: 1, rank: 32, message: "Qwen/Qwen2.5-Coder-7B needs at least 5.9 GB of video memory for its weights and adapters, and 4.9 GB is free: it cannot run on this card. Train it on Colab or Kaggle with the notebook, or choose a smaller model." } }],
  [/^\/api\/finetune\/(baselines|benchmarks)$/, { baselines: [], benchmarks: [], rules: [] }],
  [/^\/api\/train\/config$/, { sections: [{ key: "lora", title: "LoRA adapters", fields: [{ key: "lora.r", name: "r", kind: "int", value: 16, fixed: false, minimum: 1 }] }], counts: { settings: 1, fixed: 0, changeable: 1 }, file_sha256: "a".repeat(64), unresolved: [] }],
  [/^\/api\/train\/profiles$/, { profiles: { contract: { label: "As in the contract", purpose: "The pinned configuration.", rationale: "No override.", overrides: {} } } }],
  [/^\/api\/projects$/, { projects: [{ id: "shop-ab12cd", name: "The shop", folders: [{ path: "C:/code/shop", languages: { Python: 12, JavaScript: 3 }, ecosystems: ["PyPI", "npm"], manifests: {}, files_seen: 15, truncated: false }],
    languages: { Python: 12, JavaScript: 3 }, ecosystems: ["PyPI", "npm"], created_at: "2026-10-09T10:00:00Z", updated_at: "2026-10-09T10:00:00Z",
    authorization: { acknowledged: true, at: "2026-10-09T10:00:05Z", statement: "I own this code, or I am authorised to test it." }, last_scan: null }], statement: "I own this code, or I am authorised to test it. The app may read it and copy it into a sandbox to analyse it." }],
  [/^\/api\/projects\/capabilities$/, { capabilities: [{ key: "manifest_parsing", label: "Reads the manifests and lock files" }, { key: "advisory_match", label: "Matches the versions against known advisories" }],
    ecosystems: { PyPI: { manifest_parsing: { status: "partial", note: "" }, advisory_match: { status: "implemented", note: "" } }, npm: { manifest_parsing: { status: "partial", note: "" }, advisory_match: { status: "needs_data", note: "" } } } }],
  [/^\/api\/projects\/[^/]+\/chat\/endpoint$/, { configured: false, model: null, host: "", local: false, provider: "" }],
  [/^\/api\/projects\/[^/]+\/chats$/, { chats: [], damaged: 0 }],
  [/^\/api\/projects\/[^/]+\/index$/, { enabled: true, built: false }],
  [/^\/api\/projects\/[^/]+\/updates$/, { updates: [] }],
  [/^\/api\/projects\/[^/]+\/scans$/, { scans: [] }],
  [/^\/api\/projects\/[^/]+\/audit$/, { entries: [] }],
  [/^\/api\/serving\/models$/, { files: [{ id: "org/model", name: "model-q4.gguf", path: "D:/m/model-q4.gguf", bytes: 2147483648, source: "folder",
    info: { architecture: "qwen2", name: "Qwen", quantisation: "Q4_K_M", context_length: 32768, layers: 24, parameters: 494000000 } }] }],
  [/^\/api\/serving\/strategies$/, { strategies: [] }],
  [/^\/api\/serving\/derived$/, { path: "D:/m/converted", budget_gb: 0, used_bytes: 0, entries: [] }],
  [/^\/api\/settings\/migrations$/, { migrations: [] }],
  [/^\/api\/agent\/providers$/, { providers: [] }],
  [/^\/api\/agent\/mcp$/, { servers: [], limits: { servers: 8, timeout: 60 } }],
  [/^\/api\/agent\/permissions$/, { rules: [] }],
  [/^\/api\/agent\/toolchain$/, { platform: "win32", tools: [] }],
  [/^\/api\/skills$/, { skills: [], bundled: [{ name: "dependency-review", description: "Review exact dependency releases against public advisories." }] }],
  [/^\/api\/serving\/adapters$/, { adapters: [] }],
  [/^\/api\/serving\/convert-adapter\/runs$/, { adapters: [] }],
  [/^\/api\/serving\/plan$/, { info: { context_length: 32768, layers: 24, quantisation: "Q4_K_M" }, context: 4096, parts: { weights: 2147483648, kv_cache: 268435456, overhead: 805306368, total: 3221225472 },
    plan: { runs_on: "gpu", gpu_layers: 999, layers: 24, fits: true, message: "All 24 layers fit on the graphics card (about 3.0 GB of the 4.9 GB that is free)." } }],
  [/^\/api\/gpu\/mode$/, { mode: "idle", holders: [] }],
  [/^\/api\/models\/local$/, { hub_cache: "cache", hub_cache_exists: false, models_dir: null, models_dir_exists: false, models: [], total_bytes: 0 }],
  [/^\/api\/collections\/ecosystems$/, { source: "online", note: null, ecosystems: [{ name: "npm", enabled: true, remote: true, dump_bytes: 1000, snapshot: "2026-10-01T00:00:00Z", downloaded: true, ingested: true, etag: null }] }],
  [/^\/api\/campaigns$/, { campaigns: [], interrupted: [], owners: {} }],
  [/^\/api\/results\/experiments$/, []],
  [/^\/api\/sandbox\/status$/, { docker_available: false, reason: "Docker daemon not reachable", hardening: [] }],
  [/^\/api\/sandbox\/(containers|images)$/, []],
  [/^\/api\/sandbox\/image-list$/, { docker_available: false, images: [], pinned: null }],
  [/^\/api\/sandbox\/history$/, { containers: [] }],
  [/^\/api\/verify\/receipts$/, { receipts: [] }],
  [/^\/api\/verify\/pilot$/, { ran: false, summary: null }],
  [/^\/api\/verify\/cases$/, { cases: [{ case_id: "pilot_o_x_aaaa1111", library: "o/x", outcome: "verified", evidence_level: "strong" }] }],
  [/^\/api\/serving\/status$/, servingFixture()],
  [/^\/api\/serving\/models$/, { files: [{
    id: "org/model", name: "model-q4.gguf", path: "D:/m/model-q4.gguf", bytes: 1_000_000, source: "folder",
    info: { name: "model", architecture: "qwen2", quantisation: "Q4_K_M", context_length: 4096, layers: 24, parameters: 1_000_000 },
  }], not_ready: [] }],
  [/^\/api\/serving\/adapters$/, { adapters: [] }],
  [/^\/api\/serving\/convert-adapter\/runs$/, { adapters: [] }],
  [/^\/api\/serving\/derived$/, { path: "D:/m/converted", budget_gb: 0, used_bytes: 0, entries: [] }],
  [/^\/api\/rag\/status$/, { enabled: false, chat_enabled: true, configured: { kind: "gguf", engine: "local", model: "jina-code-embeddings-0.5b-Q8_0.gguf" }, mismatch: false, message: null, kb: { built: false },
    embedder: { engine: "local", model: "jina-code-embeddings-0.5b-Q8_0.gguf", revision: "0123456789ab", dimension: 896, installed: true, pinned: true, query_instruction: "", document_instruction: "", quantisation: "Q8_0", model_width: 896, licence: null, recommended: { repo: "jinaai/jina-code-embeddings-0.5b-GGUF", file: "jina-code-embeddings-0.5b-Q8_0.gguf", size_mb: 531, dimension: 896, licence: "CC-BY-NC-4.0" } } }],
  [/^\/api\/rag\/projects$/, { projects: [] }],
  [/^\/api\/config\/summary$/, { rag: { enabled: false }, model: {}, data: {} }],
];

const preview = {
  records_mined: 1, funnel: [{ step: "mined pairs", rows: 2 }], rows: 2, records: 1, cves: 1, commits: 1, split_counts: { train: 2, val: 0 }, split_cves: { train: 1, val: 0 },
  by_language: { javascript: 2 }, by_severity: { High: 2 }, top_cwe: { "CWE-1321": 2 }, by_granularity: { function: 2 }, llm_enriched: 0,
};
// Answers to the calls that read through a POST (a preview, a search).
const POST: [RegExp, Hit][] = [
  [/^\/api\/train\/resolve$/, { ok: true, problems: [], changed: [], fingerprint: "f".repeat(64) }],
  [/^\/api\/code\/dataset\/preview$/, preview],
];

export function fixtureFor(method: string, pathname: string): Hit | undefined {
  if (method === "GET") return GET.find(([re]) => re.test(pathname))?.[1];
  return POST.find(([re]) => re.test(pathname))?.[1] ?? {};
}
