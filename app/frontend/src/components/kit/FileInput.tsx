import { forwardRef, type InputHTMLAttributes, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { buttonStyles } from "./Button";
import { joinIds, useFieldContext } from "./FormRow";

// A file chooser. The control is the browser's own <input type="file"> (so the file window, a drop, the keyboard and a test that uploads a file all work), laid invisibly over a button drawn
// like the kit's buttons; the input takes the focus and the button shows it. A page that wants the chosen file's name shows it itself: the input is cleared by the caller after use.

export type FileInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size" | "prefix"> & {
  /** What the button says ("Choose a file"). */
  buttonLabel?: ReactNode;
  variant?: "subtle" | "ghost" | "primary";
};

export const FileInput = forwardRef<HTMLInputElement, FileInputProps>(function FileInput(
  { buttonLabel = "Choose a file", variant = "subtle", className, disabled, id, "aria-describedby": describedBy, ...rest }, ref,
) {
  const field = useFieldContext();
  return (
    <span className={cn("relative inline-flex", disabled && "cursor-not-allowed", className)}>
      <input {...rest} ref={ref} type="file" id={id ?? field.id} disabled={disabled} aria-describedby={joinIds(describedBy, field.describedBy)}
        className="peer absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed" />
      <span aria-hidden className={cn(buttonStyles({ variant }), "pointer-events-none peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-[-3px] peer-focus-visible:outline-fg",
        disabled && "opacity-45")}>{buttonLabel}</span>
    </span>
  );
});
