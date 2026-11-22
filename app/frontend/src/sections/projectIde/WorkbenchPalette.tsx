import { useEffect, useRef, useState } from "react";
import { Dialog } from "../../components/hud/Dialog";
import { Input } from "./Controls";

export type PaletteEntry = { id: string; title: string; detail: string; run: () => void };

export function WorkbenchPalette({ title, entries, onClose }: { title: string; entries: PaletteEntry[]; onClose: (restoreFocus?: boolean) => void }) {
  const [filter, setFilter] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const matches = entries.filter(entry => (entry.title + " " + entry.detail).toLocaleLowerCase().includes(filter.trim().toLocaleLowerCase()));
  const index = Math.min(active, matches.length - 1);
  useEffect(() => { input.current?.focus(); }, []);
  const select = (entry?: PaletteEntry) => {
    if (!entry) return;
    onClose(false);
    entry.run();
  };
  return <Dialog open onOpenChange={open => { if (!open) onClose(); }} onCloseAutoFocus={event => event.preventDefault()} title={title} className="w-[min(32rem,94vw)]">
    <Input ref={input} type="search" aria-label={"Filter " + title.toLocaleLowerCase()} placeholder="Type to filter"
      className="w-full border border-line-strong bg-terminal p-2 font-mono text-sm"
      value={filter} onChange={event => { setFilter(event.target.value); setActive(0); }} onKeyDown={event => {
        if (event.nativeEvent.isComposing || !matches.length) return;
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setActive((index + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length);
        } else if (event.key === "Enter") { event.preventDefault(); select(matches[index]); }
      }} />
    <div className="wb-palette-list">
      {matches.map((entry, position) => <button type="button" key={entry.id} data-highlighted={position === index} onClick={() => select(entry)}>
        <strong>{entry.title}</strong><small>{entry.detail}</small>
      </button>)}
      {!matches.length && <p role="status">No matches.</p>}
    </div>
  </Dialog>;
}
