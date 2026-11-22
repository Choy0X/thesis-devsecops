import { useState } from "react";
import { Input, Checkbox, Radio } from "./Controls";
import type { Pending } from "./client";
import { friendlyRunError } from "./runError";

export function PlanningQuestion({ request, onReply }: { request: Pending; onReply: (answers: string[][]) => Promise<void> }) {
  const questions = request.questions || [];
  const [choices, setChoices] = useState<string[][]>(questions.map(() => []));
  const [customSelected, setCustomSelected] = useState<boolean[]>(questions.map(() => false));
  const [custom, setCustom] = useState<string[]>(questions.map(() => ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const answers = choices.map((labels, i) => customSelected[i]
    ? [...(questions[i].multiple ? labels : []), ...(custom[i].trim() ? [custom[i].trim()] : [])] : labels);
  return <form className="ide-pending chat-planning-question" aria-label="Planning question" onSubmit={event => {
    event.preventDefault();
    if (busy || answers.some(answer => !answer.length)) return;
    setBusy(true); setError("");
    void onReply(answers).catch(reason => setError(friendlyRunError(reason))).finally(() => setBusy(false));
  }}>
    <strong>Agent needs your input</strong>
    {questions.map((question, i) => {
      const Choice = question.multiple ? Checkbox : Radio;
      const options = question.options || [];
      const recommended = options.findIndex(option => option.recommended);
      return <fieldset key={i} disabled={busy}>
        <legend>{question.question}</legend>
        {options.map((option, index) => <Choice key={option.label} name={`${request.id}-${i}`} label={option.label}
          description={<>{index === (recommended === -1 ? 0 : recommended) && <span className="chat-question-recommended">Recommended</span>}{option.description}</>}
          checked={!!choices[i]?.includes(option.label) && (question.multiple || !customSelected[i])} onChange={event => {
            const checked = event.target.checked;
            if (!question.multiple) setCustomSelected(values => values.map((value, n) => n === i ? false : value));
            setChoices(all => all.map((a, n) => n !== i ? a : question.multiple ? checked ? [...a, option.label] : a.filter(label => label !== option.label) : [option.label]));
          }} />)}
        <Choice label="Write my own answer" name={`${request.id}-${i}`} checked={customSelected[i]} onChange={event => {
          const checked = event.target.checked;
          setCustomSelected(all => all.map((value, n) => n === i ? checked : value));
        }} />
        {customSelected[i] && <Input autoFocus aria-label={`Custom answer for ${question.header || question.question}`} placeholder="Enter your answer" maxLength={2000} value={custom[i]}
          onChange={event => setCustom(all => all.map((value, n) => n === i ? event.target.value : value))} />}
      </fieldset>;
    })}
    {error && <p className="ide-error" role="alert">{error}</p>}
    <button type="submit" className="ide-primary" disabled={busy || answers.some(answer => !answer.length)}>{busy ? "Sending..." : "Send answer"}</button>
  </form>;
}
