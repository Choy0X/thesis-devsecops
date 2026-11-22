import { Children, cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { ApiError, errorText } from "../lib/apiError";
import { cn } from "../lib/cn";
import { FormRow, joinIds, useFieldContext } from "./kit/FormRow";
import { MatrixRain } from "./kit/MatrixRain";
import { Button } from "./kit/Button";
import { Input, Textarea } from "./kit/TextField";
import { Spinner } from "./kit/Spinner";

// The shared building blocks of the pages. The controls live in components/kit; the names used all over the pages (Field, Input, Textarea, Button, Spinner) are kept here and
// point at the kit, so a page gets the console look without being edited. Panel, the status pill, the empty and error states and the severity bar are drawn here.

export { Button, Input, Textarea, Spinner };

const NATIVE = new Set(["input", "select", "textarea"]);

/** A native control placed directly inside a Field (not yet moved to the kit) is tied to the label by id, as a kit control is by the row's context. */
function Associate({ children }: { children: ReactNode }) {
  const field = useFieldContext();
  return <>{Children.map(children, (child) => {
    if (!isValidElement(child) || typeof child.type !== "string" || !NATIVE.has(child.type)) return child;
    const el = child as ReactElement<{ id?: string; "aria-describedby"?: string }>;
    return cloneElement(el, { id: el.props.id ?? field.id, "aria-describedby": joinIds(el.props["aria-describedby"], field.describedBy) });
  })}</>;
}

/** A label, one control and a hint. The label is a real <label htmlFor>, not a wrapper around the control (see kit/FormRow). */
export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return <FormRow label={label} hint={hint}><Associate>{children}</Associate></FormRow>;
}

export function Panel({ title, desc, right, children, className = "" }:
  { title?: ReactNode; desc?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn("hud-frame min-w-0 border border-line bg-surface shadow-card", className)}>
      {(title || desc || right) && (
        <header className="app-panel-header flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-line bg-surface-2">
          <div className="min-w-0">
            {title && <h2 className="text-[0.92rem] font-semibold tracking-[-0.01em]">{title}</h2>}
            {desc && <p className="mt-1 text-[0.8rem] leading-snug text-muted">{desc}</p>}
          </div>
          {right}
        </header>
      )}
      <div className="app-panel-body">{children}</div>
    </section>
  );
}

export function StatusPill({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    idle: ["bg-surface-2 text-faint border-line", "idle"],
    running: ["bg-brand-tint-strong text-brand-strong border-brand-line", "running"],
    done: ["bg-ok-tint-strong text-ok border-ok-line", "done"],
    error: ["bg-crit-tint text-crit-soft border-crit-line", "error"],
    cancelled: ["bg-surface-2 text-muted border-line-strong", "stopped"],
    interrupted: ["bg-warn-tint text-warn border-warn-line", "interrupted"],
  };
  const [cls, label] = map[status] || map.idle;
  return (
    <span className={`caps inline-flex items-center gap-1.5 border px-2 py-0.5 ${cls}`}>
      <span className={`h-1.5 w-1.5 rotate-45 bg-current ${status === "running" ? "dot-pulse" : ""}`} />
      {label}
    </span>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} />;
}

/** A failed request or job: what happened, why if known, what to do, and a way to try again. */
export function ErrorState({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const e = error instanceof ApiError ? error : null;
  const heading = title ?? errorText(error);
  const detail = e && e.detail && e.detail !== e.title && e.detail !== heading ? e.detail : null;
  return (
    <div role="alert" className="chamfer-sm relative border border-crit-line bg-surface-2 py-3 pl-5 pr-4 text-[0.82rem] [--ch-line:var(--crit-line)]">
      <span aria-hidden className="absolute inset-y-0 left-0 w-1 bg-crit" />
      <p className="font-medium text-crit-soft">{heading}</p>
      {detail && <p className="mt-1 text-muted">{detail}</p>}
      {e?.hint && <p className="mt-1 text-muted">{e.hint}</p>}
      {onRetry && <div className="mt-3"><Button variant="subtle" size="sm" onClick={onRetry}>Try again</Button></div>}
    </div>
  );
}

/** Nothing here yet: says what this place is for and what to do. A faint rain of characters falls behind the words (the one place the Matrix nod lives; it stops under reduced motion). */
export function EmptyState({ icon, title, hint, action }:
  { icon?: ReactNode; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="relative flex flex-col items-center justify-center overflow-hidden px-6 py-12 text-center">
      <MatrixRain className="opacity-15" />
      <div className="relative flex flex-col items-center">
        {icon && <div className="chamfer-sm mb-3 grid h-11 w-11 place-items-center border border-line bg-surface-2 text-faint [--ch-line:var(--border)]">{icon}</div>}
        <p className="text-[0.9rem] font-medium">{title}</p>
        {hint && <p className="mt-1 max-w-sm text-[0.8rem] leading-relaxed text-muted">{hint}</p>}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </div>
  );
}

export function SevBar({ rec, warn, crit }: { rec: number; warn: number; crit: number }) {
  const total = Math.max(rec + warn + crit, 1);
  const seg = (v: number, c: string, label: string) =>
    v > 0 ? <div className={c} style={{ width: `${(v / total) * 100}%` }} title={`${label}: ${v}`} /> : null;
  return (
    <div className="flex h-2 w-full overflow-hidden bg-surface-2">
      {seg(rec, "bg-info", "Recommendation")}{seg(warn, "bg-warn", "Warning")}{seg(crit, "bg-crit", "Critical")}
    </div>
  );
}
