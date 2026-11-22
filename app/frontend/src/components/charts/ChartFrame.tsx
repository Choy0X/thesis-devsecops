import { useState, type ReactNode } from "react";
import { cn } from "../../lib/cn";

// Every chart sits in a frame with a title, a sentence about what it shows, and a button that swaps the drawing for the same numbers in a table. The
// table is the accessible form of the chart and is always one click away. Nothing in a chart is animated.

export function ChartFrame({ title, description, table, children, className }: { title: string; description?: string; table: ReactNode; children: ReactNode; className?: string }) {
  const [asTable, setAsTable] = useState(false);
  return (
    <figure className={cn("border border-line bg-surface p-4", className)}>
      <figcaption className="mb-3 flex items-start justify-between gap-3">
        <span>
          <span className="block text-[0.86rem] font-medium">{title}</span>
          {description && <span className="mt-0.5 block text-[0.76rem] text-muted">{description}</span>}
        </span>
        <button type="button" aria-pressed={asTable} onClick={() => setAsTable((v) => !v)}
          className="shrink-0 border border-line px-2 py-1 text-[0.72rem] text-muted outline-none hover:text-fg focus-visible:ring-1 focus-visible:ring-brand-strong">
          {asTable ? "View as chart" : "View as table"}
        </button>
      </figcaption>
      {asTable ? table : children}
    </figure>
  );
}

/** The numbers of a chart as a plain table. */
export function ChartTable({ caption, head, rows }: { caption: string; head: string[]; rows: (string | number)[][] }) {
  return (
    <div className="max-h-72 overflow-auto">
      <table className="w-full text-[0.78rem]">
        <caption className="sr-only">{caption}</caption>
        <thead className="text-left text-faint"><tr>{head.map((h) => <th key={h} scope="col" className="py-1 pr-4 font-medium">{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={i} className="border-t border-line">{r.map((c, j) => j === 0 ? <th key={j} scope="row" className="py-1 pr-4 text-left font-normal">{c}</th> : <td key={j} className="tnum py-1 pr-4">{c}</td>)}</tr>
        ))}</tbody>
      </table>
    </div>
  );
}
