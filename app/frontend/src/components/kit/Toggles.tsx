import { Check } from "lucide-react";
import { forwardRef, useId, type InputHTMLAttributes, type ReactElement, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Tooltip } from "../hud/Tooltip";

// Switch, Checkbox and RadioGroup. Each is a real native input (so a form, a screen reader, the keyboard and a test all see exactly what they expect: role switch, checkbox, radio, a group
// with a legend, arrow keys between radios) laid invisibly over a drawn shape. The shape is a sibling that reacts to the input through Tailwind's `peer`, so there is no state to keep in
// step. The shapes follow the HUD vocabulary: a chamfered track, a square box, a diamond. Focus is drawn inside the shape (a chamfer or a clip-path would cut an outside ring).

type NativeProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size" | "prefix">;
type Labelled = { label?: ReactNode; description?: ReactNode };

const row = "flex w-fit cursor-pointer items-start gap-3 has-[:disabled]:cursor-not-allowed";
const ring = "peer-focus-visible:shadow-[inset_0_0_0_2px_var(--fg)]";

// A `title` is shown as a tooltip on the whole row, on hover and when the input has the focus (focus events bubble to the row), instead of as a native title.
function Hinted({ title, children }: { title?: string; children: ReactElement }) {
  return title ? <Tooltip label={title}>{children}</Tooltip> : children;
}

// The input is named by the label text alone (aria-labelledby) and described by the description: a wrapping <label> would make the description part of the name.
function Text({ label, description, descId, labelId, disabled }: Labelled & { descId?: string; labelId?: string; disabled?: boolean }) {
  if (label == null && description == null) return null;
  return (
    <span className={cn("min-w-0", disabled && "opacity-45")}>
      {label != null && <span id={labelId} className="block text-[0.85rem] leading-5 text-fg">{label}</span>}
      {description != null && <span id={descId} className="block text-[0.76rem] leading-snug text-faint">{description}</span>}
    </span>
  );
}

export type SwitchProps = NativeProps & Labelled & { onCheckedChange?: (checked: boolean) => void };

/** An on/off setting that takes effect as set. A native checkbox with role="switch". */
export const Switch = forwardRef<HTMLInputElement, SwitchProps>(function Switch({ label, description, onCheckedChange, onChange, className, disabled, title, "aria-describedby": described, ...rest }, ref) {
  const descId = useId();
  const labelId = useId();
  return (
    <Hinted title={title}><label className={cn(row, className)}>
      <span className="relative mt-px inline-block h-5 w-10 shrink-0">
        <input {...rest} ref={ref} type="checkbox" role="switch" disabled={disabled} aria-labelledby={label != null ? labelId : undefined}
          aria-describedby={[described, description != null ? descId : ""].filter(Boolean).join(" ") || undefined}
          onChange={(e) => { onChange?.(e); onCheckedChange?.(e.target.checked); }} className="peer absolute inset-0 z-10 m-0 h-full w-full cursor-[inherit] opacity-0" />
        <span aria-hidden className={cn("chamfer-sm pointer-events-none absolute inset-0 border border-[var(--control-border)] bg-surface-2 transition-colors duration-fast peer-checked:border-brand-strong peer-checked:bg-brand-tint-strong peer-checked:[--ch-line:var(--brand-strong)] peer-disabled:opacity-45", ring)} />
        <span aria-hidden className="pointer-events-none absolute left-[3px] top-[3px] h-3.5 w-3.5 bg-faint transition-transform duration-fast peer-checked:translate-x-5 peer-checked:bg-brand-strong peer-disabled:opacity-45" />
      </span>
      <Text label={label} description={description} descId={descId} labelId={labelId} disabled={disabled} />
    </label></Hinted>
  );
});

export type CheckboxProps = NativeProps & Labelled & { onCheckedChange?: (checked: boolean) => void };

/** A yes or no that is read later, with a Save (a switch is for what takes effect at once). */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox({ label, description, onCheckedChange, onChange, className, disabled, title, "aria-describedby": described, ...rest }, ref) {
  const descId = useId();
  const labelId = useId();
  return (
    <Hinted title={title}><label className={cn(row, "gap-2.5", className)}>
      <span className="relative mt-0.5 inline-block h-4 w-4 shrink-0">
        <input {...rest} ref={ref} type="checkbox" disabled={disabled} aria-labelledby={label != null ? labelId : undefined}
          aria-describedby={[described, description != null ? descId : ""].filter(Boolean).join(" ") || undefined}
          onChange={(e) => { onChange?.(e); onCheckedChange?.(e.target.checked); }} className="peer absolute inset-0 z-10 m-0 h-full w-full cursor-[inherit] opacity-0" />
        <span aria-hidden className={cn("pointer-events-none absolute inset-0 border border-[var(--control-border)] bg-surface-2 transition-colors duration-fast peer-checked:border-brand-strong peer-checked:bg-brand peer-disabled:opacity-45", ring)} />
        <Check aria-hidden size={12} strokeWidth={3} className="pointer-events-none absolute left-0.5 top-0.5 text-on-brand opacity-0 peer-checked:opacity-100" />
      </span>
      <Text label={label} description={description} descId={descId} labelId={labelId} disabled={disabled} />
    </label></Hinted>
  );
});

export type RadioProps = NativeProps & Labelled & { onCheckedChange?: (checked: boolean) => void };

const DIAMOND_SHAPE = "[clip-path:polygon(50%_0,100%_50%,50%_100%,0_50%)]";

/** One radio on its own, for a choice laid out by the page (a row of a table, a card with more than a label). A native radio, so a group of them with one `name` moves with the arrow
 *  keys; give it a `label`, or an `aria-label` when the page's own text names it. */
