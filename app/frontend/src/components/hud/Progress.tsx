import { cn } from "../../lib/cn";

/** A segmented bar: `segments` blocks, filled from the left in proportion to value / max. It is a progressbar for assistive technology and
 *  says the percentage in words, so the segments are never the only carrier of the value. */
export function Progress({ value, max = 100, segments = 24, label, className }:
  { value: number; max?: number; segments?: number; label?: string; className?: string }) {
  const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  const filled = Math.round(ratio * segments);
  return (
    <div role="progressbar" aria-valuemin={0} aria-valuemax={max} aria-valuenow={Math.round(value)} aria-label={label ?? "Progress"}
      aria-valuetext={`${Math.round(ratio * 100)} percent`} className={cn("flex h-2.5 gap-[3px]", className)}>
      {Array.from({ length: segments }, (_, i) => (
        <span key={i} data-filled={i < filled} className={cn("progress-seg flex-1", i < filled ? "bg-brand-strong" : "bg-surface-3")} />
      ))}
    </div>
  );
}
