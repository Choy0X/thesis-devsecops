import { useEffect, useState } from "react";
import { api, errorText, type Capabilities, type CapabilityStatus, type DepReport, type DepRow, type Project, type ScanListItem } from "../lib/api";
import { Button, Panel, Skeleton } from "../components/ui";
import { Checkbox, Select, Hint } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { num } from "../lib/format";

// Scanning the dependencies of a project, and saying what the app can and cannot check. Four states are shown and none of the last three is "fine": a version an advisory
// affects (vulnerable, with the evidence), a version no advisory of this computer affects (no finding by advisory matching, not proof of safety), a range with no exact version
// (inconclusive), and a check that could not be made because the advisories are not here (not checked).

export const STATE_WORDS: Record<DepRow["state"], string> = {
  vulnerable: "Vulnerable", no_finding: "No finding by advisory matching", inconclusive: "Inconclusive: the version in use is not known", env_error: "Not checked",
};
const STATUS_WORDS: Record<CapabilityStatus, string> = { implemented: "Yes", partial: "In part", needs_data: "Needs advisories", not_implemented: "Not yet" };
const STATUS_TONE: Record<CapabilityStatus, string> = { implemented: "text-ok", partial: "text-warn", needs_data: "text-warn", not_implemented: "text-faint" };
const SEV_TONE: Record<string, string> = { critical: "text-crit", high: "text-crit-soft", medium: "text-warn", low: "text-muted", unknown: "text-muted" };

export const when = (iso: string) => iso.slice(0, 16).replace("T", " ");

/** What the app can do for each ecosystem, one row each; the project's own ecosystems first and marked. */
export function CapabilityTable({ data, mine = [] }: { data: Capabilities; mine?: string[] }) {
  const names = [...mine.filter((e) => e in data.ecosystems), ...Object.keys(data.ecosystems).filter((e) => !mine.includes(e))];
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[0.78rem]" aria-label="What the app can check, by ecosystem">
        <thead><tr className="border-b border-line text-left text-[0.7rem] uppercase tracking-wide text-faint">
          <th className="py-2 pr-3 font-medium">Ecosystem</th>{data.capabilities.map((c) => <th key={c.key} title={c.label} className="py-2 pr-3 font-medium">{c.label.split(" ").slice(0, 3).join(" ")}</th>)}</tr></thead>
        <tbody>
          {names.map((eco) => (
            <tr key={eco} className="border-b border-line/60">
              <td className="py-1.5 pr-3 text-fg">{eco}{mine.includes(eco) ? <span className="ml-1.5 text-[0.68rem] text-brand-strong">in this project</span> : null}</td>
              {data.capabilities.map((c) => {
                const cell = data.ecosystems[eco][c.key];
                return <td key={c.key} className={`py-1.5 pr-3 ${STATUS_TONE[cell.status]}`}><Hint label={cell.note || c.label}>{STATUS_WORDS[cell.status]}</Hint></td>;
              })}
            </tr>))}
        </tbody>
      </table>
    </div>
  );
}

function Counts({ r }: { r: DepReport }) {
  const s = r.summary;
  const cell = (n: number, label: string, tone = "") => <div className="border border-line bg-surface-2 px-3 py-2"><div className={`tnum text-[1.1rem] font-semibold ${tone}`}>{num(n)}</div><div className="text-[0.72rem] text-muted">{label}</div></div>;
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
      {cell(s.dependencies, "dependencies")}{cell(s.vulnerable, "vulnerable", s.vulnerable ? "text-crit-soft" : "")}{cell(s.no_finding, "no finding by advisory matching")}
      {cell(s.inconclusive, "inconclusive (range only)", s.inconclusive ? "text-warn" : "")}{cell(s.env_error, "not checked", s.env_error ? "text-warn" : "")}
    </div>
  );
}

function Row({ row, outdated }: { row: DepRow; outdated: boolean }) {
  const [open, setOpen] = useState(false);
  const o = row.outdated;
  return (
    <>
      <tr className="border-b border-line/60 align-top">
        <td className="py-2 pr-3 font-mono text-[0.76rem] text-fg">{row.name}</td>
        <td className="py-2 pr-3 tnum">{row.version ?? <span className="text-faint">{row.declared ?? "no version"}</span>}</td>
        <td className="py-2 pr-3 text-faint">{row.ecosystem}</td>
        <td className={`py-2 pr-3 ${row.state === "vulnerable" ? SEV_TONE[row.severity ?? "unknown"] : row.state === "no_finding" ? "text-muted" : "text-warn"}`}>
          {row.state === "vulnerable" ? `${row.severity} · ${STATE_WORDS.vulnerable.toLowerCase()}` : STATE_WORDS[row.state]}
        </td>
        {outdated && <td className="py-2 pr-3 tnum">{o ? (o.state === "outdated" ? `newer: ${o.latest} (${o.behind})` : o.state === "up_to_date" ? "latest" : o.reason) : ""}</td>}
        <td className="py-2"><Button variant="ghost" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide" : "Why"}</Button></td>
      </tr>
      {open && (
        <tr className="border-b border-line/60 bg-surface-2/50"><td colSpan={outdated ? 6 : 5} className="px-3 py-2.5 text-[0.78rem]">
          <p className="text-muted">{row.reason}</p>
          {row.recommendation && <p className="mt-1 text-fg">{row.recommendation}</p>}
          {row.advisories?.map((a) => (
            <div key={a.id} className="mt-2 border-l-2 border-line pl-3">
              <div><span className="font-mono text-[0.74rem]">{a.cve ?? a.id}</span> <span className={SEV_TONE[a.severity] ?? ""}>{a.severity}</span> {a.summary}</div>
              <div className="text-muted">Affects {a.affected_versions}; matched by {a.rule}{a.first_fixed ? `; first fixed in ${a.first_fixed}` : "; no fixed release is known"}{a.cwe ? `; ${a.cwe}` : ""}. Advisory {a.id}.</div>
            </div>))}
          <p className="mt-2 text-faint">From {row.manifests.join(", ")}{row.checks.length ? `; checks run: ${row.checks.join(", ")}` : "; no check was run"}.</p>
        </td></tr>)}
    </>
  );
}

