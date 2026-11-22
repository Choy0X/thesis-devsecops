import { Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import type { ChatSummary } from "../../lib/api";
import { Button } from "../ui";
import { IconButton, Input } from "../kit";
import { useConfirm } from "../hud/AlertDialog";

// The conversations of a project, newest first and grouped by day, with New chat, a filter, rename and delete (which asks first). A conversation is a button, so it can be reached and opened
// with the keyboard; the open one says so to a screen reader.

export const GROUPS = ["Today", "Yesterday", "Previous 7 days", "Older"] as const;

/** The group a time belongs to, by the calendar days of this computer: Today, Yesterday, Previous 7 days (two to six days ago) or Older. */
export function dayGroup(iso: string, now: Date = new Date()): (typeof GROUPS)[number] {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "Older";
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(at)) / 86_400_000);
  return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days <= 6 ? "Previous 7 days" : "Older";
}

export function ConversationList({ chats, activeId, damaged = 0, busy = false, now, onSelect, onNew, onRename, onDelete, navLabel = "Conversations", newLabel = "New chat" }: {
  chats: ChatSummary[];
  activeId: string | null;
  /** How many files of conversations could not be read; said under the list so that nothing is hidden quietly. */
  damaged?: number;
  /** An answer is being written: the open conversation cannot be switched away from, renamed or deleted. */
  busy?: boolean;
  now?: Date;
  onSelect: (id: string) => void;
  onNew: () => void;
  /** Omit for a list whose items cannot be renamed (there is no Rename button at all, rather than one that would silently do nothing). */
  onRename?: (id: string, title: string) => void | Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
  navLabel?: string;
  newLabel?: string;
}) {
  const confirm = useConfirm();
  const [filter, setFilter] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? chats.filter((c) => `${c.title} ${c.preview}`.toLowerCase().includes(q)) : chats;
  }, [chats, filter]);
  const groups = GROUPS.map((g) => ({ name: g, items: shown.filter((c) => dayGroup(c.updated_at, now) === g) })).filter((g) => g.items.length > 0);

  const remove = async (c: ChatSummary) => {
    const ok = await confirm({ title: `Delete "${c.title}"?`, description: "The conversation and its messages are removed from this computer. This cannot be undone.", confirmLabel: "Delete it", cancelLabel: "Keep it", tone: "critical" });
    if (ok) await onDelete(c.id);
  };
  const commit = async (c: ChatSummary) => {
    const title = draft.trim();
    setRenaming(null);
    if (title && title !== c.title) await onRename?.(c.id, title);
  };

  return (
    <nav aria-label={navLabel} className="flex min-h-0 flex-col gap-3">
      <Button variant="primary" icon={<Plus size={15} aria-hidden />} onClick={onNew} disabled={busy}>{newLabel}</Button>
      {chats.length > 4 && <Input aria-label="Filter the conversations" placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} />}
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
        {chats.length === 0 && <p className="text-[0.78rem] text-muted">No conversation yet. Ask a question to start one.</p>}
        {chats.length > 0 && shown.length === 0 && <p role="status" className="text-[0.78rem] text-muted">No conversation matches "{filter.trim()}".</p>}
        {groups.map((g) => (
          <section key={g.name} aria-label={g.name}>
            <h2 className="mb-1 px-1 text-[0.7rem] font-semibold uppercase tracking-wider text-faint">{g.name}</h2>
            <ul className="space-y-0.5">
              {g.items.map((c) => (
                <li key={c.id} className={`group flex items-center gap-1 border ${c.id === activeId ? "border-brand-line bg-brand-tint-strong" : "border-transparent hover:bg-surface-2"}`}>
                  {renaming === c.id ? (
                    <Input aria-label={`New name of ${c.title}`} value={draft} autoFocus maxLength={80} onChange={(e) => setDraft(e.target.value)} onBlur={() => void commit(c)}
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void commit(c); } else if (e.key === "Escape") { setRenaming(null); } }} />
                  ) : (
                    <>
                      <button type="button" aria-current={c.id === activeId ? "true" : undefined} disabled={busy && c.id !== activeId} onClick={() => onSelect(c.id)}
                        className="min-w-0 flex-1 px-2 py-1.5 text-left disabled:opacity-50">
                        <span className="block truncate text-[0.82rem] font-medium">{c.title}</span>
                        {c.preview && <span className="block truncate text-[0.72rem] text-muted">{c.preview}</span>}
                      </button>
                      {onRename && <IconButton label={`Rename ${c.title}`} size="sm" disabled={busy && c.id === activeId} onClick={() => { setDraft(c.title); setRenaming(c.id); }}><Pencil size={13} aria-hidden /></IconButton>}
                      <IconButton label={`Delete ${c.title}`} size="sm" tone="danger" disabled={busy && c.id === activeId} onClick={() => void remove(c)}><Trash2 size={13} aria-hidden /></IconButton>
                    </>
                  )}
                </li>))}
            </ul>
          </section>))}
        {damaged > 0 && <p role="status" className="text-[0.74rem] text-warn">{damaged} {damaged === 1 ? "file of a conversation" : "files of conversations"} could not be read and {damaged === 1 ? "is" : "are"} not listed.</p>}
      </div>
    </nav>
  );
}
