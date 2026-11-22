import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Tooltip } from "../hud/Tooltip";

// A button that is only an icon. It always has a name: `label` is the accessible name and, unless `tip` says otherwise, the tooltip. The danger tone turns solid critical red under
// the pointer and on keyboard focus, with the icon in the foreground colour (an icon needs 3:1, which that pair meets; the word is the tooltip and the accessible name).

const iconButton = cva(
  "chamfer-sm inline-grid shrink-0 select-none place-items-center border transition-colors duration-fast focus-visible:outline-offset-[-3px] active:translate-y-px disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      tone: {
        ghost: "border-transparent text-muted [--ch-line:transparent] hover:bg-surface-3 hover:text-fg",
        subtle: "border-line-strong bg-surface-2 text-fg [--ch-line:var(--border-strong)] hover:bg-surface-3 hover:border-[var(--control-border)] hover:[--ch-line:var(--control-border)]",
        danger: "border-transparent text-muted [--ch-line:transparent] hover:border-crit hover:bg-crit hover:text-on-brand hover:[--ch-line:var(--crit)] " +
          "focus-visible:border-crit focus-visible:bg-crit focus-visible:text-on-brand focus-visible:outline-fg active:bg-crit active:text-on-brand",
      },
      size: { md: "h-9 w-9", sm: "h-7 w-7" },
    },
    defaultVariants: { tone: "ghost", size: "md" },
  },
);

export type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label" | "title"> & VariantProps<typeof iconButton> & {
  /** The accessible name, and the tooltip text unless `tip` is given. */
  label: string;
  /** A longer tooltip, or `false` for none. */
  tip?: ReactNode | false;
};

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, tip, tone, size, className, children, type = "button", ...rest }, ref,
) {
  const button = (
    <button ref={ref} type={type} aria-label={label} className={cn(iconButton({ tone, size }), className)} {...rest}>{children}</button>
  );
  return tip === false ? button : <Tooltip label={tip ?? label}>{button}</Tooltip>;
});
