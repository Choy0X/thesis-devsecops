import * as RTabs from "@radix-ui/react-tabs";
import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { PageScope } from "../PageScope";

export type TabItem = { id: string; label: string; count?: number; content?: ReactNode };

export function Tabs({ items, value, onValueChange, className, children, keepMounted }:
  { items: TabItem[]; value: string; onValueChange: (id: string) => void; className?: string; children?: ReactNode;
    /** Compatibility layout flag. Inactive content always unmounts. */
    keepMounted?: boolean }) {
  return (
    <RTabs.Root value={value} onValueChange={onValueChange} className={className}>
      <RTabs.List className="flex gap-1 overflow-x-auto border-b border-line" aria-label="Views">
        {items.map((t) => (
          <RTabs.Trigger key={t.id} value={t.id}
            className={cn("app-tab shrink-0 whitespace-nowrap border-b-2 border-transparent px-3.5 py-2.5 text-[0.82rem] font-medium text-muted transition-colors",
              "hover:text-fg data-[state=active]:border-brand-strong data-[state=active]:bg-brand-tint data-[state=active]:text-fg")}>
            {t.label}{t.count != null ? <span className="ml-2 font-mono text-[0.72rem] text-faint">{t.count}</span> : null}
          </RTabs.Trigger>
        ))}
      </RTabs.List>
      {items.map((t) => t.content != null && (
        <RTabs.Content key={t.id} value={t.id} 
          className={cn("pt-4 outline-none", keepMounted && "flex min-h-0 flex-1 flex-col")}>
          <PageScope>{t.content}</PageScope>
        </RTabs.Content>
      ))}
      {children}
    </RTabs.Root>
  );
}
