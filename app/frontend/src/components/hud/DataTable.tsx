import { useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { cn } from "../../lib/cn";
import { visibleWindow } from "../../lib/windowing";

// A table for long lists: sortable columns (click a header; the state is announced with aria-sort), a header that stays in view, and windowed rows, so
// only what is on screen is in the document. Rows have one fixed height. Sorting is done here, on the rows given; a server-paged list sorts on the server and
// passes `sort` and `onSortChange` to keep the header honest.

export type Column<T> = {
  key: string;
  label: string;
  /** What to show in the cell (default: the field of the row named by `key`). */
  render?: (row: T) => ReactNode;
  /** A value to sort by; a column without it is not sortable. */
  sortValue?: (row: T) => string | number | null | undefined;
  width?: string;
  align?: "left" | "right";
  className?: string;
};
export type SortState = { key: string; dir: "asc" | "desc" } | null;

type Props<T> = {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  caption: string;
  /** Visible height of the body in pixels (default 420). */
  height?: number;
  rowHeight?: number;
  sort?: SortState;
  onSortChange?: (sort: SortState) => void;
  onRowClick?: (row: T) => void;
  /** The pointer or the focus came to a row (with the row) or left it (with null): lets a page start reading what the row opens a moment early. */
  onRowIntent?: (row: T | null) => void;
  /** For a page of a longer list: how many rows the whole list has (-1 when that is not known) and where this page starts (so a screen reader announces "row 51 of 10,000"). */
  rowCount?: number;
  rowOffset?: number;
  empty?: ReactNode;
};

const FLEX_MIN = "10rem";

const compare = (a: string | number | null | undefined, b: string | number | null | undefined): number => {
  if (a == null && b == null) return 0;
  if (a == null) return 1;                                     // empty values sort last in either direction
  if (b == null) return -1;
  return typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
};

export function DataTable<T>({ columns, rows, rowKey, caption, height = 420, rowHeight = 40, sort: controlled, onSortChange, onRowClick, onRowIntent, rowCount, rowOffset = 0, empty }: Props<T>) {
  const [own, setOwn] = useState<SortState>(null);
  const sort = controlled !== undefined ? controlled : own;
  const [scrollTop, setScrollTop] = useState(0);
  const box = useRef<HTMLDivElement>(null);

  const sorted = useMemo(() => {
    const col = sort && columns.find((c) => c.key === sort.key);
    if (!col?.sortValue || controlled !== undefined) return rows;   // a controlled (server) sort has already ordered the rows
    const dir = sort!.dir === "asc" ? 1 : -1;
    return [...rows].sort((x, y) => {
      const a = col.sortValue!(x), b = col.sortValue!(y);
      return a == null || b == null ? compare(a, b) : dir * compare(a, b);
    });
  }, [rows, sort, columns, controlled]);

  const change = (key: string) => {
    const next: SortState = sort?.key !== key ? { key, dir: "asc" } : sort.dir === "asc" ? { key, dir: "desc" } : null;
    if (controlled === undefined) setOwn(next);
    onSortChange?.(next);
  };

  const w = visibleWindow(sorted.length, rowHeight, scrollTop, height);
  const shown = sorted.slice(w.start, w.end);
  // A column with no width shares what is left but never gets less than FLEX_MIN; the columns together never get less than their widths added up. A table in a window too narrow for
  // that scrolls sideways inside its own frame, instead of letting the headers print over one another.
  const template = columns.map((c) => c.width ?? `minmax(${FLEX_MIN}, 1fr)`).join(" ");
  const minWidth = `calc(${columns.map((c) => c.width ?? FLEX_MIN).join(" + ")})`;
  return (
    <div role="table" aria-label={caption} aria-rowcount={rowCount === -1 ? -1 : (rowCount ?? sorted.length) + 1} className="overflow-x-auto border border-line bg-surface">
      <div role="row" className="grid border-b border-line bg-surface-2 text-[0.72rem] font-semibold uppercase tracking-wider text-faint" style={{ gridTemplateColumns: template, minWidth }}>
        {columns.map((c) => {
          const active = sort?.key === c.key;
          return (
            <div key={c.key} role="columnheader" aria-sort={active ? (sort!.dir === "asc" ? "ascending" : "descending") : c.sortValue ? "none" : undefined}
              className={cn("px-3 py-2", c.align === "right" && "text-right")}>
              {c.sortValue ? (
                <button type="button" onClick={() => change(c.key)} className="inline-flex items-center gap-1 uppercase tracking-wider hover:text-fg focus-visible:text-fg">
                  {c.label}{active ? (sort!.dir === "asc" ? <ArrowUp size={12} aria-hidden /> : <ArrowDown size={12} aria-hidden />) : null}
                </button>
              ) : c.label}
            </div>
          );
        })}
      </div>
      {sorted.length === 0 ? <div className="px-4 py-8 text-center text-[0.84rem] text-muted">{empty ?? "Nothing to show."}</div> : (
        <div ref={box} onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)} className="overflow-y-auto" style={{ maxHeight: height, minWidth }}>
          <div style={{ height: w.before }} aria-hidden />
          {shown.map((row, i) => (
            <div key={rowKey(row)} role="row" aria-rowindex={rowOffset + w.start + i + 2} tabIndex={onRowClick ? 0 : undefined}
              onMouseEnter={onRowIntent ? () => onRowIntent(row) : undefined} onMouseLeave={onRowIntent ? () => onRowIntent(null) : undefined}
              onFocus={onRowIntent ? () => onRowIntent(row) : undefined} onBlur={onRowIntent ? () => onRowIntent(null) : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={onRowClick ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRowClick(row); } } : undefined}
              className={cn("grid items-center border-b border-line/60 text-[0.82rem]", onRowClick && "cursor-pointer hover:bg-surface-2 focus-visible:bg-surface-2")}
              style={{ gridTemplateColumns: template, height: rowHeight }}>
              {columns.map((c) => (
                <div key={c.key} role="cell" className={cn("truncate px-3", c.align === "right" && "text-right tnum", c.className)}>
                  {c.render ? c.render(row) : String((row as Record<string, unknown>)[c.key] ?? "")}
                </div>
              ))}
            </div>
          ))}
          <div style={{ height: w.after }} aria-hidden />
        </div>
      )}
    </div>
  );
}
