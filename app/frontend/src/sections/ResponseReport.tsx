import { useEffect, useState } from "react";
import { api, errorText, type Candidate, type ResponseFinding, type ResponseReport, type SeverityClass } from "../lib/api";
import { Button, Skeleton } from "../components/ui";
import { Select, Hint } from "../components/kit";
import { Dialog } from "../components/hud/Dialog";
import { DiffViewer } from "../components/hud/DiffViewer";
import { num } from "../lib/format";
import { when } from "./ProjectScan";

// The report of a response run: every finding with its class and where the class came from, what was done about it, and, for a patch candidate, the diff, the steps and the tests before and after.
// A candidate is for a person to review. It is not applied, it is not verified, and no word on this page calls it correct, safe or ready to merge.

const CLASSES: SeverityClass[] = ["Critical", "Warning", "Recommendation", "Unknown"];
const CLASS_TONE: Record<SeverityClass, string> = { Critical: "text-crit", Warning: "text-warn", Recommendation: "text-muted", Unknown: "text-faint" };
export const SOURCE_WORDS: Record<string, string> = { cvss_vector: "from the CVSS vector", osv_label: "from the advisory's label", tool: "rated by the tool", rule: "rated by this app's rule",
  model: "a model's estimate, not a measured score", unknown: "no score" };
export const ACTION_WORDS: Record<string, string> = { logged: "Logged", email_sent: "In the email that was sent", email_composed: "In an email that was composed and not sent", upgrade_recommended: "Upgrade recommended",
  candidate_made: "Patch candidate made", candidate_stopped: "No patch candidate", candidate_skipped: "No candidate this run", no_candidate: "No candidate possible" };
const OUTCOME_WORDS: Record<string, string> = { improved: "fewer tests fail after the patch", same: "the same tests fail after the patch", regression: "more tests fail after the patch", not_run: "the tests were not run", env_error: "the tests could not run" };
const STEP_WORDS: Record<string, string> = { target: "The file", model: "The proposal", diff: "The diff checked", apply: "Applied to a copy of the file", tests_before: "Tests before", tests_after: "Tests after" };
const STEP_TONE: Record<string, string> = { passed: "text-ok", failed: "text-crit-soft", not_run: "text-faint", env_error: "text-warn" };
const PAGE = 100;

export function CandidateView({ projectId, id, onClose }: { projectId: string; id: string; onClose: () => void }) {
  const [c, setC] = useState<Candidate | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.projectCandidate(projectId, id).then(setC).catch((e) => setError(errorText(e))); }, [projectId, id]);
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title="Patch candidate" description="A candidate for you to review. It has not been applied to your folders.">
      {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
      {!c && !error && <Skeleton className="h-32 w-full" />}
      {c && (
        <div className="space-y-3 text-[0.8rem]">
          <p className="text-muted">{c.words}</p>
          <p><span className="font-medium">Receipt state:</span> <span className="text-warn">not verified</span>. A verified patch needs a test that fails before it and passes after it; nothing here reproduces the weakness.</p>
          <p className="font-mono text-[0.76rem]">{c.file ?? c.finding.file}{c.finding.line ? `:${c.finding.line}` : ""} <span className="font-sans text-muted">- {c.finding.rule}</span></p>
          {c.model && <p className="text-muted">Proposed by {c.model.model} on {c.model.host}{c.model.local ? " (this computer)" : " (not this computer)"}.</p>}
          <ol aria-label="Steps" className="space-y-1">
            {Object.entries(c.steps).map(([k, s]) => (
              <li key={k}><span className={STEP_TONE[s.status]}>{s.status.replace("_", " ")}</span> <span className="font-medium">{STEP_WORDS[k] ?? k}</span>{s.detail ? <span className="text-muted">: {s.detail}</span> : null}</li>))}
          </ol>
          <p><span className="font-medium">Tests:</span> {OUTCOME_WORDS[c.tests.outcome] ?? c.tests.outcome}. <span className="text-muted">{c.tests.words}</span></p>
          {c.diff ? <DiffViewer diff={c.diff} height={288} label="The diff" /> : <p className="text-muted">There is no diff: {c.stopped ?? "nothing was proposed"}</p>}
        </div>)}
    </Dialog>
  );
}

