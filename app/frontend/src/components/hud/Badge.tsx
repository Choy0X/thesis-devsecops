import { Circle, Diamond, OctagonAlert, TriangleAlert, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

// Severity is never carried by colour alone: each level has its own icon shape and its word, and only critical has a solid fill. Brand red is
// never used here.

export type Severity = "low" | "medium" | "high" | "critical";
const LEVELS: Record<Severity, { label: string; Icon: LucideIcon; cls: string }> = {
  low: { label: "Low", Icon: Circle, cls: "border-line-strong text-sev-low" },
  medium: { label: "Medium", Icon: TriangleAlert, cls: "border-warn-line bg-warn-tint text-sev-medium" },
  high: { label: "High", Icon: Diamond, cls: "border-sev-high text-sev-high" },
  critical: { label: "Critical", Icon: OctagonAlert, cls: "border-crit bg-crit text-on-brand font-semibold" },
};

export function SeverityBadge({ level, className }: { level: Severity; className?: string }) {
  const { label, Icon, cls } = LEVELS[level];
  return (
    <span data-severity={level} className={cn("inline-flex items-center gap-1.5 border px-2 py-0.5 text-[0.72rem] font-medium uppercase tracking-wider", cls, className)}>
      <Icon size={12} aria-hidden />{label}
    </span>
  );
}

/** A small neutral label: a count, a version, a tag. */
export function Badge({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("inline-flex items-center border border-line bg-surface-2 px-2 py-0.5 text-[0.72rem] text-muted", className)}>{children}</span>;
}
