import type { LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "../lib/cn";

// The menu of the Settings page: one entry per group, each with its icon, the current one drawn like the current section of the rail (a cut corner and the brand tint) and marked with
// aria-current. It is a column beside the page and, in a window narrower than the large breakpoint, a strip above it that scrolls sideways. The entries are links to `?tab=<id>`, so a group has
// an address of its own: the back button, a reload and a link from elsewhere (the Developer mode chip) all return to it.

export type SettingsMenuItem = { id: string; label: string; icon: LucideIcon };

export function SettingsMenu({ items, value, to }: { items: SettingsMenuItem[]; value: string; to: (id: string) => string }) {
  return (
    <nav aria-label="Settings groups" className="lg:sticky lg:top-6 lg:self-start">
      <ul className="-mx-1 flex gap-1 overflow-x-auto border-b border-line px-1 pb-2 lg:mx-0 lg:block lg:space-y-1 lg:overflow-visible lg:border-b-0 lg:border-r lg:px-0 lg:pb-0 lg:pr-3">
        {items.map(({ id, label, icon: Icon }) => {
          const current = id === value;
          return (
            <li key={id} className="shrink-0">
              <Link to={to(id)} aria-current={current ? "page" : undefined}
                className={cn("flex items-center gap-2.5 whitespace-nowrap border px-3 py-2 text-[0.85rem] transition-colors",
                  current ? "chamfer-sm border-brand-line bg-brand-tint-strong text-fg [--ch-line:var(--brand-line)]" : "border-transparent text-muted hover:bg-surface-2 hover:text-fg")}>
                <span className={current ? "text-brand-strong" : "text-faint"}><Icon size={17} aria-hidden /></span>{label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
