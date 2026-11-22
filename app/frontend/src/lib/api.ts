// Thin API client for the FastAPI backend. Relative URLs so it works whether
// served by the backend (desktop) or proxied in dev.
import { ApiError } from "./apiError";
import type { Receipt } from "../components/VerificationTimeline";
export { ApiError, errorText } from "./apiError";

export type Health = { ok: boolean; env: Record<string, boolean>; stages: string[] };
export type QualificationWorkflow = {id: string; parent_tag: string; dataset_tag?: string; state: string; phase?: string; reason?: string;
  shortages?: string[]; cancel_requested?: boolean; next_retry_at?: number;
  pending_verification?: {code?: string; reason?: string; next_retry_at?: number} | null;
  phase_progress?: {label?: string; processed?: number; total?: number};
  run_preparation?: {state: string; name?: string; run?: string}};
export type QualificationWorkflowPage = {items: QualificationWorkflow[]; total: number; limit: number; offset: number};
export type QualificationPhasePage = {items: Array<{id: string; phase: string; state: string; wave: number}>; total: number; limit: number; offset: number};
export type AutomaticRecoveryItem = {item_id: string; record_id: string; status: string; retryable: boolean; cached?: boolean;
  outcome_summary_truncated?: boolean;
  reasons: string[]; missing_facts?: string[]; failure_category?: string; repair_revision_id?: string;
  observation_id?: string; cursor?: number; source_locations?: Array<{side: string; path: string; revision?: string}>};
export type AutomaticRecoveryPage = {items: AutomaticRecoveryItem[]; total: number; limit: number; offset: number;
  history_available?: boolean; phase_state?: string; reason?: string};
export type AutomaticRecoveryAttemptPage = {items: Array<{attempt_id: string; action: string; state: string}>; total: number; limit: number; offset: number};
export type AutomaticRecoveryDetail = AutomaticRecoveryItem & {original: Record<string, unknown>; recovered: Record<string, unknown>;
  diff: Record<string, {changed?: boolean; unified?: string; truncated?: boolean}>; source_bindings: unknown; artifacts: unknown; outcome?: unknown;
  observations: {items: AutomaticRecoveryItem[]; has_more?: boolean}};
// The project config (GET /api/config/summary), read from training/configs/dataset_finetuning.yaml.
export type ConfigSummary = {
  config_path: string; config_sha256: string;
  rag: { enabled: boolean };
  model: { id: string; initialization: string; serialization: string;
    revision_resolved: boolean; tokenizer_revision_resolved: boolean };
  data: { pooling: string; ecosystems: string[]; ecosystem_status?: { name: string; local_data: boolean }[]; languages: string | string[]; min_review_tier: string;
    purpose: string; dataset_version: string; validation_fraction_target: number };
  training: { stage: string; precision: string; max_total_tokens: number };
};
export type JobProgress = {
  state?: string; label?: string; processed?: number; total?: number;
  model_tag?: string;
  batch?: number; batches?: number; counts?: Record<string, number>;
  until?: number; reason?: string; ts?: number;
  quota?: { remaining: number; limit: number; reset: number } | null;
  // Which kind of number processed/total are ("bytes", "files" or "percent", where processed is already out of 100; none = a plain count), how fast and how long to go, and which step of a multi-step job this is.
  unit?: "bytes" | "files" | "percent"; rate?: number | null; eta?: number | null; phase?: string; step?: number; steps?: number;
  // A job that continues earlier work (a download from the bytes on the disk, a read from its last checkpoint): where it picked up, in the unit above.
  resumed_from?: number | null; resumed?: boolean;
  // A transfer of several files: how many are finished of how many are planned, and the one being fetched now (a download of a model).
  files?: number; files_total?: number; current?: string;
  // How long a finished transfer took, in seconds.
  seconds?: number;
};
export type Job = { id: string; stage: string; status: string; params: Record<string, any>;
  started: string; finished: string; error: string; log_count: number; logs?: string[]; log_start?: number;
  progress?: JobProgress | null };
// A change a newer version of the app made to the saved settings (settings_migrations.py): what was found and what was done, until it is acknowledged.
export type SettingsMigration = { id: string; at: string; changes: string[]; acknowledged: boolean };
export type Settings = {
  // engine / credentials. Secret fields always come back empty: the backend sends
  // <name>_set and <name>_hint ("…abcd") instead of the value.
  llm_base_url: string; llm_api_key: string; llm_model: string;
  nvd_api_key: string; github_token: string; hf_token: string;
  smtp_host: string; smtp_port: string; smtp_user: string;
  smtp_password: string; smtp_from: string; smtp_to: string;
  interim_dir: string; storage: StorageInfo; database_dir: string; database_storage?: StorageInfo; cpu_workers: string; gpu_python: string;
  [extra: string]: any;
};
export type StorageInfo = {
  path: string; in_effect: string; restart_required: boolean; exists: boolean;
  free_gb: number | null; problems: string[];
};
// The Python interpreters that could drive the local GPU jobs, each probed for torch and CUDA (GET /api/compute/pythons).
export type ComputePython = { path: string; label: string; cuda: boolean; torch?: string | null; cuda_build?: string | null; name?: string | null; total_mb?: number | null; reason?: string | null };
export type ComputePythons = { pythons: ComputePython[]; automatic: string | null; chosen: string };
export type ComputeInfo = {
  physical_cores: number; logical_cores: number; ram_gb: number | null; ram_free_gb: number | null;
  cpu_workers: number; auto_cpu_workers: number; max_cpu_workers: number;
  gpu: { available: boolean; name?: string; total_mb?: number; free_mb?: number; reason?: string; python?: string };
};
export type TestResult = { ok: boolean; detail: string; response?: string };
export type DiscoverResult = { ok: boolean; models: string[]; detail?: string };

export type DataRecord = {
  id: number; osv_id: string | null; cve_id: string | null; package: string | null;
  summary: string | null; details: string | null; published: string | null;
  cvss_vector: string | null; cvss_v4_vector: string | null; cvss_score: number | null;
  osv_cwe: string | null; cwe: string | null; code_before: string | null;
  code_after: string | null; filename: string | null; source: string | null;
  ecosystem?: string | null;
  // code mining (filled live by Extract / Mine)
  language?: string | null; language_source?: string | null; fix_commits?: string | null;
  fix_commit_count?: number | null; code_status?: string | null; pair_count?: number | null;
  // dependency versions (affected ranges -> first fixed / latest safe release)
  osv_severity?: string | null;
  affected?: string | null; affected_versions?: string | null; fixed_version?: string | null;
  safe_version?: string | null; dependency_status?: string | null; deps_checked_at?: string | null;
};
export type AffectedPackage = {
  ecosystem: string; package: string;
  ranges: { introduced?: string; fixed?: string; last_affected?: string }[];
  versions?: string[]; versions_total?: number; first_fixed?: string | null; latest_fixed?: string | null;
  latest_version?: string | null; safe_version?: string | null; safe_is_prerelease?: boolean;
  registry?: boolean; status?: string;
};
export type RecordFixCommit = {
  commit_key: string; repo_url: string; owner: string; repo: string; sha: string; via?: string;
  source?: string; status: string; error?: string | null; detected_langs?: string | null;
  pairs?: number | null; commit_date?: string | null;
  fix_check?: "verified" | "suspect" | "unknown" | null; fix_check_reason?: string | null;
  link?: "declared" | "mentioned" | null;
};
/** A row of the records table: a few short fields, no long texts. The whole record is read when it is opened. */
export type RecordRow = {
  id: number; cve_id: string | null; osv_id: string | null; package: string | null; ecosystem: string | null; language: string | null;
  osv_severity: string | null; cvss_score: number | null; fix_commit_count: number | null; code_status: string | null; pair_count: number | null;
  published: string | null; dependency_status: string | null; summary: string | null;
};
/** A page of the table; a filtered count stops at 10,000 (`total_exact` is then false and the page says "10,000+"). */
export type RecordsPage = { total: number; total_exact?: boolean; limit: number; offset: number; records: RecordRow[] };
export type RecordFilters = {
  language?: string; code_status?: string; dependency_status?: string; severity?: string; ecosystem?: string;
  /** One of id, published, cve_id, ecosystem (the server refuses anything else). */
  sort?: string; dir?: "asc" | "desc";
};
/** How many records fall under each value of a filter, each counted under the other filters. */
export type RecordFacets = {
  total: number; severity: Record<string, number>; ecosystem: Record<string, number>; language: Record<string, number>;
  code_status: Record<string, number>; dependency_status: Record<string, number>;
};
export type StoreSummary = {
  total: number; by_source: Record<string, number>;
  with_cve: number; with_code: number; with_vector: number;
  by_code_status?: Record<string, number>; by_language?: Record<string, number>;
  by_dependency_status?: Record<string, number>;
};
export type PatchScores = {
  n: number; exact_match: number | null; edit_similarity: number | null;
  fix_line_recall: number | null; vuln_line_removal: number | null;
  unchanged_rate: number | null; syntax_valid_rate: number | null; plausible_fix_rate: number | null;
};
export type PatchMetrics = PatchScores & {
  task: "patch"; tag: string; by_language: Record<string, PatchScores>; dataset: string; note: string;
};
export type Metrics = {
  task?: string;
  tag: string; n: number; accuracy: number; macro_f1: number;
  per_class: Record<string, { precision: number; recall: number; f1: number; support: number }>;
  critical_false_negative_rate: number | null;
  critical_total?: number; critical_missed?: number; split?: string | null;     // recorded since the counts were added
  confusion_matrix: number[][]; confusion_matrix_labels: string[];
};

// The report of the shared evaluator (eval-shared/1): every arm, every track; an empty track is {status:"blocked", reason}, never a zero.
export type RatioCi = { estimate: number | null; numerator: number; denominator: number; clusters: number; ci: [number, number] | null; status: string; error?: string };
export type DecisionCell = {
  n: number; coverage: number | null; abstention_rate: number | null; status_counts: Record<string, number>;
  vulnerable: { n: number; detected: number; answered: number; insufficient: boolean; recall: number | null; conditional_recall: number | null; ci: { issue_group: RatioCi; repository: RatioCi } | null };
  negative: { n: number; false_positives: number; answered: number; insufficient: boolean; fpr: number | null; ci: { issue_group: RatioCi; repository: RatioCi } | null };
  insufficient_context: { n: number; correct: number; recall: number | null };
  /** For each truth label, how many cases ended in each outcome (a label, or the status of an answer that was not given). */
  confusion?: Record<string, Record<string, number>>;
};
export type CriticalReport = { critical_detection_miss_rate: RatioCi; severity_underclassification_rate: RatioCi; n_critical: number; severity_missing: number; severity_unknown_truth: number };
export type TrackBlocked = { status: "blocked"; reason: string };
export type DiscoveryReport = { overall: DecisionCell; by_ecosystem: Record<string, DecisionCell>; by_language: Record<string, DecisionCell>; critical: CriticalReport };
export type RepairInput = { n: number; verified: number; failed: number; env_error: number; unavailable: number; receipt_missing: number; verified_rate: number | null; receipt_coverage: number | null; ci: RatioCi };
export type RepairReport = { n: number; by_input: Record<string, RepairInput> };
export type EvalShared = {
  schema: "eval-shared/1"; tag: string; n_attempts: number; split?: string | null; tracks_blocked: Record<string, string>;
  arms: Record<string, { generated_decision?: DiscoveryReport | TrackBlocked; repair?: RepairReport | TrackBlocked; [track: string]: unknown }>;
  run_info?: { run: string | null; baseline: string | null; arm: string | null; role: string | null; model: string | null; smoke_test: boolean | null; partial: boolean | null;
    counts_as_evaluation: boolean | null; excluded: number | null };
};

// Two evaluation runs side by side: whether they may be compared at all, and on the items both answered, whether one is better (see compare_guard.py and compare_stats.py).
export type CompareGuard = { comparable: boolean; blockers: { field: string; reason: string; a: unknown; b: unknown }[]; unconfirmed: { field: string; reason: string }[] };
export type CompareStats = {
  items: number; both_right: number; both_wrong: number; only_a: number; only_b: number; disagreements: number; accuracy_a: number | null; accuracy_b: number | null; difference: number;
  verdict: "a_better" | "b_better" | "no_difference_shown" | "not_enough_data"; reason: string; p_value: number | null; interval: [number, number] | null;
  rule: { alpha: number; min_items: number; min_disagreements: number; test: string };
};
export type RunCompare = {
  a: string; b: string; guard: CompareGuard; warnings: string[]; items: { a: number; b: number; shared: number }; stats: CompareStats | null; stats_note: string | null;
  disagreements: { view_id: string; truth: string; a: string; b: string }[]; disagreements_total: number;
  grouped_paired?: ({independent_groups: number; views: number; direction: string; scope: string} &
    Partial<Record<'recall' | 'false_positive_rate' | 'precision' | 'f1' | 'mcc' | 'coverage',
      {estimate: number | null; interval_95: [number, number] | null; state: string}>>) | null;
};

// What differs between two evaluation runs: the settings of their training runs that are not the same and the facts of the datasets they were trained on (pipeline/compare_config.py).
export type DatasetFacts = {
  tag: string; status: string | null; manifest_sha256: string | null; current: boolean; counts: { views_by_task?: Record<string, number>; [k: string]: number | Record<string, number> | undefined };
  tier_mix: Record<string, number>; languages: Record<string, number>; external: { original_file?: string; sha256?: string; imported_at?: string } | null;
};
// The runs of a comparison by weakness class (pipeline/compare_cwe.py): the class of a case is joined from the dataset each run was prepared on.
export type CweCell = { vulnerable: number; detected: number; negatives: number; false_positives: number };
export type CweData = {
  runs: { tag: string; available: boolean; reason: string | null; min_cell: number; items: number; unmatched: number }[];
  cwes: { cwe: string; cells: (CweCell | null)[] }[]; note: string;
};
export type ConfigSide = { available: boolean; reason: string | null; run: string | null };
export type ConfigDiff = {
  a: ConfigSide; b: ConfigSide;
  config: { compared: boolean; training_compared: boolean; same: number; differing: { key: string; a: unknown; b: unknown }[] };
  dataset: { same: boolean | null; a: DatasetFacts | null; b: DatasetFacts | null };
};

// The model gateway and the graphics card's mode (pipeline/gateway, pipeline/gpu_modes.py).
export type ModelEngine = { name: string; version: string | null; build: string | null; model_file: string | null; quantisation: string | null; architecture: string | null; context: number | null;
  gpu_layers: number | null; layers: number | null; runs_on: "gpu" | "split" | "cpu" | null; adapter: string | null; adapter_sha256: string | null; adapter_trained_from: string | null;
  batch?: number | null; cache_type?: string | null; flash_attention?: boolean | null; layers_on_card?: number | null };
/** A model folder of the cache of converted models (pipeline/derived_cache.py). */
export type DerivedEntry = { name: string; path: string; bytes: number; files: number; last_used: number; used: boolean; pinned: boolean; label: string | null; in_use: string | null;
  settings_roles: string[]; adapters: number };
export type DerivedCache = { path: string; budget_gb: number; used_bytes: number; entries: DerivedEntry[] };
export type ModelSlot = { state: "absent" | "loading" | "ready" | "draining" | "unloading" | "failed"; message: string; model: string | null; runs_on: "gpu" | "split" | "cpu" | "nowhere" | null;
  gpu_bytes: number; in_flight: number; served: number; idle_seconds: number | null; loaded_seconds: number | null; error: { code: string; message: string } | null; engine: ModelEngine | null };
export type RunningOllamaModel = { name: string; size_vram: number; context_length: number | null };
export type InstalledOllamaModel = {
  tag: string; name: string; digest: string; capabilities: string[];
  location: "local" | "cloud"; available: boolean; size: number;
};
export type OllamaInventory = { models: InstalledOllamaModel[]; status: "ready" | "unreachable" };
export type OllamaInstall =
  | { kind: "pull"; tag: string }
  | { kind: "huggingface"; repo_id: string; revision: string; filename: string; tag?: string }
  | { kind: "gguf"; path: string; tag?: string }
  | { kind: "adapter"; run_name: string; base_path: string; tag?: string };
export type ServingStatus = {
  ollama: { running: boolean; version: string | null; error: string | null };
  choices: { llm: { engine: "local" | "external"; model: string } };
  slots: Partial<Record<"llm", ModelSlot>>;
  exclusive: { owner: string; since: number } | null;
  running_models: RunningOllamaModel[];
};
/** An installed model that llama.cpp cannot run as it is: stored as safetensors, with no GGUF file and no conversion made here. */
export type NotReadyModel = { id: string; name: string; source: string; bytes: number; role: "chat" | "embedding" };
export type ServingFile = { id: string; name: string; /** What the model is, for a list: for a converted model the model it was made from and its format; else the file's name. */ label?: string; /** What a converted model was installed as; absent for the others, which are told by their names. */ role?: "chat" | "embedding" | null; path: string; bytes: number; source: string; error?: string;
  info: { architecture: string; name: string | null; quantisation: string | null; context_length: number | null; layers: number | null; parameters: number | null } | null };
// The adapters converted to GGUF (pipeline/gguf_convert.py); the conversions themselves are jobs that the install and the import of a fine-tune start.
export type ServingAdapter = { name: string; path: string; bytes: number; sha256: string | null; base: string | null; revision: string | null; rank: number | null; trained_from: string | null; created_at: string | null };
export type RunAdapter = { run: string; name: string; folder: string; base: string | null; rank: number | null; bytes: number; base_installed: boolean; converted: boolean };
export type GpuMode = { mode: "idle" | "train" | "serve" | "embed"; holders: { mode: string; what: string; stoppable: boolean }[] };

// Projects: code the person owns or may test (pipeline/projects.py).
export type ProjectFolder = { path: string; languages: Record<string, number>; ecosystems: string[]; manifests: Record<string, string[]>; files_seen: number; truncated: boolean };
export type Project = { id: string; name: string; folders: ProjectFolder[]; languages: Record<string, number>; ecosystems: string[]; created_at: string; updated_at: string;
  authorization: { acknowledged: boolean; at: string; statement: string }; last_scan: { at: string; summary?: string } | null; instructions: string };
/** What a folder would be as a project. `suggested_name` is a name made from the folder (a manifest's name, the repository's, the README's title or the folder's own) and `name_source` says which. */
export type FolderLook = { ok: true; path: string; languages: Record<string, number>; ecosystems: string[]; manifests: Record<string, string[]>; files_seen: number; truncated: boolean;
  suggested_name?: string; name_source?: string } | { ok: false; reason: string };
export type AuditEntry = { at: string; tool: string; summary: string; target: string; started: string; ended: string | null; exit_status: number | string | null; evidence: string | null };

