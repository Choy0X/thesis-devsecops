import { CheckCircle2, CircleDashed, CircleDot, OctagonX, ShieldAlert, ShieldCheck, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import type { EngineRemediation, EngineRun, EngineStep, EngineTask } from "../../lib/api";
import { cn } from "../../lib/cn";
import { Badge } from "../hud/Badge";
import { COMPLETION, RUN_STATE, STEP_LABEL, STEP_NAMES, STEP_STATE, budgetWords, checkWords, remediationWords, stepGood, stopWords } from "./engineWords";

// The Task tab of the agent page: what the v2 engine recorded about the work on the newest instruction. The plan is the model's text; everything else here (the files changed, the checks and
// how they ended, the fixes and whether each is verified, why the run stopped) is the engine's own record. A badge says what happened in words as well as in colour. A section with nothing to
// say is left out, not shown empty.

const TONE = { ok: "border-ok-line text-ok", warn: "border-warn-line text-warn", muted: "border-line text-muted", crit: "border-crit-line text-crit" } as const;

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1.5 border-t border-line pt-3 first:border-t-0 first:pt-0">
      <h3 className="text-[0.7rem] font-semibold uppercase tracking-wider text-faint">{title}</h3>
      {children}
    </section>
  );
}

function StepIcon({ state }: { state: EngineStep["state"] }) {
  const common = { size: 13, "aria-hidden": true, className: "mt-0.5 shrink-0" } as const;
  if (state === "done") return <CheckCircle2 {...common} />;
  if (state === "active") return <CircleDot {...common} />;
  if (state === "blocked") return <OctagonX {...common} />;
  return <CircleDashed {...common} />;
}

