import { useEffect, useRef } from "react";
import { Compartment, EditorState } from "@codemirror/state";
import { EditorView, drawSelection, highlightActiveLine, keymap, lineNumbers } from "@codemirror/view";
import { syntaxHighlighting } from "@codemirror/language";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
import { languageFor } from "../../components/code/languages";
import { viewerHighlight } from "../../components/code/theme";

const editorText = (value: string) => value.replace(/\r\n?/g, "\n");

export function Editor({ path, text, line, readOnly = false, onChange, onSave }: { path: string; text: string; line?: number; readOnly?: boolean; onChange: (value: string) => void; onSave: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView>();
  const access = useRef(new Compartment());
  const callbacks = useRef({ onChange, onSave });
  callbacks.current = { onChange, onSave };
  useEffect(() => {
    const language = new Compartment();
    const instance = new EditorView({ parent: host.current!, state: EditorState.create({ doc: editorText(text), extensions: [
      lineNumbers(), drawSelection(), highlightActiveLine(), history(), highlightSelectionMatches(), syntaxHighlighting(viewerHighlight), language.of([]),
      access.current.of(EditorState.readOnly.of(readOnly)),
      keymap.of([{ key: "Mod-s", run: () => { callbacks.current.onSave(); return true; } }, ...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
      EditorView.contentAttributes.of({ "aria-label": `Edit ${path}`, spellcheck: "false" }),
      EditorView.updateListener.of(update => { if (update.docChanged) callbacks.current.onChange(update.state.doc.toString()); }),
      EditorView.theme({
        "&": { height: "100%", background: "var(--ide-canvas)", color: "var(--ide-text)", fontSize: "12px" },
        ".cm-scroller": { overflow: "auto", fontFamily: "'JetBrains Mono', monospace", lineHeight: "1.65" },
        ".cm-content": { caretColor: "var(--ide-accent)", padding: "12px 0" },
        ".cm-gutters": { background: "var(--ide-canvas)", color: "var(--ide-muted)", borderRight: "1px solid var(--ide-line)" },
        ".cm-lineNumbers .cm-gutterElement": { minWidth: "40px", padding: "0 8px" },
        ".cm-activeLine": { background: "var(--ide-selection)" },
        ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { background: "var(--ide-selection)" },
        ".cm-cursor": { borderLeftColor: "var(--ide-accent)" },
        "&.cm-focused": { outline: "none" },
      }),
    ] }) });
    view.current = instance;
    let alive = true;
    void languageFor(path).then(ext => { if (alive && ext) instance.dispatch({ effects: language.reconfigure(ext) }); });
    return () => { alive = false; view.current = undefined; instance.destroy(); };
  }, [path]);
  useEffect(() => {
    const instance = view.current;
    const next = editorText(text);
    if (instance && instance.state.doc.toString() !== next) instance.dispatch({ changes: { from: 0, to: instance.state.doc.length, insert: next } });
  }, [text]);
  useEffect(() => { view.current?.dispatch({ effects: access.current.reconfigure(EditorState.readOnly.of(readOnly)) }); }, [readOnly, path]);
  useEffect(() => {
    const instance = view.current;
    if (instance && line) {
      const pos = instance.state.doc.line(Math.max(1, Math.min(line, instance.state.doc.lines))).from;
      instance.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "center" }) });
      instance.focus();
    }
  }, [line, path]);
  return <div className="ide-code" ref={host} />;
}
