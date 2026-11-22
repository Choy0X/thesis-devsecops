import { useEffect, useState } from "react";
import { ApiError, api, errorText, type ProjectUpdate, type TrialFinding, type UpdateTrial } from "../lib/api";
import { RunConsole } from "../components/RunConsole";
import { Skeleton } from "../components/ui";
import { when } from "./ProjectScan";

// Trying a candidate update in the sandbox. The packages are fetched as wheels on this computer (the only use of the network, and the question says so first), installed offline in two closed
// containers next to a copy of the project, and the project's tests and the tools are run in each. The page shows what changed between the two. It never says the update is safe or correct.

const OUTCOME_WORDS: Record<string, string> = { improved: "fewer tests fail with the updated packages", same: "the same tests fail with the updated packages", regression: "more tests fail with the updated packages",
  not_run: "the tests were not compared", env_error: "the tests could not run" };
const STATE_WORDS: Record<UpdateTrial["state"], string> = { done: "Tried", prefetch_failed: "Stopped: a package could not be fetched as a wheel", install_failed: "Stopped: the packages could not be installed", running: "Running" };
export const CONFIRM = "This downloads the packages named in the update's requirements files from PyPI to this computer, as wheels only: nothing from them is run here. They are then installed in closed containers with no network, " +
  "and your project's tests and the scan tools are run there, once with today's packages and once with the updated ones. It needs Docker and the tool image. Nothing in your project changes.";

const kb = (n: number) => `${Math.round(n / 1024).toLocaleString("en")} KB`;
function Findings({ title, items }: { title: string; items: TrialFinding[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className="font-medium">{title}</p>
      <ul aria-label={title} className="mt-1 space-y-0.5 text-[0.78rem] text-muted">{items.map((f) => <li key={f.id}><span className="font-mono text-[0.74rem] text-fg">{f.file}{f.line ? `:${f.line}` : ""}</span> {f.message} <span className="text-faint">({f.severity})</span></li>)}</ul>
    </div>
  );
}

export function TrialView({ t }: { t: UpdateTrial }) {
  const c = t.comparison;
  return (
    <div className="space-y-2 text-[0.82rem]" aria-label="The result of the trial">
      <p><span className="font-medium">{STATE_WORDS[t.state]}</span> <span className="text-muted">· {when(t.finished)} · Python {t.python}, {t.platform}</span></p>
      <p className="text-muted">{t.words}</p>
      <ul aria-label="Packages fetched" className="space-y-0.5 text-[0.78rem] text-muted">
        {Object.entries(t.prefetch).map(([label, p]) => <li key={label}>{label}: {p.files.length} wheels, {kb(p.bytes)}{p.ok ? "" : ` - ${p.note}`}</li>)}
      </ul>
      {t.not_installed.length > 0 && <p className="text-warn">Not installed (only requirements files are, so far): {t.not_installed.join(", ")}.</p>}
      {(["before", "after"] as const).map((k) => t[k] && (
        <p key={k} className={t[k]!.install.state === "failed" ? "text-crit-soft" : "text-muted"}>Install {k === "before" ? "of today's packages" : "of the updated packages"}: {t[k]!.install.state === "failed" ? `failed. ${t[k]!.install.note}` : "done"}</p>))}
      {c && (
        <div className="border border-line bg-surface-2 px-3 py-2.5 space-y-2">
          <p><span className="font-medium">Tests:</span> {OUTCOME_WORDS[c.tests.outcome] ?? c.tests.outcome}. <span className="text-muted">{c.tests.words}</span></p>
          <p className="text-muted">Before: {c.tests.before?.note ?? "no test result"}. After: {c.tests.after?.note ?? "no test result"}.</p>
          <p><span className="font-medium">Findings of the tools:</span> {c.findings.new.length} new, {c.findings.gone.length} gone, {c.findings.unchanged} unchanged.</p>
          <Findings title="New with the updated packages" items={c.findings.new} />
          <Findings title="Gone with the updated packages" items={c.findings.gone} />
          {c.checks_changed.length > 0 && <ul aria-label="Checks that ended differently" className="text-warn">{c.checks_changed.map((x) => <li key={x.tool}>{x.tool}: {x.before ?? "absent"} before, {x.after ?? "absent"} after</li>)}</ul>}
        </div>)}
    </div>
  );
}

export function UpdateTrialPanel({ u }: { u: ProjectUpdate }) {
  const [trial, setTrial] = useState<UpdateTrial | null | undefined>(undefined);
  const [error, setError] = useState("");
  const load = () => api.projectUpdateTrial(u.project_id, u.id).then(setTrial).catch((e) => {
    if (e instanceof ApiError && e.status === 404) setTrial(null); else setError(errorText(e));
  });
  useEffect(() => { setTrial(undefined); load(); }, [u.id]);                 // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="space-y-2 border border-line p-3" aria-label="Try the update in the sandbox">
      <p className="font-medium">Try it in the sandbox</p>
      <p className="text-muted">Installs the updated packages in a closed container next to a copy of the project and runs its tests and the scan tools there, so you can see what changes before you apply anything. It is not a
        statement that the update is safe or correct.</p>
      <RunConsole stage="trial_update" label="Try in the sandbox" confirmText={CONFIRM} params={{ project: u.project_id, update: u.id }} launch={() => api.trialProjectUpdate(u.project_id, u.id)}
        onDone={load} hint="Needs Docker and the tool image. Takes minutes." />
      {error && <p role="alert" className="text-crit-soft">{error}</p>}
      {trial === undefined && !error && <Skeleton className="h-10 w-full" />}
      {trial === null && <p className="text-muted">This update has not been tried in the sandbox.</p>}
      {trial && <TrialView t={trial} />}
    </div>
  );
}
