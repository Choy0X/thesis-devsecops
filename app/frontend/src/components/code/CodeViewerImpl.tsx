import { Compartment, EditorState, RangeSetBuilder } from "@codemirror/state";
import { Decoration, EditorView, lineNumbers } from "@codemirror/view";
import { syntaxHighlighting } from "@codemirror/language";
import { useEffect, useRef } from "react";
import { languageFor } from "./languages";
import { viewerHighlight, viewerTheme } from "./theme";

// The only file that imports CodeMirror. It is loaded on demand by ``CodeViewer.tsx`` (a dynamic import), so no page carries the editor until a file is shown. Read-only in every way a
// reader can feel: the content cannot be edited or focused for typing, but it can be focused with the keyboard to scroll and select, and a screen reader is told what it is.

export type CodeViewerProps = {
  /** the file's text as it is, with no line numbers in it */
  code: string;
  /** the name or path of the file: its extension picks the grammar */
  path: string;
  /** the number of the first line shown (a read of lines 40 to 80 is numbered from 40) */
  startLine?: number;
  /** numbers (as in the file, not in the excerpt) of lines to mark, for example the lines a search found */
  marked?: number[];
  label: string;
  /** a CSS length: the viewer scrolls inside it */
  maxHeight?: string;
};

const mark = Decoration.line({ class: "cm-hit" });

function markedLines(doc: EditorState["doc"], lines: number[], startLine: number) {
  const builder = new RangeSetBuilder<Decoration>();
  for (const n of [...new Set(lines)].sort((a, b) => a - b)) {
    const at = n - startLine + 1;
    if (at >= 1 && at <= doc.lines) builder.add(doc.line(at).from, doc.line(at).from, mark);
  }
  return builder.finish();
}

export default function CodeViewerImpl({ code, path, startLine = 1, marked = [], label, maxHeight = "26rem" }: CodeViewerProps) {
  const host = useRef<HTMLDivElement>(null);
  const markKey = marked.join(",");
  useEffect(() => {
    const language = new Compartment();
    const doc = EditorState.create({ doc: code }).doc;
    const view = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: code,
        extensions: [
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          lineNumbers({ formatNumber: (n) => String(n + startLine - 1) }),
          syntaxHighlighting(viewerHighlight),
          viewerTheme,
          EditorView.theme({ "&": { maxHeight } }),
          EditorView.contentAttributes.of({ tabindex: "0", role: "group", "aria-label": label, "aria-readonly": "true" }),
          EditorView.decorations.of(markedLines(doc, markKey ? markKey.split(",").map(Number) : [], startLine)),
          language.of([]),
        ],
      }),
    });
    let alive = true;
    languageFor(path).then((ext) => { if (alive && ext) view.dispatch({ effects: language.reconfigure(ext) }); }).catch(() => { /* ignored: a grammar that fails to load leaves the file as plain text */ });
    return () => { alive = false; view.destroy(); };
  }, [code, path, startLine, markKey, label, maxHeight]);
  return <div ref={host} className="overflow-hidden border border-line bg-terminal" />;
}
