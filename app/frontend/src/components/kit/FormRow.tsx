import { createContext, useContext, useId, useMemo, type ReactNode } from "react";
import { cn } from "../../lib/cn";

// A labelled place for one control: the label, the control, a hint and an error. The label is a real <label htmlFor>, NOT a wrapper around the control. A wrapping label sends a click
// on its text to the first labelable thing inside it, and when that thing is a button (the old "remove" of a secret field) the click pressed the button. Here a click on the label focuses
// the control and nothing else. The controls of the kit (Input, Textarea, NumberInput, SelectField, SecretField) read the id and the described-by list from this context.
//
// Inside a SettingsCard the same field is a ROW: the name and the explanation on the left, the control on the right, in the same two columns as every other row of every card, so the controls
// of a page share one vertical line (RowsContext, set by SettingsCard; the sizes of the two columns are SETTING_ROW below and are used by SettingRow and SettingFacts too).

export type FieldContextValue = { id?: string; describedBy?: string; invalid?: boolean };
export const FieldContext = createContext<FieldContextValue>({});
export const useFieldContext = () => useContext(FieldContext);

/** True inside a SettingsCard: a FormRow is then drawn as a row of two columns. */
export const RowsContext = createContext(false);

/** The grid of a row of a settings card: the name and its explanation, then the control. Below the large breakpoint the two stack (the page then has a strip for a menu, not a column). */
export const SETTING_ROW = "grid gap-x-8 gap-y-2 px-5 py-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]";

/** Joins the ids a control is described by, leaving out what is empty. */
export const joinIds = (...ids: (string | undefined | false | null)[]): string | undefined => ids.filter(Boolean).join(" ") || undefined;

export function FormRow({ label, hint, error, id, action, required, className, children }: {
  label: ReactNode; hint?: ReactNode; error?: ReactNode; id?: string;
  /** Something small on the label line, at the right (a Discover button, a Show link). */
  action?: ReactNode; required?: boolean; className?: string; children: ReactNode;
}) {
  const auto = useId();
  const rows = useContext(RowsContext);
  const fid = id ?? auto;
  const hintId = hint ? `${fid}-hint` : undefined;
  const errorId = error ? `${fid}-error` : undefined;
  const describedBy = joinIds(hintId, errorId);
  const value = useMemo<FieldContextValue>(() => ({ id: fid, describedBy, invalid: !!error }), [fid, describedBy, error]);
  if (rows) {
    return (
      <div className={cn(SETTING_ROW, className)}>
        <div className="min-w-0 lg:pt-2">
          <div className="flex flex-wrap items-baseline gap-x-3">
            <label htmlFor={fid} className="text-[0.85rem] font-medium leading-5 text-fg">{label}{required && <span aria-hidden className="text-crit-soft"> *</span>}</label>
            {action}
          </div>
          {hint && <p id={hintId} className="mt-1 text-[0.76rem] leading-snug text-faint">{hint}</p>}
        </div>
        <div className="min-w-0">
          <FieldContext.Provider value={value}>{children}</FieldContext.Provider>
          {error && <p id={errorId} role="alert" className="mt-1.5 text-[0.76rem] leading-snug text-crit-soft">{error}</p>}
        </div>
      </div>
    );
  }
  return (
    <div className={cn("block", className)}>
      <div className="mb-1.5 flex min-h-5 items-end justify-between gap-2">
        <label htmlFor={fid} className="caps text-muted">{label}{required && <span aria-hidden className="text-crit-soft"> *</span>}</label>
        {action}
      </div>
      <FieldContext.Provider value={value}>{children}</FieldContext.Provider>
      {hint && <p id={hintId} className="mt-1.5 text-[0.74rem] leading-snug text-faint">{hint}</p>}
      {error && <p id={errorId} role="alert" className="mt-1.5 text-[0.76rem] leading-snug text-crit-soft">{error}</p>}
    </div>
  );
}