// What the app can do per package ecosystem, and the report of a dependency scan (pipeline/scan/capabilities.py, project_deps.py).
export type CapabilityStatus = "implemented" | "partial" | "needs_data" | "not_implemented";
export type Capabilities = { capabilities: { key: string; label: string }[]; ecosystems: Record<string, Record<string, { status: CapabilityStatus; note: string }>> };
export type DepAdvisory = { id: string; cve: string | null; summary: string | null; severity: string; cwe: string | null; affected_versions: string; first_fixed: string | null; safe_version: string | null; rule: string };
export type DepRow = { id: string; ecosystem: string; name: string; version: string | null; declared: string | null; source: string; manifests: string[]; state: "vulnerable" | "no_finding" | "inconclusive" | "env_error";
  reason: string; checks: string[]; severity?: string; advisories?: DepAdvisory[]; recommendation?: string; outdated?: { state: string; latest?: string; behind?: string | null; reason?: string } };
export type DepScanSummary = { dependencies: number; vulnerable: number; no_finding: number; inconclusive: number; env_error: number; severity: Record<string, number>; manifests_not_read: number };
export type DepReport = { schema: string; kind: string; scan_id: string; project_id: string; started: string; finished: string; outdated_checked: boolean; words: string;
  ecosystems: Record<string, { checked: boolean; reason?: string; dependencies?: number }>; problems: { ecosystem: string; manifest: string; reason: string }[]; dependencies: DepRow[];
  outdated: { looked_up: number; outdated: number; up_to_date: number; unavailable: number; not_looked_up: number } | null; summary: DepScanSummary };
export type CodeSeverity = "critical" | "high" | "medium" | "low";
export type CodeFinding = { id: string; rule: string; severity: CodeSeverity; cwe: string; summary: string; why: string; advice: string; state: "candidate"; file: string; folder: string; line: number;
  language: string; in_tests: boolean; snippet: string; context: { line: number; text: string; match: boolean }[]; note?: string; more_in_file?: number };
export type CodeScanSummary = { files: number; findings: number; severity: Record<CodeSeverity, number>; by_rule: Record<string, number>; in_tests: number; secrets_only_languages: string[]; skipped: number; cut_short: boolean };
export type CodeReport = { schema: string; kind: "code"; scan_id: string; project_id: string; started: string; finished: string; words: string; rules: number;
  languages: Record<string, { files: number; scanned: number; coverage: "rules" | "secrets_only"; reason: string }>; skipped: Record<string, number>; findings: CodeFinding[]; summary: CodeScanSummary };
// The full Scan Codebase (pipeline/scan/deep_scan.py): checks (one per tool) and findings that tools agree on, in the sandbox.
export type DeepSeverity = CodeSeverity | "unknown";
export type DeepFinding = { id: string; tool: string; sources: string[]; rule: string; rules?: string[]; state: "vulnerable" | "inconclusive"; basis: string; severity: DeepSeverity; severity_source: "tool" | "rule" | "unknown";
  severity_by?: string; file: string; line: number | null; cwe: string[]; type: string; message: string; snippet: string; confidence: string | null; evidence: { file: string; sha256: string } | null;
  extra?: { why?: string; advice?: string; in_tests?: boolean; context?: { line: number; text: string; match: boolean }[] } };
export type DeepCheck = { tool: string; state: "ran" | "partial" | "env_error" | "skipped"; rule_set: string; version: string | null; files: number | null; findings: number; note: string; evidence: string | null; sha256: string | null;
  problems: string[]; folder?: string; tests?: number; passed?: number; failed?: number; errors?: number; skipped?: number; failing?: string[] };
export type DeepSummary = { findings: number; by_severity: Record<DeepSeverity, number>; by_state: { vulnerable: number; inconclusive: number }; by_type: Record<string, number>; checks_ran: string[]; checks_partial: string[]; checks_failed: string[]; checks_skipped: string[]; words: string };
export type DeepReport = { schema: string; kind: "codebase"; scan_id: string; project_id: string; started: string; finished: string; words: string; image: { tag: string; id?: string; built_at?: string };
  container: Record<string, unknown>; copy: { folder: string; as: string; files: number; bytes: number; skipped: Record<string, number>; secret_like_names: string[] }[]; checks: DeepCheck[]; findings: DeepFinding[]; summary: DeepSummary };
// The response orchestrator (pipeline/agent/orchestrator.py): findings classified into three classes (or Unknown), each with the action taken; patch candidates are for review and never applied.
export type SeverityClass = "Recommendation" | "Warning" | "Critical" | "Unknown";
export type ClassSource = "cvss_vector" | "osv_label" | "tool" | "rule" | "model" | "unknown";
export type ResponseAction = { action: string; candidate_id?: string; outcome?: string; receipt_state?: string; why?: string; upgrade_to?: string; how?: string };
export type ResponseFinding = { id: string; kind: "dependency" | "codebase"; rule?: string; message: string; state: string; file: string | null; line: number | null; package?: string; version?: string; ecosystem?: string;
  advisory?: string; cve?: string | null; fixed_in?: string | null; cwe: string[]; severity: string; severity_class: SeverityClass; class_source: ClassSource; class_note: string; action: ResponseAction };
export type ResponseReport = { schema: string; kind: "response"; scan_id: string; project_id: string; started: string; finished: string; scope: string; mode: string; depth: string;
  options: { assist: boolean; allow_remote: boolean; test: boolean }; findings: ResponseFinding[]; email: { subject: string; body: string; sent: boolean; reason?: string } | null; notes: string[];
  summary: { findings: number; by_class: Record<SeverityClass, number>; by_action: Record<string, number> }; words: string };
export type CandidateStep = { status: "passed" | "failed" | "not_run" | "env_error"; detail: string | null };
export type Candidate = { schema: string; candidate_id: string; project_id: string; created: string; label: string; receipt_state: string; file: string | null; stopped?: string; reasons: string[]; words: string;
  steps: Record<string, CandidateStep>; model: { model: string; host: string; local: boolean } | null; diff?: string;
  tests: { before: { note?: string } | null; after: { note?: string } | null; outcome: string; words: string }; finding: { id: string; file: string; line: number; rule: string; message: string } };
export type CandidateListItem = { candidate_id: string; file: string | null; rule: string | null; outcome: string; receipt_state: string; stopped: string | null; created: string };
// Trying an update in the sandbox (pipeline/scan/update_trial.py): packages fetched as wheels, installed offline in two closed containers, the tests and tools compared before and after.
export type TrialSet = { label: string; install: { state: string; exit_code: number | null; evidence: string | null; note: string }; checks: DeepCheck[]; findings: DeepFinding[]; skipped?: string };
export type TrialFinding = { id: string; file: string; line: number | null; rule: string; message: string; state: string; severity: string };
export type UpdateTrial = { schema: string; id: string; update_id: string; project_id: string; started: string; finished: string; state: "done" | "prefetch_failed" | "install_failed" | "running";
  files: string[]; not_installed: string[]; python: string; platform: string; prefetch: Record<string, { ok: boolean; files: string[]; bytes: number; note: string }>; before: TrialSet | null; after: TrialSet | null;
  comparison: { tests: { outcome: string; words: string; before: { note?: string } | null; after: { note?: string } | null }; findings: { new: TrialFinding[]; gone: TrialFinding[]; unchanged: number };
    checks_changed: { tool: string; before: string | null; after: string | null }[] } | null; note: string; words: string };
// Chat about a project (pipeline/project_chat.py): the answer is streamed as lines of JSON events.
export type ChatEndpoint = { configured: boolean; model: string | null; host: string; local: boolean; provider: string; system_prompt?: string; system_prompt_default?: string; system_prompt_custom?: boolean;
  /** Which web search provider is set up in Settings ("brave" | "google" | "duckduckgo"), or "" when none is. */
  web_search_provider?: string };
export type ChatSource = { tool: string; summary: string };
export type ChatUsage = { prompt_tokens: number | null; completion_tokens: number | null; seconds?: number | null; tokens_per_second?: number | null };
export type ChatEvent =
  | { type: "start"; model: string; model_label: string; host: string; local: boolean }
  | { type: "token"; text: string }
  | { type: "thinking"; text: string }
  | { type: "tool"; name: string; arguments: string; ok: boolean; summary: string }
  | { type: "notice"; text: string }
  | { type: "retrieved"; items: { file: string; line: number; score: number }[] }
  | { type: "retrieved_web"; items: { url: string; title: string; domain: string; score: number }[] }
  | { type: "saved"; user_id: string; assistant_id: string; title: string }
  | { type: "error"; message: string }
  | { type: "done"; sources: ChatSource[]; usage: ChatUsage | null; model: string; model_label: string; stopped?: boolean; trimmed?: boolean; compacted?: boolean };
// Stored conversations of the project chat (pipeline/project_chats.py): one file each, with its parameters and its messages.
export type ChatParams = { system: string; temperature: number | null; top_p: number | null; max_tokens: number | null; autocompact: boolean; web_search: boolean };
// Skills (pipeline/skills.py): a named block of system-prompt text, shared across every project, applied to a conversation's own ``system`` with /skill <name>.
export type SkillSummary = { schema: string; name: string; description: string; approved: boolean; created_at: string; prompt_sha256: string };
export type Skill = SkillSummary & { prompt: string };
export type ChatToolLine = { ok: boolean; summary: string };
/** ``model`` is the literal name a request carries ("llm" for the local gateway); ``model_label`` is what to show, the real model a result says answered. A message stored before
 * this existed has only ``model``, so a reader falls back to it. ``trimmed``/``compacted`` say whether the oldest turns were left out of, or folded into a summary for, what the
 * model was sent to answer this one (the full conversation itself is always kept, in full, regardless). */
export type StoredMessage = { id: string; role: "user" | "assistant"; content: string; created_at: string; thinking?: string; tools?: ChatToolLine[]; sources?: ChatSource[]; notices?: string[];
  usage?: ChatUsage | null; model?: string; model_label?: string; error?: string; stopped?: boolean; trimmed?: boolean; compacted?: boolean };
export type StoredChat = { schema: string; id: string; title: string; created_at: string; updated_at: string; params: ChatParams; messages: StoredMessage[] };
export type ChatSummary = { id: string; title: string; created_at: string; updated_at: string; message_count: number; preview: string };
/** What is sent for one turn of a stored conversation: a new question, or the last answer again, or a question edited and asked again from there. ``deep_research``/``plan_mode`` are
 * one-shot modifiers for this turn only (``/deep-research``, ``/plan``): not saved to ``ChatParams``. */
export type ChatTurnBody = { text?: string; regenerate?: boolean; edit_of?: string; allow_remote: boolean; deep_research?: boolean; plan_mode?: boolean; force_web_search?: boolean };
// Agentic mode (pipeline/agent_sessions.py, agent_loop.py): a session's mode is live, changeable any time nothing is pending (projectChatUpdate-style PATCH), not fixed at
// creation. Plan is read-only; Manual pauses for approval before a write or a command; Auto runs on unattended, capped. Nothing in any mode ever writes to the real project --
// only to the project's one shared sandbox copy.
export type AgentMode = "plan" | "manual" | "auto";
export type AgentStatus = "idle" | "waiting_approval" | "done" | "error" | "stopped";
/** What a call was asked under (agentic/permissions.py): why the owner is asked, and the patterns "Allow always" would add (empty: this call can only be allowed once). */
export type AgentPermission = { action: "allow" | "ask" | "deny"; permission: string; value: string; reason: string; source: string; always: string[] };
export type ToolchainTool = { tool: string; wanted: string; licence: string; note: string; supported: boolean; size: number | null; installed: boolean; tested: boolean; notes: string[]; installed_at: string | null; preparing?: boolean; preparation_error?: string | null };
export type ToolchainStatus = { platform: string | null; unsupported?: string; tools: ToolchainTool[] };
export type AgentRule = { tool: string; pattern: string; action: "allow" | "ask" | "deny" };
/** One of the owner's MCP servers as the page may see it: the names of its variables, never their values. */
export type McpServer = { name: string; command: string[]; env_names: string[]; enabled: boolean; timeout: number };
/** What the owner sends for a server: a variable sent as null keeps the value that is saved. */
export type McpServerIn = { command: string[]; env: Record<string, string | null>; enabled: boolean; timeout: number };
export type McpCheck = { servers: { name: string; ok: boolean; tools: number; error: string }[]; tools: { name: string; server: string; tool: string }[] };
export type AgentPendingCall = { id: string; name: string; arguments: string; asked_at: string; permission?: AgentPermission };
export type AgentToolCall = { id: string; type: "function"; function: { name: string; arguments: string } };
/** A file the owner attached to an instruction with an at sign: its path as the agent reads it, and whether only the start of it was given. */
export type AttachedFile = { path: string; cut: boolean };
export type AgentMessage = { id: string; role: "user" | "assistant" | "tool"; content: string | null; created_at: string; attachments?: AttachedFile[]; tool_calls?: AgentToolCall[]; tool_call_id?: string;
  ok?: boolean; name?: string; summary?: string; thinking?: string; thinking_kind?: "reasoning" | "deliberation"; thought?: string;
  /** what the round that wrote this message cost: the tokens the server counted and the seconds it took */
  usage?: ChatUsage | null;
  /** a tool result as the page is given it (fuller than what the model was sent), whole JSON */ display?: string };
export type AgentTask = { text: string; done: boolean };
/** What is known of a model: how a round asks it for its decision (``native`` tools, or a ``schema`` for the reply), where that came from (read from its file, tested, set by the owner), how it
 * thinks and how much it may write. */
/** How much a model thinks before it acts (agentic/thinking.py): a model that thinks by itself is told the level, one that does not gets a short planning step of its own before an action. */
export type Effort = "off" | "low" | "medium" | "high" | "max";
export type ModelProfileSummary = { protocol: "native" | "text" | "schema"; source: "derived" | "probed" | "manual"; family: string; reasoning: "none" | "toggle" | "effort" | "always"; tool_format: string;
  native_tools: boolean; max_output: number; context: number | null; notes: string[]; efforts?: Effort[]; thinks_itself?: boolean };
export type ProviderModel = { id: string; name?: string; context?: number; max_output?: number; protocol?: "native" | "schema" | "text" };
/** A model server the owner added. Its key is never returned: only that one is set and its last characters. ``trust``: this computer, another computer on the network, or a hosted service. */
export type AgentProvider = { id: string; name: string; kind: string; base_url: string; api_key_set: boolean; api_key_hint: string; headers: string[]; trust: "local" | "lan" | "remote"; models: ProviderModel[] };
export type AgentProviderBody = { name?: string; base_url: string; api_key?: string; models?: Record<string, Omit<ProviderModel, "id">> };
/** One model a session can be given. An empty ``ref`` is the model the settings name. */
export type AgentModel = { ref: string; label: string; provider: string; provider_name: string; trust: "local" | "lan" | "remote"; configured: boolean; profile: ModelProfileSummary | null };
/** A session's own model settings -- the same shape a chat's own ChatParams has (minus `system`, which is `instructions` here), plus `auto_approve`: Manual mode's write/run
 * confirmation pause is skipped when set. An owner-set toggle; nothing a model's own tool call can turn on. */
export type AgentSessionParams = { temperature: number | null; top_p: number | null; max_tokens: number | null; autocompact: boolean; web_search: boolean; auto_approve: boolean; effort?: Effort;
  /** The engine that runs the session: "v2" keeps a record of each run (budgets, a task, checks) and adds the Task tab; anything else is the engine used so far. */
  engine?: "legacy" | "v2" | null };
/** What a session has cost so far, kept in the session: tokens and seconds over every round (a subagent's too), and how big the newest request was. */
export type AgentUsageTotal = { prompt_tokens?: number; completion_tokens?: number; reasoning_tokens?: number; seconds?: number; last_prompt_tokens?: number; rounds?: number };
export type AgentSession = { usage_total?: AgentUsageTotal; schema: string; id: string; project_id: string; mode: AgentMode; title: string; instructions: string; params: AgentSessionParams; model_ref?: string; forked_from?: { session: string; message: string }; created_at: string;
  updated_at: string; status: AgentStatus; transcript: AgentMessage[]; task_list: AgentTask[]; pending_call: AgentPendingCall | null; rounds: number; grants?: AgentRule[];
  /** What the v2 engine keeps about the work on the newest instruction (agentic/engine/task.py): written by the engine, not by the model. Absent for a session of the legacy engine. */
  task?: EngineTask };
// The task of a v2 run, the checks it made and the fixes it verified. Everything here is the engine's own record; the model only writes the text of the plan steps.
export type EngineStep = { id: string; text: string; state: "pending" | "active" | "done" | "blocked" | "skipped"; files?: string[] };
export type EngineValidation = { id: string; call: string; cmd: string; kind: "test" | "lint" | "type" | "scan"; class: "passed" | "validation_failed" | "validation_env"; readable?: string; env_reason?: string; by?: string };
export type EngineRemediation = { id: string; status: "verified" | "unverified"; reason: string; grade: string; note?: string; test: { path: string; sha256: string }; steps: Record<string, string>;
  patch: { files: string[]; lines: number; sha256: string }; finding?: { id?: string; source?: string; rule?: string; file?: string; line?: number; severity?: string; description?: string; message?: string };
  receipt: string | null; tree?: string; seconds?: number; at?: string;
  /** Files were changed after this was verified: it describes the files as they were, not as they are. */
  stale?: boolean;
  /** Plain notes on what the fix adds (a skipped test, a risky call, a large patch): advice for the owner, never part of the verdict. */
  review?: string[] };
export type EngineTask = { id: string; objective: string; mode: "plan" | "build"; state: string; constraints: { text: string; protect?: string[]; implicit?: boolean }[]; plan: EngineStep[];
  observations: { id?: string; kind: string; text: string }[]; files_changed: { path: string; sha_before: string | null; sha_after: string | null }[]; validations: EngineValidation[];
  attempts: { outcome: string }[]; remediations: EngineRemediation[]; completion: { status: "open" | "complete" | "unverified" | "partial"; reason: string }; replans?: number };
/** One run of a v2 session: why it stopped, what it was allowed and what it has used (GET .../agent/sessions/{id}/runs, newest first). */
export type EngineRun = { id: string; n: number; state: string; stop_reason?: string | null; budget: { rounds: number; seconds?: number; tool_calls?: number; tokens?: number };
  spent: { rounds: number; tool_calls: number; tokens: number; active_seconds: number; failures?: { model: number; env: number; policy: number } }; started_at: string; updated_at: string };
export type AgentSessionSummary = { id: string; mode: AgentMode; title: string; status: AgentStatus; created_at: string; updated_at: string; rounds: number; message_count: number;
  pending_call: AgentPendingCall | null };
