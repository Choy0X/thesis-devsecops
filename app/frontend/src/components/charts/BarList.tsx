import { num } from "../../lib/format";
import { ChartFrame, ChartTable } from "./ChartFrame";

// Counts by name as horizontal bars, largest first: what a campaign produced by language, weakness class or severity, or why its steps failed. The number is written beside
// every bar, a row can be reached with the keyboard and says its value, and the table button gives the same numbers as a table.

export type BarItem = { label: string; value: number };

export function BarList({ title, description, items, unit = "", empty = "Nothing to show yet.", limit = 12 }:
  { title: string; description?: string; items: BarItem[]; unit?: string; empty?: string; limit?: number }) {
  const sorted = [...items].filter((i) => i.value > 0).sort((a, b) => b.value - a.value);
  const shown = sorted.slice(0, limit);
  const rest = sorted.slice(limit).reduce((n, i) => n + i.value, 0);
  const total = sorted.reduce((n, i) => n + i.value, 0);
  const max = shown[0]?.value ?? 1;
  const table = <ChartTable caption={title} head={["Name", unit ? `Count (${unit})` : "Count", "Share"]}
    rows={sorted.map((i) => [i.label, num(i.value), `${total ? Math.round((100 * i.value) / total) : 0}%`])} />;
  return (
    <ChartFrame title={title} description={description} table={table}>
      {shown.length === 0 ? <p className="text-[0.8rem] text-muted">{empty}</p> : (
        <ul className="space-y-1.5">
          {shown.map((i) => (
            <li key={i.label} tabIndex={0} aria-label={`${i.label}: ${num(i.value)}${unit ? ` ${unit}` : ""}, ${total ? Math.round((100 * i.value) / total) : 0} percent`}
              className="grid grid-cols-[minmax(6rem,11rem)_1fr_auto] items-center gap-3 text-[0.8rem] outline-none focus-visible:ring-1 focus-visible:ring-brand-strong">
              <span className="truncate" title={i.label}>{i.label}</span>
              <span aria-hidden className="h-2.5 bg-surface-2"><span className="block h-full bg-brand" style={{ width: `${(100 * i.value) / max}%` }} /></span>
              <span className="tnum text-muted">{num(i.value)}</span>
            </li>
          ))}
          {rest > 0 && <li className="text-[0.74rem] text-faint">{num(rest)} more in {sorted.length - shown.length} smaller group{sorted.length - shown.length === 1 ? "" : "s"}</li>}
        </ul>
      )}
    </ChartFrame>
  );
}
