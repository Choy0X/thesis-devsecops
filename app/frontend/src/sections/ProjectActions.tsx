import { useId, type ReactNode } from "react";
import { Radio } from "../components/kit";

// What to do with a project, and how: three actions (chat, scan dependencies, scan codebase) and, inside the two scans, the mode. Each choice is a card that says in a sentence what it does
// and what it does not, as real radio buttons so the arrow keys, the screen reader and the form behave as people expect. Agentic mode is not its own action here: it is a tab inside
// Chat's own conversation view (one locked-at-first-message mode per conversation), not a separate entry point.

export type Choice = { id: string; label: string; text: string };

export const ACTIONS: Choice[] = [
  { id: "chat", label: "Chat", text: "Ask about this project, or let the assistant plan, or write and run commands in a sandboxed copy of it -- two tabs of the one conversation. Agentic mode never touches the real files; you review and apply what it did." },
  { id: "dependencies", label: "Scan dependencies", text: "Are the packages the project uses known to be vulnerable, and are they up to date? Your own code is not analysed here." },
  { id: "codebase", label: "Scan codebase", text: "Read the project's own code, tests included, and say what each check found. The dependencies are not checked here." },
];

export const DEPENDENCY_MODES: Choice[] = [
  { id: "only", label: "Dependencies only", text: "Check the declared or installed packages against the known advisories and ask the registries for newer releases. Nothing is changed and nothing is run." },
  { id: "update", label: "Dependencies and code check", text: "As above; then apply the updates on a copy, check the code again, and offer the changes as a patch candidate for you to review. The real folder changes only after you confirm the list of files." },
];

export const CODEBASE_DEPTHS: Choice[] = [
  { id: "quick", label: "Quick: rules only", text: "Pattern rules read the files on this computer. No Docker needed. It finds places to look, not proof." },
  { id: "full", label: "Full: in the sandbox", text: "The rules, then the approved tools, the project's own tests and, if you say how it starts, a few safe questions on its own loopback, all inside a closed container on a copy. Needs Docker and the tool image." },
];

export function ChoiceCards({ legend, choices, value, onChange, children }: { legend: string; choices: Choice[]; value: string; onChange: (id: string) => void; children?: ReactNode }) {
  const name = useId();
  return (
    <fieldset className="space-y-2">
      <legend className="mb-1 text-[0.8rem] font-medium">{legend}</legend>
      <div className={`grid gap-2 ${choices.length === 3 ? "sm:grid-cols-3" : "sm:grid-cols-2"}`}>
        {choices.map((c) => (
          <label key={c.id} className={`block cursor-pointer border p-3 text-left transition-colors focus-within:ring-1 focus-within:ring-brand ${value === c.id ? "border-brand bg-brand-tint" : "border-line bg-surface-2 hover:border-line-strong"}`}>
            <Radio name={name} value={c.id} checked={value === c.id} onChange={() => onChange(c.id)} className="sr-only" />
            <span className="block text-[0.88rem] font-semibold">{c.label}</span>
            <span className="mt-1 block text-[0.76rem] leading-snug text-muted">{c.text}</span>
          </label>))}
      </div>
      {children}
    </fieldset>
  );
}