export type AgentEvent =
  | { type: "start"; model: string; model_label: string; host: string; local: boolean; mode: AgentMode }
  | { type: "token"; text: string }
  | { type: "thinking"; text: string }
  | { type: "attached"; files: AttachedFile[] }
  | { type: "sub"; call: string; child: string; agent: string; event: AgentEvent }
  | { type: "narration"; text: string }
  | { type: "narration_drop" }
  | { type: "tool_start"; id: string; name: string; arguments: string }
  | { type: "tool_output"; id: string; stream: "stdout" | "stderr"; text: string }
  | { type: "tool"; id: string; name: string; arguments: string; ok: boolean; summary: string; result?: string }
  | { type: "pending_approval"; call: { id: string; name: string; arguments: string }; permission?: AgentPermission }
  | { type: "notice"; text: string }
  /** What the run is busy with while nothing else is shown (the one-time check of how the model takes tools): set, and cleared by the same event with an empty text. */
  | { type: "status"; text: string }
  | { type: "error"; message: string; code?: string }
  | { type: "done"; answer?: string; stopped?: boolean };
export type AgentRunBody = { instruction?: string; edit_of?: string; allow_remote: boolean };
export type AgentApproveBody = { call_id: string; decision: "approve" | "deny"; edited_arguments?: Record<string, unknown>; feedback?: string; remember?: "session" | "project" | "always" };
/** The real round/time caps and tool names of each mode (GET /agent/capabilities), read from agent_loop.py's own constants: one source of truth for a UI description of them. */
export type AgentCapabilities = { plan: { max_rounds: number; tools: string[] }; manual: { max_rounds: number; tools: string[]; confirm_tools: string[] };
  auto: { max_rounds: number; max_seconds: number; tools: string[] } };
// Applying a session's work to the real project (pipeline/agent_apply.py): the only path that ever writes there, never a model tool. One agent sandbox is shared by every Manual
// and Auto session of a project, so the diff and the applies below belong to the project, not to one session.
export type AgentDiffFile = { file: string; folder: string; rel: string; label: string; kind: "added" | "changed" | "deleted"; sha256_before: string | null; sha256_after: string | null;
  diff: string; note?: string;
  /** Your project changed this file after the sandbox copy was made: applying it would put the session's older version over your edit. */
  drift?: boolean;
  /** A removed file the person may tick to remove from the real project too (false when your project changed it since the copy, or it is already gone). */
  can_delete?: boolean };
export type AgentApply = { schema: string; id: string; project_id: string; created: string; status: "candidate" | "applied" | "reverted"; applied_at?: string; reverted_at?: string;
  files: AgentDiffFile[]; deletions?: AgentDiffFile[]; deleted?: string[]; conflicts?: string[]; not_applied: AgentDiffFile[]; words: string };
export type AgentApplyListItem = { id: string; created: string; status: AgentApply["status"]; files: number; deletions?: number; applied_at?: string };
/** What "Refresh from project" did: the files brought in from your project, and the files both sides changed (left as the session has them). */
export type AgentRefresh = { updated: string[]; conflicts: string[]; summary: string };
// Checkpointing the agent sandbox (a real git commit in each root) and rolling one back: symmetric with the model's own checkpoint tool, but rollback is human-only.
export type AgentCheckpoint = { root: string; hash: string; at: number; message: string };
// Updating the dependencies of a project (pipeline/scan/update_plan.py): a candidate made on a copy of the manifests, applied only after the person confirms the files.
export type UpdateChange = { ecosystem: string; name: string; from: string; to: string; kind: "fix" | "newer"; bump: string; folder: string; advisories: string[]; files: string[]; locks: string[] };
export type UpdateFile = { file: string; folder: string; rel: string; label: string; sha256_before: string; sha256_after: string; diff: string;
  edits: { name: string; line: number; before: string; after: string; section?: string }[] };
export type UpdatePredicted = { packages: { ecosystem: string; name: string; from: string; to: string; state: string; fixed: string[]; still_affected_by: string[]; newly_affected_by: string[]; reason: string }[];
  advisories_fixed: string[]; advisories_remaining: string[]; advisories_introduced: string[]; words: string };
export type ProjectUpdate = { schema: string; id: string; project_id: string; created: string; mode: "security" | "all"; mode_words: string; from_scan: string; status: "candidate" | "applied" | "reverted";
  applied_at?: string; reverted_at?: string; plan: { mode: string; changes: UpdateChange[]; not_updated: { ecosystem: string; name: string; version: string | null; reason: string }[] };
  files: UpdateFile[]; skipped: { file: string; reason: string }[]; locks_to_regenerate: string[]; predicted: UpdatePredicted; words: string };
export type UpdateListItem = { id: string; created: string; mode: string; mode_words: string; status: ProjectUpdate["status"]; changes: number; files: number; applied_at?: string };
export type ScanListItem = { scan_id: string; kind: string; finished: string; summary: DepScanSummary | CodeScanSummary | DeepSummary | ResponseReport["summary"] };

// Hard reset (GET /api/reset/inventory, POST /api/reset/start, GET /api/reset/status): what can be deleted with its exact sizes, and the run.
export type ResetDatabase = { name: string; bytes: number; documents: number; collections: number };
export type ResetCategory = { id: string; label: string; note: string; default: boolean; warning: string | null; present: boolean; bytes: number; files: number; paths: string[];
  blocked: string | null; databases: ResetDatabase[] };
export type ResetInventory = { running: boolean; done: boolean; token: string | null; taken: number; progress: { category: string; files: number; bytes: number }; error: string | null;
  root: string; categories: ResetCategory[]; docker_note: string; confirm_word: string };
export type ResetStep = { id: string; label: string; ok: boolean; detail: string };
export type ResetStatus = { running: boolean; done: boolean; started: number | null; steps: ResetStep[]; ok: boolean | null; error: string | null; pending: number; categories: string[] };

// The orderly shutdown (POST /api/shutdown, GET /api/shutdown/status).
export type ShutdownStatus = { running: boolean; done: boolean; started: string | null; deadline_seconds: number; ok: boolean;
  steps: { name: string; ok: boolean; detail: string; seconds: number }[] };

// What this computer has (GET /api/device): the graphics card comes from nvidia-smi and carries its reason when there is none.
export type DeviceInfo = {
  cpu: { physical_cores: number; logical_cores: number; workers: number };
  ram: { total_gb: number | null; free_gb: number | null };
  disks: { drive: string; fstype: string; total_gb: number; free_gb: number }[];
  gpu: { available: boolean; reason: string | null; cards: { name: string; driver: string; total_mb: number; free_mb: number; used_mb: number; utilization_pct: number }[] };
};

// The embedded SQLite database (GET /api/db/*).
export type DbStatus = {
  engine: "sqlite"; embedded: true; path: string; journal_mode?: string; vector_search?: string;
  uri: string; database: string; reachable: boolean; reason?: string | null;
  container: { name: string; state: string; detail?: string };
  version?: string; replica_set?: boolean; schema_version?: number | null; search?: boolean;
  collections?: { expected: number; present: number };
};
export type DbCollection = { name: string; what: string; documents: number; bytes: number; storage_bytes: number; index_bytes: number; indexes: number };
export type DbOverview = { database: string; collections: DbCollection[]; documents: number; bytes: number };
export type DbBackup = { file: string; database?: string; created_at?: string; bytes: number; sha256?: string; verified: boolean | null;
  documents?: number; collections?: Record<string, number> };

export type FixCommitSummary = {
  total: number; by_status: Record<string, number>; by_host: Record<string, number>;
  by_fix_check?: Record<string, number>;
  by_source: Record<string, number>; advisories_with_fix: number; cves_with_fix: number;
};
export type PairSummary = {
  total: number; usable: number; tangled: number; tests: number; functions: number; cves: number;
  commits: number; enriched: number; by_language: Record<string, number>;
  by_granularity: Record<string, number>; by_source: Record<string, number>;
  by_skip_reason: Record<string, number>; top_cwe: [string, number][];
};
export type LocalModel = { training_eligible?: boolean; training_reason?: string; id: string; source: "hub_cache" | "folder" | "converted" | "ollama"; path: string; bytes: number; revisions: string[]; main_revision: string | null;
  safetensors: boolean; gguf: string[]; weights: boolean; config: boolean };
export type LocalModels = { hub_cache: string; hub_cache_exists: boolean; models_dir: string | null; models_dir_exists: boolean; models: LocalModel[]; total_bytes: number; total?: number; limit?: number; offset?: number };
export type HubModel = { training_eligible?: boolean; training_reason?: string; id: string; downloads: number | null; likes: number | null; task: string | null; library: string | null; gated: boolean; parameters: number | null; updated: string | null };
export type GgufVersions = { base: string; how: string; training_bytes: number | null; free_bytes: number | null;
  candidates: { id: string; downloads: number | null; likes: number | null; gated: boolean; quants: Record<string, number> }[] };
export type PartialDownload = { repo_id: string; partial_bytes: number; partial_files: number; complete_bytes: number };
export type RemovalPlan = { repo_id: string; bytes: number; partial_bytes: number; revisions: number; path: string; in_use: string | null };
/** What deleting one installed model would do (nothing is deleted by asking). `removable` false carries the reason in `refusal`; `in_use` is a sentence when something uses the model. */
export type InstalledRemovalPlan = { source: LocalModel["source"]; id: string; path: string; kind: "cache" | "folder" | "ollama" | null; removable: boolean; refusal: string | null; bytes: number; files: number;
  revisions: number; kept_shared_files: number; in_use: string | null; settings_use: string[]; ollama_running: boolean; ollama_processes: string[] };
export type InstalledRemoval = { source: LocalModel["source"]; id: string; freed_bytes: number; files: number; stopped: string[]; left_behind: string[] };
export type DownloadPlan = { repo_id: string; revision: string | null; gated: boolean; token_set?: boolean; partial_bytes?: number; quants: string[]; quant_bytes?: Record<string, number>; needs_quant: boolean; files: number; bytes: number; free_bytes: number | null; enough: boolean;
  gguf_files?: { name: string; bytes: number }[];
  /** For a repository of GGUF files: the quantisation to offer first on this computer and why (`fit`: card, split, too_big or unknown). */
  recommended_quant?: { quant: string; reason: string; fit: "card" | "split" | "too_big" | "unknown" } | null;
  /** The file patterns the download fetches: with "*.safetensors" among them the model has to be converted before llama.cpp can run it. */
  patterns?: string[]; parameters?: number | null };
/** What installing a model so that it is ready to run would do and cost (pipeline/model_install.py): `convert` (download and convert), `finish` (an installed model, converted only), `gguf` (a
 *  download is all it takes) or `other` (cannot be made ready). */
export type InstallPlan = {
  kind: "convert" | "finish" | "gguf" | "other"; repo_id: string; revision?: string | null; source?: string | null; role: "chat" | "embedding" | null; role_detected?: boolean;
  outtype?: string; quant?: string | null; format?: string; format_reason?: string; format_fit?: string | null; parameters?: number; parameters_estimated?: boolean; architecture?: string | null; supported?: boolean | null;
  download: { files: number; bytes: number; partial_bytes: number; free_bytes: number | null; enough: boolean; gated: boolean; token_set: boolean } | null;
  convert: { intermediate_bytes: number; final_bytes: number; final_bytes_max: number; peak_bytes: number; fallback: boolean; seconds: number; seconds_note: string } | null;
  scripts: { tag: string | null; installed: boolean; installed_tag: string | null; will_fetch: boolean; blockers: string[]; note: string | null } | null;
  disk_needed_bytes?: number; problems: string[]; ready: boolean; notes: string[] };
export type ShelfModel = Partial<HubModel> & { id: string; role: string; sha?: string | null; error?: string };
export type HubSearch = { query: string; task: string; models: HubModel[] };
export type FitRequest = { model_id: string; source?: string; mode: "inference" | "qlora"; context: number; micro_batch?: number; rank?: number; gguf_file?: string };
export type FitVerdict = "fits" | "tight" | "too_big" | "unknown";
export type FitResult = { mode: string; parts: Record<string, number>; total: number; notes: string[]; estimate_complete?: boolean;
  check: { gpu: FitVerdict; ram: FitVerdict; disk: FitVerdict; runs_on: "gpu" | "cpu" | "nowhere"; gpu_free_bytes: number | null; ram_free_bytes: number | null } };
export type CurveSource = { label: string; rows: number; skipped: number; steps: number[]; loss?: (number | null)[]; grad_norm?: (number | null)[]; lr?: (number | null)[];
  family_pass?: (number | null)[]; tokens?: (number | null)[]; step_seconds?: (number | null)[]; supervised_tokens_per_second?: (number | null)[];
  fields: Record<string, (number | null)[]>;
  analytics?: { schema: string; import_id?: string; run?: { state?: string }; summary?: { committed_steps?: number; committed_tokens?: number; committed_sequences?: number; completeness?: string };
    environment?: Record<string, unknown>; dataset?: Record<string, unknown>; provenance?: Record<string, unknown>; warnings?: unknown[] };
  evaluations?: { step: number; pass: number | null; family_pass: number | null; metric: number | null; metric_name: string | null; val_loss: number | null; n_examples: number | null }[] };
export type RunCurves = { sources: CurveSource[] };
// Vulnerabilities published per year, split by the severity word (GET /api/dataset/by-year); every year from the first to the last is listed.
export type YearRow = { year: number; unrated: number; low: number; medium: number; high: number; critical: number; total: number };
export type ByYear = { total: number; dated: number; undated: number; unrated: number; years: YearRow[] };
export type RunProfile = { label: string; purpose: string; rationale: string; overrides: Record<string, unknown>;
  revision?: string; tokenizer_revision?: string };
export type TrainResolve = { ok: boolean; problems: { key: string; reason: string }[]; changed: { key: string; default: unknown; value: unknown }[]; fingerprint: string | null };
export type TrainSetting = { key: string; name: string; kind: string; value: unknown; fixed: boolean; fixed_reason?: string; choices?: unknown[];
  minimum?: number; above?: number; maximum?: number; below?: number; min_length?: number };
// What a length of training comes to (training/finetune/schedule.py): the steps, the family passes ("epochs") and the evaluation points in steps.
export type ScheduleInfo = { steps: number; family_passes: number; sequences: number; families: number; effective_batch: number; evaluation_steps: number[];
  last_evaluation_step: number | null; steps_after_last_evaluation: number };
// Training data brought in from outside (pipeline/external_data.py): an upload, the check of its rows, and the imports with where each came from.
// Where a heavy job could run (pipeline/compute_choice.py): the processor, the graphics card, both; estimates are labelled and a time is only ever what was measured.
export type ComputeEstimate = { vram_gb: number | null; ram_gb: number | null; basis: string | null; time: { runs: number; median_seconds: number; last_seconds: number } | null; time_basis?: string };
export type ComputeChoice = { device: "cpu" | "gpu" | "hybrid"; label: string; available: boolean; reason: string | null; means: string | null; estimate: ComputeEstimate };
export type ComputeChoices = { stage: string; heavy: boolean; kind?: string; choices: ComputeChoice[]; workers?: number; remembered?: "cpu" | "gpu" | "hybrid" | null };

// An advisory found in the downloaded OSV dumps, with whether the store holds it (pipeline/ingest/cve_search.py).
export type CveHit = { osv_id: string; cve_id: string | null; ecosystem: string; packages: string[]; more_packages: number; summary: string | null; published: string | null; severity: string | null;
  aliases: string[]; state: "in_store" | "downloaded" };
export type CveSearch = { query: string; ecosystem: string; state: string; total: number; offset: number; limit: number; ecosystems_searched: string[]; downloaded_ecosystems: number;
  unread: { ecosystem: string; dump_bytes: number | null; partial?: ReadPartial }[]; results: CveHit[] };
// One advisory in full (GET /api/collections/cves/{ecosystem}/{osv_id}): read from the downloaded dump ("dump") or rebuilt from the parsed table ("table").
export type CveAffected = { package: string | null; ecosystem: string | null; purl: string | null; ranges: { type: string | null; repo: string | null; events: Record<string, string>[] }[]; versions: string[]; versions_more: number };
export type CveDetail = { osv_id: string; cve_id: string | null; ecosystem: string; aliases: string[]; summary: string | null; details: string | null; published: string | null; modified: string | null; withdrawn: string | null;
  severity_label: string | null; cvss_v3: string | null; cvss_v4: string | null; cwe: string[]; github_reviewed: boolean | null; nvd_published_at: string | null; affected: CveAffected[];
  fix_commits: { commit_key?: string; repo_url?: string; sha?: string; via?: string }[]; references: { type?: string; url: string }[]; source_record_hash: string | null;
  source: "dump" | "table"; state: "in_store" | "downloaded"; raw: Record<string, unknown> | null; raw_omitted?: boolean };

// A file from a GPU run checked before it is imported, as steps in plain words (pipeline/import_guide.py).
export type ImportStep = { name: string; title: string; what: string; state: "passed" | "failed" | "not_checked"; failures: string[]; fix: string | null };
export type ImportCheck = { kind: "outputs" | "eval" | "trial"; ok: boolean; already_imported: boolean; steps: ImportStep[]; summary: Record<string, unknown> | null };

// Datasets on the Hugging Face Hub (pipeline/hub_datasets.py): metadata to look at, then one named file fetched into an upload.
export type HubDataset = { id: string; downloads: number | null; likes: number | null; gated: boolean; license: string | null; updated: string | null };
export type HubDatasetFiles = { id: string; revision: string | null; gated: boolean; license: string | null; limit_bytes: number; total_importable: number;
  files: { path: string; size_bytes: number | null; within_limit: boolean }[] };
export type ExternalUpload = { upload_id: string; filename: string; size_bytes: number; sha256: string };
export type ExternalCheck = { file: string; filename?: string; format: string; rows: number; valid_rows: number; rows_with_problems: number; problems_total: number; problems: string[];
  warnings: string[]; tasks: Record<string, number>; splits: Record<string, number>; families: number; ok: boolean };
export type ExternalImport = { name: string; tag: string; original_file: string; size_bytes: number; sha256: string; imported_at: string; imported_by: string; rows: number; views: number;
  tasks: Record<string, number>; splits: Record<string, number>; families: number; warnings: string[]; path: string };
export type TrainConfig = { sections: { key: string; title: string; fields: TrainSetting[] }[]; counts: { settings: number; fixed: number; changeable: number }; file_sha256: string | null; unresolved: string[] };
export type PilotCase = { case_id: string; library: string; outcome: string | null; evidence_level: string };
export type ReceiptRow = { name: string; readable: boolean; run?: string; case_id?: string; verdict?: string; grade?: string; runner?: string; fixture_only?: boolean; sound?: boolean };
export type PilotSummary = { cases_attempted: number; verified: number; reached_a_verdict: number; reproduced_before_the_fix: number; outcomes: Record<string, number>; libraries: string[]; seconds: number };
export type UpdateState = "not_downloaded" | "unknown" | "current" | "update_available";
// snapshot = when the dump was taken on this computer; remote_modified = OSV's own date for the copy; update_state and update_checked_at = the last look at OSV.
// A read or a download that was cut keeps its progress; these say where it stopped (only present when there is one). `active`: some process is working on it right now.
export type ReadPartial = { percent: number; done: number; total: number; rows: number; phase: "parsing" | "assembling"; updated_at: string | null; active: boolean };
export type DownloadPartial = { bytes: number; total: number | null; percent: number | null; update: boolean; active: boolean; updated_at: string | null };
export type EcosystemRow = { name: string; enabled: boolean; remote: boolean; dump_bytes: number | null; snapshot: string | null; downloaded: boolean; ingested: boolean; etag: string | null;
  remote_modified?: string | null; update_state?: UpdateState; update_checked_at?: string | null; read_partial?: ReadPartial; download_partial?: DownloadPartial };
