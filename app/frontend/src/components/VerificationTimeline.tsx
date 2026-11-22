import { Dev } from "../lib/devMode";

// The six steps of a verification and what they decided. A step is judged against what it must be: the security check must FAIL before the patch (that is
// the vulnerability showing up) and pass after; every other step must pass. So a step reads "as required" or "not as required", never a bare pass or
// fail that would call a reproduced vulnerability a failure. Words and shapes carry the state; colour only adds to them.

export type StepStatus = "passed" | "failed" | "not_run" | "env_error";
export type ReceiptStep = { status: StepStatus; detail?: string | null };
export type Receipt = {
  receipt_id?: string;
  steps: Record<string, ReceiptStep>;
  verdict: "verified" | "not_verified" | "env_error";
  grade: "G3" | "G2" | "none";
  reasons?: string[];
  runner?: { kind?: string; fixture_only?: boolean; image_digest?: string | null };
};

export const STEPS: { id: string; title: string; must: StepStatus; asRequired: string; notRequired: string }[] = [
  { id: "apply", title: "The patch applies", must: "passed", asRequired: "It applied cleanly.", notRequired: "It did not apply." },
  { id: "build", title: "The code builds", must: "passed", asRequired: "It built.", notRequired: "It did not build." },
  { id: "security_before", title: "The vulnerability shows before the patch", must: "failed", asRequired: "The security check failed on the original code, so the vulnerability was reproduced.",
    notRequired: "The security check passed on the original code, so the vulnerability was not reproduced and nothing can be shown to be fixed." },
  { id: "security_after", title: "The vulnerability is gone after the patch", must: "passed", asRequired: "The security check passes on the patched code.", notRequired: "The security check still fails on the patched code." },
  { id: "functionality", title: "The code still works", must: "passed", asRequired: "The functionality tests pass.", notRequired: "The functionality tests fail, so the patch may have broken something." },
  { id: "integrity", title: "The tests and the verifier were left alone", must: "passed", asRequired: "Nothing the verifier relies on was changed.", notRequired: "The patch changed files the verifier relies on." },
];

const VERDICT: Record<Receipt["verdict"], { title: string; tone: string }> = {
  verified: { title: "Verified", tone: "border-ok-line bg-ok-tint" },
  not_verified: { title: "Not verified", tone: "border-warn-line bg-warn-tint" },
  env_error: { title: "Could not be decided", tone: "border-crit-line bg-crit-tint" },
};
const GRADE: Record<Receipt["grade"], string> = {
  G3: "Grade G3: the vulnerability is gone and the functionality still passes.",
  G2: "Grade G2: the vulnerability is gone, but the functionality check did not pass.",
  none: "No grade: the vulnerability was not shown to be fixed.",
};

export const judge = (stepId: string, status: StepStatus): "as_required" | "not_as_required" | "not_run" | "env_error" => {
  const step = STEPS.find((s) => s.id === stepId)!;
  return status === "not_run" ? "not_run" : status === "env_error" ? "env_error" : status === step.must ? "as_required" : "not_as_required";
};
const WORD = { as_required: "As required", not_as_required: "Not as required", not_run: "Not run", env_error: "Environment problem" } as const;
const MARK = { as_required: "OK", not_as_required: "NO", not_run: "--", env_error: "!!" } as const;
const TONE = {
  as_required: "border-ok-line text-ok", not_as_required: "border-crit-line text-crit-soft", not_run: "border-line text-faint border-dashed", env_error: "border-warn-line text-warn border-dotted",
} as const;

export function VerificationTimeline({ receipt }: { receipt: Receipt }) {
  const verdict = VERDICT[receipt.verdict];
  const fixtureOnly = receipt.runner?.fixture_only === true;
  return (
    <section aria-label="Verification result" className="space-y-4">
      <div role="status" className={`border px-4 py-3 ${verdict.tone}`}>
        <p className="text-[0.95rem] font-semibold">{verdict.title}</p>
        <p className="mt-0.5 text-[0.8rem] text-muted">{GRADE[receipt.grade]}</p>
        {fixtureOnly && <p className="mt-2 border border-warn-line bg-warn-tint px-2 py-1 text-[0.76rem] text-warn">Test run only. This was run by the app's own test runner, not in a container, so it cannot count as evidence for a real record.</p>}
      </div>
      <ol aria-label="Verification steps" className="space-y-1.5">
        {STEPS.map((s, i) => {
          const step = receipt.steps[s.id] ?? { status: "not_run" as StepStatus };
          const state = judge(s.id, step.status);
          return (
            <li key={s.id} className="flex gap-3 border border-line bg-surface-2 px-3 py-2" data-state={state}>
              <span aria-hidden className={`mt-0.5 grid h-6 w-6 shrink-0 place-items-center border font-mono text-[0.66rem] ${TONE[state]}`}>{MARK[state]}</span>
              <div className="min-w-0 text-[0.82rem]">
                <p><span className="font-medium">{i + 1}. {s.title}</span> <span className="text-muted">({WORD[state]})</span></p>
                <p className="text-[0.76rem] text-muted">{state === "as_required" ? s.asRequired : state === "not_as_required" ? s.notRequired : state === "env_error" ? "The environment failed (a timeout, no container service or a refused setup), so this step decided nothing." : "This step was not reached."}</p>
                <Dev>{step.detail ? <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap bg-terminal p-2 font-mono text-[0.68rem] text-faint">{step.detail}</pre> : null}</Dev>
              </div>
            </li>
          );
        })}
      </ol>
      {receipt.reasons && receipt.reasons.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-[0.78rem] text-muted" aria-label="Reasons">{receipt.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      )}
      <Dev>
        <p className="break-all font-mono text-[0.7rem] text-faint">
          receipt {receipt.receipt_id ?? "none"} · runner {receipt.runner?.kind ?? "unknown"}{receipt.runner?.image_digest ? ` · ${receipt.runner.image_digest}` : ""}
        </p>
      </Dev>
    </section>
  );
}
