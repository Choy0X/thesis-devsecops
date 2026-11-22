import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { matchCommands } from "../../lib/commands";
import { Button, Textarea } from "../ui";

// Where a question is typed. It grows with what is typed (up to eight lines, then it scrolls), Enter sends and Shift+Enter starts a new line (a key that is only choosing a character in an input
// method is not Enter), and while an answer is written the Send button is Stop. The question is kept when sending is refused, so nothing typed is lost.
//
// A leading, still-unfinished "/word" (no space typed after it yet) opens a menu of the commands it could be: arrow keys move through it, Enter or Tab picks the highlighted one (replacing the
// draft with "/name " and keeping the focus), Escape dismisses it without touching the draft. Recognizing and running a picked command happens a level up, in onSend: this box only ever edits
// its own draft text. The slash menu is chat-specific (its commands all act on a stored conversation); a caller with no use for it (agentic mode's own composer) passes commands={false}.

const MAX_LINES = 8;
const MENU_ID = "composer-slash-menu";

export function Composer({ onSend, onStop, busy, disabled, placeholder = "Ask about this project's security", hint, beforeSend, commands = true }: {
  /** Called with the question; returns false when it was refused (nothing is sent), so the words stay in the box. */
  onSend: (text: string) => Promise<boolean | void> | boolean | void;
  onStop: () => void;
  busy: boolean;
  disabled?: boolean;
  placeholder?: string;
  hint?: string;
  /** A control shown between the box and the Send/Stop button, e.g. a mode picker. */
  beforeSend?: ReactNode;
  /** The leading-"/word" command menu; off for a composer whose commands do not apply (there is nothing chat-specific to run). */
  commands?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [highlighted, setHighlighted] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const lines = Math.min(MAX_LINES, Math.max(1, draft.split("\n").length));
  const slashWord = commands ? /^\/(\S*)$/.exec(draft) : null;
  const menu = slashWord && !dismissed ? matchCommands(slashWord[1]) : [];
  const active = menu[Math.min(highlighted, menu.length - 1)];
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";                                              // grow with the words, not with the rows asked for: a long line wraps
    if (el.scrollHeight) el.style.height = `${Math.min(el.scrollHeight, MAX_LINES * 24 + 16)}px`;
  }, [draft]);
  const send = async () => {
    const text = draft.trim();
    if (!text || busy || disabled) return;
    const sent = await onSend(text);
    if (sent !== false) setDraft("");
    box.current?.focus();
  };
  const pick = (name: string) => {
    setDraft(`/${name} `);
    setHighlighted(0);
    box.current?.focus();
  };
  return (
    <form className="space-y-1.5" onSubmit={(e) => { e.preventDefault(); void send(); }}>
      <div className="relative flex items-end gap-2">
        {/* 2.75rem, not the kit's usual button height (h-9/2.25rem): matches the textarea's own min-h-[2.75rem] below, so beforeSend and Send/Stop sit exactly as tall as the box
            they are beside, not shorter than it. */}
        <Textarea ref={box} aria-label="Your question" value={draft} rows={lines} disabled={disabled} placeholder={placeholder} className="flex-1" inputClassName="min-h-[2.75rem] max-h-[13rem] resize-none"
          aria-expanded={menu.length > 0} aria-controls={menu.length > 0 ? MENU_ID : undefined}
          onChange={(e) => { setDraft(e.target.value); setDismissed(false); setHighlighted(0); }}
          onKeyDown={(e) => {
            if (menu.length > 0) {
              if (e.key === "ArrowDown") { e.preventDefault(); setHighlighted((i) => (i + 1) % menu.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setHighlighted((i) => (i - 1 + menu.length) % menu.length); return; }
              if (e.key === "Escape") { e.preventDefault(); setDismissed(true); return; }
              if ((e.key === "Enter" || e.key === "Tab") && !e.nativeEvent.isComposing) { e.preventDefault(); if (active) pick(active.name); return; }
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); }
          }} />
        {beforeSend}
        {busy ? <Button type="button" className="h-[2.75rem]" onClick={onStop}>Stop</Button>
          : <Button type="submit" variant="primary" className="h-[2.75rem]" disabled={!draft.trim() || disabled}>Send</Button>}
        {menu.length > 0 && (
          <ul id={MENU_ID} role="listbox" aria-label="Commands" className="chamfer-sm absolute bottom-full left-0 z-toast mb-1 max-h-56 w-80 overflow-y-auto border border-line-strong bg-surface-2 p-1 shadow-pop">
            {menu.map((c, i) => (
              <li key={c.name} role="option" aria-selected={c === active} onMouseEnter={() => setHighlighted(i)} onMouseDown={(e) => { e.preventDefault(); pick(c.name); }}
                className={`flex cursor-default items-baseline gap-2 px-2.5 py-1.5 text-[0.82rem] ${c === active ? "bg-brand-tint text-fg" : "text-muted"}`}>
                <span className="font-mono">/{c.name}</span>
                <span className="truncate text-[0.74rem] text-faint">{c.hint}</span>
              </li>))}
          </ul>)}
      </div>
      <p className="text-[0.74rem] text-faint">{hint ?? "Press Enter to send and Shift+Enter for a new line. Answers come from a language model and can be wrong; they are not a verification of anything."}</p>
    </form>
  );
}
