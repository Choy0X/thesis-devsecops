import { Fragment, useEffect, useState } from "react";
import { api, errorText, type CodeFinding, type CodeReport, type CodeSeverity, type Project } from "../lib/api";
import { Button, Panel, Skeleton } from "../components/ui";
import { Checkbox, Select } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { num } from "../lib/format";
import { when } from "./ProjectScan";

// Scanning the source code of a project with pattern rules. Every entry is a candidate: a text pattern matched a line, which is a place to look and not a proof that the program is
// vulnerable. The page says which languages were checked and how (rules, or hard-coded secrets only), which files were not read, and that no entry is not the same as no problem.

export const SEVERITIES: CodeSeverity[] = ["critical", "high", "medium", "low"];
const SEV_TONE: Record<CodeSeverity, string> = { critical: "text-crit", high: "text-crit-soft", medium: "text-warn", low: "text-muted" };
const SKIP_WORDS: Record<string, string> = { too_large: "too large", binary: "not text", unreadable: "could not be read", links: "links that leave the folder" };
export const PAGE = 100;

function Counts({ r }: { r: CodeReport }) {
  const s = r.summary;
  const cell = (n: number, label: string, tone = "") => <div className="border border-line bg-surface-2 px-3 py-2"><div className={`tnum text-[1.1rem] font-semibold ${tone}`}>{num(n)}</div><div className="text-[0.72rem] text-muted">{label}</div></div>;
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
      {cell(s.files, "source files read")}{cell(s.findings, "candidates")}
      {SEVERITIES.map((v) => <Fragment key={v}>{cell(s.severity[v], v, s.severity[v] ? SEV_TONE[v] : "")}</Fragment>)}
    </div>
  );
}

function Evidence({ f }: { f: CodeFinding }) {
  return (
    <tr className="border-b border-line/60 bg-surface-2/50"><td colSpan={5} className="px-3 py-2.5 text-[0.78rem]">
      <p className="text-fg">{f.why}</p>
      <p className="mt-1 text-muted">To check: {f.advice}</p>
      {f.note && <p className="mt-1 text-muted">{f.note}</p>}
      {f.context.length > 0 ? (
        <pre aria-label={`Lines around ${f.file} line ${f.line}`} className="mt-2 overflow-x-auto border border-line bg-surface px-3 py-2 font-mono text-[0.74rem] leading-relaxed">
          {f.context.map((c) => <div key={c.line} className={c.match ? "bg-warn/10 text-fg" : "text-muted"}><span className="mr-3 inline-block w-8 select-none text-right text-faint">{c.line}</span>{c.text}</div>)}
        </pre>
      ) : <p className="mt-2 font-mono text-[0.74rem] text-muted">{f.snippet}</p>}
      <p className="mt-2 text-faint">{f.rule} · {f.cwe} · {f.language}{f.in_tests ? " · in test code" : ""} · a candidate: a pattern matched this line, nothing was run.{f.more_in_file ? ` ${f.more_in_file} more lines of this file match the same rule and are not listed.` : ""}</p>
    </td></tr>
  );
}

