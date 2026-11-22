import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

// Two small pieces for telemetry: a Stat is one number with its name (a big monospace figure under a small caps label, as in the reference consoles); a KeyValue is a list of
// name and value pairs (a real <dl>, so a screen reader reads the pairs). Numbers are tabular so a column of them lines up.

export function Stat({ label, value, unit, hint, tone = "normal", className }: {
  label: ReactNode; value: ReactNode; unit?: ReactNode; hint?: ReactNode; tone?: "normal" | "warn" | "crit"; className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className="caps text-faint">{label}</div>
      <div className={cn("tnum mt-1 flex items-baseline gap-1.5 font-mono text-[1.7rem] font-medium leading-none tracking-tight",
        tone === "crit" ? "text-crit-soft" : tone === "warn" ? "text-warn" : "text-fg")}>
        {value}{unit != null && <span className="text-[0.8rem] font-normal text-muted">{unit}</span>}
      </div>
      {hint != null && <div className="mt-1.5 text-[0.74rem] leading-snug text-muted">{hint}</div>}
    </div>
  );
}

export function KeyValue({ items, className, mono = true }: { items: { label: ReactNode; value: ReactNode }[]; className?: string; mono?: boolean }) {
  return (
    <dl className={cn("grid grid-cols-[minmax(7rem,max-content)_1fr] gap-x-5 gap-y-1.5 text-[0.82rem]", className)}>
      {items.map((item, i) => (
        <div key={i} className="contents">
          <dt className="caps pt-0.5 text-faint">{item.label}</dt>
          <dd className={cn("m-0 min-w-0 break-words text-fg", mono && "tnum font-mono text-[0.8rem]")}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
