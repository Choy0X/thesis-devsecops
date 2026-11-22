import { useState } from "react";
import { Button, Textarea } from "../ui";

// Shown in place of the composer once Plan mode's own ask_question tool pauses the session (pending_call.name === "ask_question"): one or more questions, each with the
// model's own suggested options as buttons plus an always-available "Type my own answer" box, and one Submit once every question has an answer. The answers are sent back as
// edited_arguments on /approve (agent_loop.approve: "approve" runs the real tool with edited_arguments in place of the model's own), not as free text on a denial -- the plan
// session simply continues once answered, the same way an approved write or command does.

export function AskQuestionCard({ questions, busy, onAnswer, onStop }: {
  questions: { question: string; options?: string[] }[];
  busy: boolean;
  onAnswer: (answers: string[]) => void;
  onStop: () => void;
}) {
  const [answers, setAnswers] = useState<string[]>(() => questions.map(() => ""));
  const [typing, setTyping] = useState<boolean[]>(() => questions.map(() => false));
  const setAnswer = (i: number, text: string) => setAnswers((a) => a.map((x, j) => (j === i ? text : x)));
  const setTypingAt = (i: number, v: boolean) => setTyping((t) => t.map((x, j) => (j === i ? v : x)));
  const allAnswered = answers.every((a) => a.trim());
  return (
    <div role="alertdialog" aria-label={questions.length > 1 ? "Questions for your review" : "A question for your review"} className="space-y-4 border border-warn-line bg-warn-tint p-3">
      {questions.map((q, i) => (
        <div key={i} className="space-y-1.5">
          <p className="text-[0.84rem] font-semibold">{q.question}</p>
          <div className="flex flex-wrap gap-2">
            {(q.options ?? []).map((opt) => (
              <Button key={opt} variant={!typing[i] && answers[i] === opt ? "primary" : "ghost"} disabled={busy}
                onClick={() => { setAnswer(i, opt); setTypingAt(i, false); }}>{opt}</Button>))}
            <Button variant={typing[i] ? "primary" : "ghost"} disabled={busy} onClick={() => setTypingAt(i, true)}>Type my own answer</Button>
          </div>
          {typing[i] && (
            <Textarea aria-label={`Your answer to: ${q.question}`} value={answers[i]} rows={2} autoFocus disabled={busy} onChange={(e) => setAnswer(i, e.target.value)} />)}
        </div>
      ))}
      <div className="flex gap-2">
        <Button variant="primary" disabled={busy || !allAnswered} onClick={() => onAnswer(answers)}>Submit answer{questions.length > 1 ? "s" : ""}</Button>
        <Button variant="ghost" disabled={busy} onClick={onStop}>Stop</Button>
      </div>
    </div>
  );
}