export type EcosystemUpdate = { name: string; state: UpdateState | "unreachable" | "not_listed"; detail?: string; remote_modified?: string | null };
export type EcosystemCatalog = { source: "online" | "saved" | "none"; note: string | null; ecosystems: EcosystemRow[] };
export type FlowReason = { id: string; label: string; count: number };
export type FlowStage = { id: string; label: string; unit: string; count: number; lost: FlowReason[]; reasons_overlap?: boolean; tier_mix?: Record<string, number> };
export type Lineage = { stages: FlowStage[]; set_aside?: Record<string, number> };
export type Mix = { scope: string; total: number; language: Record<string, number>; ecosystem: Record<string, number>; severity: Record<string, number> | null };
export type CodeSummary = { fix_commits: FixCommitSummary; pairs: PairSummary; dataset: any | null; languages: string[] };
export type CodePair = {
  id: number; pair_key: string; commit_key: string; cve_id: string | null; ghsa_id: string | null;
  ecosystem: string | null; package: string | null; cwe: string | null; cvss_vector: string | null;
  published: string | null; commit_date: string | null; repo_url: string | null; fix_commit: string | null;
  parent_commit: string | null; file_path: string; language: string | null; function_name: string | null;
  granularity: string; is_test: number; tangled: number; code_available: number; skip_reason: string | null;
  source: string | null; llm_model: string | null; edited_at?: string | null;
  has_advisory?: number; has_msg?: number; has_llm?: number; span?: string | null;
  fix_check?: string | null;
  code_before?: string | null; code_after?: string | null; diff?: string | null;
  explanation?: string | null; commit_message?: string | null;
  root_cause_llm?: string | null; secure_pattern_llm?: string | null; explanation_llm?: string | null;
};
export type DatasetFilters = {
  date_from: string | null; date_to: string | null; languages: string[]; severities: string[];
  require_cvss: boolean; cwes: string[]; require_cwe: boolean; ecosystems: string[];
  exclude_suspect: boolean; only_verified: boolean;
  granularity: string; prefer_function: boolean; max_lines: number; min_lines: number;
  max_changed: number; require_advisory: boolean; require_commit_msg: boolean;
  only_enriched: boolean; include_synthetic: boolean; include_tests: boolean;
  include_tangled: boolean; unit: "pairs" | "best"; per_cve_cap: number;
  collapse_backports: boolean; attribution: "" | "supporting" | "decision";
  weak_context: "keep" | "enriched_only" | "drop";
  val_share: number; train_all: boolean; safe_ratio: number; include_untouched: boolean; max_chars: number; profile: string;
};
export type BuildProfile = { id: string; name: string; description: string; filters: Partial<DatasetFilters> };
export type EnrichFilters = {
  val_share: number; train_all: boolean;
  levels: string[]; missing: string[]; min_advisory: number; splits: string[];
  status: "missing" | "older" | "any"; order: "weakest" | "newest";
  languages: string[]; severities: string[]; cwes: string[]; ecosystems: string[];
  granularity: string; prefer_function: boolean; max_lines: number; per_cve_cap: number;
  date_from: string | null; date_to: string | null;
  include_tests: boolean; include_tangled: boolean; exclude_suspect: boolean;
};
export type EnrichCandidate = {
  pair_id: number; record_id: number; cve: string | null; osv_id: string | null; file: string;
  function: string | null; span: string | null; granularity: string; language: string;
  severity: string | null; cwe: string | null; split: string; changed_lines: number;
  level: "weak" | "partial" | "strong"; missing: string[]; advisory_state: string; commit_state: string;
  advisory_len: number; advisory: string; commit_message: string; enriched: boolean;
  llm_prompt_version: string | null; llm_model: string | null;
};
export type EnrichCandidates = {
  dataset_rows: number; matching?: number; total: number; enriched: number; older_prompt: number;
  levels: Record<string, number>; all_levels?: Record<string, number>; missing: Record<string, number>;
  missing_labels: Record<string, string>; prompt_version: string; rows: EnrichCandidate[];
  splits?: Record<string, number>;
};
export type DatasetOptions = {
  records_mined: number; profiles?: BuildProfile[]; split: { splits: string[]; val_share: number; train_all: boolean; rule: string };
  window: { since: string | null; collected_at: string | null; earliest: string | null; latest: string | null };
  languages: Record<string, number>; ecosystems: Record<string, number>; cwes: Record<string, number>;
  severities: Record<string, number>; defaults: DatasetFilters;
  fix_checks?: Record<string, number>; records_suspect?: number;
};
export type DatasetPreview = {
  records_mined: number; funnel: { step: string; rows: number }[]; rows: number; records: number;
  cves: number; commits: number; split_counts: Record<string, number>; split_cves: Record<string, number>;
  by_language: Record<string, number>; by_severity: Record<string, number>; top_cwe: Record<string, number>;
  by_granularity: Record<string, number>; llm_enriched: number; duplicates_removed?: number;
  by_fix_check?: Record<string, number>; by_severity_source?: Record<string, number>;
  by_context?: Record<string, number>; warnings?: string[];
  leak_guard_removed?: number; no_date?: number;
  dependencies?: { rows: number; records?: number; split_counts?: Record<string, number>;
    by_status?: Record<string, number>; by_ecosystem?: Record<string, number> };
};
export type MiningRun = {
  state: string; params: Record<string, any>; started: string; pid: number; processed: number;
  total: number; batch: number; heartbeat: string; finished?: string; resumed_from?: string | null;
  counts?: Record<string, number>;
};
export type MiningCheckpoint = {
  run: MiningRun | null; interrupted: boolean; active: boolean; active_here: boolean;
  pending: number; failed: number; repos_bytes: number | null;     // null while the folder is first measured
};
export type EnrichResult = { enriched: number; failed: number; errors: string[]; model: string; pair?: CodePair };


// ── Reviewed-dataset builds and the review queue ─────────
// ── The campaigns of the new system (/api/campaigns): the list, one campaign, what the start dialog offers, and the refusals ──
export type CampaignState = "running" | "pausing" | "paused" | "interrupted" | "done" | "partial" | "failed";
export type CampaignRunPlan = {
  name: string | null; ecosystems: string[]; preset: string | null; stages: Record<string, boolean>; knobs: Record<string, number | boolean | null>;
  build_profile: string | null; context: { profile: string; workers: number; cap: number | null };
};
export type CampaignProgress = { target?: string; stage?: string; step?: number; steps?: number; percent?: number; label?: string; state?: string;
  processed?: number; total?: number; unit?: string; phase?: string; counts?: Record<string, number>; quota?: Record<string, number> };
export type CampaignSummary = { unresolved_items?: number; skipped_sources?: number; steps: { total: number; done: number; quota: number; skipped: number; error: number; paused: number; pending: number };
  targets: Record<string, string>; errors: { target: string; stage: string; error: string | null }[]; seconds: number };
export type CampaignRow = {
  campaign_id: string; name: string | null; state: CampaignState; created_at: string | null; started_at: string | null; finished_at: string | null;
  heartbeat_ts: number | null; control: string | null; job_id: string | null; pid: number | null; resumes: number; error: string | null;
  ecosystems: string[]; progress: CampaignProgress; summary: Partial<CampaignSummary>; declared: Record<string, any>; legacy: boolean; plan: CampaignRunPlan | null;
};
export type CampaignListing = { total?: number; limit?: number; offset?: number; campaigns: CampaignRow[]; interrupted: string[]; owners: Record<string, string> };
export type CampaignStepStatus = "pending" | "running" | "paused" | "done" | "quota" | "error" | "skipped";
export type CampaignStepRow = { target: string; stage: string; status: CampaignStepStatus; started_at: string | null; finished_at: string | null;
  detail: Record<string, any>; error: string | null; attempts: number; progress: Record<string, any> };
export type CampaignDetail = { campaign: CampaignRow; steps: CampaignStepRow[]; job: Job | null;
  context_review?: { ready: boolean; reason: string | null; unresolved_sources: number; drafts_only: boolean } };
export type CampaignEvent = { id: number; at: string; kind: string; data: Record<string, any> };
export type CampaignChoice = { name: string; downloaded: boolean; ingested: boolean; dump_bytes: number | null; snapshot: string | null; mineable: boolean;
  reason: string | null; owner: string | null; stopped: "read" | "download" | null };
export type CampaignOptions = {
  targets: CampaignChoice[]; presets: { id: string; name: string; stages: string[] }[]; stages: { id: string; label: string; scope: "ecosystem" | "campaign" }[];
  knobs: Record<string, number | boolean | null>; knob_limits: Record<string, [number, number | null]>; build_profiles: { id: string; name: string; description: string }[];
  context_profiles: { id: string; name: string; description: string }[]; context_defaults: { profile: string; workers: number; cap: number | null }; context_workers: [number, number];
  default_preset: string;
};
export type CampaignResources = {
  cores: number; cpu_cap: number; ram_total_gb: number; ram_free_gb: number; ram_cap_gb: number; disk_free_gb: number; jobs_running: number; jobs_max: number;
  github_token: boolean; github_per_hour: number; used: { cpu: number; ram_gb: number; github_per_hour: number };
  campaigns: { campaign_id: string; name: string | null; state: string; declared: Record<string, any> }[];
};
export type CampaignRequest = {
  mining?: {version: 2 | 3 | 4; parser_workers?: number};
  compute?: { mode: 'cpu' | 'cuda'; version?: number };
  name?: string | null; ecosystems: string[]; preset?: string | null; stages?: Record<string, boolean>; knobs?: Record<string, number | boolean | null>;
  build_profile?: string | null; context?: { profile?: string; workers?: number; cap?: number | null };
};
export type CampaignProblem = { code: string; text: string; stage?: string; ecosystem?: string; knob?: string; source?: "plan" | "machine"; numbers?: Record<string, unknown> };
export type CampaignVerdict = {
  ok: boolean; plan: CampaignRunPlan; errors: CampaignProblem[]; warnings: CampaignProblem[]; per_ecosystem: Record<string, { stages: string[]; notes: string[] }>;
  needs: { cpu: number; ram_gb: number; github_per_hour: number; disk_gb: number; context: boolean; build: boolean };
  free: { cpu: number; ram_gb: number; github_per_hour: number; disk_gb: number; jobs: number };
};
export type AnalyticsStep = { target: string; stage: string; label: string; status: CampaignStepStatus; attempts: number; started_at: string | null; finished_at: string | null;
  seconds: number | null; error: string | null; eta_seconds: number | null; fraction: number | null };
export type FunnelNumbers = Record<"advisories" | "with_fix_pointer" | "mined" | "pairs" | "usable_pairs", number>;
export type CampaignContextStats = { model: string | null; prompt_version: string | null; candidates: number; written: number; failed: number; seconds: number; seconds_per_pair: number | null;
  coverage_before: Record<string, { total: number; enriched: number }>; coverage_after: Record<string, { total: number; enriched: number }>; steps: { target: string; status: string }[] };
export type CampaignAnalytics = {
  campaign_id: string; name: string | null; state: CampaignState; legacy: boolean; ecosystems: string[]; created_at: string | null; started_at: string | null; finished_at: string | null;
  generated_at: string; note: string | null;
  timeline: { steps: AnalyticsStep[]; total: number; done: number; errors: number; percent: number | null; elapsed_seconds: number | null; active_seconds: number };
  errors: { by_cause: { cause: string; n: number }[]; latest: { at: string; target?: string; stage?: string; cause?: string; message?: string }[];
            commits: { status: string; reason: string; n: number }[]; forced_kills: number; interruptions: number };
  funnel: { ecosystems: Record<string, { before: FunnelNumbers; now: FunnelNumbers; gained: FunnelNumbers }>; total: FunnelNumbers; flow: Lineage; has_baseline: boolean };
  throughput: { bucket_seconds: number; buckets: { at: string; commits: number; pairs: number }[]; per_hour: { commits: number; pairs: number } | null;
                samples: { at: string; rss_mb?: number | null; pending?: number | null; remaining?: number | null; waiting: boolean; stage?: string | null; target?: string | null }[];
                waits: { start: string; end: string | null }[] } | null;
  mix: { pairs: number; commits: number; language: Record<string, number>; granularity: Record<string, number>; ecosystem: Record<string, number>; cwe: Record<string, number>; severity: Record<string, number> } | null;
  context: CampaignContextStats | null;
  outcomes?: { commits: number; by_status: Record<string, number>; reasons: { status: string; reason: string; n: number }[] };
};
export type SecurityPartitionLimit = {partition: string; pool: string; source_groups: number; zero_error_design_groups: number; shortage?: number};
export type DataQualification = {schema: string; policy: string; sampling_unit: string; qualified: boolean; validated_families: number; blockers: string[];
  requirements: {validated_families: number; train_groups_per_mechanism: number; held_out_groups_per_mechanism_per_role: number; mechanisms: string[]};
  partition_mechanism_groups: Record<string, Record<string, number>>; excluded_records?: Record<string, number>};
export type SecurityPerformanceDesign = {schema?: string; full_run_suitability?: string; limitations?: SecurityPartitionLimit[]; security_targets_measured?: boolean};
export type SecurityReadiness = {schema?: string; qualification_policy?: string; data_qualification?: DataQualification; training_ready?: boolean; blockers?: string[]; scale_assessment?: SecurityPerformanceDesign; security_performance_design?: SecurityPerformanceDesign; partition_coverage?: Record<string, {source_groups?: Record<string, number>; examples?: Record<string, number>; mechanism_groups?: Record<string, number>}>};
export type TrainingAssessment = {schema: string; allowed: boolean; blockers: string[]; integrity: {state: string; blockers: string[]}; full_run: {state: string; blockers: string[]; limitations: SecurityPartitionLimit[]}; diagnostics: {state: string}; qualification_policy?: string; data_qualification?: DataQualification; security_performance_design?: SecurityPerformanceDesign; security_performance?: {state: string}};
export type BuildInfo = { tag: string; status: string; path: string; counts: Record<string, any>; tier_mix: Record<string, number>; full_training?: TrainingAssessment;
  versions: Record<string, any>; blocked: Record<string, { status: string; reason?: string; views?: number }>;
  files: Record<string, number | null> };
export type SecurityBuildProfile = { id: string; version: string; label: string; description: string; default: boolean; languages: string[]; ecosystems: string[]; quality_profile: string; min_tier: string; injection_rate: number; admission_policy?: string; counts_estimated?: boolean; counts: { source_pairs: number; units?: number; source_units?: number; human_reviewed?: number; automatically_eligible?: number | null; insufficient_context?: number | null; excluded?: number | null }; blockers: string[] };
export type EvidenceAction = {available: boolean; reason?: string};
export type BuildEvidencePage = {items: {record_id: string; pair_key: string; outcome: string; reasons: string[]; mechanism?: string; proof_status?: string}[]; total: number; limit: number; offset: number; summary: {counts: Record<string, number>; reason_counts: Record<string, number>; coverage?: unknown; admission_policy: string}};
export type ContextRepairOutcome = {record_id?: string; attempt_id?: string; status: string; resolution?: string; reasons?: string[]; error?: string; retryable?: boolean; remaining_missing_facts?: string[]; repair_ids?: string[]; artifact_id?: string; legacy_binding?: string; detail_unavailable?: boolean};
export type ContextRepairPage = {items: {record_id: string; pair_key: string; missing_facts: string[]; explanation_state: string; generation_eligible: boolean; reasons: string[]; recovery?: ContextRepairOutcome | null}[]; total: number; offset: number; limit: number; summary: {total: number; explanation_eligible: number; source_gaps: number; source_manifest_sha256: string}};
export type ContextRepairAttempt = {attempt_id: string; state: string; created: string; finished?: string | null; total: number; counts: Record<string, number>; source_manifest_sha256?: string; implementation?: string};
export type ContextRepairAttempts = {items: ContextRepairAttempt[]; total: number; offset: number; limit: number};
export type ContextRepairOutcomes = {items: ContextRepairOutcome[]; total: number; offset: number; limit: number; attempt_id: string};
export type ContextRepairEvents = {items: {cursor: number; record_id?: string | null; kind: string; created: string; body: Record<string, unknown>}[]; total: number; limit: number; next_cursor: number; has_more: boolean};
export type ContextRepairDetail = {record_id: string; source_manifest_sha256: string; original: Record<string, unknown>; input?: Record<string, unknown>; repaired: Record<string, unknown> | null; diff: Record<string, string>; repair_ids: string[]; source_binding: Record<string, unknown>; input_binding?: Record<string, unknown>; repaired_binding?: Record<string, unknown>; artifact_id?: string; outcome: ContextRepairOutcome | null; detail_unavailable?: boolean};
export type ProfileBuild = BuildInfo & { id?: string; profile?: string; profile_version?: string; admission_policy?: string; readiness?: SecurityReadiness; state?: string; job_id?: string; progress?: JobProgress; blockers?: string[]; created_at?: string; evidence_actions?: {repair: EvidenceAction; verify: EvidenceAction; rebuild: EvidenceAction}; evidence_job_id?: string; evidence_artifact_available?: boolean };
export type BuildList = { builds: ProfileBuild[]; total?: number; limit?: number; offset?: number; next_version: string; next_versions: Record<string, string> };
export type CoverageRow = { ecosystem: string; language: string; year: string; mechanism: string; groups: number; records: number;
  auto_checked: number; reviewed: number; views: number; train: number; val: number; quarantine: number };
export type BuildDetail = { build?: ProfileBuild | null; full_training?: TrainingAssessment; manifest: Record<string, any>; stats: { coverage?: CoverageRow[]; concentration?: Record<string, any>;
  exclusions?: Record<string, any>; splits?: Record<string, any>; val_roles?: Record<string, any> } };
export type SplitCheck = {
  split_versions: { version: string; parent: string | null; sha256: string; summary: Record<string, any>; val_roles: Record<string, any> | null }[] };
export type Gate = { passed: boolean; tier: string; failed: string[]; not_evaluated: string[] };
export type ReviewItem = { record_id: string; pair_key: string; ecosystem: string | null; package: string | null; repository: string;
  granularity: string | null; function_name: string | null; file_path: string | null; language: string | null;
  evidence_level: string | null; attribution_basis: string | null; mechanism: string; gate: Gate;
  effective: { review_status: string; label_state: string } };
export type ReviewQueue = { total: number; limit: number; offset: number; items: ReviewItem[] };
export type ReviewSummary = { rows: number; candidates: number; auto_checked: number; tiers: Record<string, number>;
  failed: Record<string, number>; not_evaluated: string[]; decisions: number; audit_id: string | null; rubric_version: string;
  audit_error_rate: number | "unestimated" };
