import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Panel } from "../components/ui";
import { SelectField, TextField } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { api } from "../lib/api";
import { pct } from "../lib/format";
import { useOnActivate } from "../components/ActivePage";

// Evaluating the model that is set up in Settings on one of the two older tasks: the severity of a described vulnerability, or whether a piece of code is vulnerable. One panel, one set
// of choices (the split, how many items, the model under test); each run also writes its answers as attempts and is scored by the shared evaluator, so it can be compared with another run
// of the same kind on the Results tab, with the same paired test as the fine-tuning controls.

type Task = "classify" | "detect";
const TASKS: { id: Task; label: string; about: string }[] = [
  { id: "classify", label: "Severity of a described vulnerability", about: "The model reads an advisory (and the vulnerable code when there is some) and names its severity: Recommendation, Warning or Critical. Scored on accuracy, macro F1 and how many Critical ones it missed; an unanswered item counts as wrong." },
  { id: "detect", label: "Is this code vulnerable?", about: "The model reads one function and answers vulnerable or safe (prompt detect-v1, the one the detection notebook trains on). The detection set holds the vulnerable function, its fixed twin and untouched functions beside it. Scored on recall and on false alarms on safe code." },
];
const SPLITS = [["val", "validation"], ["train", "train"], ["all", "all rows"]];                // there is no test split
const FIELD = "h-9 rounded-lg border border-line bg-surface-2 px-3 text-[0.85rem] outline-none focus:border-brand";

type Run = Record<string, any>;
const kindOf = (m: Run): Task | null => (m.task === "detection" ? "detect" : typeof m.macro_f1 === "number" && m.shared ? "classify" : null);       // only the runs that the shared evaluator also scored
const find = (m: Run): string => {
  if (kindOf(m) === "detect") return `recall ${pct(m.recall)}, false alarms on safe code ${pct(m.false_positive_rate_safe)}, answered ${pct(m.coverage)}`;
  return `accuracy ${pct(m.accuracy)}, macro F1 ${m.macro_f1.toFixed(2)}, Critical missed ${m.critical_missed ?? "?"} of ${m.critical_total ?? "?"}, answered ${pct(m.coverage)}`;
};

export function Evaluate() {
  const [task, setTask] = useState<Task>("classify");
  const [split, setSplit] = useState("val");
  const [n, setN] = useState(50);
  const [thr, setThr] = useState("");
  const [model, setModel] = useState<string | null>(null);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const load = () => {
    api.getSettings().then((s) => setModel(s.llm_model || null)).catch(() => setModel(null));          // ignored: the model line then says it is not configured
    api.experiments().then((r) => setRuns(r as Run[])).catch(() => setRuns([]));                       // ignored: the list of earlier runs is a help; the panel still runs
  };
  useEffect(load, []);
  useOnActivate(load);

  const t = thr.trim() === "" ? null : Number(thr);
  const bad = task === "detect" && t !== null && !(t >= 0 && t <= 1);
  const params = task === "classify" ? { split, n } : { split, n, threshold: t };
  const recent = (runs ?? []).filter((m) => kindOf(m) === task).slice(-8).reverse();
  const current = TASKS.find((x) => x.id === task)!;

  return (
    <div className="space-y-5">
      <Panel title="Evaluate the model under test" desc="Runs the language model set up in Settings (or served on the Model Hub) over a sample of one task and scores it with the shared evaluator. Compare the original model with a fine-tuned one by running each on the same split and sample.">
        <div className="space-y-4">
          <div role="radiogroup" aria-label="What to evaluate" className="flex flex-wrap gap-2">
            {TASKS.map((x) => (
              <button key={x.id} type="button" role="radio" aria-checked={task === x.id} onClick={() => setTask(x.id)}
                className={`border px-3 py-1.5 text-[0.82rem] ${task === x.id ? "border-brand bg-brand-tint text-fg" : "border-line text-muted hover:text-fg"}`}>{x.label}</button>))}
          </div>
          <p className="text-[0.8rem] text-muted">{current.about}</p>
          <div className="rounded-lg border border-line bg-surface-2/50 px-3 py-2 text-[0.82rem]">
            <span className="text-muted">Model under test: </span>
            <b className="font-mono font-medium">{model || "not configured (Settings, language model)"}</b>
            <span className="ml-2 text-[0.74rem] text-faint">Change it in Settings, or serve a model on the Model Hub.</span>
          </div>
          <div className="flex flex-wrap items-end gap-4">
            <SelectField label="Split" aria-label="Split" value={split} onValueChange={setSplit} rowClassName="w-44" options={SPLITS.map(([v, l]) => ({ value: v, label: l }))} />
            <TextField label="Samples (0 = all)" aria-label="Samples" type="number" min={0} value={n} onChange={(e) => setN(Number(e.target.value))} rowClassName="w-32" inputClassName="tnum" />
            {task === "detect" && (
              <TextField label="Threshold (optional)" aria-label="Threshold" value={thr} onChange={(e) => setThr(e.target.value)} placeholder="from detector_config.json" rowClassName="w-56"
                inputClassName="tnum" invalid={bad} />
            )}
          </div>
          {bad && <p role="alert" className="text-[0.78rem] text-crit-soft">The threshold is a probability from 0 to 1.</p>}
          <RunConsole key={task} stage={task} params={params} label={task === "classify" ? "Run classification" : "Run detection"} disabled={bad} onDone={load}
            hint={task === "classify" ? "Streams per-sample progress, then writes metrics and the attempts to experiments/<model> (a new folder if it exists)."
              : "Uses P(vulnerable) when the endpoint returns log-probabilities, else the model's word. Writes experiments/detect_<model> with predictions, metrics, the attempts and run_config.json."} />
        </div>
      </Panel>

      <Panel title="Earlier evaluations of this kind" desc="The last runs that the shared evaluator also scored. Open the Results tab to read one in full or to set two side by side.">
        {runs === null ? <p className="text-[0.8rem] text-muted">Reading the runs...</p> : recent.length === 0 ? (
          <p className="text-[0.8rem] text-muted">No run of this kind yet. A run made before the shared evaluator scored these tasks has no attempts and is not listed here; it is still in Results.</p>
        ) : (
          <ul aria-label="Earlier evaluations" className="divide-y divide-line/60">
            {recent.map((m) => (
              <li key={m.tag} className="py-2 text-[0.82rem]">
                <span className="font-mono text-[0.78rem]">{m.tag}</span>
                <span className="ml-3 tnum text-muted">{m.split ? `${m.split}, ` : ""}{m.n ?? m.shared?.n_attempts} items: {find(m)}</span>
              </li>))}
          </ul>)}
        <p className="mt-3 text-[0.76rem] text-faint"><Link className="text-brand-strong hover:underline" to="/train?tab=results">Open the Results tab</Link></p>
      </Panel>
    </div>
  );
}
