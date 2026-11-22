import { HighlightStyle } from "@codemirror/language";
import { EditorView } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";

// The colours of the code viewer are the interface's own tokens (``var(--...)`` in index.css), never a colour written here: a change of a token reaches the editor with no change to this file,
// and the same colours the offline highlighter of the diff pages uses (comments faint, strings, numbers and keywords on the chart colours) are used here, which the contrast table measures
// against the terminal surface.

export const viewerTheme = EditorView.theme({
  "&": { color: "var(--fg)", backgroundColor: "var(--terminal)", fontSize: "0.76rem" },
  "&.cm-focused": { outline: "1px solid var(--brand-strong)", outlineOffset: "-1px" },
  ".cm-scroller": { fontFamily: "'JetBrains Mono', ui-monospace, monospace", lineHeight: "1.25rem", overflow: "auto" },
  ".cm-content": { padding: "0.25rem 0", caretColor: "transparent" },
  ".cm-line": { padding: "0 0.75rem 0 0.5rem" },
  ".cm-gutters": { backgroundColor: "var(--terminal)", color: "var(--faint)", border: "none", borderRight: "1px solid var(--border)" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 0.5rem 0 0.75rem", minWidth: "2.75rem" },
  ".cm-line.cm-hit": { backgroundColor: "var(--surface-3)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { backgroundColor: "var(--surface-3)" },
}, { dark: true });

export const viewerHighlight = HighlightStyle.define([
  { tag: [t.comment, t.lineComment, t.blockComment, t.docComment], color: "var(--faint)", fontStyle: "italic" },
  { tag: [t.string, t.special(t.string), t.regexp, t.character], color: "var(--chart-4)" },
  { tag: [t.number, t.bool, t.null, t.atom, t.integer, t.float], color: "var(--chart-5)" },
  { tag: [t.keyword, t.controlKeyword, t.operatorKeyword, t.modifier, t.definitionKeyword, t.moduleKeyword, t.self], color: "var(--chart-3)" },
  { tag: [t.function(t.variableName), t.function(t.propertyName), t.definition(t.function(t.variableName))], color: "var(--info)" },
  { tag: [t.typeName, t.className, t.namespace], color: "var(--chart-2)" },
  { tag: [t.tagName, t.angleBracket], color: "var(--chart-3)" },
  { tag: [t.attributeName], color: "var(--chart-2)" },
  { tag: [t.heading], color: "var(--fg)", fontWeight: "600" },
  { tag: [t.link, t.url], color: "var(--info)", textDecoration: "underline" },
  { tag: [t.invalid], color: "var(--crit-soft)" },
]);
