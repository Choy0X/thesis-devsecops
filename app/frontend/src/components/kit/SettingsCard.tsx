import { useId, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { RowsContext, SETTING_ROW } from "./FormRow";

// The frame of a group of settings, and what goes in it. A card is one concern (the language model, the NVD key, the models folder): a header with the name, a sentence that says what it is,
// and one small action on the name's line (Look again, Refresh); then rows, one under the other, each a setting; then, when there is something to press, a footer with the buttons.
// Every card is as wide as the column, so no card is bigger than the next, and every row has the same two columns (SETTING_ROW): the name and the explanation on the left, the control on
// the right. A kit field (TextField, SelectField, SecretField, NumberField) inside a card becomes such a row by itself (FormRow reads RowsContext); SettingRow is for what is not a field
// (a switch, a choice of engine, a button), SettingFacts for the read-only facts, SettingNote for a sentence that belongs to the card.

export function SettingsCard({ title, desc, action, footer, children, className, id, as: Heading = "h3" }: {
  /** The level of the title: a card sits under the heading of its group. */
  as?: "h2" | "h3";
  title: ReactNode; desc?: ReactNode;
  /** One small control on the title's line (an icon button with a tooltip). */
  action?: ReactNode;
  /** The buttons of the card: Save, Test. */
  footer?: ReactNode;
  children: ReactNode; className?: string; id?: string;
}) {
  const titleId = useId();
  return (
    <section id={id} aria-labelledby={titleId} className={cn("hud-frame chamfer-r w-full border border-line bg-surface shadow-card [--ch-line:var(--border-strong)]", className)}>
      <header className="flex items-start gap-4 border-b border-line bg-surface-2 px-5 py-3">
        <div className="min-w-0 flex-1">
          <Heading id={titleId} className="flex items-center gap-2.5 text-[0.92rem] font-semibold tracking-[-0.01em]"><span aria-hidden className="h-1.5 w-1.5 shrink-0 rotate-45 bg-brand-strong" />{title}</Heading>
          {desc && <p className="mt-1 max-w-[70ch] text-[0.8rem] leading-snug text-muted">{desc}</p>}
        </div>
        {action && <div className="-my-1 shrink-0">{action}</div>}
      </header>
      <RowsContext.Provider value><div className="divide-y divide-line">{children}</div></RowsContext.Provider>
      {footer && <div className="flex flex-wrap items-center gap-2 border-t border-line px-5 py-3">{footer}</div>}
    </section>
  );
}

/** One setting that is not a field of the kit: its name and explanation on the left, the control (a switch, a choice, a button) on the right. */
export function SettingRow({ label, hint, action, children, className, tight }: {
  label: ReactNode; hint?: ReactNode; action?: ReactNode; children: ReactNode; className?: string;
  /** For a control that is as tall as the name (a switch): the name is not lowered to the middle of a 36 px control. */
  tight?: boolean;
}) {
  return (
    <div className={cn(SETTING_ROW, className)}>
      <div className={cn("min-w-0", !tight && "lg:pt-2")}>
        <div className="flex flex-wrap items-baseline gap-x-3"><span className="text-[0.85rem] font-medium leading-5 text-fg">{label}</span>{action}</div>
        {hint && <p className="mt-1 text-[0.76rem] leading-snug text-faint">{hint}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** Read-only facts in the two columns of a row: write `<dt>` and `<dd>` pairs inside. The values line up with the controls above and below. */
export function SettingFacts({ children, label, className }: { children: ReactNode; label?: string; className?: string }) {
  return <dl aria-label={label} className={cn("grid gap-x-8 gap-y-1.5 px-5 py-4 text-[0.8rem] lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] [&>dt]:text-muted [&>dd]:min-w-0 [&>dd]:break-words", className)}>{children}</dl>;
}

/** A sentence or a status that belongs to the card, across the full width. */
export function SettingNote({ children, className, ...rest }: { children: ReactNode; className?: string } & HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={cn("px-5 py-3 text-[0.8rem] text-muted", className)}>{children}</div>;
}
