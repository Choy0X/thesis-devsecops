import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Check, Pencil, Pin, PinOff, Trash2, X } from "lucide-react";
import { whenWords } from "../../lib/format";
import { useConfirm } from "../../components/hud/AlertDialog";
import { Input } from "./Controls";
import type { Chat } from "./client";
import { friendlyRunError } from "./runError";

function lastActivity(chat: Chat) {
  return Date.parse(chat.last_active_at || "") || 0;
}

export function ResumePicker({ chats, currentId, query, onPick, onClose, onUpdate, onDelete }: {
  chats: Chat[]; currentId: string; query: string; onPick: (id: string) => void; onClose: () => void;
  onUpdate?: (id: string, fields: { title?: string; pinned?: boolean }) => Promise<void>;
  onDelete?: (id: string) => Promise<void>;
}) {
  const confirm = useConfirm();
  const [filter, setFilter] = useState(query);
  const [highlight, setHighlight] = useState("");
  const [editing, setEditing] = useState<{ id: string; title: string }>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const field = useRef<HTMLInputElement>(null);
  const rename = useRef<HTMLInputElement>(null);
  const listId = useId();
  const search = filter.trim().toLowerCase();
  const rows = chats.filter(chat => chat.title.toLowerCase().includes(search)).sort((a, b) =>
    Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) ||
    Number(Boolean(a.archived)) - Number(Boolean(b.archived)) || lastActivity(b) - lastActivity(a));
  const pinned = rows.filter(chat => chat.pinned);
  const open = rows.filter(chat => !chat.pinned && !chat.archived);
  const archived = rows.filter(chat => !chat.pinned && chat.archived);
  const options = rows.filter(chat => chat.id !== currentId);
  const currentIndex = Math.max(0, options.findIndex(chat => chat.id === highlight));
  const selected = options[currentIndex];
  const selectedOptionId = selected ? `${listId}-${selected.id}` : undefined;

  useEffect(() => { field.current?.focus(); }, []);
  useEffect(() => { if (editing?.id) rename.current?.select(); }, [editing?.id]);
  useEffect(() => { if (selectedOptionId) document.getElementById(selectedOptionId)?.scrollIntoView({ block: "nearest" }); }, [selectedOptionId]);

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation();
      if (editing) { setEditing(undefined); field.current?.focus(); }
      else onClose();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (event.target !== field.current) return;
      event.preventDefault();
      const next = currentIndex + (event.key === "ArrowDown" ? 1 : -1);
      const option = options[Math.min(options.length - 1, Math.max(0, next))];
      if (option) setHighlight(option.id);
    } else if (event.key === "Enter" && event.target === field.current) {
      event.preventDefault();
      if (selected) onPick(selected.id);
    }
  };
  const manage = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError("");
    try { await action(); }
    catch (reason) { setError(friendlyRunError(reason)); }
    finally { setBusy(false); }
  };
  const renderRow = (chat: Chat) => {
    const current = chat.id === currentId;
    const activity = lastActivity(chat);
    const edit = editing?.id === chat.id ? editing : undefined;
    return <div key={chat.id} className="chat-resume-row" role="row" aria-selected={chat.id === selected?.id}>
      <div role="gridcell" id={`${listId}-${chat.id}`} className="chat-resume-content">
        {edit ? <form className="chat-resume-rename" onSubmit={event => {
          event.preventDefault();
          if (!edit.title.trim() || !onUpdate) return;
          void manage(async () => {
            await onUpdate(chat.id, { title: edit.title.trim() });
            setEditing(undefined); field.current?.focus();
          });
        }}>
          <Input ref={rename} aria-label="Conversation title" value={edit.title} maxLength={160} disabled={busy}
            onChange={event => setEditing({ ...edit, title: event.target.value })} />
          <button type="submit" className="ide-icon" aria-label="Save conversation title" disabled={busy || !edit.title.trim()}><Check size={15} aria-hidden /></button>
          <button type="button" className="ide-icon" aria-label="Cancel rename" onClick={() => { setEditing(undefined); field.current?.focus(); }}><X size={15} aria-hidden /></button>
        </form> : <button type="button" className="chat-resume-option" aria-disabled={current || undefined} onClick={() => { if (!current) onPick(chat.id); }}>
          <span className="chat-resume-name"><span>{chat.title}</span>{current && <small>Current</small>}{chat.pinned && chat.archived && <small>Archived</small>}</span>
          <span className="chat-resume-meta"><span>{chat.model?.name || "Unknown model"}</span>{activity !== 0 && <time dateTime={chat.last_active_at} title={new Date(activity).toLocaleString()}>{whenWords(activity / 1000)}</time>}</span>
        </button>}
      </div>
      {(onUpdate || onDelete) && <div role="gridcell" className="chat-resume-actions">
        {onUpdate && <>
          <button type="button" className="ide-icon" disabled={busy} title={chat.pinned ? "Unpin" : "Pin"}
            aria-label={`${chat.pinned ? "Unpin" : "Pin"} ${chat.title}`} onClick={() => void manage(() => onUpdate(chat.id, { pinned: !chat.pinned }))}>
            {chat.pinned ? <PinOff size={14} aria-hidden /> : <Pin size={14} aria-hidden />}
          </button>
          <button type="button" className="ide-icon" disabled={busy} title="Rename" aria-label={`Rename ${chat.title}`}
            onClick={() => { setError(""); setEditing({ id: chat.id, title: chat.title }); }}><Pencil size={14} aria-hidden /></button>
        </>}
        {onDelete && <button type="button" className="ide-icon" disabled={busy} title="Delete" aria-label={`Delete ${chat.title}`}
          onClick={() => void manage(async () => {
            if (await confirm({
              title: "Delete conversation?", confirmLabel: "Delete conversation", tone: "critical",
              description: <>Delete <strong>{chat.title}</strong>, its saved messages and its workspace copy. The project files stay as they are. If the workspace holds changes that were not integrated, the conversation is not deleted.</>,
            })) {
              await onDelete(chat.id);
              if (editing?.id === chat.id) setEditing(undefined);
              field.current?.focus();
            }
          })}><Trash2 size={14} aria-hidden /></button>}
      </div>}
    </div>;
  };

  return <section className="chat-resume-picker" aria-label="Resume conversation" onKeyDown={onKeyDown}>
    <div className="chat-resume-heading"><strong>Resume conversation</strong><button className="ide-icon" aria-label="Close conversation picker" onClick={onClose}><X size={15} /></button></div>
    <Input ref={field} value={filter} onChange={event => { setFilter(event.target.value); setHighlight(""); }} autoComplete="off" placeholder="Filter by title"
      role="combobox" aria-label="Filter conversations" aria-autocomplete="list" aria-haspopup="grid" aria-expanded="true" aria-controls={listId} aria-activedescendant={selectedOptionId} />
    {error && <p className="ide-error" role="alert">{error}</p>}
    {!options.length && <p className="chat-resume-empty" role="status">{search && chats.some(chat => chat.id !== currentId) ? "No conversations match this filter" : "No earlier conversations in this project"}</p>}
    <div className="chat-resume-results" role="grid" id={listId} aria-label="Project conversations">
      {!!pinned.length && <div role="rowgroup" aria-label="Pinned"><div className="chat-resume-group-label" aria-hidden="true">Pinned</div>{pinned.map(renderRow)}</div>}
      {open.map(renderRow)}
      {!!archived.length && <div role="rowgroup" aria-label="Archived"><div className="chat-resume-group-label" aria-hidden="true">Archived</div>{archived.map(renderRow)}</div>}
    </div>
  </section>;
}
