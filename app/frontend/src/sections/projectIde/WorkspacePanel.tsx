import { ChevronDown, FileDiff, GitBranch, PanelRight } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { get } from "./client";

type Checkout = { available: boolean; branch: string | null; revision: string | null; files: unknown[]; truncated: boolean };
type Action = { id: string; label: string; icon: ReactNode; disabled?: boolean; run: () => void };

export function WorkspacePanel({ projectId, actions, onChanges, trigger }: {
  projectId: string; actions: Action[]; onChanges: () => void; trigger: RefObject<HTMLButtonElement>;
}) {
  const [open, setOpen] = useState(false);
  const [checkout, setCheckout] = useState<Checkout>();
  const [failed, setFailed] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const pending = new AbortController();
    setCheckout(undefined);
    setFailed(false);
    void get<Checkout>(`/api/ide/projects/${encodeURIComponent(projectId)}/checkout`, pending.signal)
      .then(result => { if (!pending.signal.aborted) setCheckout(result); })
      .catch(() => { if (!pending.signal.aborted) setFailed(true); });
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => { pending.abort(); document.removeEventListener("pointerdown", dismiss); };
  }, [open, projectId]);

  const select = (run: () => void) => {
    setOpen(false);
    trigger.current?.focus();
    run();
  };
  const branch = failed ? "Branch unavailable" : !checkout ? "Reading branch..." :
    !checkout.available ? "No Git checkout" : checkout.branch || (checkout.revision ? "Detached HEAD" : "No branch");

  return <div ref={root} className="chat-workspace-anchor" onBlur={event => {
    if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }} onKeyDown={event => {
    if (open && event.key === "Escape") { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
  }}>
    <button ref={trigger} type="button" aria-label="Workspace" aria-expanded={open} aria-controls={open ? panelId : undefined}
      className="chat-workspace-trigger" title="Workspace" onClick={() => setOpen(value => !value)}>
      <PanelRight size={16} aria-hidden /><span>Workspace</span><ChevronDown size={13} aria-hidden />
    </button>
    {open && <section id={panelId} className="chat-workspace-panel" aria-label="Workspace details">
      <div className="chat-workspace-branch" aria-live="polite">
        <GitBranch size={16} aria-hidden /><span title={branch}>{branch}</span>
        {checkout?.revision && <code>{checkout.revision.slice(0, 8)}</code>}
      </div>
      <button type="button" onClick={() => select(onChanges)}>
        <FileDiff size={16} aria-hidden /><span>Changes</span>
        {checkout?.available && <small>{checkout.files.length}{checkout.truncated ? "+" : ""} {checkout.files.length === 1 ? "file" : "files"}</small>}
      </button>
      <div className="chat-workspace-actions">
        {actions.map(action => <button key={action.id} type="button" disabled={action.disabled} onClick={() => select(action.run)}>
          {action.icon}<span>{action.label}</span>
        </button>)}
      </div>
    </section>}
  </div>;
}
