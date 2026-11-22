import { ChevronDown } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Button, type ButtonProps } from "./Button";

// A button that opens a short list of actions (Export: Markdown, JSON). It is a menu in the sense a screen reader and the keyboard know it: the button says it opens one and whether it is
// open, the list is a ``menu`` of ``menuitem`` entries, Down and Up move between the entries (Home and End go to the first and the last), Escape closes it and gives the focus back to the
// button, Tab or a click anywhere else closes it. An entry is a link (``href``, with ``download`` for a file: the browser, or the window's own download handler, does the saving) or an action
// (``onSelect``). No dependency: the kit has no dropdown, and the list is short.

export type MenuEntry = {
  key: string; label: ReactNode; description?: ReactNode; disabled?: boolean;
  /** A link: opened or, with ``download``, saved. */
  href?: string; download?: boolean | string;
  /** An action, when it is not a link. */
  onSelect?: () => void;
};

const item = "flex w-full cursor-pointer select-none flex-col gap-0.5 border-l-2 border-transparent px-2.5 py-1.5 text-left text-[0.82rem] text-muted outline-none no-underline " +
  "hover:border-brand-strong hover:bg-brand-tint hover:text-fg focus-visible:border-brand-strong focus-visible:bg-brand-tint focus-visible:text-fg aria-disabled:pointer-events-none aria-disabled:opacity-45";

export function Menu({ label, items, icon, variant = "ghost", size, disabled, title, align = "end", side = "bottom", showChevron = true, menuLabel, className }: {
  label: ReactNode; items: MenuEntry[]; icon?: ReactNode; variant?: ButtonProps["variant"]; size?: ButtonProps["size"]; disabled?: boolean; title?: string;
  /** Which edge of the button the list lines up with. */
  align?: "start" | "end"; side?: "top" | "bottom"; showChevron?: boolean; menuLabel?: string; className?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();

  const entries = () => Array.from(root.current?.querySelectorAll<HTMLElement>("[role='menuitem']:not([aria-disabled='true'])") ?? []);
  const close = (refocus = false) => { setOpen(false); if (refocus) trigger.current?.focus(); };

  useEffect(() => { if (open) entries()[0]?.focus(); }, [open]);             // eslint-disable-line react-hooks/exhaustive-deps; ignored: entries() reads the DOM, it is not a value of the render
  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  const keys = (e: KeyboardEvent) => {
    const list = entries();
    const at = list.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") { e.preventDefault(); close(true); }
    else if (e.key === "Tab") setOpen(false);
    else if (e.key === "ArrowDown") { e.preventDefault(); list[(at + 1) % list.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(at - 1 + list.length) % list.length]?.focus(); }
    else if (e.key === "Home") { e.preventDefault(); list[0]?.focus(); }
    else if (e.key === "End") { e.preventDefault(); list[list.length - 1]?.focus(); }
  };

  return (
    <span ref={root} className={cn("relative inline-block", className)} onKeyDown={open ? keys : undefined}>
      <Button ref={trigger} variant={variant} size={size} icon={icon} disabled={disabled} title={title} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined}
        onClick={() => setOpen((v) => !v)} onKeyDown={(e) => { if (!open && (e.key === "ArrowDown")) { e.preventDefault(); setOpen(true); } }}>
        {label}{showChevron && <ChevronDown size={14} aria-hidden />}
      </Button>
      {open && (
        <div id={id} role="menu" aria-label={menuLabel ?? (typeof label === "string" ? label : undefined)}
          className={cn("chamfer-sm absolute z-toast min-w-[12rem] border border-line-strong bg-surface-2 p-1 shadow-pop", side === "top" ? "bottom-full mb-1" : "top-full mt-1", align === "end" ? "right-0" : "left-0")}>
          {items.map((it) => {
            const content = <><span className="text-fg">{it.label}</span>{it.description != null && <span className="text-[0.72rem] text-faint">{it.description}</span>}</>;
            return it.href ? (
              <a key={it.key} role="menuitem" tabIndex={-1} href={it.href} download={it.download} aria-disabled={it.disabled || undefined} className={item} onClick={(e) => { if (it.disabled) e.preventDefault(); else close(); }}>{content}</a>
            ) : (
              <button key={it.key} type="button" role="menuitem" tabIndex={-1} aria-disabled={it.disabled || undefined} className={item} onClick={() => { if (it.disabled) return; close(true); it.onSelect?.(); }}>{content}</button>
            );
          })}
        </div>)}
    </span>
  );
}
