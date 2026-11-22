import { Checkbox } from '../components/kit';
import { Fragment, useEffect, useState } from "react";
import { api, errorText, type DeepCheck, type DeepFinding, type DeepReport, type DeepSeverity, type Project } from "../lib/api";
import { Button, Field, Input, Panel, Skeleton } from "../components/ui";
import { Select } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { RadarChart } from "../components/charts/RadarChart";
import { num } from "../lib/format";
import { when } from "./ProjectScan";

// The full Scan Codebase: the rules that run in this app, then the approved tools, the project's own tests and (when the project says how it starts) a few questions asked of the running
// program, all inside a closed container on a copy of the project. The page says, for each check, whether it ran, how far, and what it found, so that "no finding" is always read
// with the list of checks behind it. A finding that is only flagged is never called a vulnerability; one with evidence beside it is.

const SEVERITIES: DeepSeverity[] = ["critical", "high", "medium", "low", "unknown"];
const SEV_TONE: Record<DeepSeverity, string> = { critical: "text-crit", high: "text-crit-soft", medium: "text-warn", low: "text-muted", unknown: "text-faint" };
const SOURCE_WORDS = { tool: "by the tool", rule: "by this app's policy", unknown: "not rated" } as const;
const STATE_WORDS = { vulnerable: "Vulnerable, with evidence", inconclusive: "Flagged, not confirmed" } as const;
const CHECK_WORDS: Record<DeepCheck["state"], string> = { ran: "Ran", partial: "Ran, could not read every file", env_error: "Did not run to its end", skipped: "Not attempted" };
const CHECK_TONE: Record<DeepCheck["state"], string> = { ran: "text-ok", partial: "text-warn", env_error: "text-crit-soft", skipped: "text-faint" };
const PAGE = 100;
const COPY_SKIP_WORDS: Record<string, string> = { skipped_folders_and_caches: "folders of history, caches and packages", secret_like: "files that look like keys or credentials", links: "links", not_plain: "special files", too_large: "files above 5 MB" };

export const portProblem = (text: string): string => {
  if (!text.trim()) return "";
  const n = Number(text);
  return Number.isInteger(n) && n >= 1024 && n <= 65535 ? "" : "A port is a whole number from 1024 to 65535.";
};

function Counts({ r }: { r: DeepReport }) {
  const s = r.summary;
  const cell = (n: number, label: string, tone = "") => <div className="border border-line bg-surface-2 px-3 py-2"><div className={`tnum text-[1.1rem] font-semibold ${tone}`}>{num(n)}</div><div className="text-[0.72rem] text-muted">{label}</div></div>;
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
      {cell(s.findings, "findings")}{cell(s.by_state.vulnerable, "with evidence", s.by_state.vulnerable ? "text-crit-soft" : "")}{cell(s.by_state.inconclusive, "flagged, not confirmed")}
      {cell(s.checks_ran.length, "checks that ran")}{cell(s.checks_failed.length, "did not run to the end", s.checks_failed.length ? "text-crit-soft" : "")}{cell(s.checks_skipped.length, "not attempted")}
    </div>
  );
}

