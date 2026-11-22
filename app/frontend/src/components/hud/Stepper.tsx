import { cn } from "../../lib/cn";

// The steps of a guided task. The current step carries aria-current="step"; a step that has been reached can be returned to, one that has not cannot
// be skipped to. Done and optional are said in words as well as shown, so nothing depends on colour.

export type StepItem = { id: string; label: string; optional?: boolean };

export function Stepper({ steps, current, reached, onSelect }: { steps: StepItem[]; current: number; reached: number; onSelect: (index: number) => void }) {
  return (
    <nav aria-label="Steps">
      <ol className="flex flex-wrap gap-x-2 gap-y-1">
        {steps.map((s, i) => {
          const state = i === current ? "current" : i < current ? "done" : i <= reached ? "visited" : "todo";
          return (
            <li key={s.id} className="flex items-center gap-2">
              <button type="button" disabled={i > reached} onClick={() => onSelect(i)} aria-current={i === current ? "step" : undefined}
                className={cn("flex items-center gap-2 border px-3 py-1.5 text-[0.8rem] outline-none transition-colors focus-visible:ring-1 focus-visible:ring-brand-strong",
                  state === "current" ? "border-brand bg-brand-tint text-fg" : "border-line text-muted",
                  i <= reached && state !== "current" && "hover:border-line-strong hover:text-fg", i > reached && "cursor-not-allowed opacity-60")}>
                <span aria-hidden className="tnum grid h-5 w-5 place-items-center border border-line-strong text-[0.68rem]">{i + 1}</span>
                <span>{s.label}</span>
                {s.optional && <span className="text-[0.68rem] text-faint">(optional)</span>}
                {state === "done" && <span className="sr-only"> (done)</span>}
              </button>
              {i < steps.length - 1 && <span aria-hidden className="h-px w-4 bg-line-strong" />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
