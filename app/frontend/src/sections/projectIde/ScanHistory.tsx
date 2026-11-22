import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { activeRun, describe, type ChatDetail, type Run } from "./client";
import { DependencyReport } from "./DependencyReport";
import { friendlyRunError } from "./runError";
import { ScanIcon, ScanProgress, scanKind, scanTitles, type ScanProgressSnapshot } from "./ScanProgress";
import type { ScanCommand } from "./state";

type ModelFinding = {
  path: string;
  line: number | null;
  class: string;
  quote: string;
  explanation: string;
  gate_ok: boolean;
  gate_reasons: string[];
  state?: string;
  function?: string;
  attacker_control?: string;
  exposure?: { distance: number | null; reason: string };
  confirmation?: { state: string; reason: string };
};

type CodeFinding = {
  id: string; file: string; line: number; rule: string; title?: string; severity?: string; snippet?: string;
  summary?: string; cwe?: string; why?: string; advice?: string; note?: string;
};

function CodeScanResult({ result }: { result: Record<string, unknown> }) {
  const [visible, setVisible] = useState(12);
  const summary = result.summary as { scanned_files?: number; skipped_entries?: number; cut_short?: boolean } | undefined;
  const findings: CodeFinding[] = Array.isArray(result.findings) ? result.findings : [];
  const skipped = Object.entries((result.skipped || {}) as Record<string, number>);
  const languages = Object.entries((result.languages || {}) as Record<string, { files: number; coverage: string }>);
  const coverage = result.coverage as { skipped_long_lines?: number; rules?: number } | undefined;
  return <>
    <dl className="chat-scan-metrics" role="group" aria-label="Code scan results">
      <div><dt>Files scanned</dt><dd><strong>{summary?.scanned_files ?? 0}</strong></dd></div>
      <div data-tone={findings.length ? "pending" : undefined}><dt>Rule matches</dt><dd><strong>{findings.length}</strong></dd></div>
      <div><dt>Entries skipped</dt><dd>{summary?.skipped_entries ?? 0}</dd></div>
    </dl>
    <p className="chat-scan-explainer">{findings.length ? "Review each match in its source context."
      : summary?.scanned_files === 0 ? "No supported source files were scanned." : "No static rule matches in the scanned files."}</p>
    {findings.length > 0 && <div className="chat-scan-findings">
      {findings.slice(0, visible).map(finding => <details className="chat-scan-finding" key={finding.id}>
        <summary><ChevronDown size={14} aria-hidden="true" /><span><strong>{finding.title || finding.summary || finding.rule}</strong><code>{finding.file}:{finding.line}</code></span>
          {finding.severity && <span className="chat-scan-tag" data-tone={finding.severity === "critical" || finding.severity === "high" ? "danger" : "pending"}>{finding.severity}</span>}</summary>
        <div className="chat-scan-finding-body">
          <p><code>{finding.rule}</code>{finding.cwe && <span> / {finding.cwe}</span>}</p>
          {finding.snippet && <pre><code>{finding.snippet}</code></pre>}
          {finding.why && <p>{finding.why}</p>}
          {finding.note && <p>{finding.note}</p>}
          {finding.advice && <p><strong>Review</strong> {finding.advice}</p>}
        </div>
      </details>)}
      {visible < findings.length && <button type="button" className="chat-scan-more" onClick={() => setVisible(visible + 12)}>Show more matches</button>}
    </div>}
    {(languages.length > 0 || skipped.length > 0 || coverage) && <details className="chat-scan-disclosure">
      <summary>Coverage and skipped files</summary>
      <div className="chat-scan-coverage">
        {summary?.cut_short && <p>The scan reached a configured limit. Some files or matches may be missing.</p>}
        {coverage?.rules != null && <p>{coverage.rules} static rules available. Runtime behavior and data flow were outside this scan.</p>}
        {languages.length > 0 && <dl>{languages.map(([language, info]) => <div key={language}><dt>{language}</dt><dd>{info.files} {info.files === 1 ? "file" : "files"}{info.coverage === "secrets_only" ? ", secret patterns only" : ""}</dd></div>)}</dl>}
        {skipped.length > 0 && <dl>{skipped.map(([reason, count]) => <div key={reason}><dt>{reason.replace(/_/g, " ")}</dt><dd>{count}</dd></div>)}</dl>}
        {Boolean(coverage?.skipped_long_lines) && <p>{coverage?.skipped_long_lines} long lines skipped.</p>}
      </div>
    </details>}
  </>;
}

type ReviewCoverage = {
  units?: number; by_kind?: Record<string, number>; reviewed?: number; invalid_answers?: number;
  confirmed?: number; unconfirmed?: number; rejected?: number; unlocated?: number;
};

const STATE_LABEL: Record<string, string> = { confirmed: "Confirmed", unconfirmed: "Needs review" };