function Row({ f, onCandidate }: { f: ResponseFinding; onCandidate: (id: string) => void }) {
  const where = f.kind === "dependency" ? `${f.package} ${f.version ?? ""}` : f.line != null ? `${f.file}:${f.line}` : String(f.file ?? "");
  return (
    <tr className="border-b border-line/60 align-top">
      <td className={`py-2 pr-3 ${CLASS_TONE[f.severity_class]}`}>{f.severity_class}<span className="block text-[0.68rem] text-faint">{f.class_note ? <Hint label={f.class_note}>{SOURCE_WORDS[f.class_source] ?? f.class_source}</Hint> : (SOURCE_WORDS[f.class_source] ?? f.class_source)}</span></td>
      <td className="py-2 pr-3 text-muted">{f.kind === "dependency" ? "Dependency" : "Code"}</td>
      <td className="py-2 pr-3 font-mono text-[0.76rem] break-all">{where}</td>
      <td className="py-2 pr-3">{f.message}{f.cve ? <span className="block text-[0.72rem] text-faint">{f.cve}</span> : null}</td>
      <td className="py-2">
        <span className="block">{ACTION_WORDS[f.action.action] ?? f.action.action}</span>
        {f.action.upgrade_to && <span className="block text-[0.72rem] text-muted">to {f.action.upgrade_to}</span>}
        {f.action.why && <span className="block text-[0.72rem] text-muted">{f.action.why}</span>}
        {f.action.candidate_id && <Button variant="ghost" onClick={() => onCandidate(f.action.candidate_id!)} aria-label={`Open the candidate for ${where}`}>Open</Button>}
      </td>
    </tr>
  );
}

export function ResponseReportView({ r }: { r: ResponseReport }) {
  const [klass, setKlass] = useState("");
  const [action, setAction] = useState("");
  const [shown, setShown] = useState(PAGE);
  const [open, setOpen] = useState<string | null>(null);
  const rows = r.findings.filter((f) => (!klass || f.severity_class === klass) && (!action || f.action.action === action));
  const select = "w-auto min-w-[8rem]";
  const cell = (n: number, label: string, tone = "") => <div className="border border-line bg-surface-2 px-3 py-2"><div className={`tnum text-[1.1rem] font-semibold ${tone}`}>{num(n)}</div><div className="text-[0.72rem] text-muted">{label}</div></div>;
  return (
    <div className="space-y-3">
      <p className="text-[0.78rem] text-muted">Run {when(r.finished)}: scope {r.scope}, mode {r.mode}{r.scope !== "dependencies" ? `, ${r.depth} code scan` : ""}. {r.words}</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {cell(r.summary.findings, "findings")}{CLASSES.map((c) => <div key={c}>{cell(r.summary.by_class[c] ?? 0, c, r.summary.by_class[c] ? CLASS_TONE[c] : "")}</div>)}
      </div>
      {r.notes.length > 0 && <ul aria-label="Notes" className="list-disc space-y-0.5 pl-5 text-[0.78rem] text-warn">{r.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
      {r.email && (
        <div className="border border-line bg-surface-2 p-3 text-[0.78rem]">
          <p className="font-medium">{r.email.sent ? "Email sent" : "Email composed, not sent"}: {r.email.subject}</p>
          {!r.email.sent && r.email.reason && <p className="text-muted">{r.email.reason}</p>}
          <pre aria-label="The email" className="mt-2 max-h-40 overflow-auto font-mono text-[0.72rem] text-muted">{r.email.body}</pre>
        </div>)}
      <div className="flex flex-wrap items-center gap-3 text-[0.78rem] text-muted">
        <div className="flex items-center gap-2">Class
          <Select aria-label="Class" value={klass} onValueChange={(v) => { setKlass(v); setShown(PAGE); }} className={select}
            options={[{ value: "", label: "all" }, ...CLASSES.map((c) => ({ value: c, label: `${c} (${r.summary.by_class[c] ?? 0})` }))]} /></div>
        <div className="flex items-center gap-2">Action
          <Select aria-label="Action" value={action} onValueChange={(v) => { setAction(v); setShown(PAGE); }} className={select}
            options={[{ value: "", label: "all" }, ...Object.entries(r.summary.by_action).map(([k, n]) => ({ value: k, label: `${ACTION_WORDS[k] ?? k} (${n})` }))]} /></div>
      </div>
      {r.findings.length === 0 ? <p className="text-[0.8rem] text-muted">Nothing was found by the checks that ran. That is not the same as no problem.</p>
        : rows.length === 0 ? <p className="text-[0.8rem] text-muted">Nothing in this group.</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-[0.8rem]" aria-label="Findings and responses">
              <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-3 font-medium">Class</th><th className="py-2 pr-3 font-medium">Kind</th>
                <th className="py-2 pr-3 font-medium">Where</th><th className="py-2 pr-3 font-medium">What</th><th className="py-2 font-medium">Response</th></tr></thead>
              <tbody>{rows.slice(0, shown).map((f) => <Row key={f.id} f={f} onCandidate={setOpen} />)}</tbody>
            </table>
            {rows.length > shown && <div className="pt-2"><Button variant="ghost" onClick={() => setShown(shown + PAGE)}>Show {Math.min(PAGE, rows.length - shown)} more of {num(rows.length - shown)}</Button></div>}
          </div>)}
      {open && <CandidateView projectId={r.project_id} id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}
