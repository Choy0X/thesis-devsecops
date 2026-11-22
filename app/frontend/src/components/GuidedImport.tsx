import { useState } from "react";
import { api, errorText, type ImportCheck } from "../lib/api";
import { Button, Field, Input } from "./ui";
import { RunConsole } from "./RunConsole";
import { num } from "../lib/format";

// Importing the file a GPU run wrote, in three steps: find it, check it, import it. The check runs every test the import runs, on a copy that is thrown away, and says in words which
// passed, which failed (with the reasons exactly as the test wrote them and what to do) and which were not looked at because an earlier one had already failed. Nothing is imported
// or scored by checking; the import is the separate step that follows, and is only offered for a file that passed.

const KINDS = {
  trial: {
    stage: 'import_trial_outputs', noun: 'trial_outputs.zip', label: 'Path of trial_outputs.zip', placeholder: 'the path of the downloaded trial_outputs.zip',
    hint: 'Download this file from the portable trial notebook, including when it stops early.', button: 'Import trial',
    after: 'Keeps experimental loss, raw examples and resource reports. A trial does not count as full training or security evaluation.',
  },
  outputs: {
    stage: "import_finetune_outputs", noun: "outputs.zip", label: "Path of outputs.zip", placeholder: "C:\\Users\\you\\Downloads\\outputs.zip",
    hint: "The file the notebook wrote after training. Paste its full path; quotes around it are fine.", button: "Import",
    after: "An accepted import is typed (software smoke, GPU smoke, trained, incomplete); a rejected one keeps nothing.",
  },
  eval: {
    stage: "import_finetune_eval", noun: "eval_outputs.zip", label: "Path of eval_outputs.zip", placeholder: "the path of the downloaded eval_outputs.zip",
    hint: "Written by the notebook's evaluate cells (the untouched base B0, or a trained control). Paste its full path; quotes around it are fine.", button: "Import and score",
    after: "Scored by the shared evaluator into a new run folder under experiments/. A software check or a capped evaluation is kept as evidence and never counts as the control evaluated.",
  },
} as const;

const MARK = { passed: "passed", failed: "failed", not_checked: "not checked" } as const;
const TONE = { passed: "text-ok", failed: "text-crit", not_checked: "text-faint" } as const;
const ICON = { passed: "v", failed: "x", not_checked: "-" } as const;

function Summary({ check }: { check: ImportCheck }) {
  const s = check.summary as Record<string, unknown> | null;
  if (!s) return null;
  if (check.kind === 'trial') return <p className="text-muted">It is a <b className="font-medium">{String(s.state)} trial</b> with a total budget of {Number(s.total_budget_minutes)} minutes. It is experimental and does not count as full training or security evaluation.</p>;
  if (check.kind === "outputs") {
    return (
      <p className="text-muted">It is a <b className="font-medium">{String(s.type).replace(/_/g, " ")}</b> run ({String(s.state)}, {num(Number(s.steps_done ?? 0))} steps) with an adapter of {num(Number(s.adapter_parameters ?? 0))} parameters.
        {s.smoke_test ? " It is marked as a software check, so it is not a training result." : ""}
        {Array.isArray(s.warnings) && s.warnings.length > 0 ? ` Noted: ${(s.warnings as string[]).join(" ")}` : ""}</p>);
  }
  return (
    <p className="text-muted">It is the evaluation of <b className="font-medium">{String(s.baseline)}</b> ({String(s.arm)}, {String(s.role)} role): {num(Number(s.attempts ?? 0))} answers{Number(s.excluded) ? `, ${num(Number(s.excluded))} views excluded` : ""}.
      {s.counts_as_evaluation ? " It counts as an evaluation of that control." : s.smoke_test ? " It is a software check, so it will not count as an evaluation." : s.partial ? " It is capped to part of the views, so it will not count as an evaluation." : " Pre-generation exclusions remain unresolved, so this does not count as a complete evaluation."}</p>);
}

