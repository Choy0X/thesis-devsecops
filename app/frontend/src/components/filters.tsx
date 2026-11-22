import type { ReactNode } from "react";
import { Checkbox } from "./kit";
import { num } from "../lib/format";

// Filter building blocks shared by the Build dataset card and the Enrich panel.

export const fmt = (o: Record<string, number>) => Object.entries(o).slice(0, 5).map(([k, v]) => `${k} ${num(v)}`).join(" · ") || "—";
export const take = (o: Record<string, number>, n: number) => Object.fromEntries(Object.entries(o).slice(0, n));
export const pick = (o: Record<string, number>, keys: string[]) => Object.fromEntries(keys.map((k) => [k, o[k] || 0]));

export function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-2 text-[0.72rem] font-medium uppercase tracking-wide text-faint">{title}</div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

export function Check({ label, v, on }: { label: string; v: boolean; on: (v: boolean) => void }) {
  return <Checkbox checked={v} onCheckedChange={on} label={label} className="[&_span]:text-[0.78rem] [&_span]:text-muted" />;
}

export function Chips({ label, all, items, picked, onToggle, labels }:
  { label: string; all: string; items: Record<string, number>; picked: string[]; onToggle: (v: string) => void;
    labels?: Record<string, string> }) {
  return (
    <div>
      <div className="mb-1 text-[0.74rem] text-muted">{label} <span className="text-faint">({picked.length ? picked.length + " selected" : all})</span></div>
      <div className="flex flex-wrap gap-1">
        {Object.entries(items).map(([k, n]) => {
          const on = picked.includes(k);
          return (
            <button key={k} type="button" onClick={() => onToggle(k)}
              className={`h-6 rounded-full border px-2.5 text-[0.72rem] transition-colors ${on
                ? "border-brand bg-brand/15 text-fg" : "border-line bg-surface text-muted hover:border-line-strong"}`}>
              {labels?.[k] ?? k} <span className="text-faint tnum">{num(n)}</span></button>
          );
        })}
        {Object.keys(items).length === 0 && <span className="text-[0.74rem] text-faint">none</span>}
      </div>
    </div>
  );
}

export const SELECT = "h-9 w-full rounded-lg border border-line bg-surface-2 px-2 text-[0.82rem] outline-none focus:border-brand";
