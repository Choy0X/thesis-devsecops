import { Check, FileSearch, PackageSearch, ScanLine, Square } from "lucide-react";
import type { ChatDetail } from "./client";
import "./projectScan.css";

export type ScanProgressEvent = {
  phase: string; completed?: number; total?: number; path?: string; package_id?: string; package_name?: string;
  affected?: number; inconclusive?: number; findings?: number; scanned?: number; skipped?: number;
};
export type ScanProgressSnapshot = { runId: string; payload: ScanProgressEvent };
export type ScanKind = "dependencies" | "code" | "model" | "source";

export function scanKind(payload: { scope?: string; dependency_mode?: string }): ScanKind {
  if (payload.scope === "project_code") return "code";
  if (payload.scope === "model_review") return "model";
  return payload.dependency_mode === "versions" ? "dependencies" : "source";
}

export const scanTitles: Record<ScanKind, string> = {
  dependencies: "Dependency check", code: "Code scan", model: "v40 source review", source: "Dependency source review",
};

export function ScanIcon({ kind }: { kind: ScanKind }) {
  const Icon = kind === "dependencies" ? PackageSearch : kind === "code" ? ScanLine : FileSearch;
  return <span className="chat-scan-icon" aria-hidden="true"><Icon size={17} /></span>;
}

type Stage = { phase: string; label: string; current: string };
const discoverFiles = { phase: "inventory", label: "Find files", current: "Finding project files" };
const inventory = { phase: "inventory", label: "Inventory", current: "Reading dependency manifests and installed versions" };
const report = { phase: "finalizing", label: "Report", current: "Preparing the report" };
const steps: Record<ScanKind, Stage[]> = {
  dependencies: [inventory, { phase: "versions", label: "Advisories", current: "Checking dependency versions" }, report],
  code: [discoverFiles, { phase: "static_analysis", label: "Static rules", current: "Checking files against static rules" }, report],
  model: [], source: [],
};

export function ScanProgress({ kind, state, progress, activity, onCancel }: {
  kind: ScanKind; state: string; progress?: ScanProgressEvent; activity?: ChatDetail["activity"]; onCancel?: () => void;
}) {
  const stages = steps[kind];
  const stageIndex = stages.findIndex(stage => stage.phase === progress?.phase);
  const title = scanTitles[kind];
  const label = state === "cancelling" ? "Stopping" : state === "starting" ? "Starting the scan"
    : state === "queued" ? "Waiting to run" : stages[stageIndex]?.current
      ?? (activity?.phase === "preparing_workspace" ? "Preparing the workspace" : activity?.message || "Preparing the scan");
  const completed = progress?.completed;
  const total = progress?.total;
  const hasTotal = typeof completed === "number" && typeof total === "number" && total > 0 && completed >= 0 && completed <= total;
  const count = hasTotal ? `${completed} of ${total} ${kind === "dependencies" ? "packages checked" : "files processed"}` : undefined;
  const current = progress?.package_name || progress?.package_id || progress?.path;
  const stopTitle = kind === "dependencies" ? "Stop check" : "Stop scan";
  return <article className="chat-scan-card chat-scan-live" aria-label={title}>
    <header className="chat-scan-head">
      <ScanIcon kind={kind} />
      <div className="chat-scan-heading"><strong>{title}</strong><p role="status">{label}</p></div>
      {onCancel && <button type="button" className="chat-scan-stop" disabled={state === "cancelling"} onClick={onCancel}><Square size={11} aria-hidden="true" />{stopTitle}</button>}
    </header>
    <div className="chat-scan-body">
      <div className="chat-scan-meter" role="progressbar" aria-label={`${title} progress`}
        aria-valuemin={hasTotal ? 0 : undefined} aria-valuemax={hasTotal ? total : undefined}
        aria-valuenow={hasTotal ? completed : undefined} aria-valuetext={count || label} data-measured={hasTotal}>
        {hasTotal && <span style={{ inlineSize: `${completed / total * 100}%` }} />}
      </div>
      {(count || current) && <div className="chat-scan-meter-detail">
        {count && <span>{count}</span>}
        {current && <code title={current}>{current}</code>}
      </div>}
      {(progress?.affected != null || progress?.inconclusive != null || progress?.findings != null) && <div className="chat-scan-live-counts">
        {progress.affected != null && <span>{progress.affected} affected</span>}
        {progress.inconclusive != null && <span>{progress.inconclusive} inconclusive</span>}
        {progress.scanned != null && <span>{progress.scanned} scanned</span>}
        {progress.skipped != null && <span>{progress.skipped} skipped</span>}
        {progress.findings != null && <span>{progress.findings} rule matches</span>}
      </div>}
      {stages.length > 0 && <ol className="chat-scan-steps" aria-label="Scan stages">
        {stages.map((stage, index) => <li key={stage.phase} data-state={index < stageIndex ? "done" : index === stageIndex ? "active" : undefined}
          aria-current={index === stageIndex ? "step" : undefined}>
          <span className="chat-scan-step-dot" aria-hidden="true">{index < stageIndex && <Check size={10} />}</span>{stage.label}
        </li>)}
      </ol>}
    </div>
  </article>;
}