export type ReviewDetail = { record_id: string; row: Record<string, any>; gate: Gate; decision: Record<string, any> | null;
  effective: { review_status: string; label_state: string }; rubric_version: string };
export type AuditSample = { audit_id: string; items: { record_id: string; pair_key: string; stratum: Record<string, string>;
  decision: { review_status: string; label_state: string } | null }[] };
export type ErrorRates = { audit_id: string | null; status: string; n_sampled: number; n_reviewed: number; errors: number;
  rate: number | null; wilson_low: number | null; wilson_high: number | null;
  failed_strata: { dimension: string; value: string; n: number; errors: number; rate: number; wilson_low: number }[];
  uncertain_strata: { dimension: string; value: string; n: number; errors: number; rate: number }[] };

// ── Fine-tune runs, baselines and the benchmark registry ──────────────────────
export type RunBlocked = { state: string; reason: string };
export type RunImport = { import_id: string; type: string; state: string; stage: number | null; loss_mode: string | null;
  invariance: boolean; adapter_sha256: string; imported_at: string };
export type RunState = { state: string; blocked: RunBlocked[];
  evidence: { bundle_verified?: boolean; imports?: RunImport[]; evaluations?: string[];
    cpu_validation?: { passed: boolean; bundle_sha256: string; checked_at: string } | null } };
export type EvaluationAvailability = {state: 'blocked' | 'not_checked' | 'ready'; reason_code: string; detail: string; remediation: string};
export type FinetuneRun = { name: string; state: string; dataset: { tag: string; manifest_sha256: string; status: string };
  mode?: 'trial' | 'full'; total_budget_minutes?: number; trial_coverage?: Record<string, unknown>;
  evaluation_availability?: EvaluationAvailability | null;
  model_revision: string; stage: number; prepared_at?: string; blocked: RunBlocked[];
  model?: string | null; purpose?: string | null; profile?: string | null; overrides?: number;
  bundle: { path: string; sha256: string; bytes: number; files: number; warnings: { code: string; detail: string }[] } };
export type FinetuneRunDetail = { name: string; full_training?: TrainingAssessment; prepared: Record<string, any>; reexport?: Record<string, unknown>; cpu_validation: Record<string, any> | null;
  trial_imports?: TrialImport[];
  evaluation_availability?: EvaluationAvailability | null;
  state: RunState; imports: Record<string, any>[]; eval_imports: Record<string, any>[];
  local_gpu: LocalGpuReport | null; local_train?: LocalGpuReport | null };
export type TrialImport = {import_id: string; mode: 'trial'; state: 'finished' | 'partial' | 'blocked' | 'failed';
  adapter_evidence?: 'verified' | 'not_produced' | 'unavailable_in_compact_recovery';
  total_budget_minutes: number; software_test: boolean; experimental: true; counts_as_full_training: false; security_evaluation: false;
  config: Record<string, any>; reports: Record<string, Record<string, any> | null>};
export type TrialCapability = {allowed: boolean; blockers: string[]; tasks?: string[]; trial_policy?: string; fresh_adapter_required?: boolean};
export type LocalGpuReport = { kind: string; model: string; passed: boolean | null; seconds?: number; peak_vram_mb?: number | null; tokens_per_second?: number | null;
  gpu: { name: string } | null; device?: "gpu" | "cpu"; checked_at?: string; folder: string; state?: string; complete?: boolean; steps_done?: number; steps?: number | null; loss?: number | null;
  resumed_from_step?: number | null; interrupted_by?: string | null; pinned_base?: boolean; fit?: { verdict: string; message: string }; outputs_zip?: string | null };
export type LocalFit = { available: boolean; reason?: string; pinned_base: string | null; default_model: string; running: boolean; model?: string; is_pinned_base?: boolean;
  gpu?: { name: string; total_mb: number; free_mb: number };
  gate: { verdict: "fits" | "tight" | "too_big" | "cannot_fit" | "unknown"; refuse: boolean; message: string; model: string; context: number; micro_batch: number; rank: number;
    least_bytes?: number; estimate_bytes?: number } | null };
export type BaselineRecord = { id: string; title: string; optional: boolean; status: string; reason: string | null;
  evidence: { imports: { run: string; import_id: string; adapter_sha256: string }[]; evaluations: string[] } };
export type BenchmarkEntry = { name: string; status: string; ambiguity: string | null; notes: string[]; source_checked: boolean;
  blocking_reason: string };

// Every call goes through lib/request.ts: shared GETs, a short-lived cache, abort when the page is left, a timeout, and the loop that
// asks again while the backend is still computing a slow view (it answers 202 "warming" instead of holding the connection open).
import { request as j, VIEW_TTL_MS } from "./request";


export type ContextPreview = { packet_id: string; missing: string[]; items: { item_id: string; status: string; reason?: string | null; outcome?: string; draft?: unknown }[] };
export type ContextJsonExport = {export_id: string; campaign_id?: string; job_id?: string; state: string; processed: number; total: number; elapsed_seconds: number; max_file_bytes: number; prompt: string;
  skipped?: {pair_id: number; reason: string}[]; failures: {pair_id: number | null; reason: string}[]; files: {index: number; packet_id: string; name: string; bytes: number; item_count: number; sha256: string}[]};
export type ContextJsonPreview = ContextPreview & {upload_id: string};
export type ReviewExchangeItem = {item_id: string; status: 'valid' | 'invalid' | 'stale' | 'conflict'; reason?: string | null; label_state?: string; completion?: 'complete' | 'needs_context'; import_state?: 'imported' | 'unchanged' | 'incomplete'};
export type ReviewExchangeTask = {id: string; job_id?: string | null; state: 'queued' | 'running' | 'complete' | 'failed' | 'interrupted';
  processed: number; total: number; elapsed_seconds: number; error?: string | null; items: ReviewExchangeItem[]; missing: string[];
  imported?: number; unchanged?: number; incomplete?: number; conflicts?: number; rejected?: number};
const reviewExchangePath = '/api/data/review/exchange';
export const reviewExchange = {
  createExport: (body: {ecosystem?: string; pair_keys?: string[]; max_file_bytes: number}) => j<ContextJsonExport>(`${reviewExchangePath}/exports`, {method: 'POST', body: JSON.stringify(body)}),
  exportStatus: (id: string) => j<ContextJsonExport>(`${reviewExchangePath}/exports/${encodeURIComponent(id)}`, {ttl: 0}),
  resumeExport: (id: string) => j<ContextJsonExport>(`${reviewExchangePath}/exports/${encodeURIComponent(id)}/resume`, {method: 'POST'}),
  cancelExport: (id: string) => j<ContextJsonExport>(`${reviewExchangePath}/exports/${encodeURIComponent(id)}/cancel`, {method: 'POST'}),
  download: (id: string, index: number) => `${reviewExchangePath}/exports/${encodeURIComponent(id)}/files/${index}`,
  createValidation: (file: File, max_file_bytes: number) => j<ReviewExchangeTask>(`${reviewExchangePath}/validations?${new URLSearchParams({max_file_bytes: String(max_file_bytes), filename: file.name})}`, {method: 'POST', headers: {'content-type': 'application/json'}, body: file}),
  validationStatus: (id: string) => j<ReviewExchangeTask>(`${reviewExchangePath}/validations/${encodeURIComponent(id)}`, {ttl: 0}),
  startValidation: (id: string) => j<ReviewExchangeTask>(`${reviewExchangePath}/validations/${encodeURIComponent(id)}/start`, {method: 'POST'}),
  createImport: (validation_id: string, selected: string[]) => j<ReviewExchangeTask>(`${reviewExchangePath}/imports`, {method: 'POST', body: JSON.stringify({validation_id, selected})}),
  importStatus: (id: string) => j<ReviewExchangeTask>(`${reviewExchangePath}/imports/${encodeURIComponent(id)}`, {ttl: 0}),
  startImport: (id: string) => j<ReviewExchangeTask>(`${reviewExchangePath}/imports/${encodeURIComponent(id)}/start`, {method: 'POST'}),
};
export type CampaignSourceFailure = {commit_key: string; url?: string; sha?: string; reason?: string; attempts: number; retryable: boolean; status: string; resolution?: {action: 'skip' | 'retry'; at?: string} | null};
export type ContextItemFilters = {language?: string; granularity?: string; status?: string; search?: string; cwe?: string; exclude_tests?: boolean; usable_only?: boolean; context_quality?: string; draft_quality?: string};
export type ContextAdmissionMetadata = {counts?: {automatically_eligible: number | null; insufficient_context: number | null; excluded: number | null; matching_candidates: number}; counts_estimated?: boolean; readiness_evaluated?: boolean};
export type ContextListItem = {pair_id: number; pair_key?: string; file_path?: string | null; language?: string | null; status?: string; reason?: string | null; context_quality?: string; context_missing?: string[]; context_suggestions?: string[]; draft_quality?: string; draft_problem?: string | null; explanation_state?: 'present_usable' | 'missing' | 'unusable'; explanation_reasons?: string[]; generation_eligible?: boolean; evidence_state?: 'not_evaluated' | 'incomplete'; evidence_reasons?: string[]};
export type ExplanationProvider = {id: string; name: string; models: string[]; free_verified: boolean; automation_eligible?: boolean; requires_key?: boolean; eligibility: string; quota: string; restrictions: string[]; official_urls: string[]; verified_at: string; reason?: string | null};
export type ExplanationProviderProfile = {provider_id: string; model: string; account_id?: string; enabled: boolean; free_account_confirmed: boolean; terms_confirmed: boolean; has_key: boolean; key_masked?: string | null; eligible: boolean; blocked_reason?: string | null};
export type ExplanationProviderConfig = {enabled: boolean; max_concurrency: number; profiles: ExplanationProviderProfile[]};
export type ExplanationProviderStatus = {enabled: boolean; running: boolean; counts: Record<'queued' | 'running' | 'completed' | 'failed' | 'waiting' | 'cancelled' | 'blocked', number>; last_error: string | null};
const explanationProviderPath = '/api/context-providers';
export const explanationProviders = {
  catalog: () => j<{providers: ExplanationProvider[]}>(`${explanationProviderPath}/catalog`, {ttl: 0}),
  config: () => j<ExplanationProviderConfig>(`${explanationProviderPath}/config`, {ttl: 0}),
  status: () => j<ExplanationProviderStatus>(`${explanationProviderPath}/status`, {ttl: 0}),
  saveProfile: (id: string, profile: {model: string; enabled: boolean; free_account_confirmed: boolean; terms_confirmed: boolean; api_key?: string; account_id?: string}) => j<ExplanationProviderConfig>(`${explanationProviderPath}/profiles/${encodeURIComponent(id)}`, {method: 'PUT', body: JSON.stringify(profile)}),
  removeKey: (id: string) => j<ExplanationProviderConfig>(`${explanationProviderPath}/profiles/${encodeURIComponent(id)}/key`, {method: 'DELETE'}),
  start: () => j<ExplanationProviderStatus>(`${explanationProviderPath}/start`, {method: 'POST'}),
  stop: () => j<ExplanationProviderStatus>(`${explanationProviderPath}/stop`, {method: 'POST'}),
  resume: () => j<ExplanationProviderStatus>(`${explanationProviderPath}/resume`, {method: 'POST'}),
  cancel: () => j<ExplanationProviderStatus>(`${explanationProviderPath}/cancel`, {method: 'POST'}),
};
const contextFilterQuery = (filters: ContextItemFilters = {}) => new URLSearchParams(Object.entries(filters).map(([key, value]) => [key, String(value)])).toString();
export const contextReview = {
  jsonCreate: (ids: number[], campaign_id?: string, max_file_bytes = 40_000_000) => j<ContextJsonExport>('/api/code/enrich/json/exports', {method: 'POST', body: JSON.stringify({ids, campaign_id, max_file_bytes})}),
  jsonStatus: (id: string) => j<ContextJsonExport>(`/api/code/enrich/json/exports/${encodeURIComponent(id)}`, {ttl: 0}),
  jsonResume: (id: string) => j<ContextJsonExport>(`/api/code/enrich/json/exports/${encodeURIComponent(id)}/resume`, {method: 'POST'}),
  jsonCancel: (id: string) => j<ContextJsonExport>(`/api/code/enrich/json/exports/${encodeURIComponent(id)}/cancel`, {method: 'POST'}),
  jsonDownload: (id: string, index: number) => `/api/code/enrich/json/exports/${encodeURIComponent(id)}/files/${index}`,
  jsonPreview: (file: File, max_file_bytes = 40_000_000) => j<ContextJsonPreview>(`/api/code/enrich/json/imports/preview?max_file_bytes=${max_file_bytes}`, {method: 'POST', headers: {'content-type': 'application/json'}, body: file}),
  jsonApply: (id: string, selected: string[]) => j<ContextJsonPreview & {imported: number; unchanged: number}>(`/api/code/enrich/json/imports/${encodeURIComponent(id)}`, {method: 'POST', body: JSON.stringify({selected})}),
  campaignItemIds: (id: string, filters?: ContextItemFilters, selectionLimit?: number) => j<{ids: number[]; total: number}>(`/api/campaigns/${encodeURIComponent(id)}/context-item-ids?${contextFilterQuery(filters)}${selectionLimit === undefined ? '' : `&selection_limit=${selectionLimit}`}`, {ttl: 0}),
  campaignItems: (id: string, offset = 0, filters?: ContextItemFilters) => j<{items: ContextListItem[]; total?: number} & ContextAdmissionMetadata>(`/api/campaigns/${encodeURIComponent(id)}/context-items?offset=${offset}&limit=100&${contextFilterQuery(filters)}`, {ttl: 0}),
  collectedItemIds: (filters?: ContextItemFilters) => j<{ids: number[]; total: number}>(`/api/code/context-item-ids?${contextFilterQuery(filters)}`, {ttl: 0}),
  collectedItems: (offset = 0, filters?: ContextItemFilters) => j<{items: ContextListItem[]; total: number; admission_policy: string; context_improvement_optional: boolean} & ContextAdmissionMetadata>(`/api/code/context-items?offset=${offset}&limit=100&${contextFilterQuery(filters)}`, {ttl: 0}),
  status: () => j<{ engine: string; model: string; configured: boolean; execution: any; models?: { id: string }[] }>("/api/code/enrich/local-status", { ttl: 0 }),
  export: (ids: number[], campaign_id?: string) => j<ContextJsonExport>("/api/code/enrich/export", { method: "POST", body: JSON.stringify({ ids, campaign_id }) }),
  preview: (response: unknown) => j<ContextPreview>("/api/code/enrich/import/preview", { method: "POST", body: JSON.stringify({ response }) }),
  apply: (response: unknown, selected: string[]) => j<{ imported: number; unchanged: number }>("/api/code/enrich/import", { method: "POST", body: JSON.stringify({ response, selected }) }),
};
export { isAbort, setRequestScope, clearRequestCache, requestEpoch } from "./request";

/** POST a body and read the answer as lines of JSON as they arrive (the chat and the agent loop both stream their events this way). Rejects with an ApiError when the request is
 *  refused before the stream starts; aborting the signal ends it quietly. */
async function streamEvents<E>(url: string, body: unknown, signal: AbortSignal, onEvent: (e: E) => void): Promise<void> {
  let r: Response;
  try { r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal }); }
  catch (cause) { if (signal.aborted) return; throw ApiError.unreachable(url, cause); }
  if (!r.ok) throw ApiError.fromBody(r.status, await r.text().catch(() => ""), url);
  if (!r.body) return;
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let at: number;
      while ((at = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, at).trim();
        buffer = buffer.slice(at + 1);
        if (line) onEvent(JSON.parse(line) as E);
      }
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer) as E);
  } catch (cause) { if (!signal.aborted) throw cause instanceof ApiError ? cause : ApiError.unreachable(url, cause); }
}