export function DepReportView({ r }: { r: DepReport }) {
  const [filter, setFilter] = useState<"" | DepRow["state"]>("");
  const rows = r.dependencies.filter((d) => !filter || d.state === filter);
  const notChecked = Object.entries(r.ecosystems).filter(([, e]) => !e.checked);
  return (
    <div className="space-y-3">
      <p className="text-[0.78rem] text-muted">Scanned {when(r.finished)}. {r.words}</p>
      <Counts r={r} />
      {notChecked.map(([eco, e]) => <p key={eco} role="status" className="text-[0.8rem] text-warn">{eco}: {e.reason}</p>)}
      {r.problems.length > 0 && (
        <details className="text-[0.78rem]"><summary className="cursor-pointer text-warn">{r.problems.length} manifest{r.problems.length === 1 ? " was" : "s were"} not read</summary>
          <ul className="mt-1 space-y-0.5 text-muted">{r.problems.map((p) => <li key={p.manifest}><span className="font-mono text-[0.74rem]">{p.manifest}</span>: {p.reason}</li>)}</ul></details>)}
      {r.outdated && <p className="text-[0.78rem] text-muted">Registries: {r.outdated.outdated} outdated, {r.outdated.up_to_date} on the latest release, {r.outdated.unavailable} without an answer{r.outdated.not_looked_up ? `, ${r.outdated.not_looked_up} not looked up` : ""}.</p>}
      <div className="flex items-center gap-2 text-[0.78rem] text-muted">Show
        <Select aria-label="Show" value={filter} onValueChange={(v) => setFilter(v as typeof filter)} className="w-auto min-w-[12rem]"
          options={[{ value: "", label: `all ${r.dependencies.length}` }, ...(["vulnerable", "inconclusive", "env_error", "no_finding"] as const).map((s) => ({ value: s, label: `${STATE_WORDS[s]} (${r.dependencies.filter((d) => d.state === s).length})` }))]} />
      </div>
      {rows.length === 0 ? <p className="text-[0.8rem] text-muted">Nothing in this group.</p> : (
        <div className="overflow-x-auto">
          <table className="w-full text-[0.8rem]">
            <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-3 font-medium">Package</th><th className="py-2 pr-3 font-medium">Version</th>
              <th className="py-2 pr-3 font-medium">Ecosystem</th><th className="py-2 pr-3 font-medium">State</th>{r.outdated_checked && <th className="py-2 pr-3 font-medium">Latest release</th>}<th /></tr></thead>
            <tbody>{rows.map((d) => <Row key={d.id} row={d} outdated={r.outdated_checked} />)}</tbody>
          </table>
        </div>)}
    </div>
  );
}

/** The dependency scan of one project: start it (with or without the registry lookup), and read the newest report. */
export function DependencyScan({ project, onScanned }: { project: Project; onScanned?: () => void }) {
  const [lookup, setLookup] = useState(false);
  const [scans, setScans] = useState<ScanListItem[] | null>(null);
  const [report, setReport] = useState<DepReport | null>(null);
  const [error, setError] = useState("");
  const load = () => api.projectScans(project.id).then((r) => {
    const mine = r.scans.filter((s) => s.kind === "dependencies");                         // the newest scan of dependencies; the scans of code have their own panel
    setScans(mine);
    if (mine[0]) api.projectScan(project.id, mine[0].scan_id).then(setReport).catch((e) => setError(errorText(e)));
  }).catch((e) => setError(errorText(e)));
  useEffect(() => { load(); }, [project.id]);                                 // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Panel title="Scan dependencies" desc="Reads the manifests and lock files of the project and matches each exact version against the advisories on this computer. The project's code is not run.">
      <div className="space-y-3">
        <Checkbox checked={lookup} onCheckedChange={setLookup} label="Also look up newer releases"
          description="This sends the package names (not the project's code) to the public registries: PyPI, npmjs.org and the like." />
        <RunConsole stage="scan_project_deps" label="Scan the dependencies" params={{ project: project.id, outdated: lookup }}
          launch={(p) => api.scanProjectDeps(project.id, !!p.outdated)} onDone={() => { load(); onScanned?.(); }} hint="Takes a few seconds to a minute." />
        {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
        {scans === null && !error && <Skeleton className="h-16 w-full" />}
        {scans && scans.length === 0 && <p className="text-[0.8rem] text-muted">This project has not been scanned yet.</p>}
        {report && <DepReportView r={report} />}
      </div>
    </Panel>
  );
}