export function GuidedImport({ run, kind, onDone }: { run: string; kind: "outputs" | "eval" | "trial"; onDone: () => void }) {
  const k = KINDS[kind];
  const [path, setPath] = useState("");
  const [benchmark, setBenchmark] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ path: string; benchmark: string; check: ImportCheck } | null>(null);
  const clean = path.trim().replace(/^"|"$/g, "");
  const benchmarkPath = kind === 'eval' ? benchmark.trim().replace(/^"|"$/g, '') : '';
  const checked = result && result.path === clean && result.benchmark === benchmarkPath ? result.check : null;

  const verify = async () => {
    setBusy(true); setError("");
    try { setResult({ path: clean, benchmark: benchmarkPath,
      check: await (benchmarkPath ? api.finetuneCheckImport(run, kind, clean, benchmarkPath) : api.finetuneCheckImport(run, kind, clean)) }); }
    catch (e) { setError(errorText(e)); setResult(null); }
    finally { setBusy(false); }
  };

  return (
    <div className="space-y-3">
      <ol className="flex flex-wrap gap-x-6 gap-y-1 text-[0.78rem]" aria-label={`Steps to import ${k.noun}`}>
        <li className={!checked ? "font-medium" : "text-muted"}>1. Find the file</li>
        <li className={checked && !checked.ok ? "font-medium text-crit" : checked ? "text-muted" : "text-faint"}>2. Check it</li>
        <li className={checked?.ok ? "font-medium" : "text-faint"}>3. Import it</li>
      </ol>
      <Field label={k.label} hint={k.hint}>
        <Input aria-label={k.label} value={path} onChange={(e) => setPath(e.target.value)} placeholder={k.placeholder} />
      </Field>
      {kind === 'eval' && <Field label="Independent benchmark directory (optional)"
        hint="For an independent evaluation, provide the reviewed frozen benchmark used by the notebook. Leave empty when it is packaged in the original bundle.">
        <Input aria-label="Independent benchmark directory (optional)" value={benchmark} onChange={event => setBenchmark(event.target.value)} placeholder="Path to the frozen benchmark" />
      </Field>}
      <div className="flex items-center gap-3">
        <Button variant="ghost" loading={busy} disabled={!clean} onClick={() => void verify()}>Check the file</Button>
        <span className="text-[0.74rem] text-faint">Nothing is imported by checking.</span>
      </div>
      {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}

      {checked && (
        <div className="space-y-2 text-[0.82rem]" aria-label="Result of the check">
          <p className={`font-medium ${checked.ok ? "text-ok" : "text-crit"}`}>
            {checked.ok ? "Every check passed." : `The file was rejected: ${checked.steps.filter((s) => s.state === "failed").length} check${checked.steps.filter((s) => s.state === "failed").length === 1 ? "" : "s"} failed, so nothing can be imported from it.`}
          </p>
          <ul className="space-y-1.5" aria-label="Checks">
            {checked.steps.map((s) => (
              <li key={s.name} className="border-b border-line/60 pb-1.5">
                <div className="flex items-baseline gap-2">
                  <span aria-hidden="true" className={`w-3 shrink-0 font-mono ${TONE[s.state]}`}>{ICON[s.state]}</span>
                  <span className="min-w-0 flex-1"><span className="font-medium">{s.title}</span> <span className={`text-[0.74rem] ${TONE[s.state]}`}>{MARK[s.state]}</span></span>
                </div>
                {s.state === "failed" && (
                  <div className="ml-5 mt-1 space-y-1">
                    <ul className="list-disc space-y-0.5 pl-4 text-muted">{s.failures.map((f, i) => <li key={i}>{f}</li>)}</ul>
                    {s.fix && <p className="text-[0.78rem]"><b className="font-medium">What to do:</b> {s.fix}</p>}
                    <p className="text-[0.74rem] text-faint">This check looks at: {s.what}</p>
                  </div>)}
              </li>))}
          </ul>
          {checked.ok && <Summary check={checked} />}
          {checked.ok && checked.already_imported && <p className="text-warn">This exact file was imported before; importing it again changes nothing.</p>}
        </div>)}

      {checked?.ok && (
        <RunConsole stage={k.stage} label={k.button} params={{ run, path: clean, ...(benchmarkPath ? {evaluation_dataset_dir: benchmarkPath} : {}) }} onDone={onDone} hint={k.after} />)}
    </div>
  );
}
