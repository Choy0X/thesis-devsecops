import * as RSelect from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { FormRow, joinIds, useFieldContext } from "./FormRow";

// The select of the kit, on Radix Select: a trigger that looks like the text fields, a list in a chamfered card with the highlighted row framed by a bracket on its left, typeahead and
// full keyboard support. It has the roles a screen reader and the accessibility spec look for (combobox, listbox, option). Radix cannot have an item whose value is "", but a native select
// often does ("Any", "All"), so "" is mapped to a private value inside and back to "" outside: callers never see it.

export type SelectOption = { value: string; label: ReactNode; hint?: ReactNode; disabled?: boolean };
export type SelectGroup = { label: string; options: SelectOption[] };

const EMPTY = "__empty__";
const isGroup = (item: SelectOption | SelectGroup): item is SelectGroup => "options" in item;
const inside = (value: string) => (value === "" ? EMPTY : value);

export type SelectProps = {
  value: string;
  onValueChange: (value: string) => void;
  options: (SelectOption | SelectGroup)[];
  placeholder?: ReactNode;
  /** Why there is nothing to choose, for a list that can be empty (models found on the computer). With no option but an empty one (None, Automatic) the select is disabled instead of opening
   *  an empty list, and a field shows the reason as its hint. Without it a select behaves as before. */
  emptyReason?: ReactNode;
  disabled?: boolean;
  invalid?: boolean;
  id?: string;
  name?: string;
  className?: string;
  "aria-label"?: string;
  "aria-describedby"?: string;
};

/** Whether a list has no real option: nothing but an entry with an empty value (None, Automatic), or nothing at all. */
export const hasNothingToChoose = (options: (SelectOption | SelectGroup)[]) => !options.flatMap((item) => (isGroup(item) ? item.options : [item])).some((o) => o.value !== "");

const triggerCls =
  "chamfer-sm flex h-9 w-full items-center justify-between gap-2 border border-[var(--control-border)] bg-surface-2 px-3 text-left text-[0.85rem] text-fg " +
  "transition-colors duration-fast hover:bg-surface-3 focus-visible:border-brand-strong focus-visible:bg-surface-3 focus-visible:shadow-[inset_0_0_0_1px_var(--brand-strong)] focus-visible:[--ch-line:var(--brand-strong)] " +
  "focus-visible:outline-none data-[state=open]:border-brand-strong data-[state=open]:[--ch-line:var(--brand-strong)] disabled:pointer-events-none disabled:opacity-45 " +
  "data-[invalid=true]:border-crit data-[invalid=true]:[--ch-line:var(--crit)] [&>span:first-child]:truncate";

function Item({ option }: { option: SelectOption }) {
  return (
    <RSelect.Item value={inside(option.value)} disabled={option.disabled} textValue={typeof option.label === "string" ? option.label : option.value}
      className={"relative flex min-h-8 cursor-default select-none items-center gap-2 border-l-2 border-transparent py-1 pl-2.5 pr-2 text-[0.82rem] text-muted outline-none focus-visible:outline-none " +
        "data-[highlighted]:border-brand-strong data-[highlighted]:bg-brand-tint data-[highlighted]:text-fg data-[state=checked]:text-fg data-[disabled]:opacity-45"}>
      <RSelect.ItemText>{option.label}</RSelect.ItemText>
      <span className="ml-auto flex shrink-0 items-center gap-2 pl-3">
        {option.hint != null && <span className="font-mono text-[0.7rem] text-faint">{option.hint}</span>}
        <span className="grid w-4 place-items-center text-brand-strong"><RSelect.ItemIndicator><Check size={14} aria-hidden /></RSelect.ItemIndicator></span>
      </span>
    </RSelect.Item>
  );
}

/** A select on its own (no label), for filters and compact rows. Inside a FormRow it takes the row's id and its hint and error links. */
export function Select({ value, onValueChange, options, placeholder = "Select", emptyReason, disabled, invalid, id, name, className, "aria-label": ariaLabel, "aria-describedby": describedBy }: SelectProps) {
  const field = useFieldContext();
  const bad = invalid ?? field.invalid;
  const flat = options.flatMap((item) => (isGroup(item) ? item.options : [item]));
  const unset = value === "" && !flat.some((o) => o.value === "");        // "" with no "" option: show the placeholder
  const nothing = emptyReason != null && emptyReason !== false && hasNothingToChoose(options);
  return (
    <RSelect.Root value={unset ? undefined : inside(value)} onValueChange={(v) => onValueChange(v === EMPTY ? "" : v)} disabled={disabled || nothing} name={name}>
      <RSelect.Trigger id={id ?? field.id} aria-label={ariaLabel} aria-invalid={bad || undefined} aria-describedby={joinIds(describedBy, field.describedBy)}
        data-invalid={bad || undefined} data-empty={nothing || undefined} title={nothing && typeof emptyReason === "string" ? emptyReason : undefined} className={cn(triggerCls, className)}>
        <RSelect.Value placeholder={<span className="text-faint">{nothing ? "Nothing to choose" : placeholder}</span>} />
        <RSelect.Icon className="shrink-0 text-faint"><ChevronDown size={15} aria-hidden /></RSelect.Icon>
      </RSelect.Trigger>
      <RSelect.Portal>
        <RSelect.Content position="popper" sideOffset={4} align="start"
          className="chamfer-sm z-toast max-h-[min(22rem,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)] overflow-hidden border border-line-strong bg-surface-2 shadow-pop">
          <RSelect.Viewport className="p-1">
            {options.map((item, i) => isGroup(item) ? (
              <RSelect.Group key={`g${i}`}>
                <RSelect.Label className="caps px-2.5 pb-1 pt-2 text-faint">{item.label}</RSelect.Label>
                {item.options.map((o) => <Item key={o.value} option={o} />)}
              </RSelect.Group>
            ) : <Item key={item.value} option={item} />)}
          </RSelect.Viewport>
        </RSelect.Content>
      </RSelect.Portal>
    </RSelect.Root>
  );
}

export type SelectFieldProps = SelectProps & { label: ReactNode; hint?: ReactNode; error?: ReactNode; action?: ReactNode; required?: boolean; rowClassName?: string };

/** A labelled select: the label, the select, a hint and an error, tied together by ids. */
export function SelectField({ label, hint, error, action, required, rowClassName, id, ...select }: SelectFieldProps) {
  const nothing = select.emptyReason != null && select.emptyReason !== false && hasNothingToChoose(select.options);
  return (
    <FormRow label={label} hint={nothing ? select.emptyReason : hint} error={error} action={action} required={required} id={id} className={rowClassName}>
      <Select {...select} />
    </FormRow>
  );
}