function ModelReviewResult({ result, runId, onPropose }: { result: Record<string, unknown>; runId: string; onPropose?: (findingId: string) => void }) {
  if (result.version === 2) return <TwoPassReviewResult result={result} runId={runId} onPropose={onPropose} />;
  const findings = Array.isArray(result.findings) ? result.findings as ModelFinding[] : [];
  const skipped = Array.isArray(result.skipped) ? result.skipped as { function?: string }[] : [];
  const skippedFunctions = skipped.filter(row => row.function).length;
  const fileLimitations = skipped.length - skippedFunctions;
  const reviewed = typeof result.reviewed === "number" ? result.reviewed : 0;
  const withheld = findings.filter(finding => !finding.gate_ok).length;
  return <>
    <p>{reviewed} {reviewed === 1 ? "function" : "functions"} reviewed, {skippedFunctions} skipped.</p>
    {fileLimitations > 0 && <p>{fileLimitations} {fileLimitations === 1 ? "file has" : "files have"} partial or unavailable coverage.</p>}
    {withheld > 0 && <p>{withheld} {withheld === 1 ? "finding withheld" : "findings withheld"} by evidence checks.</p>}
    {findings.length > 0 && <ul>
      {findings.map((finding, index) => finding.gate_ok && <li key={`${finding.path}:${finding.line}:${finding.class}:${index}`}>
        <p><code>{finding.path}{finding.line != null ? `:${finding.line}` : ""}</code>{finding.class ? ` ${finding.class}` : ""}</p>
        <p>Evidence passed</p>
        {finding.quote && <pre><code>{finding.quote}</code></pre>}
        {finding.explanation && <p>{finding.explanation}</p>}
        {onPropose && <button className="chat-result-action" onClick={() => onPropose(runId + ":model_review:" + index)}>Propose patch</button>}
      </li>)}
    </ul>}
  </>;
}

// A scan made by the two-pass reviewer (result.version 2): every candidate was asked again about its class only.
function TwoPassReviewResult({ result, runId, onPropose }: { result: Record<string, unknown>; runId: string; onPropose?: (findingId: string) => void }) {
  const [visible, setVisible] = useState(12);
  const findings = Array.isArray(result.findings) ? result.findings as ModelFinding[] : [];
  const coverage = (result.coverage || {}) as ReviewCoverage;
  const skipped = Array.isArray(result.skipped) ? result.skipped as { path?: string; reason?: string }[] : [];
  const kinds = Object.entries(coverage.by_kind || {});
  return <>
    <dl className="chat-scan-metrics" role="group" aria-label="Model review results">
      <div data-tone={coverage.confirmed ? "danger" : undefined}><dt>Confirmed</dt><dd><strong>{coverage.confirmed ?? 0}</strong></dd></div>
      <div data-tone={coverage.unconfirmed ? "pending" : undefined}><dt>Needs review</dt><dd><strong>{coverage.unconfirmed ?? 0}</strong></dd></div>
      <div><dt>Ruled out on re-check</dt><dd>{coverage.rejected ?? 0}</dd></div>
      <div><dt>Code units reviewed</dt><dd>{coverage.reviewed ?? 0} of {coverage.units ?? 0}</dd></div>
    </dl>
    <p className="chat-scan-explainer">{findings.length
      ? "Each finding was found in an open review and then checked again for its class alone. Confirmed means both passes point at the same place; it is still a lead to verify, not a proof."
      : "No finding survived the second, class-specific check."}</p>
    {findings.length > 0 && <div className="chat-scan-findings">
      {findings.slice(0, visible).map((finding, index) => <details className="chat-scan-finding" key={`${finding.path}:${finding.line}:${finding.class}:${index}`}>
        <summary><ChevronDown size={14} aria-hidden="true" /><span><strong>{finding.class || "Unnamed weakness"}</strong><code>{finding.path}{finding.line != null ? `:${finding.line}` : ""}</code></span>
          <span className="chat-scan-tag" data-tone={finding.state === "confirmed" ? "danger" : "pending"}>{STATE_LABEL[finding.state || ""] || finding.state}</span></summary>
        <div className="chat-scan-finding-body">
          {finding.quote && <pre><code>{finding.quote}</code></pre>}
          {finding.explanation && <p>{finding.explanation}</p>}
          {finding.attacker_control && <p><strong>Input</strong> {finding.attacker_control}</p>}
          {finding.exposure && <p><strong>Exposure</strong> {finding.function ? `${finding.function} ` : ""}{finding.exposure.reason}.</p>}
          {finding.confirmation && <p><strong>Re-check</strong> {finding.confirmation.reason}</p>}
          {onPropose && <button className="chat-result-action" onClick={() => onPropose(runId + ":model_review:" + index)}>Propose patch</button>}
        </div>
      </details>)}
      {visible < findings.length && <button type="button" className="chat-scan-more" onClick={() => setVisible(visible + 12)}>Show more findings</button>}
    </div>}
    <details className="chat-scan-disclosure">
      <summary>Coverage and skipped files</summary>
      <div className="chat-scan-coverage">
        {kinds.length > 0 && <dl>{kinds.map(([kind, count]) => <div key={kind}><dt>{kind === "module" ? "module code" : kind === "window" ? "parts of long functions" : "functions"}</dt><dd>{count}</dd></div>)}</dl>}
        {Boolean(coverage.invalid_answers) && <p>{coverage.invalid_answers} {coverage.invalid_answers === 1 ? "answer was" : "answers were"} not valid and counted as reviewed without a result.</p>}
        {Boolean(coverage.unlocated) && <p>{coverage.unlocated} {coverage.unlocated === 1 ? "candidate" : "candidates"} could not be placed on a source line or named a class outside the catalogue; kept in the scan record.</p>}
        {skipped.length > 0 && <ul>{skipped.slice(0, 20).map((row, index) => <li key={index}><code>{row.path}</code> {(row.reason || "").replace(/_/g, " ")}</li>)}</ul>}
      </div>
    </details>
  </>;
}

