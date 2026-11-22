import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Tooltip } from "../hud/Tooltip";

type HintProps = Omit<HTMLAttributes<HTMLSpanElement>, "title"> & { label: ReactNode; side?: "top" | "right" | "bottom" | "left" };

/** Text that has an explanation behind it: the explanation shows on hover and on keyboard focus (a native title shows on neither for keyboard users and
 * cannot be styled). It marks itself with a dotted underline (add `no-underline` where the text already looks like a chip) and takes one tab stop. For a long
 * text that is only cut short use a native title instead. */
export function Hint({ label, children, className, side, ...rest }: HintProps) {
  return (
    <Tooltip label={label} side={side}>
      <span tabIndex={0} {...rest} className={cn("cursor-help underline decoration-dotted decoration-faint underline-offset-[3px]", className)}>{children}</span>
    </Tooltip>
  );
}