export const api = {
  health: () => j<Health>("/api/health", { ttl: 0, global: true }),
  configSummary: () => j<ConfigSummary>("/api/config/summary", { ttl: VIEW_TTL_MS }),
  getSettings: () => j<Settings>("/api/settings", { global: true }),
  compute: () => j<ComputeInfo>("/api/compute", { global: true }),
  computePythons: (refresh = false) => j<ComputePythons>(`/api/compute/pythons${refresh ? "?refresh=true" : ""}`),
  openFolder: (which: "models" | "interim") => j<{ ok: boolean; path?: string; detail?: string }>("/api/shell/open-folder", { method: "POST", body: JSON.stringify({ which }) }),
  device: () => j<DeviceInfo>("/api/device", { ttl: 5000 }),
  byYear: () => j<ByYear>("/api/dataset/by-year"),
  runCurves: (name: string) => j<RunCurves>(`/api/finetune/runs/${encodeURIComponent(name)}/curves`),
  trainConfig: () => j<TrainConfig>("/api/train/config"),
  trainSchedule: (q: { families: number; effective_batch: number; epochs?: number; steps?: number; evaluate_every_steps?: number; evaluate_at?: number[] }) =>
    j<ScheduleInfo>(`/api/train/schedule?${new URLSearchParams(Object.entries({ ...q, evaluate_at: q.evaluate_at?.join(",") }).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]))}`, { readOnly: true }),
  trainProfiles: () => j<{ profiles: Record<string, RunProfile> }>("/api/train/profiles"),
  createTrainingRun: (body: Record<string, unknown>) => j<{name: string; run_id: string; request_id: string; job_id: string}>('/api/finetune/runs', {method: 'POST', body: JSON.stringify(body)}),
  trialCapability: (tag: string) => j<TrialCapability>(`/api/finetune/trial-capability?${new URLSearchParams({tag})}`),
  trainResolve: (overrides: Record<string, unknown>) => j<TrainResolve>("/api/train/resolve", { method: "POST", body: JSON.stringify({ overrides }), readOnly: true }),
  ecosystems: () => j<EcosystemCatalog>("/api/collections/ecosystems"),
  cveDetail: (ecosystem: string, osvId: string) => j<CveDetail>(`/api/collections/cves/${encodeURIComponent(ecosystem)}/${encodeURIComponent(osvId)}`, { readOnly: true }),
  ecosystemUpdate: (name: string) => j<EcosystemUpdate>(`/api/collections/ecosystems/${encodeURIComponent(name)}/update`),
  verifyReceipts: () => j<{ receipts: ReceiptRow[] }>("/api/verify/receipts"),
  verifyReceipt: (name: string) => j<{ name: string; receipt: Receipt; problems: string[] }>(`/api/verify/receipts/${encodeURIComponent(name)}`),
  verifyCases: () => j<{ cases: PilotCase[] }>("/api/verify/cases"),
  verifyPilot: () => j<{ ran: boolean; summary: PilotSummary | null }>("/api/verify/pilot"),
  localModels: (p?: { limit?: number; offset?: number; search?: string }) => j<LocalModels>(`/api/models/local${p ? `?${new URLSearchParams({ limit: String(p.limit ?? 100), offset: String(p.offset ?? 0), search: p.search ?? "" })}` : ""}`),
  partialDownloads: () => j<{ partial: PartialDownload[] }>("/api/models/partial", { readOnly: true }),
  discardPartial: (repo_id: string) => j<{ repo_id: string; freed_bytes: number; files: number }>("/api/models/partial/discard", { method: "POST", body: JSON.stringify({ repo_id }) }),
  removalPlan: (repo_id: string) => j<RemovalPlan>(`/api/models/remove-plan?${new URLSearchParams({ repo_id })}`, { readOnly: true }),
  removeModel: (repo_id: string) => j<{ repo_id: string; freed_bytes: number }>(`/api/models/cache?${new URLSearchParams({ repo_id })}`, { method: "DELETE" }),
  installedRemovalPlan: (m: Pick<LocalModel, "source" | "id" | "path">) => j<InstalledRemovalPlan>(`/api/models/installed/remove-plan?${new URLSearchParams({ source: m.source, id: m.id, path: m.path })}`, { readOnly: true }),
  removeInstalled: (m: Pick<LocalModel, "source" | "id" | "path">, stopOllama: boolean) =>
    j<InstalledRemoval>(`/api/models/installed?${new URLSearchParams({ source: m.source, id: m.id, path: m.path, stop_ollama: String(stopOllama) })}`, { method: "DELETE" }),
  downloadPlan: (repo_id: string, quant = "") => j<DownloadPlan>(`/api/models/download-plan?${new URLSearchParams({ repo_id, quant })}`, { readOnly: true }),
  installPlan: (repo_id: string, role = "", quant = "") => j<InstallPlan>(`/api/models/install-plan?${new URLSearchParams({ repo_id, role, quant })}`, { readOnly: true }),
  finishPlan: (model_id: string, source: string, role = "", quant = "") => j<InstallPlan>(`/api/models/finish-plan?${new URLSearchParams({ model_id, source, role, quant })}`, { readOnly: true }),
  modelShelf: () => j<{ models: ShelfModel[] }>("/api/models/shelf", { readOnly: true }),
  searchModels: (q: string, task: string, limit = 20) => j<HubSearch>(`/api/models/search?${new URLSearchParams({ q, task, limit: String(limit) })}`, { readOnly: true }),
  modelFit: (b: FitRequest) => j<FitResult>("/api/models/fit", { method: "POST", body: JSON.stringify(b), readOnly: true }),
  shutdown: () => j<ShutdownStatus>("/api/shutdown", { method: "POST", global: true }),
  shutdownStatus: () => j<ShutdownStatus>("/api/shutdown/status", { ttl: 0, global: true }),
  shellCloseAck: () => j<{ ok: boolean }>("/api/shell/close-ack", { method: "POST", global: true }),
  shellNotify: (title: string, message: string) => j<{ ok: boolean; delivered: boolean; detail: string | null }>("/api/shell/notify", { method: "POST", body: JSON.stringify({ title, message }), global: true }),
  shellQuit: () => j<{ ok: boolean }>("/api/shell/quit", { method: "POST", global: true }),
  resetInventory: (refresh = false) => j<ResetInventory>(`/api/reset/inventory${refresh ? "?refresh=true" : ""}`, { ttl: 0 }),
  resetStart: (b: { token: string; categories: string[]; confirm: string; backup: boolean }) => j<ResetStatus>("/api/reset/start", { method: "POST", body: JSON.stringify(b) }),
  resetStatus: () => j<ResetStatus>("/api/reset/status", { ttl: 0 }),
  shellReboot: () => j<{ ok: boolean; pid?: number; detail?: string }>("/api/shell/reboot", { method: "POST", global: true }),
  lifecycle: () => j<{ clean: boolean; note: string; at: string | null }>("/api/lifecycle", { ttl: 0 }),
  storageInfo: (path: string) => j<StorageInfo>(`/api/settings/storage?path=${encodeURIComponent(path)}`),
  databaseStorageInfo: (path: string) => j<StorageInfo>(`/api/settings/database-storage?path=${encodeURIComponent(path)}`),
  putSettings: (b: Record<string, any>) => j<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(b) }),
  /** The saved value of a key, token or password, read only when the person presses Show (the settings answer never carries one). */
  revealSecret: (name: string) => j<{ value: string }>("/api/settings/secret", { method: "POST", body: JSON.stringify({ name }) }).then((r) => r.value),
  testConnection: (target: string, body: Record<string, any> = {}) =>
    j<TestResult>(`/api/settings/test/${target}`, { method: "POST", body: JSON.stringify(body) }),
  settingsMigrations: () => j<{ migrations: SettingsMigration[] }>("/api/settings/migrations"),
  acknowledgeSettingsMigrations: (ids?: string[]) => j<{ acknowledged: number }>("/api/settings/migrations/acknowledge", { method: "POST", body: JSON.stringify({ ids: ids ?? null }) }),
  discoverModels: (body: { base_url?: string; api_key?: string }) =>
    j<DiscoverResult>("/api/settings/models", { method: "POST", body: JSON.stringify(body) }),
  startJob: (stage: string, params: Record<string, any> = {}) =>
    j<Job>("/api/jobs", { method: "POST", body: JSON.stringify({ stage, params }) }),
  listJobs: () => j<Job[]>("/api/jobs", { ttl: 0, global: true }),
  // The latest lines of the output and where they start; `after` asks for the lines from that number on.
  getJob: (id: string, after?: number) => j<Job>(`/api/jobs/${id}${after == null ? "" : `?after=${after}`}`, { ttl: 0 }),
  cancelJob: (id: string) => j<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  forgetJob: (id: string) => j<{ ok: boolean }>(`/api/jobs/${id}`, { method: "DELETE" }),
  datasetStats: () => j<any>("/api/results/dataset-stats"),
  lineage: () => j<Lineage>("/api/dataset/lineage"),
  mix: (dataset = "") => j<Mix>(`/api/dataset/mix${dataset ? `?dataset=${encodeURIComponent(dataset)}` : ""}`),
  // ── working store (editable dataset) ──
  records: (search = "", limit = 50, offset = 0, filters: RecordFilters = {}) =>
    j<RecordsPage>(`/api/dataset/records?${new URLSearchParams({ search, limit: String(limit), offset: String(offset), language: filters.language || "",
      code_status: filters.code_status || "", dependency_status: filters.dependency_status || "", severity: filters.severity || "", ecosystem: filters.ecosystem || "",
      sort: filters.sort || "id", dir: filters.dir || "desc", view: "list" })}`),
  recordFacets: (search = "", filters: RecordFilters = {}) =>
    j<RecordFacets>(`/api/dataset/facets?${new URLSearchParams({ search, language: filters.language || "", code_status: filters.code_status || "",
      dependency_status: filters.dependency_status || "", severity: filters.severity || "", ecosystem: filters.ecosystem || "" })}`, { ttl: 10_000 }),
  refreshDepVersions: (id: number) => j<DataRecord>(`/api/dataset/records/${id}/dep-versions`, { method: "POST" }),
  recordFixCommits: (id: number) => j<RecordFixCommit[]>(`/api/dataset/records/${id}/fix-commits`),
  storeSummary: () => j<StoreSummary>("/api/dataset/summary", { ttl: VIEW_TTL_MS }),
  addRecord: (b: Partial<DataRecord>) =>
    j<DataRecord>("/api/dataset/records", { method: "POST", body: JSON.stringify(b) }),
  getRecord: (id: number, signal?: AbortSignal) => j<DataRecord>(`/api/dataset/records/${id}`, { signal }),
  updateRecord: (id: number, b: Partial<DataRecord>) =>
    j<DataRecord>(`/api/dataset/records/${id}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteRecord: (id: number) =>
    j<{ deleted: boolean }>(`/api/dataset/records/${id}`, { method: "DELETE" }),
  clearRecords: () => j<{ cleared: number }>("/api/dataset/records", { method: "DELETE" }),
  fetchOsv: (b: { cve_id?: string; package?: string }) =>
    j<{ added: number; records: any[] }>("/api/dataset/fetch", { method: "POST", body: JSON.stringify(b) }),
  codeSummary: () => j<CodeSummary>("/api/code/summary", { ttl: VIEW_TTL_MS }),
  codePair: (id: number) => j<CodePair>(`/api/code/pairs/${id}`),
  codePairs: (filters: { search?: string; language?: string; granularity?: string; available?: string; source?: string } = {}, limit = 50, offset = 0) =>
    j<{ total: number; records: CodePair[] }>(`/api/code/pairs?${new URLSearchParams({ search: filters.search || "", language: filters.language || "",
      granularity: filters.granularity || "", available: filters.available || "", source: filters.source || "", limit: String(limit), offset: String(offset) })}`),
  recordPairs: (recId: number) => j<CodePair[]>(`/api/dataset/records/${recId}/pairs`),
  enrichRecord: (recId: number) => j<EnrichResult>(`/api/dataset/records/${recId}/enrich`, { method: "POST" }),
  dbStatus: () => j<DbStatus>("/api/db/status", { ttl: 0, global: true }),
  dbStart: () => j<{ ok: boolean; detail: string }>("/api/db/start", { method: "POST", global: true }),
  dbEnsureSchema: () => j<{ ok: boolean }>("/api/db/ensure-schema", { method: "POST" }),
  dbOverview: () => j<DbOverview>("/api/db/overview", { ttl: 0 }),
  dbBackups: () => j<{ backups: DbBackup[]; folder: string }>("/api/db/backups", { ttl: 0 }),
  // the campaigns of the new system
  campaigns: (p: { limit?: number; offset?: number; search?: string; state?: string } = {}) => j<CampaignListing>(`/api/campaigns?${new URLSearchParams({ limit: String(p.limit ?? 100), offset: String(p.offset ?? 0), search: p.search ?? "", state: p.state ?? "" })}`, { ttl: 0 }),
  campaignDetail: (id: string) => j<CampaignDetail>(`/api/campaigns/${encodeURIComponent(id)}`, { ttl: 0 }),
  campaignEvents: (id: string, after = 0, limit = 200) => j<{ events: CampaignEvent[]; last: number }>(`/api/campaigns/${encodeURIComponent(id)}/events?after=${after}&limit=${limit}`, { ttl: 0 }),
  campaignOptions: () => j<CampaignOptions>("/api/campaigns/options", { ttl: 10_000 }),
  campaignResources: () => j<CampaignResources>("/api/campaigns/resources", { ttl: 0 }),
  campaignComputeResources: () => j<any>('/api/campaigns/compute/resources', {ttl: 0}),
  campaignPerformance: (id: string) => j<any>(`/api/campaigns/${encodeURIComponent(id)}/performance`, {ttl: 0}),
  stopCampaignGpuOwner: (owner: string) => j<any>('/api/campaigns/compute/stop-owner', {method: 'POST', body: JSON.stringify({owner})}),
  chatGpuView: (owner: string, active: boolean) => j<any>('/api/campaigns/compute/chat-view', {method: 'POST', body: JSON.stringify({owner, active})}),
  campaignComputePolicy: (id: string, mode: string) => j<any>(`/api/campaigns/${encodeURIComponent(id)}/compute-policy`, {method: 'POST', body: JSON.stringify({mode})}),
  campaignMiningPolicy: (id: string, version: 2 | 3 | 4 = 2) => j<any>(`/api/campaigns/${encodeURIComponent(id)}/mining-policy`, {method:'POST',body:JSON.stringify({version,mine_workers:version === 3 ? 2 : 4,parser_workers:version === 4 ? 5 : 1})}),
  campaignValidate: (b: CampaignRequest) => j<CampaignVerdict>("/api/campaigns/validate", { method: "POST", body: JSON.stringify(b), readOnly: true }),
  campaignCreate: (b: CampaignRequest) => j<{ ok: boolean; campaign: CampaignRow; job: Job; warnings: CampaignProblem[] }>("/api/campaigns", { method: "POST", body: JSON.stringify(b) }),
  campaignAnalytics: (id: string) => j<CampaignAnalytics>(`/api/campaigns/${encodeURIComponent(id)}/analytics`, { ttl: 0 }),
  campaignPauseOne: (id: string) => j<{ ok: boolean; campaign: CampaignRow }>(`/api/campaigns/${encodeURIComponent(id)}/pause`, { method: "POST" }),
  campaignResume: (id: string) => j<{ ok: boolean; campaign: CampaignRow; job: Job }>(`/api/campaigns/${encodeURIComponent(id)}/resume`, { method: "POST" }),
  campaignSources: (id: string, offset = 0) => j<{items: CampaignSourceFailure[]; total: number; unresolved: number; skipped: number}>(`/api/campaigns/${encodeURIComponent(id)}/sources?offset=${offset}&limit=100`, {ttl: 0}),
  campaignResolveSources: (id: string, action: 'retry' | 'skip', commit_keys: string[]) => j<{ok: boolean; resume_required: boolean; campaign: CampaignRow; unresolved: number; skipped: number}>(`/api/campaigns/${encodeURIComponent(id)}/sources/resolve`, {method: 'POST', body: JSON.stringify({action, commit_keys})}),
  campaignDelete: (id: string) => j<{ ok: boolean; deleted: string; kept: string }>(`/api/campaigns/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // fine-tune runs (evidence-based state), baselines B0-B6 and the benchmark registry
  finetuneRuns: (p: { limit?: number; offset?: number; search?: string; state?: string } = {}) => j<{ runs: FinetuneRun[]; root: string; total: number; limit: number; offset: number }>(`/api/finetune/runs?${new URLSearchParams({ limit: String(p.limit ?? 100), offset: String(p.offset ?? 0), search: p.search ?? "", state: p.state ?? "" })}`),
  previewRunDeletion: (selection: { names: string[] } | { select_all: true; search: string; state: string }) => j<{ preview_token: string; names: string[]; total: number; bytes: number; artifacts: unknown; dependencies: unknown; blocked: boolean }>("/api/finetune/runs/delete-preview", { method: "POST", body: JSON.stringify(selection), readOnly: true }),
  deleteRuns: (preview_token: string) => j<{ job_id: string }>("/api/finetune/runs/delete", { method: "POST", body: JSON.stringify({ preview_token }) }),
  finetuneRun: (name: string) => j<FinetuneRunDetail>(`/api/finetune/runs/${encodeURIComponent(name)}`),
  localFit: (name: string, model = "") => j<LocalFit>(`/api/finetune/runs/${encodeURIComponent(name)}/local-fit${model ? `?model=${encodeURIComponent(model)}` : ""}`),
  compareRuns: (a: string, b: string) => j<RunCompare>("/api/compare/runs", { method: "POST", body: JSON.stringify({ a, b }), readOnly: true }),
  compareCwe: (tags: string[]) => j<CweData>(`/api/compare/cwe?runs=${encodeURIComponent(tags.join(","))}`),
  compareConfig: (a: string, b: string) => j<ConfigDiff>(`/api/compare/config?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  servingStatus: () => j<ServingStatus>("/api/serving/status"),
  /** The same status, always asked fresh: for a dialog polling every second or two, where the plain, cached servingStatus would hide exactly the movement it exists to show. */
  servingStatusLive: () => j<ServingStatus>("/api/serving/status", { ttl: 0 }),
  servingModels: () => j<{ files: ServingFile[]; not_ready?: NotReadyModel[] }>("/api/serving/models"),
  ollamaModels: () => j<OllamaInventory>("/api/serving/ollama/models", { ttl: 0 }),
  installOllamaModel: (body: OllamaInstall) => j<{ id: string; stage: string }>("/api/serving/ollama/install", { method: "POST", body: JSON.stringify(body) }),
  servingLoad: (role: "llm") => j<{ started: boolean; status: ServingStatus }>("/api/serving/load", { method: "POST", body: JSON.stringify({ role }) }),
  servingUnload: (target?: { role: "llm" } | { model: string }) => j<{ unloaded: string[]; status: unknown }>("/api/serving/unload", { method: "POST", body: JSON.stringify(target ?? {}) }),
  servingAdapters: () => j<{ adapters: ServingAdapter[] }>("/api/serving/adapters"),
  convertAdapterRuns: () => j<{ adapters: RunAdapter[] }>("/api/serving/convert-adapter/runs"),
  servingDerived: () => j<DerivedCache>("/api/serving/derived", { ttl: 0 }),
  servingDerivedPin: (name: string, pinned: boolean) => j<{ name: string; pinned: boolean }>("/api/serving/derived/pin", { method: "PUT", body: JSON.stringify({ name, pinned }) }),
  servingDerivedTrim: () => j<{ removed: string[]; freed_bytes: number; message: string }>("/api/serving/derived/trim", { method: "POST" }),
  gpuMode: () => j<GpuMode>("/api/gpu/mode", { global: true }),
  gpuRelease: () => j<GpuMode & { stopped: string[]; still_holding: string[] }>("/api/gpu/release", { method: "POST" }),
  ggufFor: (base: string) => j<GgufVersions>(`/api/models/gguf-for?base=${encodeURIComponent(base)}`),
  projects: () => j<{ projects: Project[]; statement: string }>("/api/projects"),
  projectLook: (path: string) => j<FolderLook>(`/api/projects/look?path=${encodeURIComponent(path)}`),
  createProject: (body: { name: string; folders: string[]; authorized: boolean }) => j<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  updateProject: (id: string, patch: { name?: string; folders?: string[]; instructions?: string }) => j<Project>(`/api/projects/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(patch) }),
  deleteProject: (id: string) => j<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" }),
  projectAudit: (id: string) => j<{ entries: AuditEntry[] }>(`/api/projects/${encodeURIComponent(id)}/audit`),
  projectCapabilities: () => j<Capabilities>("/api/projects/capabilities"),
  scanProjectDeps: (id: string, outdated: boolean) => j<{ id: string; stage: string }>(`/api/projects/${encodeURIComponent(id)}/scan-dependencies`, { method: "POST", body: JSON.stringify({ outdated }) }),
  projectScans: (id: string) => j<{ scans: ScanListItem[] }>(`/api/projects/${encodeURIComponent(id)}/scans`),
  projectScan: <T extends DepReport | CodeReport | DeepReport | ResponseReport = DepReport>(id: string, scanId: string) => j<T>(`/api/projects/${encodeURIComponent(id)}/scans/${encodeURIComponent(scanId)}`),
  projectChatEndpoint: (id: string) => j<ChatEndpoint>(`/api/projects/${encodeURIComponent(id)}/chat/endpoint`),
  /** Ask a question and read the events as they arrive; aborting the signal stops the answer. Rejects with an ApiError when the chat is refused before it starts (no model, a hosted model not confirmed). */
  // The stored conversations of a project: list (with the number of files that could not be read), create, read, rename or change the parameters, delete, and one turn (streamed).
  projectChats: (id: string) => j<{ chats: ChatSummary[]; damaged: number }>(`/api/projects/${encodeURIComponent(id)}/chats`, { ttl: 0 }),               // never from the short-lived cache: a turn has just changed them
  projectChatCreate: (id: string, body: { title?: string; params?: Partial<ChatParams> } = {}) => j<StoredChat>(`/api/projects/${encodeURIComponent(id)}/chats`, { method: "POST", body: JSON.stringify(body) }),
  projectChatGet: (id: string, chatId: string) => j<StoredChat>(`/api/projects/${encodeURIComponent(id)}/chats/${encodeURIComponent(chatId)}`, { ttl: 0 }),
  projectChatUpdate: (id: string, chatId: string, body: { title?: string; params?: Partial<ChatParams> }) =>
    j<StoredChat>(`/api/projects/${encodeURIComponent(id)}/chats/${encodeURIComponent(chatId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  projectChatDelete: (id: string, chatId: string) => j<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/chats/${encodeURIComponent(chatId)}`, { method: "DELETE" }),
  /** Stop the answer that is being written in a conversation: the server ends it at the next word, keeps what was written (marked stopped) and ends the stream with a done event. */
  projectChatStop: (id: string, chatId: string) => j<{ stopped: boolean }>(`/api/projects/${encodeURIComponent(id)}/chats/${encodeURIComponent(chatId)}/stop`, { method: "POST" }),
  /** ``/compact``: fold the conversation's oldest turns into a summary now, regardless of its size. Never changes the stored conversation; only warms the cache a later turn reuses. */
  projectChatCompact: (id: string, chatId: string, allowRemote: boolean) =>
    j<{ compacted: boolean; messages_before: number; messages_after: number; summary_chars: number }>(
      `/api/projects/${encodeURIComponent(id)}/chats/${encodeURIComponent(chatId)}/compact`, { method: "POST", body: JSON.stringify({ allow_remote: allowRemote }) }),
  /** ``/clear``: empty a conversation's messages, keeping its id, title and settings. */
  projectChatClear: (id: string, chatId: string) => j<StoredChat>(`/api/projects/${encodeURIComponent(id)}/chats/${encodeURIComponent(chatId)}/clear`, { method: "POST" }),
  /** One turn of a stored conversation; the events arrive as they are made. Aborting the signal only leaves the page: the server keeps what it has written so far (marked stopped). */
  projectChatSend: (id: string, chatId: string, body: ChatTurnBody, signal: AbortSignal, onEvent: (e: ChatEvent) => void): Promise<void> =>
    streamEvents(`/api/projects/${encodeURIComponent(id)}/chats/${encodeURIComponent(chatId)}/messages`, body, signal, onEvent),
  // Agentic mode
  agentSessions: (id: string) => j<{ sessions: AgentSessionSummary[]; damaged: number }>(`/api/projects/${encodeURIComponent(id)}/agent/sessions`),
  agentSessionCreate: (id: string, mode: AgentMode, instructions: string, params?: Partial<AgentSessionParams>, modelRef?: string) =>
    j<AgentSession>(`/api/projects/${encodeURIComponent(id)}/agent/sessions`, { method: "POST", body: JSON.stringify({ mode, instructions, params, ...(modelRef ? { model_ref: modelRef } : {}) }) }),
  /** A new session holding this one up to the end of the turn the message belongs to (the original and the sandbox are not touched). */
  agentSessionFork: (id: string, sessionId: string, messageId: string) =>
    j<AgentSession>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/fork`, { method: "POST", body: JSON.stringify({ message_id: messageId }) }),
  /** Where a session can be saved as a file: Markdown to read, or JSON (masked once more on the way out). */
  agentSessionExportUrl: (id: string, sessionId: string, format: "md" | "json") => `/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/export?format=${format}`,
  agentSessionGet: (id: string, sessionId: string) => j<AgentSession>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}`),
  agentSessionUpdate: (id: string, sessionId: string, body: { title?: string; instructions?: string; params?: Partial<AgentSessionParams>; mode?: AgentMode; model_ref?: string }) =>
    j<AgentSession>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  agentSessionDelete: (id: string, sessionId: string) =>
    j<{ deleted: boolean }>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }),
  agentCapabilities: (id: string) => j<AgentCapabilities>(`/api/projects/${encodeURIComponent(id)}/agent/capabilities`),
  // The models an agent session can use: the settings' own, and those of the providers that were added
  agentProviders: () => j<{ providers: AgentProvider[]; default: AgentModel }>("/api/agent/providers"),
  agentProviderPut: (id: string, body: AgentProviderBody) => j<AgentProvider>(`/api/agent/providers/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  agentProviderDelete: (id: string) => j<{ deleted: boolean }>(`/api/agent/providers/${encodeURIComponent(id)}`, { method: "DELETE" }),
  agentProviderTest: (id: string) => j<{ ok: boolean; models?: string[]; count?: number; error?: string }>(`/api/agent/providers/${encodeURIComponent(id)}/test`, { method: "POST" }),
  agentModels: () => j<{ models: AgentModel[] }>("/api/agent/models"),
  agentModelProbe: (ref: string) => j<{ profile: ModelProfileSummary }>("/api/agent/models/probe", { method: "POST", body: JSON.stringify({ ref }) }),
  /** One run of a session; the events arrive as they are made. Aborting the signal only leaves the page: the server keeps what it has done so far. */
  agentSessionRun: (id: string, sessionId: string, body: AgentRunBody, signal: AbortSignal, onEvent: (e: AgentEvent) => void): Promise<void> =>
    streamEvents(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/run`, body, signal, onEvent),
  agentSessionApprove: (id: string, sessionId: string, body: AgentApproveBody) =>
    j<{ ok: boolean; summary: string; session: AgentSession }>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/approve`, { method: "POST", body: JSON.stringify(body) }),
  /** The owner's rules for what the agent may do without asking, a project's own, and a session's grants ("Allow always"). */
  agentPermissions: () => j<{ rules: AgentRule[] }>("/api/agent/permissions"),
  agentPermissionsSave: (rules: AgentRule[]) => j<{ rules: AgentRule[] }>("/api/agent/permissions", { method: "PUT", body: JSON.stringify({ rules }) }),
  agentProjectPermissions: (id: string) => j<{ rules: AgentRule[] }>(`/api/projects/${encodeURIComponent(id)}/agent/permissions`),
  agentProjectPermissionsSave: (id: string, rules: AgentRule[]) => j<{ rules: AgentRule[] }>(`/api/projects/${encodeURIComponent(id)}/agent/permissions`, { method: "PUT", body: JSON.stringify({ rules }) }),
  agentSessionGrantsClear: (id: string, sessionId: string) => j<AgentSession>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/grants`, { method: "DELETE" }),
  /** The command tools of the agent's terminal (rtk and ripgrep): what is pinned and installed, installing one (a download of a few MB, the owner's own action) and testing it again. */
  agentMcp: () => j<{ servers: McpServer[]; limits: { servers: number; timeout: number } }>("/api/agent/mcp"),
  agentMcpSave: (servers: Record<string, McpServerIn>) => j<{ servers: McpServer[]; limits: { servers: number; timeout: number } }>("/api/agent/mcp", { method: "PUT", body: JSON.stringify({ servers }) }),
  agentMcpCheck: (projectId: string) => j<McpCheck>("/api/agent/mcp/check", { method: "POST", body: JSON.stringify({ project_id: projectId }) }),
  agentToolchain: () => j<ToolchainStatus>("/api/agent/toolchain"),
  agentToolchainInstall: (tool: string) => j<ToolchainStatus>(`/api/agent/toolchain/${encodeURIComponent(tool)}/install`, { method: "POST" }),
  agentToolchainTest: (tool: string) => j<ToolchainStatus>(`/api/agent/toolchain/${encodeURIComponent(tool)}/test`, { method: "POST" }),
  /** A part of the whole output of a command that was cut (kept beside the session): from a 1-based line, up to some lines, masked. */
  agentSessionOutput: (id: string, sessionId: string, handle: string, start = 1, lines = 400) =>
    j<{ handle: string; text: string; from: number; to: number; total_lines: number; more: number }>(
      `/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/outputs/${encodeURIComponent(handle)}?start=${start}&lines=${lines}`),
  agentRuns: (id: string, sessionId: string) => j<{ runs: EngineRun[] }>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/runs`),
  agentSessionCancel: (id: string, sessionId: string) =>
    j<{ stopped: boolean }>(`/api/projects/${encodeURIComponent(id)}/agent/sessions/${encodeURIComponent(sessionId)}/cancel`, { method: "POST" }),
  /** Read-only: what the project's agent sandbox has changed, against the real project right now. Nothing is staged or written. */
  agentDiff: (id: string) => j<{ files: AgentDiffFile[] }>(`/api/projects/${encodeURIComponent(id)}/agent/diff`),
  agentApplies: (id: string) => j<{ applies: AgentApplyListItem[] }>(`/api/projects/${encodeURIComponent(id)}/agent/applies`),
  /** Stage a candidate from the sandbox's diff right now: a copy of each changed/added file's content is kept, so what is applied later is exactly what was reviewed. */
  agentApplyStage: (id: string) => j<AgentApply>(`/api/projects/${encodeURIComponent(id)}/agent/applies`, { method: "POST" }),
  agentApplyGet: (id: string, aid: string) => j<AgentApply>(`/api/projects/${encodeURIComponent(id)}/agent/applies/${encodeURIComponent(aid)}`),
  agentApplyDiscard: (id: string, aid: string) => j<{ discarded: boolean }>(`/api/projects/${encodeURIComponent(id)}/agent/applies/${encodeURIComponent(aid)}`, { method: "DELETE" }),
  agentApplyApply: (id: string, aid: string, files: string[], remove: string[] = []) =>
    j<AgentApply>(`/api/projects/${encodeURIComponent(id)}/agent/applies/${encodeURIComponent(aid)}/apply`, { method: "POST", body: JSON.stringify({ files, delete: remove }) }),
  /** "Approve and apply all": stage what the sandbox copy has changed right now and write all of it into the real folders in one step, all or nothing. A removal is applied only when named in `remove`.
   *  `reviewed` is the files the person looked at: when the sandbox has changed since, nothing is written. */
  agentApproveAll: (id: string, remove: string[] = [], reviewed?: { file: string; sha256_after: string | null }[], skip: string[] = []) =>
    j<AgentApply>(`/api/projects/${encodeURIComponent(id)}/agent/approve-all`, { method: "POST", body: JSON.stringify({ delete: remove, ...(reviewed ? { reviewed } : {}), ...(skip.length ? { skip } : {}) }) }),
  /** Puts the sandbox copy back to the project as it was when it was copied (the state before is kept as a checkpoint). Refused while a session of the project runs. */
  agentDiscardChanges: (id: string) => j<{ roots: Record<string, string>; summary: string }>(`/api/projects/${encodeURIComponent(id)}/agent/discard-changes`, { method: "POST" }),
  /** Brings into the sandbox copy what you changed in the real project since the copy was made, and makes that the new baseline. */
  agentRefresh: (id: string) => j<AgentRefresh>(`/api/projects/${encodeURIComponent(id)}/agent/refresh`, { method: "POST" }),
  agentApplyRevert: (id: string, aid: string) => j<AgentApply>(`/api/projects/${encodeURIComponent(id)}/agent/applies/${encodeURIComponent(aid)}/revert`, { method: "POST" }),
  /** Commits the current state of every root of the project's agent sandbox as a checkpoint; symmetric with the model's own checkpoint tool. */
  agentCheckpoint: (id: string, message: string) =>
    j<{ message: string; commits: Record<string, string> }>(`/api/projects/${encodeURIComponent(id)}/agent/checkpoint`, { method: "POST", body: JSON.stringify({ message }) }),
  agentCheckpoints: (id: string) => j<{ checkpoints: AgentCheckpoint[] }>(`/api/projects/${encodeURIComponent(id)}/agent/checkpoints`),
  /** Resets one root of the sandbox to an earlier checkpoint, discarding anything written since. Human-only: there is no model tool for this. */
  agentRollback: (id: string, root: string, hash: string) =>
    j<{ root: string; hash: string }>(`/api/projects/${encodeURIComponent(id)}/agent/rollback`, { method: "POST", body: JSON.stringify({ root, hash }) }),
  /** Whether this project has an agent sandbox at all, and its container id only while it is actually running (DeviceStrip's own stats chip). Never an error for "none yet". */
  agentSandboxStatus: (id: string) => j<{ running: boolean; container: string | null }>(`/api/projects/${encodeURIComponent(id)}/agent/sandbox`),
  // Skills: a named block of system-prompt text, shared across every project, applied with /skill <name>.
  skills: () => j<{ skills: SkillSummary[]; bundled?: { name: string; description: string }[] }>("/api/skills"),
  skillCreate: (name: string, description: string, prompt: string) => j<Skill>("/api/skills", { method: "POST", body: JSON.stringify({ name, description, prompt }) }),
  skillGet: (name: string) => j<Skill>(`/api/skills/${encodeURIComponent(name)}`),
  skillApprove: (name: string) => j<Skill>(`/api/skills/${encodeURIComponent(name)}/approve`, { method: "POST" }),
  skillDelete: (name: string) => j<{ ok: boolean }>(`/api/skills/${encodeURIComponent(name)}`, { method: "DELETE" }),
  planProjectUpdate: (id: string, mode: "security" | "all") => j<{ id: string; stage: string }>(`/api/projects/${encodeURIComponent(id)}/updates`, { method: "POST", body: JSON.stringify({ mode }) }),
  projectUpdates: (id: string) => j<{ updates: UpdateListItem[] }>(`/api/projects/${encodeURIComponent(id)}/updates`),
  projectUpdate: (id: string, uid: string) => j<ProjectUpdate>(`/api/projects/${encodeURIComponent(id)}/updates/${encodeURIComponent(uid)}`),
  trialProjectUpdate: (id: string, uid: string) => j<{ id: string; stage: string }>(`/api/projects/${encodeURIComponent(id)}/updates/${encodeURIComponent(uid)}/trial`, { method: "POST" }),
  projectUpdateTrial: (id: string, uid: string) => j<UpdateTrial>(`/api/projects/${encodeURIComponent(id)}/updates/${encodeURIComponent(uid)}/trial`, { ttl: 0 }),
  applyProjectUpdate: (id: string, uid: string, files: string[]) => j<ProjectUpdate>(`/api/projects/${encodeURIComponent(id)}/updates/${encodeURIComponent(uid)}/apply`, { method: "POST", body: JSON.stringify({ files }) }),
  revertProjectUpdate: (id: string, uid: string) => j<ProjectUpdate>(`/api/projects/${encodeURIComponent(id)}/updates/${encodeURIComponent(uid)}/revert`, { method: "POST" }),
  discardProjectUpdate: (id: string, uid: string) => j<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/updates/${encodeURIComponent(uid)}`, { method: "DELETE" }),
  scanProjectCode: (id: string) => j<{ id: string; stage: string }>(`/api/projects/${encodeURIComponent(id)}/scan-code`, { method: "POST" }),
  respondProject: (id: string, b: { scope: string; mode: string; depth: string; assist: boolean; allow_remote: boolean; test?: boolean; start_command?: string; port?: number | null }) =>
    j<{ id: string; stage: string }>(`/api/projects/${encodeURIComponent(id)}/respond`, { method: "POST", body: JSON.stringify(b) }),
  projectCandidates: (id: string) => j<{ candidates: CandidateListItem[] }>(`/api/projects/${encodeURIComponent(id)}/candidates`, { ttl: 0 }),
  projectCandidate: (id: string, cid: string) => j<Candidate>(`/api/projects/${encodeURIComponent(id)}/candidates/${encodeURIComponent(cid)}`),
  scanProjectCodebase: (id: string, b: { start_command?: string; port?: number | null; model_review?: boolean; model_max_files?: number }) =>
    j<{ id: string; stage: string }>(`/api/projects/${encodeURIComponent(id)}/scan-codebase`, { method: "POST", body: JSON.stringify({ start_command: b.start_command ?? "", port: b.port ?? null,
      ...(b.model_review ? {model_review: true, model_max_files: b.model_max_files || 32} : {}) }) }),
  pickFolder: (title: string) => j<{ ok: boolean; id?: string; detail?: string }>("/api/shell/pick-folder", { method: "POST", body: JSON.stringify({ title }), global: true }),
  pickFolderResult: (id: string) => j<{ state: "waiting" | "chosen" | "cancelled"; path: string | null }>(`/api/shell/pick-folder/${encodeURIComponent(id)}`, { global: true }),
  finetuneBaselines: () => j<{ baselines: BaselineRecord[] }>("/api/finetune/baselines"),
  finetuneBenchmarks: () => j<{ rules: string[]; benchmarks: BenchmarkEntry[] }>("/api/finetune/benchmarks"),
  // reviewed-dataset builds
  builds: (p: { limit?: number; offset?: number; search?: string; state?: string } = {}) => j<BuildList>(`/api/data/builds?${new URLSearchParams({ limit: String(p.limit ?? 100), offset: String(p.offset ?? 0), search: p.search ?? "", state: p.state ?? "" })}`),
  buildProfiles: () => j<{ profiles: SecurityBuildProfile[]; default_profile: string }>("/api/data/build-profiles"),
  createBuild: (profile: string) => j<{ build: ProfileBuild }>("/api/data/builds", { method: "POST", body: JSON.stringify({ profile }) }),
  retryBuild: (tag: string) => j<{ build: ProfileBuild }>(`/api/data/builds/${encodeURIComponent(tag)}/retry`, { method: "POST", body: "{}" }),
  repairBuildEvidence: (tag: string, action: 'repair' | 'verify_failed') => j<{job_id: string}>(`/api/data/builds/${encodeURIComponent(tag)}/repair-evidence`, {method: 'POST', body: JSON.stringify({action})}),
  qualificationWorkflows: (tag: string, signal?: AbortSignal) => j<QualificationWorkflowPage>(`/api/data/builds/${encodeURIComponent(tag)}/qualification-workflows?limit=100`, {ttl: 0, signal}),
  startQualificationWorkflow: (tag: string, request_id: string) => j<{workflow: QualificationWorkflow; job_id: string | null}>(`/api/data/builds/${encodeURIComponent(tag)}/qualification-workflows`, {method: 'POST', body: JSON.stringify({request_id})}),
  qualificationWorkflow: (id: string, signal?: AbortSignal) => j<{workflow: QualificationWorkflow; phases: QualificationPhasePage}>(`/api/data/qualification-workflows/${encodeURIComponent(id)}`, {ttl: 0, signal}),
  cancelQualificationWorkflow: (id: string) => j<{workflow: QualificationWorkflow}>(`/api/data/qualification-workflows/${encodeURIComponent(id)}/cancel`, {method: 'POST', body: '{}'}),
  automaticRecoveryAttempts: (id: string, offset = 0, signal?: AbortSignal) => j<AutomaticRecoveryAttemptPage>(`/api/data/qualification-workflows/${encodeURIComponent(id)}/recovery-attempts?limit=100&offset=${offset}`, {ttl: 0, signal}),
  automaticRecoveryItems: (id: string, attempt: string, offset = 0, outcome = 'all', retryable = false, signal?: AbortSignal) => j<AutomaticRecoveryPage>(`/api/data/qualification-workflows/${encodeURIComponent(id)}/phases/${encodeURIComponent(attempt)}/items?limit=100&offset=${offset}${outcome === 'all' ? '' : `&outcome=${encodeURIComponent(outcome)}`}${retryable ? '&retryable=true' : ''}`, {ttl: 0, signal}),
  automaticRecoveryDetail: (id: string, attempt: string, item: string, observation?: string, signal?: AbortSignal) => j<AutomaticRecoveryDetail>(`/api/data/qualification-workflows/${encodeURIComponent(id)}/phases/${encodeURIComponent(attempt)}/items/${encodeURIComponent(item)}${observation ? `?observation_id=${encodeURIComponent(observation)}` : ''}`, {ttl: 0, signal}),
  retryAutomaticRecoveryItems: (id: string, attempt: string, item_ids: string[]) => j<{job_id: string; attempt_id: string; selected: number}>(`/api/data/qualification-workflows/${encodeURIComponent(id)}/phases/${encodeURIComponent(attempt)}/retry`, {method: 'POST', body: JSON.stringify({item_ids})}),
  contextRepairItems: (tag: string, offset = 0, state = 'all') => j<ContextRepairPage>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair?limit=100&offset=${offset}&state=${encodeURIComponent(state)}`, {ttl: 0}),
  contextRepairAttempts: (tag: string, offset = 0) => j<ContextRepairAttempts>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair/attempts?limit=100&offset=${offset}`, {ttl: 0}),
  contextRepairOutcomes: (tag: string, attempt: string, offset = 0) => j<ContextRepairOutcomes>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair/attempts/${encodeURIComponent(attempt)}/outcomes?limit=100&offset=${offset}`, {ttl: 0}),
  contextRepairEvents: (tag: string, attempt: string, after = 0) => j<ContextRepairEvents>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair/attempts/${encodeURIComponent(attempt)}/events?limit=100&after=${after}`, {ttl: 0}),
  contextRepairDetail: (tag: string, record: string, attempt?: string) => j<ContextRepairDetail>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair/items/${encodeURIComponent(record)}${attempt ? `?attempt_id=${encodeURIComponent(attempt)}` : ''}`, {ttl: 0}),
  cancelContextRepairAttempt: (tag: string, attempt: string) => j<{attempt_id: string; state: string}>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair/attempts/${encodeURIComponent(attempt)}/cancel`, {method: 'POST'}),
  contextRepairIds: (tag: string, state = 'all') => j<{ids: string[]; total: number}>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair/ids?state=${encodeURIComponent(state)}`, {ttl: 0}),
  repairContext: (tag: string, ids: string[], method: 'source' | 'local' | 'json' | 'free_api') => j<{job_id?: string; attempt_id?: string; ids?: number[]}>(`/api/data/builds/${encodeURIComponent(tag)}/context-repair`, {method: 'POST', body: JSON.stringify({ids, method})}),
  queueContextGaps: async (ids: number[]) => {
    const total = {queued: 0, unchanged: 0, blocked: 0};
    for (let offset = 0; offset < ids.length; offset += 128) {
      const result = await j<typeof total>('/api/context-providers/queue-selected', {method: 'POST', body: JSON.stringify({ids: ids.slice(offset, offset + 128)})});
      total.queued += result.queued; total.unchanged += result.unchanged; total.blocked += result.blocked;
    }
    return total;
  },
  buildEvidence: (tag: string, offset = 0) => j<BuildEvidencePage>(`/api/data/builds/${encodeURIComponent(tag)}/evidence?offset=${offset}&limit=100`, {ttl: 0}),
  externalUpload: (file: File) => j<ExternalUpload>(`/api/data/external/upload?filename=${encodeURIComponent(file.name)}`, { method: "PUT", body: file, headers: { "content-type": "application/octet-stream" } }),
  externalCheck: (uploadId: string) => j<ExternalCheck>("/api/data/external/check", { method: "POST", body: JSON.stringify({ upload_id: uploadId }), readOnly: true }),
  externalImport: (uploadId: string, name: string) => j<{ id: string; stage: string; tag: string }>("/api/data/external", { method: "POST", body: JSON.stringify({ upload_id: uploadId, name }) }),
  externalDiscard: (uploadId: string) => j<{ discarded: boolean }>(`/api/data/external/upload/${encodeURIComponent(uploadId)}`, { method: "DELETE" }),
  computeChoices: (stage: string, run?: string, model?: string) => j<ComputeChoices>(`/api/compute/choices?stage=${encodeURIComponent(stage)}${run ? `&run=${encodeURIComponent(run)}` : ""}${model ? `&model=${encodeURIComponent(model)}` : ""}`, { readOnly: true }),
  cveSearch: (p: { q?: string; ecosystem?: string; state?: string; limit?: number; offset?: number }) => j<CveSearch>(`/api/collections/cves?${new URLSearchParams({ q: p.q ?? "", ecosystem: p.ecosystem ?? "", state: p.state ?? "", limit: String(p.limit ?? 50), offset: String(p.offset ?? 0) })}`, { readOnly: true }),
  finetuneCheckImport: (run: string, kind: "outputs" | "eval" | "trial", path: string, evaluation_dataset_dir?: string) => j<ImportCheck>(`/api/finetune/runs/${encodeURIComponent(run)}/check-import`, { method: "POST", body: JSON.stringify({ kind, path, ...(evaluation_dataset_dir ? {evaluation_dataset_dir} : {}) }), readOnly: true }),
  hubDatasetSearch: (q: string) => j<{ query: string; datasets: HubDataset[] }>(`/api/data/hub/search?q=${encodeURIComponent(q)}`),
  hubDatasetFiles: (repo: string) => j<HubDatasetFiles>(`/api/data/hub/files?repo=${encodeURIComponent(repo)}`),
  hubDatasetFetch: (repo: string, revision: string, path: string) => j<{ id: string; stage: string; upload_id: string }>("/api/data/hub/fetch", { method: "POST", body: JSON.stringify({ repo, revision, path }) }),
  externalImports: () => j<{ imports: ExternalImport[] }>("/api/data/external"),
  externalRemove: (tag: string) => j<{ removed: string; original_file: string | null }>(`/api/data/external/${encodeURIComponent(tag)}`, { method: "DELETE" }),
  buildDetail: (tag: string) => j<BuildDetail>(`/api/data/builds/${encodeURIComponent(tag)}`),
  splitCheck: () => j<SplitCheck>("/api/data/split-check"),
  // review queue
  reviewSummary: () => j<ReviewSummary>("/api/data/review/summary"),
  reviewQueue: (q: { limit?: number; offset?: number; ecosystem?: string; review_state?: 'waiting' | 'ai_adjudicated' | 'all' }) =>
    j<ReviewQueue>(`/api/data/review/queue?${new URLSearchParams(Object.entries(q).filter(([, v]) => v !== undefined && v !== "")
      .map(([k, v]) => [k, String(v)]))}`),
  reviewItem: (pairKey: string) => j<ReviewDetail>(`/api/data/review/item?${new URLSearchParams({ pair_key: pairKey })}`),
  reviewDecision: (b: { pair_key: string; reviewer: string; rubric_version: string; review_status: string; label_state: string;
    fields?: Record<string, any>; notes?: string }) =>
    j<{ ok: boolean; decision_id: number }>("/api/data/review/decision", { method: "POST", body: JSON.stringify(b) }),
  reviewAudit: (b: { audit_id?: string; n: number; ecosystem?: string }) =>
    j<AuditSample>("/api/data/review/audit", { method: "POST", body: JSON.stringify(b) }),
  reviewErrorRates: (auditId = "") => j<ErrorRates>(`/api/data/review/error-rates?${new URLSearchParams({ audit_id: auditId })}`),
  dataStamp: () => j<{ stamp: string; running: string[] }>("/api/data/stamp", { ttl: 0, global: true }),
  enrichCandidates: (filters: Partial<EnrichFilters>, limit = 50, offset = 0, signal?: AbortSignal) =>
    j<EnrichCandidates>("/api/code/enrich/candidates", { method: "POST", body: JSON.stringify({ filters, limit, offset }), signal, readOnly: true }),
  enrichPair: (id: number) => j<EnrichResult>(`/api/code/pairs/${id}/enrich`, { method: "POST" }),
  miningCheckpoint: () => j<MiningCheckpoint>("/api/code/mining/checkpoint"),
  miningResume: () => j<{ ok: boolean; job: string }>("/api/code/mining/resume", { method: "POST" }),
  miningDiscard: () => j<{ ok: boolean }>("/api/code/mining/discard", { method: "POST" }),
  miningStopOrphan: () => j<{ ok: boolean; pid: number }>("/api/code/mining/stop-orphan", { method: "POST" }),
  resetMined: (b: { scope: "mining" | "all"; keep_manual: boolean; delete_repos: boolean }) =>
    j<Record<string, number | string>>("/api/code/reset", { method: "POST", body: JSON.stringify(b) }),
  datasetOptions: () => j<DatasetOptions>("/api/code/dataset/options", { ttl: VIEW_TTL_MS }),
  datasetPreview: (f: Partial<DatasetFilters>, signal?: AbortSignal) =>
    j<DatasetPreview>("/api/code/dataset/preview", { method: "POST", body: JSON.stringify(f), signal, readOnly: true }),
  createPair: (b: Partial<CodePair> & { osv_id?: string | null }) =>
    j<CodePair>("/api/code/pairs", { method: "POST", body: JSON.stringify(b) }),
  updatePair: (id: number, b: Partial<CodePair>) =>
    j<CodePair>(`/api/code/pairs/${id}`, { method: "PUT", body: JSON.stringify(b) }),
  deletePair: (id: number) => j<{ deleted: boolean }>(`/api/code/pairs/${id}`, { method: "DELETE" }),
  importRecords: (format: string, content: string) =>
    j<{ imported: number; total: number }>("/api/dataset/import", { method: "POST", body: JSON.stringify({ format, content }) }),
  experiments: () => j<(Metrics | PatchMetrics | EvalShared)[]>("/api/results/experiments"),
  report: (tag: string) => j<any>(`/api/results/${tag}/report`),
};

/** The job's stream; `after` is the number of the next line this client needs (a reconnect asks only for what it missed). */
export function wsUrl(jobId: string, after?: number): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/jobs/${jobId}${after ? `?after=${after}` : ""}`;
}