function Checks({ checks }: { checks: DeepCheck[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[0.8rem]" aria-label="Checks">
        <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-3 font-medium">Check</th><th className="py-2 pr-3 font-medium">Result</th>
          <th className="py-2 pr-3 font-medium">Looked at</th><th className="py-2 font-medium">Found / note</th></tr></thead>
        <tbody>
          {checks.map((c, i) => (
            <tr key={`${c.tool}-${c.folder ?? ""}-${i}`} className="border-b border-line/60 align-top">
              <td className="py-2 pr-3 font-mono text-[0.76rem]">{c.tool}{c.folder ? <span className="ml-1 font-sans text-[0.68rem] text-faint">{c.folder}</span> : null}</td>
              <td className={`py-2 pr-3 ${CHECK_TONE[c.state]}`}>{CHECK_WORDS[c.state]}</td>
              <td className="py-2 pr-3 text-muted">{c.rule_set}{c.files != null ? `, ${num(c.files)} files` : ""}</td>
              <td className="py-2 text-muted">
                {c.findings > 0 ? `${num(c.findings)} findings. ` : ""}{c.note}
                {c.problems.length > 0 && <ul className="mt-1 list-disc pl-4 text-[0.74rem]">{c.problems.map((p) => <li key={p}>{p}</li>)}</ul>}
                {c.failing && c.failing.length > 0 && <ul aria-label="Failing tests" className="mt-1 list-disc pl-4 text-[0.74rem]">{c.failing.map((p) => <li key={p}>{p}</li>)}</ul>}
                {c.evidence && <div className="mt-0.5 break-all font-mono text-[0.68rem] text-faint">Raw output: {c.evidence}</div>}
              </td>
            </tr>))}
        </tbody>
      </table>
    </div>
  );
}

function Detail({ f }: { f: DeepFinding }) {
  return (
    <tr className="border-b border-line/60 bg-surface-2/50"><td colSpan={7} className="px-3 py-2.5 text-[0.78rem]">
      <p className="text-fg">{f.message}</p>
      {f.extra?.why && <p className="mt-1 text-muted">{f.extra.why}</p>}
      {f.extra?.advice && <p className="mt-1 text-muted">To check: {f.extra.advice}</p>}
      {f.extra?.context && f.extra.context.length > 0 ? (
        <pre aria-label={`Lines around ${f.file} line ${f.line}`} className="mt-2 overflow-x-auto border border-line bg-surface px-3 py-2 font-mono text-[0.74rem] leading-relaxed">
          {f.extra.context.map((c) => <div key={c.line} className={c.match ? "bg-warn/10 text-fg" : "text-muted"}><span className="mr-3 inline-block w-8 select-none text-right text-faint">{c.line}</span>{c.text}</div>)}
        </pre>
      ) : f.snippet ? <p className="mt-2 font-mono text-[0.74rem] text-muted">{f.snippet}</p> : null}
      <p className="mt-2 text-faint">
        {f.rules && f.rules.length > 1 ? f.rules.join(", ") : f.rule} · found by {f.sources.join(" and ")} · {f.basis}
        {f.confidence ? ` · confidence ${f.confidence}` : ""}{f.severity_by ? ` · the higher rating is ${f.severity_by}'s` : ""}
        {f.evidence ? ` · raw output: ${f.evidence.file}` : ""}
      </p>
    </td></tr>
  );
}

function Row({ f }: { f: DeepFinding }) {
  const [open, setOpen] = useState(false);
  const where = f.line != null ? `${f.file}:${f.line}` : f.file;
  return (
    <>
      <tr className="border-b border-line/60 align-top">
        <td className={`py-2 pr-3 ${SEV_TONE[f.severity]}`}>{f.severity}<span className="block text-[0.68rem] text-faint">{SOURCE_WORDS[f.severity_source]}</span></td>
        <td className={`py-2 pr-3 ${f.state === "vulnerable" ? "text-crit-soft" : "text-muted"}`}>{STATE_WORDS[f.state]}</td>
        <td className="py-2 pr-3 font-mono text-[0.76rem] break-all">{where}</td>
        <td className="py-2 pr-3">{f.message}</td>
        <td className="py-2 pr-3 text-muted">{f.type}</td>
        <td className="py-2 pr-3 tnum text-muted">{f.cwe.join(", ")}</td>
        <td className="py-2"><Button variant="ghost" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} the evidence for ${where}`}>{open ? "Hide" : "Evidence"}</Button></td>
      </tr>
      {open && <Detail f={f} />}
    </>
  );
}

export function DeepReportView({ r }: { r: DeepReport }) {
  const [state, setState] = useState("");
  const [severity, setSeverity] = useState("");
  const [tool, setTool] = useState("");
  const [type, setType] = useState("");
  const [shown, setShown] = useState(PAGE);
  const tools = Array.from(new Set(r.findings.flatMap((f) => f.sources))).sort();
  const rows = r.findings.filter((f) => (!state || f.state === state) && (!severity || f.severity === severity) && (!tool || f.sources.includes(tool)) && (!type || f.type === type));
  const select = "w-auto min-w-[8rem]";
  const reset = (fn: () => void) => { fn(); setShown(PAGE); };
  return (
    <div className="space-y-3">
      <p className="text-[0.78rem] text-muted">Scanned {when(r.finished)} in a closed container from {r.image.tag}. {r.words}</p>
      <Counts r={r} />
      <Checks checks={r.checks} />
      <RadarChart title="Finding types" description="How many findings fall under each kind of weakness. This app groups them from the CWE numbers the tools give; the table has the same numbers." unit="findings"
        axes={Object.entries(r.summary.by_type).map(([label, value]) => ({ label, value }))} />
      <ul aria-label="What was copied" className="space-y-0.5 text-[0.78rem] text-muted">
        {r.copy.map((c) => {
          const left = Object.entries(c.skipped).filter(([, n]) => n > 0).map(([k, n]) => `${num(n)} ${COPY_SKIP_WORDS[k] ?? k}`);
          return (
            <li key={c.as}>{num(c.files)} files ({num(Math.round(c.bytes / 1024))} KB) of {c.folder} were copied to {c.as}{left.length ? `; left out: ${left.join(", ")}` : ""}.
              {c.secret_like_names.length > 0 && <> Files that look like credentials were not copied and not read: {c.secret_like_names.join(", ")}.</>}</li>);
        })}
      </ul>
      <div className="flex flex-wrap items-center gap-3 text-[0.78rem] text-muted">
        <div className="flex items-center gap-2">State
          <Select aria-label="State" value={state} onValueChange={(v) => reset(() => setState(v))} className={select}
            options={[{ value: "", label: "all" }, { value: "vulnerable", label: `${STATE_WORDS.vulnerable} (${r.summary.by_state.vulnerable})` }, { value: "inconclusive", label: `${STATE_WORDS.inconclusive} (${r.summary.by_state.inconclusive})` }]} /></div>
        <div className="flex items-center gap-2">Severity
          <Select aria-label="Severity" value={severity} onValueChange={(v) => reset(() => setSeverity(v))} className={select}
            options={[{ value: "", label: "all" }, ...SEVERITIES.map((v) => ({ value: v, label: `${v} (${r.summary.by_severity[v] ?? 0})` }))]} /></div>
        <div className="flex items-center gap-2">Type
          <Select aria-label="Type" value={type} onValueChange={(v) => reset(() => setType(v))} className={select}
            options={[{ value: "", label: "all" }, ...Object.entries(r.summary.by_type).map(([k, n]) => ({ value: k, label: `${k} (${n})` }))]} /></div>
        <div className="flex items-center gap-2">Found by
          <Select aria-label="Found by" value={tool} onValueChange={(v) => reset(() => setTool(v))} className={select}
            options={[{ value: "", label: "all" }, ...tools.map((t) => ({ value: t, label: t }))]} /></div>
      </div>
      {r.findings.length === 0 ? <p className="text-[0.8rem] text-muted">No check found anything. Read the list of checks above: only what ran is covered, and that is not the same as no problem.</p>
        : rows.length === 0 ? <p className="text-[0.8rem] text-muted">Nothing in this group.</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-[0.8rem]" aria-label="Findings">
              <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-3 font-medium">Severity</th><th className="py-2 pr-3 font-medium">State</th>
                <th className="py-2 pr-3 font-medium">Where</th><th className="py-2 pr-3 font-medium">What</th><th className="py-2 pr-3 font-medium">Type</th><th className="py-2 pr-3 font-medium">CWE</th><th /></tr></thead>
              <tbody>{rows.slice(0, shown).map((f) => <Fragment key={f.id}><Row f={f} /></Fragment>)}</tbody>
            </table>
            {rows.length > shown && <div className="pt-2"><Button variant="ghost" onClick={() => setShown(shown + PAGE)}>Show {Math.min(PAGE, rows.length - shown)} more of {num(rows.length - shown)}</Button></div>}
          </div>)}
    </div>
  );
}

/** The full scan of one project: say how it starts (optional), start the scan, and read the newest report. */
export function DeepScan({ project, onScanned }: { project: Project; onScanned?: () => void }) {
  const [report, setReport] = useState<DeepReport | null | undefined>(undefined);
  const [error, setError] = useState("");
  const [command, setCommand] = useState("");
  const [modelReview, setModelReview] = useState(false);
  const [port, setPort] = useState("");
  const load = () => api.projectScans(project.id).then((r) => {
    const mine = r.scans.find((s) => s.kind === "codebase");
    if (!mine) { setReport(null); return; }
    return api.projectScan<DeepReport>(project.id, mine.scan_id).then(setReport);
  }).catch((e) => setError(errorText(e)));
  useEffect(() => { load(); }, [project.id]);                                 // eslint-disable-line react-hooks/exhaustive-deps
  const bad = portProblem(port);
  const half = (command.trim() === "") !== (port.trim() === "");
  return (
    <Panel title="Scan codebase in the sandbox" desc="Copies the project into a closed container (no network, nothing of your computer mounted) and runs the approved tools, the project's own tests and, if you say how it starts, a few safe questions on its own loopback. Your folders are only read.">
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-[1fr_9rem]">
          <Field label="How does it start? (optional)" hint="For example: uvicorn app:app --port $PORT. The program is started inside the container and stopped after the questions.">
            <Input aria-label="Start command" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="python app.py" maxLength={500} autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="Port" hint={bad || undefined}>
            <Input aria-label="Port" value={port} onChange={(e) => setPort(e.target.value)} placeholder="8000" inputMode="numeric" autoComplete="off" />
          </Field>
        </div>
        {half && !bad && <p role="status" className="text-[0.76rem] text-muted">The questions need both a start command and a port; with only one of them they are not asked.</p>}
        <Checkbox checked={modelReview} onChange={event => setModelReview(event.target.checked)} label="Include local model security review (JavaScript/TypeScript, up to 32 files)" />
        <RunConsole stage="scan_project_codebase" label="Scan in the sandbox" params={{ project: project.id }} disabled={!!bad}
          launch={() => api.scanProjectCodebase(project.id, { start_command: command.trim(), port: port.trim() ? Number(port) : null, ...(modelReview ? {model_review: true, model_max_files: 32} : {}) })}
          onDone={() => { load(); onScanned?.(); }} hint="Takes a few minutes. Needs Docker and the tool image." />
        {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
        {report === undefined && !error && <Skeleton className="h-16 w-full" />}
        {report === null && <p className="text-[0.8rem] text-muted">The code of this project has not been scanned in the sandbox yet.</p>}
        {report && <DeepReportView r={report} />}
        {report && (report as any).model_review && <details><summary className="cursor-pointer text-sm">Local model, tool evidence and review referrals</summary><pre className="max-h-96 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify((report as any).model_review, null, 2)}</pre></details>}
      </div>
    </Panel>
  );
}
