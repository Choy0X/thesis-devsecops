import { forwardRef, useRef, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from "react";
import { Minus, Plus } from "lucide-react";
import { cn } from "../../lib/cn";
import { FormRow, joinIds, useFieldContext } from "./FormRow";
import { IconButton } from "./IconButton";

// Text controls. The frame (a chamfered box with a hairline) is a div and the control inside it has no border of its own, so the frame can hold a prefix, a suffix and buttons.
// Focus is shown on the frame: the line turns brand red and an inset line doubles it (a clipped shape would cut an outline outside it). `className` goes to the frame, which is
// what a caller wants for a width; `inputClassName` goes to the control. An error turns the frame critical red and the message below says why, in words.

export const frameCls =
  "chamfer-sm flex min-h-9 w-full items-stretch self-start border border-[var(--control-border)] bg-surface-2 text-fg transition-colors duration-fast " +
  "focus-within:border-brand-strong focus-within:bg-surface-3 focus-within:shadow-[inset_0_0_0_1px_var(--brand-strong)] focus-within:[--ch-line:var(--brand-strong)] " +
  "has-[:disabled]:opacity-45 data-[invalid=true]:border-crit data-[invalid=true]:[--ch-line:var(--crit)]";
const controlCls = "min-w-0 flex-1 border-0 bg-transparent px-3 text-[0.85rem] text-fg outline-none placeholder:text-faint focus-visible:outline-none";
const affixCls = "grid shrink-0 place-items-center px-2.5 font-mono text-[0.76rem] text-faint";

type Common = { prefix?: ReactNode; suffix?: ReactNode; invalid?: boolean; inputClassName?: string; suffixClassName?: string };
export type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "prefix"> & Common;

/** A single-line text control on its own (no label), for filters and compact rows. Inside a FormRow it takes the row's id and its hint and error links. */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { prefix, suffix, invalid, className, inputClassName, suffixClassName, id, "aria-describedby": describedBy, ...rest }, ref,
) {
  const field = useFieldContext();
  const bad = invalid ?? field.invalid;
  return (
    <div className={cn(frameCls, className)} data-invalid={bad || undefined}>
      {prefix != null && <span className={cn(affixCls, "border-r border-line")}>{prefix}</span>}
      <input {...rest} ref={ref} id={id ?? field.id} aria-invalid={bad || undefined} aria-describedby={joinIds(describedBy, field.describedBy)}
        className={cn(controlCls, "h-9", inputClassName)} />
      {suffix != null && <span className={cn(affixCls, "border-l border-line", suffixClassName)}>{suffix}</span>}
    </div>
  );
});

export type TextFieldProps = InputProps & { label: ReactNode; hint?: ReactNode; error?: ReactNode; action?: ReactNode; rowClassName?: string };

/** A labelled text field: the label, the control, a hint and an error, tied together by ids. */
export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(
  { label, hint, error, action, rowClassName, id, required, ...rest }, ref,
) {
  return (
    <FormRow label={label} hint={hint} error={error} action={action} id={id} required={required} className={rowClassName}>
      <Input ref={ref} required={required} {...rest} />
    </FormRow>
  );
});

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean; inputClassName?: string };

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid, className, inputClassName, id, "aria-describedby": describedBy, ...rest }, ref,
) {
  const field = useFieldContext();
  const bad = invalid ?? field.invalid;
  return (
    <div className={cn(frameCls, className)} data-invalid={bad || undefined}>
      <textarea {...rest} ref={ref} id={id ?? field.id} aria-invalid={bad || undefined} aria-describedby={joinIds(describedBy, field.describedBy)}
        className={cn(controlCls, "min-h-[4.5rem] resize-y py-2", inputClassName)} />
    </div>
  );
});

export type TextareaFieldProps = TextareaProps & { label: ReactNode; hint?: ReactNode; error?: ReactNode; action?: ReactNode; rowClassName?: string };

export const TextareaField = forwardRef<HTMLTextAreaElement, TextareaFieldProps>(function TextareaField(
  { label, hint, error, action, rowClassName, id, required, ...rest }, ref,
) {
  return (
    <FormRow label={label} hint={hint} error={error} action={action} id={id} required={required} className={rowClassName}>
      <Textarea ref={ref} required={required} {...rest} />
    </FormRow>
  );
});

/** Sets the value of a controlled input the way a person typing would, so that React's onChange runs (assigning `.value` alone is ignored by React). */
function typeInto(el: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

const decimals = (n: number) => (String(n).split(".")[1] ?? "").length;

export type NumberInputProps = Omit<InputProps, "type" | "prefix" | "suffix"> & { unit?: ReactNode };

/** A number control: the browser's tiny spin arrows are replaced by two real buttons, and the native onChange still fires, so a form can swap its <input type="number"> for this. */
export const NumberInput = forwardRef<HTMLInputElement, NumberInputProps>(function NumberInput(
  { unit, className, disabled, readOnly, min, max, step, ...rest }, forwarded,
) {
  const own = useRef<HTMLInputElement | null>(null);
  const bump = (direction: 1 | -1) => {
    const el = own.current;
    if (!el) return;
    const by = Number(step) > 0 ? Number(step) : 1;
    const from = el.value === "" || Number.isNaN(Number(el.value)) ? (direction > 0 ? Number(min ?? 0) - by : Number(min ?? 0) + by) : Number(el.value);
    let next = from + direction * by;
    if (min !== undefined) next = Math.max(Number(min), next);
    if (max !== undefined) next = Math.min(Number(max), next);
    typeInto(el, next.toFixed(Math.max(decimals(by), decimals(from))));
  };
  const locked = disabled || readOnly;
  const stepper = "chamfer-none h-auto w-8 self-stretch border-0 border-l border-line";
  return (
    <Input {...rest} type="number" min={min} max={max} step={step} disabled={disabled} readOnly={readOnly} className={className}
      ref={(node) => { own.current = node; if (typeof forwarded === "function") forwarded(node); else if (forwarded) forwarded.current = node; }}
      inputClassName="[appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
      suffixClassName="grid-flow-col gap-0 p-0"
      suffix={(
        <>
          {unit != null && <span className="px-2.5">{unit}</span>}
          <IconButton label="Decrease" tip={false} size="sm" disabled={locked} className={stepper} onClick={() => bump(-1)}><Minus size={12} aria-hidden /></IconButton>
          <IconButton label="Increase" tip={false} size="sm" disabled={locked} className={stepper} onClick={() => bump(1)}><Plus size={12} aria-hidden /></IconButton>
        </>
      )} />
  );
});

export type NumberFieldProps = NumberInputProps & { label: ReactNode; hint?: ReactNode; error?: ReactNode; action?: ReactNode; rowClassName?: string };

export const NumberField = forwardRef<HTMLInputElement, NumberFieldProps>(function NumberField(
  { label, hint, error, action, rowClassName, id, required, ...rest }, ref,
) {
  return (
    <FormRow label={label} hint={hint} error={error} action={action} id={id} required={required} className={rowClassName}>
      <NumberInput ref={ref} required={required} {...rest} />
    </FormRow>
  );
});