// ── Sandbox (Docker) ──────────────────────────────────────────────────────────
export type Container = { id: string; name: string; status: string; image: string; created: string; network: string; execution?: {active: boolean; owners: number; cleanup_error?: string | null} };
export const sandbox = {
  cleanup: () => j<any>('/api/sandbox/cleanup'),
  retryCleanup: () => j<any>('/api/sandbox/cleanup/retry', { method: 'POST' }),
  status: () => j<any>("/api/sandbox/status"),
  list: () => j<Container[]>("/api/sandbox/containers"),
  containersPage: (p: { limit?: number; offset?: number; search?: string }) => j<{ containers: Container[]; total: number; limit: number; offset: number }>(`/api/sandbox/containers?${new URLSearchParams({ limit: String(p.limit ?? 100), offset: String(p.offset ?? 0), search: p.search ?? "" })}`),
  images: () => j<any[]>("/api/sandbox/images"),
  create: (b: any) => j<any>("/api/sandbox/containers", { method: "POST", body: JSON.stringify(b) }),
  action: (cid: string, act: string) => j<any>(`/api/sandbox/containers/${cid}/action`, { method: "POST", body: JSON.stringify({ act }) }),
  fileAction: (cid: string, operation: string, path: string, destination?: string) => j<{ confirmed: boolean }>(`/api/sandbox/containers/${cid}/files/action`, { method: "POST", body: JSON.stringify({ operation, path, destination }) }),
  deletionPreview: (cid: string) => j<{ container: string; image: { status: string; reason?: string } }>(`/api/sandbox/containers/${cid}/delete-preview`, { ttl: 0 }),
  exec: (cid: string, command: string) => j<any>(`/api/sandbox/containers/${cid}/exec`, { method: "POST", body: JSON.stringify({ command }) }),
  logs: (cid: string, tail = 200) => j<any>(`/api/sandbox/containers/${cid}/logs?tail=${tail}`),
  stats: (cid: string) => j<any>(`/api/sandbox/containers/${cid}/stats`),
  commit: (cid: string, repository: string, tag = "latest") => j<any>(`/api/sandbox/containers/${cid}/commit`, { method: "POST", body: JSON.stringify({ repository, tag }) }),
  pull: (image: string) => j<any>("/api/sandbox/images/pull", { method: "POST", body: JSON.stringify({ image }) }),
  scan: (cid: string, host_path = ".") => j<any>(`/api/sandbox/containers/${cid}/scan`, { method: "POST", body: JSON.stringify({ host_path }) }),

  // limits and the network
  policy: (cid: string) => j<SandboxPolicy>(`/api/sandbox/containers/${cid}/policy`, { ttl: 0 }),
  setLimits: (cid: string, b: { cpus?: number; memory_mb?: number; pids?: number }) => j<SandboxPolicyNow & { changed: Record<string, number> }>(`/api/sandbox/containers/${cid}/limits`, { method: "PUT", body: JSON.stringify(b) }),
  recreate: (cid: string, b: { network?: boolean; read_only?: boolean }) => j<SandboxPolicyNow & { id: string; name: string; warnings: string[] }>(`/api/sandbox/containers/${cid}/recreate`, { method: "POST", body: JSON.stringify(b) }),

  // files
  files: (cid: string, path: string) => j<SandboxListing>(`/api/sandbox/containers/${cid}/files?${new URLSearchParams({ path })}`, { ttl: 0 }),
  downloadUrl: (cid: string, path: string) => `/api/sandbox/containers/${cid}/files/download?${new URLSearchParams({ path })}`,
  upload: (cid: string, folder: string, file: File, overwrite = false) =>
    j<{ folder: string; name: string; bytes: number }>(`/api/sandbox/containers/${cid}/files?${new URLSearchParams({ folder, name: file.name, overwrite: String(overwrite) })}`,
      { method: "PUT", body: file, headers: { "content-type": "application/octet-stream" } }),
  changes: (cid: string) => j<SandboxChanges>(`/api/sandbox/containers/${cid}/changes`, { ttl: 0 }),

  // images
  imageList: (p?: { limit?: number; offset?: number; search?: string }) => j<SandboxImages>(`/api/sandbox/image-list${p ? `?${new URLSearchParams({ limit: String(p.limit ?? 100), offset: String(p.offset ?? 0), search: p.search ?? "" })}` : ""}`, { ttl: 0 }),
  pin: (image: string) => j<SandboxPin>("/api/sandbox/images/pin", { method: "POST", body: JSON.stringify({ image }) }),
  unpin: () => j<{ pinned: null }>("/api/sandbox/images/pin", { method: "DELETE" }),
  build: (b: { dockerfile: string; tag: string; network: boolean }) => j<{ id: string; stage: string; tag: string }>("/api/sandbox/images/build", { method: "POST", body: JSON.stringify(b) }),
  prunePlan: () => j<SandboxPrunePlan>("/api/sandbox/images/prune-plan", { ttl: 0 }),
  prune: (images: string[]) => j<{ removed: string[]; skipped: { id: string; why: string }[]; freed_mb: number }>("/api/sandbox/images/prune", { method: "POST", body: JSON.stringify({ images }) }),

  // what happened
  history: (cid: string) => j<SandboxHistory>(`/api/sandbox/containers/${cid}/history`, { ttl: 0 }),
  historyKnown: (p?: { limit?: number; offset?: number; search?: string }) => j<{ containers: SandboxRecord[]; total?: number; limit?: number; offset?: number }>(`/api/sandbox/history${p ? `?${new URLSearchParams({ limit: String(p.limit ?? 100), offset: String(p.offset ?? 0), search: p.search ?? "" })}` : ""}`, { ttl: 0 }),
  historyOf: (name: string) => j<SandboxHistory>(`/api/sandbox/history/${encodeURIComponent(name)}`, { ttl: 0 }),
  forget: (name: string) => j<{ name: string; forgotten: boolean }>(`/api/sandbox/history/${encodeURIComponent(name)}`, { method: "DELETE" }),
  saveSession: (name: string, label: string) => j<{ id: string; label: string; events: number }>(`/api/sandbox/history/${encodeURIComponent(name)}/sessions`, { method: "POST", body: JSON.stringify({ label }) }),
  session: (name: string, id: string) => j<SandboxSessionDoc>(`/api/sandbox/history/${encodeURIComponent(name)}/sessions/${encodeURIComponent(id)}`, { ttl: 0 }),
  deleteSession: (name: string, id: string) => j<{ id: string; deleted: boolean }>(`/api/sandbox/history/${encodeURIComponent(name)}/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

export type SandboxPolicyNow = { cpus: number | null; memory_mb: number | null; swap_mb: number | null; pids: number | null; network: string; read_only: boolean; editable: boolean; reason: string | null };
export type SandboxBounds = { max_cpus: number; max_memory_mb: number | null; min_cpus: number; min_memory_mb: number; min_pids: number; max_pids: number };
export type SandboxPolicy = SandboxPolicyNow & { bounds: SandboxBounds; warnings: string[] };
export type SandboxEntry = { name: string; kind: "folder" | "file" | "link" | "other"; size: number | null; mode: string; modified: number | null; link: string | null };
export type SandboxListing = { path: string; parent: string | null; entries: SandboxEntry[]; truncated: boolean };
export type SandboxChanges = { changes: { path: string; kind: "added" | "changed" | "deleted" }[]; truncated: boolean; counts: Record<"added" | "changed" | "deleted", number>; total: number };
export type SandboxImage = { id: string; full_id: string; tags: string[]; digest: string; size_mb: number; created: string; containers: number; ours: boolean; dangling: boolean; pinned: boolean; toolkit?: boolean };
export type SandboxPin = { tag: string; id: string; digest: string; pinned_at: string };
export type SandboxImages = { total?: number; limit?: number; offset?: number; docker_available: boolean; images: SandboxImage[]; pinned: SandboxPin | null };
export type SandboxPrunePlan = { images: { id: string; full_id: string; tags: string[]; size_mb: number; ours: boolean; dangling: boolean; created: string }[]; size_mb: number };
export type SandboxEvent = { at: string; kind: string; command?: string; exit_code?: number | null; seconds?: number; timed_out?: boolean; output_tail?: string; [k: string]: unknown };
export type SandboxSample = { at: string; cpu_pct: number | null; mem_mb: number | null; mem_limit_mb: number | null };
export type SandboxSession = { id: string; label: string; saved_at: string; events: number; commands: number };
export type SandboxHistory = { name: string; events: SandboxEvent[]; samples: SandboxSample[]; sessions: SandboxSession[] };
export type SandboxSessionDoc = { id: string; label: string; container: string; saved_at: string; events: SandboxEvent[] };
export type SandboxRecord = { name: string; events: number; commands: number; last: string | null; sessions: number };
