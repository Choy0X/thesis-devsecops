// Windowing for long lists: only the rows that can be seen (plus a few above and below) are drawn, so a table of 200,000 rows or a diff of 20,000 lines
// costs the same as one of 50. Rows have a fixed height, which makes the arithmetic exact and the scroll bar honest.

export type Window = { start: number; end: number; before: number; after: number };

/** The slice [start, end) of `count` rows of `rowHeight` pixels to draw for a viewport of `viewport` pixels scrolled by `scrollTop`. */
export function visibleWindow(count: number, rowHeight: number, scrollTop: number, viewport: number, overscan = 6): Window {
  if (count <= 0 || rowHeight <= 0) return { start: 0, end: 0, before: 0, after: 0 };
  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const last = Math.min(count, Math.ceil((scrollTop + Math.max(viewport, rowHeight)) / rowHeight) + overscan);
  const start = Math.min(first, count);
  const end = Math.max(start, last);
  return { start, end, before: start * rowHeight, after: (count - end) * rowHeight };
}
