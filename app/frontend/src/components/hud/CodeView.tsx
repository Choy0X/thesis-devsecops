import { memo, useMemo, useState } from "react";
import { KIND_CLASS, tokenize } from "../../lib/highlight";
import { visibleWindow } from "../../lib/windowing";

// One side of a code pair (the vulnerable code or the fixed code) with line numbers and offline colouring. Like the diff viewer it keeps only the
// lines in view in the document, so a file of 20,000 lines is as cheap as one of 40.

const Line = memo(function Line({ no, text, language }: { no: number; text: string; language: string | null | undefined }) {
  return (
    <div className="flex font-mono text-[0.76rem] leading-5" style={{ height: 20 }}>
      <span aria-hidden className="w-12 shrink-0 select-none px-2 text-right text-faint">{no}</span>
      <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-pre">
        {tokenize(text, language).map((t, i) => <span key={i} className={KIND_CLASS[t.kind]}>{t.text}</span>)}
      </span>
    </div>
  );
});

export function CodeView({ text, language, height = 420, label = "Code" }: { text: string | null | undefined; language?: string | null; height?: number; label?: string }) {
  const lines = useMemo(() => (text ? text.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n") : []), [text]);
  const [scrollTop, setScrollTop] = useState(0);
  const w = visibleWindow(lines.length, 20, scrollTop, height);
  if (!lines.length) return <p className="text-[0.82rem] text-muted">There is no code stored for this side of the pair.</p>;
  return (
    <section aria-label={label} className="border border-line bg-terminal">
      <div className="border-b border-line px-3 py-1.5 font-mono text-[0.7rem] text-faint">{lines.length} lines</div>
      <div tabIndex={0} role="group" aria-label={`${label}, ${lines.length} lines`} onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        className="overflow-auto outline-none focus-visible:ring-1 focus-visible:ring-brand-strong" style={{ maxHeight: height }}>
        <div style={{ height: w.before }} aria-hidden />
        {lines.slice(w.start, w.end).map((line, i) => <Line key={w.start + i} no={w.start + i + 1} text={line} language={language} />)}
        <div style={{ height: w.after }} aria-hidden />
      </div>
    </section>
  );
}
