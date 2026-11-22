import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Tooltip } from "../hud/Tooltip";
import { Spinner } from "./Spinner";

// The button of the kit: square with one small cut corner pair, a hairline, a small caps label. The cut is drawn by .chamfer-sm (index.css), whose hairline colour is --ch-line,
// so each variant sets that to its own border colour. A filled button draws its focus ring in the foreground colour, inside the shape, because a brand-red ring on a brand-red fill
// could not be seen. A disabled button is dimmed and takes no pointer events.

export const buttonStyles = cva(
  "chamfer-sm inline-flex select-none items-center justify-center gap-2 whitespace-nowrap border font-semibold transition-colors duration-fast " +
  "focus-visible:outline-offset-[-3px] active:translate-y-px disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        primary: "border-brand-strong bg-brand text-on-brand [--ch-line:var(--brand-strong)] hover:bg-brand-hover focus-visible:outline-fg",
        subtle: "border-line-strong bg-surface-2 text-fg [--ch-line:var(--border-strong)] hover:bg-surface-3 hover:[--ch-line:var(--control-border)] hover:border-[var(--control-border)]",
        ghost: "border-transparent bg-transparent text-muted [--ch-line:transparent] hover:bg-surface-2 hover:text-fg",
        danger: "border-crit-line bg-crit-tint text-crit-soft [--ch-line:var(--crit-line)] hover:border-crit hover:bg-crit hover:text-bg hover:[--ch-line:var(--crit)] focus-visible:outline-fg",
        // the confirm button of a destructive question: the solid critical fill with dark text on it (the pair the contrast table measures), never decoration
        critical: "border-crit bg-crit text-bg [--ch-line:var(--crit)] hover:border-crit-soft hover:bg-crit-soft hover:[--ch-line:var(--crit-soft)] focus-visible:outline-fg",
      },
      size: {
        md: "h-9 px-4 text-[0.8rem]",
        sm: "h-7 px-3 text-[0.76rem]",
      },
    },
    defaultVariants: { variant: "subtle", size: "md" },
  },
);

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonStyles> & { loading?: boolean; icon?: ReactNode };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant, size, loading, icon, children, className, disabled, type = "button", title, ...rest }, ref,
) {
  const tipped = !!title && !(disabled || loading);
  const button = (
    <button ref={ref} type={type} className={cn(buttonStyles({ variant, size }), className)} disabled={disabled || loading} aria-busy={loading || undefined} title={tipped ? undefined : title} {...rest}>
      {loading ? <Spinner /> : icon}{children}
    </button>
  );
  // A `title` is a tooltip (hover and keyboard focus), not a native title. A disabled button keeps a native title (a tooltip needs events it does not take).
  return tipped ? <Tooltip label={title}>{button}</Tooltip> : button;
});
