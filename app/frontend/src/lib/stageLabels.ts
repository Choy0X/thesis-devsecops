import type { Job } from "./api";

// What a backend stage is called in the interface. A stage that is not listed is shown with its name made readable.
const LABELS: Record<string, string> = {
  ingest: "OSV ingest", osv_download: "Download an OSV dump", osv_install: "Install an OSV dump", verify_case: "Verify one case", hf_download: "Download a model", install_model: "Install a model", campaign: "Collection campaign", campaign_run: "Campaign", collection_report: "Collection report", extract_fixes: "Find fix commits", mine_code: "Mine code pairs",
  dep_versions: "Dependency versions", verify_fixes: "Check fix commits", resplit_pairs: "Split file pairs", recover_fixes: "Recover fix links",
  build_code_dataset: "Build the code dataset", build_reviewed_dataset: "Build the reviewed dataset", dataset: "Build the severity dataset",
  enrich_pairs: "Draft descriptions", rag_index: "Build the retrieval index", rag_recall: "Measure retrieval recall",
  rag_index_project: "Refresh local project search",
  export_training_bundle: "Export the training bundle", finetune_validate: "Validate a fine-tuning run", import_finetune_outputs: "Import fine-tuning outputs",
  import_finetune_eval: "Import an evaluation", gpu_smoke: "GPU check", gpu_eval: "GPU evaluation", train_local: "Train on this computer", classify: "Classify", detect: "Detect", eval: "Evaluate",
  eval_detection: "Evaluate detection", eval_patch: "Evaluate patches", patch_eval: "Evaluate patches", compare: "Compare runs", scan_deps: "Scan dependencies",
  scan_codebase: "Scan a codebase", patch: "Propose a patch", respond_project: "Respond to findings", trial_update: "Try an update in the sandbox", phase_audit: "Phase audit", db_backup: "Back up the database",
  sandbox_build_image: "Build a sandbox image",
  install_ollama_model: "Install in Ollama",
  propose_project_patch: "Draft project patch",
};

export const stageLabel = (stage: string): string => LABELS[stage] ?? (stage.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));

/** The label of a job, by name where one is known: every campaign's job used to say only "Campaign", so a leftover entry (an old run, or one whose campaign was since deleted) looked
 *  exactly like a brand new one in the Jobs tray. A campaign started or resumed after this was added carries its own name in its job's params; an older job, or an unnamed campaign,
 *  still falls back to the plain stage label. */
export const jobLabel = (job: Job): string => {
  if (job.stage === "campaign_run") {
    const name = job.params?.campaign_name;
    if (typeof name === "string" && name.trim()) return `Campaign: ${name}`;
  }
  return stageLabel(job.stage);
};
