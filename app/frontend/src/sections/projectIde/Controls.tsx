import * as Select from "@radix-ui/react-select";
import { Children, isValidElement, type ChangeEvent, type ReactNode } from "react";
import { ChevronDown, Check } from "lucide-react";
export { Input, Textarea } from "../../components/kit/TextField";
export { Checkbox, Radio } from "../../components/kit/Toggles";

// Keep selectors inside the workbench and use the application's shared tokens.
export function IdeSelect({ value, onChange, children, disabled, "aria-label": label }: {
  value: string; onChange: (event: ChangeEvent<HTMLSelectElement>) => void; children: ReactNode; disabled?: boolean; "aria-label"?: string;
}) {
  const options = Children.toArray(children).filter(isValidElement<{ value: string; disabled?: boolean; children: ReactNode }>);
  return <Select.Root value={value || "__ide_empty"} disabled={disabled} onValueChange={next => onChange({ target: { value: next === "__ide_empty" ? "" : next } } as ChangeEvent<HTMLSelectElement>)}>
    <Select.Trigger className="ide-select" aria-label={label}><Select.Value placeholder="Select" /><Select.Icon><ChevronDown size={13} /></Select.Icon></Select.Trigger>
    <Select.Content className="ide-select-popup" position="popper" sideOffset={4} avoidCollisions><Select.Viewport>{options.map((option, index) => <Select.Item className="ide-select-option" key={`${option.props.value}-${index}`} value={option.props.value || "__ide_empty"} disabled={option.props.disabled}><Select.ItemText>{option.props.children}</Select.ItemText><Select.ItemIndicator><Check size={12} /></Select.ItemIndicator></Select.Item>)}</Select.Viewport></Select.Content>
  </Select.Root>;
}