function Row({ f }: { f: CodeFinding }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className="border-b border-line/60 align-top">
        <td className={`py-2 pr-3 ${SEV_TONE[f.severity]}`}>{f.severity}</td>
        <td className="py-2 pr-3 font-mono text-[0.76rem] text-fg break-all">{f.file}:{f.line}{f.in_tests && <span className="ml-1.5 font-sans text-[0.68rem] text-faint">test code</span>}</td>
        <td className="py-2 pr-3">{f.summary}</td>
        <td className="py-2 pr-3 tnum text-muted">{f.cwe}</td>
        <td className="py-2"><Button variant="ghost" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} the evidence for ${f.file} line ${f.line}`}>{open ? "Hide" : "Evidence"}</Button></td>
      </tr>
      {open && <Evidence f={f} />}
    </>
  );
}

export function CodeReportView({ r }: { r: CodeReport }) {
  const [severity, setSeverity] = useState<"" | CodeSeverity>("");
  const [rule, setRule] = useState("");
  const [language, setLanguage] = useState("");
  const [hideTests, setHideTests] = useState(false);
  const [shown, setShown] = useState(PAGE);
  const rows = r.findings.filter((f) => (!severity || f.severity === severity) && (!rule || f.rule === rule) && (!language || f.language === language) && !(hideTests && f.in_tests));
  const skipped = Object.entries(r.skipped).filter(([, n]) => n > 0);
  const select = "w-auto min-w-[8rem]";
  const reset = (fn: () => void) => { fn(); setShown(PAGE); };
  return (
    <div className="space-y-3">
      <p className="text-[0.78rem] text-muted">Scanned {when(r.finished)} with {r.rules} rules. {r.words}</p>
      <Counts r={r} />
      <ul aria-label="What was checked" className="space-y-0.5 text-[0.78rem]">
        {Object.entries(r.languages).map(([lang, l]) => (
          <li key={lang} className={l.coverage === "rules" ? "text-muted" : "text-warn"}>
            <span className="text-fg">{lang}</span> ({num(l.scanned)} of {num(l.files)} files read): {l.coverage === "rules" ? "checked with rules" : l.reason}
          </li>))}
        {Object.keys(r.languages).length === 0 && <li className="text-warn">No source files of a known language were found in this project, so nothing was checked.</li>}
      </ul>
      {skipped.length > 0 && <p className="text-[0.78rem] text-warn">Files not read: {skipped.map(([k, n]) => `${num(n)} ${SKIP_WORDS[k] ?? k}`).join(", ")}.</p>}
      {r.summary.cut_short && <p role="status" className="text-[0.8rem] text-warn">The scan stopped at its limits, so more candidates may exist.</p>}
      <div className="flex flex-wrap items-center gap-3 text-[0.78rem] text-muted">
        <div className="flex items-center gap-2">Severity
          <Select aria-label="Severity" value={severity} onValueChange={(v) => reset(() => setSeverity(v as typeof severity))} className={select}
            options={[{ value: "", label: "all" }, ...SEVERITIES.map((v) => ({ value: v, label: `${v} (${r.summary.severity[v]})` }))]} /></div>
        <div className="flex items-center gap-2">Rule
          <Select aria-label="Rule" value={rule} onValueChange={(v) => reset(() => setRule(v))} className={select}
            options={[{ value: "", label: "all" }, ...Object.entries(r.summary.by_rule).map(([k, n]) => ({ value: k, label: `${k} (${n})` }))]} /></div>
        <div className="flex items-center gap-2">Language
          <Select aria-label="Language" value={language} onValueChange={(v) => reset(() => setLanguage(v))} className={select}
            options={[{ value: "", label: "all" }, ...Object.keys(r.languages).map((k) => ({ value: k, label: k }))]} /></div>
        <Checkbox aria-label="Hide candidates in test code" checked={hideTests} onCheckedChange={(v) => reset(() => setHideTests(v))} label={`Hide test code${r.summary.in_tests ? ` (${r.summary.in_tests})` : ""}`} />
      </div>
      {r.findings.length === 0 ? <p className="text-[0.8rem] text-muted">No pattern matched. That is not the same as no problem: only the languages and patterns above were checked.</p>
        : rows.length === 0 ? <p className="text-[0.8rem] text-muted">Nothing in this group.</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-[0.8rem]" aria-label="Candidates found in the code">
              <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-3 font-medium">Severity</th><th className="py-2 pr-3 font-medium">Where</th>
                <th className="py-2 pr-3 font-medium">What</th><th className="py-2 pr-3 font-medium">CWE</th><th /></tr></thead>
              <tbody>{rows.slice(0, shown).map((f) => <Row key={f.id} f={f} />)}</tbody>
            </table>
            {rows.length > shown && <div className="pt-2"><Button variant="ghost" onClick={() => setShown(shown + PAGE)}>Show {Math.min(PAGE, rows.length - shown)} more of {num(rows.length - shown)}</Button></div>}
          </div>)}
    </div>
  );
}

/** The code scan of one project: start it, and read the newest report. */
export function CodeScan({ project, onScanned }: { project: Project; onScanned?: () => void }) {
  const [report, setReport] = useState<CodeReport | null | undefined>(undefined);        // undefined: still loading; null: never scanned
  const [error, setError] = useState("");
  const load = () => api.projectScans(project.id).then((r) => {
    const mine = r.scans.find((s) => s.kind === "code");
    if (!mine) { setReport(null); return; }
    return api.projectScan<CodeReport>(project.id, mine.scan_id).then(setReport);
  }).catch((e) => setError(errorText(e)));
  useEffect(() => { load(); }, [project.id]);                                 // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Panel title="Scan code" desc="Reads the source files of the project and looks for patterns that are often vulnerabilities. Nothing is run or changed. Each match is a candidate to look at, not a proof.">
      <div className="space-y-3">
        <RunConsole stage="scan_project_code" label="Scan the code" params={{ project: project.id }} launch={() => api.scanProjectCode(project.id)}
          onDone={() => { load(); onScanned?.(); }} hint="Takes a few seconds to a minute." />
        {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
        {report === undefined && !error && <Skeleton className="h-16 w-full" />}
        {report === null && <p className="text-[0.8rem] text-muted">The code of this project has not been scanned yet.</p>}
        {report && <CodeReportView r={report} />}
      </div>
    </Panel>
  );
}