export const Radio = forwardRef<HTMLInputElement, RadioProps>(function Radio({ label, description, onCheckedChange, onChange, className, disabled, title, "aria-describedby": described, ...rest }, ref) {
  const descId = useId();
  const labelId = useId();
  const mark = (
    <span className="relative mt-0.5 inline-block h-4 w-4 shrink-0">
      <input {...rest} ref={ref} type="radio" disabled={disabled} aria-labelledby={label != null ? labelId : undefined}
        aria-describedby={[described, description != null ? descId : ""].filter(Boolean).join(" ") || undefined}
        onChange={(e) => { onChange?.(e); onCheckedChange?.(e.target.checked); }} className="peer absolute inset-0 z-10 m-0 h-full w-full cursor-[inherit] opacity-0" />
      <span aria-hidden className={cn("pointer-events-none absolute inset-0 bg-[var(--control-border)] transition-colors duration-fast peer-checked:bg-brand-strong peer-focus-visible:bg-fg peer-disabled:opacity-45", DIAMOND_SHAPE)} />
      <span aria-hidden className={cn("pointer-events-none absolute inset-[2px] bg-surface-2", DIAMOND_SHAPE)} />
      <span aria-hidden className={cn("pointer-events-none absolute inset-[5px] bg-brand-strong opacity-0 peer-checked:opacity-100", DIAMOND_SHAPE)} />
    </span>
  );
  if (label == null && description == null) return <Hinted title={title}><span className={cn("inline-flex", className)}>{mark}</span></Hinted>;
  return (
    <Hinted title={title}><label className={cn(row, "gap-2.5", className)}>
      {mark}
      <Text label={label} description={description} descId={descId} labelId={labelId} disabled={disabled} />
    </label></Hinted>
  );
});

export type RadioOption = { value: string; label: ReactNode; description?: ReactNode; disabled?: boolean };

export type RadioGroupProps = {
  /** The name of the group, read out by a screen reader. */
  legend: ReactNode;
  value: string;
  onValueChange: (value: string) => void;
  options: RadioOption[];
  /** "list" is a column of diamonds with text; "segmented" is a row of chamfered choices, for a small either-or such as Local or External. */
  variant?: "list" | "segmented";
  hint?: ReactNode;
  disabled?: boolean;
  /** Hides the legend visually (it is still the group's name). */
  hideLegend?: boolean;
  className?: string;
};

const DIAMOND = "[clip-path:polygon(50%_0,100%_50%,50%_100%,0_50%)]";

/** A choice of one among a few, all visible. Native radios with one name, so the arrow keys move between them. */
export function RadioGroup({ legend, value, onValueChange, options, variant = "list", hint, disabled, hideLegend, className }: RadioGroupProps) {
  const name = useId();
  const hintId = useId();
  return (
    <fieldset role="radiogroup" aria-describedby={hint ? hintId : undefined} disabled={disabled} className={cn("min-w-0 border-0 p-0", className)}>
      <legend className={cn("caps mb-1.5 p-0 text-muted", hideLegend && "sr-only")}>{legend}</legend>
      <div className={variant === "segmented" ? "inline-flex flex-wrap gap-1.5" : "flex flex-col gap-2.5"}>
        {options.map((o, i) => variant === "segmented" ? (
          <label key={o.value} className="relative inline-flex cursor-pointer has-[:disabled]:cursor-not-allowed">
            <input type="radio" name={name} value={o.value} checked={value === o.value} disabled={o.disabled} onChange={() => onValueChange(o.value)}
              className="peer absolute inset-0 z-10 m-0 h-full w-full cursor-[inherit] opacity-0" />
            <span className={cn("chamfer-sm inline-flex h-9 items-center border border-line-strong bg-surface-2 px-4 text-[0.74rem] font-semibold uppercase tracking-[0.07em] text-muted transition-colors duration-fast",
              "[--ch-line:var(--border-strong)] hover:bg-surface-3 hover:text-fg peer-checked:border-brand-strong peer-checked:bg-brand-tint-strong peer-checked:text-fg peer-checked:[--ch-line:var(--brand-strong)]",
              "peer-disabled:opacity-45", ring)}>{o.label}</span>
          </label>
        ) : (
          <label key={o.value} className={cn(row, "gap-2.5")}>
            <span className="relative mt-0.5 inline-block h-4 w-4 shrink-0">
              <input type="radio" name={name} value={o.value} checked={value === o.value} disabled={o.disabled} onChange={() => onValueChange(o.value)}
                aria-labelledby={`${name}-l${i}`} aria-describedby={o.description != null ? `${name}-d${i}` : undefined}
                className="peer absolute inset-0 z-10 m-0 h-full w-full cursor-[inherit] opacity-0" />
              <span aria-hidden className={cn("pointer-events-none absolute inset-0 bg-[var(--control-border)] transition-colors duration-fast peer-checked:bg-brand-strong peer-focus-visible:bg-fg peer-disabled:opacity-45", DIAMOND)} />
              <span aria-hidden className={cn("pointer-events-none absolute inset-[2px] bg-surface-2", DIAMOND)} />
              <span aria-hidden className={cn("pointer-events-none absolute inset-[5px] bg-brand-strong opacity-0 peer-checked:opacity-100", DIAMOND)} />
            </span>
            <Text label={o.label} description={o.description} labelId={`${name}-l${i}`} descId={`${name}-d${i}`} disabled={o.disabled} />
          </label>
        ))}
      </div>
      {hint && <p id={hintId} className="mt-1.5 text-[0.74rem] leading-snug text-faint">{hint}</p>}
    </fieldset>
  );
}
