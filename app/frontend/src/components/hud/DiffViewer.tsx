import { memo, useMemo, useState } from "react";
import { cn } from "../../lib/cn";
import { KIND_CLASS, tokenize } from "../../lib/highlight";
import { visibleWindow } from "../../lib/windowing";

// A unified diff, one fixed-height line at a time. Only the lines in view are in the document, so a diff of 20,000 lines is as cheap as one of 40. A changed
// line says so with its sign and its line numbers as well as its colour; nothing depends on telling red from green.

export type DiffRow = { kind: "meta" | "hunk" | "add" | "del" | "ctx"; oldNo: number | null; newNo: number | null; text: string };

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

const FILE_HEADER = /^(diff --git |index [0-9a-f]+\.\.|--- (a\/|\/dev\/null)|\+\+\+ (b\/|\/dev\/null))/;

/** Reads unified diff text into rows with their line numbers (lines before the first hunk, such as `--- a/x`, are `meta`). Text with no `@@` header at
 *  all is read as one hunk from line 1, so a `+` line is still an added line. */
export function parseDiff(diff: string): DiffRow[] {
  const rows: DiffRow[] = [];
  const lines = diff.replace(/\r\n/g, "\n").split("\n");
  const loose = !lines.some((l) => HUNK.test(l));
  let oldNo = loose ? 1 : 0, newNo = loose ? 1 : 0, inHunk = loose;
  for (const line of lines) {
    const h = HUNK.exec(line);
    if (h) { oldNo = Number(h[1]); newNo = Number(h[2]); inHunk = true; rows.push({ kind: "hunk", oldNo: null, newNo: null, text: line }); continue; }
    if (loose) {
      if (FILE_HEADER.test(line) && rows.every((r) => r.kind === "meta")) { rows.push({ kind: "meta", oldNo: null, newNo: null, text: line }); continue; }
    } else if (!inHunk || line.startsWith("diff --git")) { inHunk = inHunk && !line.startsWith("diff --git"); rows.push({ kind: "meta", oldNo: null, newNo: null, text: line }); continue; }
    if (line.startsWith("\\")) { rows.push({ kind: "meta", oldNo: null, newNo: null, text: line }); continue; }       // "\ No newline at end of file"
    if (line.startsWith("+")) rows.push({ kind: "add", oldNo: null, newNo: newNo++, text: line.slice(1) });
    else if (line.startsWith("-")) rows.push({ kind: "del", oldNo: oldNo++, newNo: null, text: line.slice(1) });
    else rows.push({ kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: line.startsWith(" ") ? line.slice(1) : line });
  }
  if (rows.length && rows[rows.length - 1].kind !== "hunk" && rows[rows.length - 1].text === "") rows.pop();        // the empty piece after the final newline
  return rows;
}

const SIGN = { add: "+", del: "-", ctx: " ", hunk: "", meta: "" } as const;
const TONE: Record<DiffRow["kind"], string> = {
  add: "border-l-2 border-diff-add bg-diff-add-bg", del: "border-l-2 border-diff-del bg-diff-del-bg", ctx: "border-l-2 border-transparent",
  hunk: "border-l-2 border-transparent bg-surface-2 text-faint", meta: "border-l-2 border-transparent text-faint",
};
const SIGN_TONE: Record<DiffRow["kind"], string> = { add: "font-semibold text-diff-add", del: "font-semibold text-diff-del", ctx: "text-muted", hunk: "text-muted", meta: "text-muted" };

const Line = memo(function Line({ row, language }: { row: DiffRow; language: string | null | undefined }) {
  const code = row.kind === "add" || row.kind === "del" || row.kind === "ctx";
  return (
    <div data-kind={row.kind} className={cn("flex font-mono text-[0.76rem] leading-5", TONE[row.kind])} style={{ height: 20 }}>
      <span aria-hidden className="w-12 shrink-0 select-none px-2 text-right text-faint">{row.oldNo ?? ""}</span>
      <span aria-hidden className="w-12 shrink-0 select-none px-2 text-right text-faint">{row.newNo ?? ""}</span>
      <span aria-hidden className={cn("w-4 shrink-0 select-none text-center", SIGN_TONE[row.kind])}>{SIGN[row.kind]}</span>
      {row.kind === "add" && <span className="sr-only">added: </span>}
      {row.kind === "del" && <span className="sr-only">removed: </span>}
      <span className="min-w-0 flex-1 whitespace-pre overflow-hidden text-ellipsis">
        {code ? tokenize(row.text, language).map((t, i) => <span key={i} className={KIND_CLASS[t.kind]}>{t.text}</span>) : row.text}
      </span>
    </div>
  );
});

export function DiffViewer({ diff, language, height = 420, label = "Code change" }: { diff: string; language?: string | null; height?: number; label?: string }) {
  const rows = useMemo(() => parseDiff(diff), [diff]);
  const [scrollTop, setScrollTop] = useState(0);
  const w = visibleWindow(rows.length, 20, scrollTop, height);
  const changed = useMemo(() => ({ add: rows.filter((r) => r.kind === "add").length, del: rows.filter((r) => r.kind === "del").length }), [rows]);
  if (!rows.length) return <p className="text-[0.82rem] text-muted">There is no change to show.</p>;
  return (
    <section aria-label={label} className="border border-line bg-terminal">
      <div className="border-b border-line px-3 py-1.5 font-mono text-[0.7rem] text-faint"><span className="text-diff-add">{changed.add} added</span>, <span className="text-diff-del">{changed.del} removed</span></div>
      <div tabIndex={0} role="group" aria-label={`${label}, ${rows.length} lines`} onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        className="overflow-auto outline-none focus-visible:ring-1 focus-visible:ring-brand-strong" style={{ maxHeight: height }}>
        <div style={{ height: w.before }} aria-hidden />
        {rows.slice(w.start, w.end).map((row, i) => <Line key={w.start + i} row={row} language={language} />)}
        <div style={{ height: w.after }} aria-hidden />
      </div>
    </section>
  );
}