export function RemediationCard({ r }: { r: EngineRemediation }) {
  const verified = r.status === "verified";
  const env = r.reason === "env_error";
  const stale = verified && r.stale === true;
  const Icon = verified && !stale ? ShieldCheck : env ? TriangleAlert : ShieldAlert;
  const tone = verified && !stale ? TONE.ok : env ? TONE.muted : TONE.warn;
  const finding = r.finding?.rule ? `${r.finding.rule}${r.finding.file ? ` in ${r.finding.file}${r.finding.line ? `:${r.finding.line}` : ""}` : ""}` : r.finding?.description ?? r.finding?.id ?? "";
  return (
    <li className="space-y-1.5 border border-line bg-surface-2 p-2 text-[0.76rem]" aria-label={`Fix check: ${verified ? "verified" : "unverified"}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={cn("inline-flex items-center gap-1 border px-1.5 py-0.5 text-[0.72rem] font-medium", tone)}><Icon size={12} aria-hidden />{verified ? `Verified${r.grade && r.grade !== "none" ? ` (grade ${r.grade})` : ""}${stale ? ", then the files changed" : ""}` : "Unverified"}</span>
        {r.finding?.source === "claimed" && <Badge>the model's own description</Badge>}
        {r.finding?.source === "scan" && <Badge>from the scan</Badge>}
      </div>
      <p className="text-muted">{remediationWords(r)}</p>
      {stale && <p className="text-warn">Files were changed after this check, so it describes the files as they were. Check again before relying on it.</p>}
      {finding && <p className="break-words text-faint">Problem: {finding}</p>}
      <p className="break-all font-mono text-[0.7rem] text-faint">Test: {r.test.path}</p>
      {r.patch.files.length > 0 && <p className="break-all text-faint">Changed to fix it: {r.patch.files.join(", ")}</p>}
      {Object.keys(r.steps).length > 0 && (
        <ul aria-label="Steps of the check" className="flex flex-wrap gap-1">
          {STEP_NAMES.map((n) => {
            const status = r.steps[n];
            const good = stepGood(n, status);
            return <li key={n} className={cn("border px-1.5 py-0.5 text-[0.68rem]", status === undefined || status === "not_run" ? TONE.muted : good ? TONE.ok : TONE.warn)}>{STEP_LABEL[n]}: {status === "not_run" || status === undefined ? "not run" : good ? "yes" : "no"}</li>;
          })}
        </ul>
      )}
      {!verified && r.note && <p className="break-words border-l-2 border-line pl-2 text-faint">{r.note.slice(0, 280)}</p>}
      {r.review && r.review.length > 0 && (
        <div className="space-y-0.5 text-faint">
          <p>Review notes (advice, not part of the verdict):</p>
          <ul aria-label="Review notes" className="list-disc pl-4">{r.review.map((n, i) => <li key={i} className="break-words">The patch {n}</li>)}</ul>
        </div>)}
      {r.receipt && <p className="font-mono text-[0.68rem] text-faint">Receipt {r.receipt.slice(0, 12)}</p>}
    </li>
  );
}

export function TaskPanel({ task, run }: { task: EngineTask; run?: EngineRun | null }) {
  const completion = COMPLETION[task.completion.status] ?? COMPLETION.open;
  const used = budgetWords(run);
  const observations = task.observations.slice(-6);
  return (
    <div className="space-y-3" aria-label="Task">
      <Block title="State">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className={cn("inline-flex items-center border px-1.5 py-0.5 text-[0.72rem] font-medium", TONE[completion.tone])}>{completion.label}</span>
          {run && <Badge>{RUN_STATE[run.state] ?? run.state}</Badge>}
          {(task.replans ?? 0) > 0 && <Badge>{`Plan changed ${task.replans} ${task.replans === 1 ? "time" : "times"}`}</Badge>}
        </div>
        {task.completion.reason && <p className="text-[0.76rem] text-muted">{task.completion.reason}</p>}
        {run?.stop_reason && <p className="text-[0.76rem] text-muted">{stopWords(run.stop_reason)}.</p>}
        {used && <p className="text-[0.74rem] text-faint">Used {used}.</p>}
      </Block>
      <Block title="Asked">
        <p className="text-[0.78rem]">{task.objective}</p>
      </Block>
      {task.constraints.some((c) => !c.implicit) && (
        <Block title="Ruled out by you">
          <ul className="space-y-1 text-[0.76rem]">
            {task.constraints.filter((c) => !c.implicit).map((c, i) => (
              <li key={i}>
                <span className="text-muted">{c.text}</span>
                {c.protect && c.protect.length > 0 && <span className="block break-all font-mono text-[0.68rem] text-faint">Not written: {c.protect.join(", ")}</span>}
              </li>))}
          </ul>
        </Block>
      )}
      {task.constraints.some((c) => c.implicit) && (
        <Block title="Left alone by the engine">
          <ul className="space-y-1 text-[0.76rem]">
            {task.constraints.filter((c) => c.implicit).map((c, i) => (
              <li key={i}>
                <span className="text-muted">{c.text}</span>
                {c.protect && c.protect.length > 0 && <span className="block break-all font-mono text-[0.68rem] text-faint">Not written: {c.protect.slice(0, 6).join(", ")}{c.protect.length > 6 ? ` and ${c.protect.length - 6} more` : ""}</span>}
              </li>))}
          </ul>
        </Block>
      )}
      {task.plan.length > 0 && (
        <Block title="Plan">
          <ol aria-label="Plan steps" className="space-y-1 text-[0.78rem]">
            {task.plan.map((s) => (
              <li key={s.id} className={cn("flex gap-1.5", s.state === "done" && "text-muted", s.state === "skipped" && "text-faint line-through")}>
                <StepIcon state={s.state} />
                <span className="min-w-0"><span className="sr-only">{STEP_STATE[s.state]}: </span>{s.text}</span>
              </li>))}
          </ol>
        </Block>
      )}
      {task.remediations.length > 0 && (
        <Block title="Fix checks">
          <ul aria-label="Fix checks" className="space-y-2">{task.remediations.slice().reverse().map((r) => <RemediationCard key={r.id} r={r} />)}</ul>
        </Block>
      )}
      {task.validations.length > 0 && (
        <Block title="Checks">
          <ul aria-label="Checks" className="space-y-1 text-[0.76rem]">
            {task.validations.slice(-8).map((v) => (
              <li key={v.id} className="flex gap-1.5">
                <span className={cn("mt-0.5 shrink-0 border px-1 text-[0.66rem]", v.class === "passed" ? TONE.ok : v.class === "validation_env" ? TONE.muted : TONE.warn)}>{v.class === "passed" ? "pass" : v.class === "validation_env" ? "n/a" : "fail"}</span>
                <span className="min-w-0 break-words text-muted">{checkWords(v)}</span>
              </li>))}
          </ul>
        </Block>
      )}
      {task.files_changed.length > 0 && (
        <Block title="Files changed">
          <ul aria-label="Files changed" className="space-y-0.5 text-[0.76rem]">
            {task.files_changed.map((f) => <li key={f.path} className="break-all font-mono text-[0.72rem] text-muted">{f.path}</li>)}
          </ul>
        </Block>
      )}
      {observations.length > 0 && (
        <Block title="What the engine noticed">
          <ul aria-label="Observations" className="space-y-0.5 text-[0.74rem] text-faint">
            {observations.map((o, i) => <li key={o.id ?? i} className="break-words">{o.text}</li>)}
          </ul>
        </Block>
      )}
    </div>
  );
}