function ScanEntry({ run, onPropose }: { run: Run; onPropose?: (findingId: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const payload = (run.payload || {}) as { scope?: string; dependency_mode?: string; selected_package_ids?: string[] };
  const result = (run.result || {}) as Record<string, unknown>;
  const agent = result.agent && typeof result.agent === "object" ? result.agent as Record<string, unknown> : undefined;
  const evidence = { ...result, ...(agent ? { agent: Object.fromEntries(Object.entries(agent).filter(([key]) => key !== "messages")) } : {}) };
  const packages = Array.isArray(result.packages) ? result.packages.length : undefined;
  const kind = scanKind(payload);
  const modelReview = kind === "model";
  const dependencyCheck = kind === "dependencies";
  const codeScan = kind === "code";
  const cutShort = (result.summary as { cut_short?: boolean } | undefined)?.cut_short;
  const partial = cutShort || result.status === "partial" || result.status === "incomplete";
  const finished = run.state === "completed";
  const error = run.error || result.error;
  const status = finished ? (partial ? "Partial coverage" : "Completed")
    : run.state === "interrupted" ? "Stopped" : run.state === "failed" ? "Failed" : run.state.replace(/_/g, " ");
  return <article className="chat-scan-card" aria-label={scanTitles[kind]}>
    <header className="chat-scan-head">
      <ScanIcon kind={kind} />
      <div className="chat-scan-heading"><strong>{scanTitles[kind]}</strong></div>
      <span className="chat-scan-tag" data-tone={run.state === "failed" ? "danger" : partial ? "pending" : undefined}>{status}</span>
    </header>
    <div className="chat-scan-body">
      {payload.selected_package_ids?.length ? <p>Selected packages: {payload.selected_package_ids.join(", ")}</p> : null}
      {typeof packages === "number" && !dependencyCheck && <p>{packages} {packages === 1 ? "package" : "packages"} in the recorded result.</p>}
      {error != null && <p className="ide-error" role="alert">{friendlyRunError(error)}</p>}
      {!error && !finished && <p className="chat-scan-explainer">The scan stopped before a full report was available.</p>}
      {modelReview && run.result != null && typeof result.reviewed === "number" && <ModelReviewResult result={result} runId={run.id} onPropose={finished ? onPropose : undefined} />}
      {dependencyCheck && run.result != null && (finished || packages != null) && <DependencyReport result={result} />}
      {codeScan && finished && run.result != null && <CodeScanResult result={result} />}
      {result.coverage != null && !codeScan && <details className="chat-scan-disclosure"><summary>Coverage and limitations</summary><pre>{describe(result.coverage)}</pre></details>}
      {run.result != null && !dependencyCheck && !modelReview && (!codeScan || !!agent) && <details className="chat-scan-disclosure" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}><summary>Scan result and evidence</summary>{expanded && <pre>{describe(evidence)}</pre>}</details>}
    </div>
  </article>;
}
export function ScanHistory({ runs, progress, starting, activity, onCancel, onPropose }: {
  runs: Run[]; progress?: ScanProgressSnapshot | null; starting?: ScanCommand | null; activity?: ChatDetail["activity"];
  onCancel?: () => void; onPropose?: (findingId: string) => void;
}) {
  const scans = runs.filter(run => run.kind === "scan");
  const active = activeRun(runs);
  return <>{scans.map(run => activeRun([run]) ? <ScanProgress key={run.id} state={run.state}
    kind={scanKind((run.payload || {}) as Parameters<typeof scanKind>[0])} progress={progress?.runId === run.id ? progress.payload : undefined}
    activity={activity?.run_id === run.id ? activity : undefined} onCancel={run.id === active?.id ? onCancel : undefined} /> : <ScanEntry key={run.id} run={run} onPropose={onPropose} />)}
    {starting && !activeRun(scans) && <ScanProgress kind={scanKind(starting)} state="starting" />}
  </>;
}
