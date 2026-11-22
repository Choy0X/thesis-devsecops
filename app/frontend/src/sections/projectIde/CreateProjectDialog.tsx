import { useEffect, useRef, useState, type DragEvent } from "react";
import { Folder, FolderOpen, Upload, X } from "lucide-react";
import { Dialog } from "../../components/hud/Dialog";
import { Button, Field, Input } from "../../components/ui";
import { get, write, type Project } from "./client";

const MAX_PROJECT_NAME = 120;

function folderBasename(path: string) {
  if (/^[a-z]:[\\/]*$/i.test(path)) return "";
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
}

export function CreateProjectDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (project: Project) => void }) {
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const [dragging, setDragging] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const zone = useRef<HTMLDivElement>(null);
  const selection = useRef("");
  const controller = useRef<AbortController>();
  const dropTimer = useRef<ReturnType<typeof setTimeout>>();
  const submitting = useRef(false);
  const nativeDropAt = useRef(-Infinity);
  const choose = (path: string) => {
    selection.current = path; setSource(path); setError(""); setDragging(false);
    const suggestion = folderBasename(path).trim();
    if (suggestion) setName(current => current.trim() ? current : Array.from(suggestion).slice(0, MAX_PROJECT_NAME).join(""));
    clearTimeout(dropTimer.current);
  };
  useEffect(() => {
    const selected = (event: Event) => {
      if (submitting.current || !(event.target instanceof Node) || !zone.current?.contains(event.target)) return;
      const path = (event as CustomEvent<{ path?: unknown }>).detail?.path;
      if (typeof path === "string" && path.trim()) { nativeDropAt.current = performance.now(); choose(path); }
    };
    document.addEventListener("project-source-folder", selected);
    return () => { document.removeEventListener("project-source-folder", selected); controller.current?.abort(); clearTimeout(dropTimer.current); };
  }, []);
  const browse = async () => {
    if (browsing || creating) return;
    const active = new AbortController(); controller.current?.abort(); controller.current = active;
    setBrowsing(true); setError("");
    try {
      const result = await write<{ ok: boolean; id?: string }>("/api/shell/pick-folder", { title: "Choose the project source folder" });
      if (active.signal.aborted) return;
      if (!result.ok || !result.id) throw new Error("Folder browsing requires the desktop app. Open it to choose a local project folder.");
      const deadline = Date.now() + 10 * 60 * 1000;
      while (!active.signal.aborted && Date.now() < deadline) {
        const picked = await get<{ state: string; path: string | null }>(`/api/shell/pick-folder/${encodeURIComponent(result.id)}`, active.signal);
        if (active.signal.aborted || picked.state === "cancelled") return;
        if (picked.state === "chosen" && picked.path) { choose(picked.path); return; }
        await new Promise(resolve => setTimeout(resolve, 300));
      }
      if (!active.signal.aborted) throw new Error("The folder selection expired. Browse again to choose a folder.");
    } catch (reason) { if (!active.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (!active.signal.aborted) setBrowsing(false); }
  };
  const drop = (event: DragEvent) => {
    event.preventDefault(); setDragging(false); setError("");
    if (creating || browsing) return;
    if (performance.now() - nativeDropAt.current < 100) return;
    const files = Array.from(event.dataTransfer.files);
    const entries = Array.from(event.dataTransfer.items || []).filter(item => item.kind === "file");
    if (files.length > 1 || entries.length > 1) { setError("Drop one project folder at a time."); return; }
    const entry = entries[0]?.webkitGetAsEntry?.();
    if (entry && !entry.isDirectory) { setError("Choose a folder, rather than an individual file."); return; }
    const path = (files[0] as (File & { path?: string }) | undefined)?.path;
    if (path && (!entry || entry.isDirectory)) { choose(path); return; }
    // The desktop bridge supplies directory paths that Chromium deliberately hides.
    choose("");
    clearTimeout(dropTimer.current);
    dropTimer.current = setTimeout(() => {
      if (!selection.current) setError("The folder location was not available. Use Browse folders in the desktop app.");
    }, 800);
  };
  const create = async () => {
    if (!name.trim() || !source || submitting.current || browsing) return;
    submitting.current = true; setCreating(true); setError("");
    try {
      const result = await write<Project | { project: Project }>("/api/ide/projects", { name: name.trim(), source });
      onCreated("project" in result ? result.project : result);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { submitting.current = false; setCreating(false); }
  };
  const folderName = folderBasename(source) || source;
  return <Dialog open onOpenChange={open => { if (!open) onClose(); }} dismissible={!creating} title="Create project" description="Name your project and choose its local source folder.">
    <form className="ide-create-project" onSubmit={event => { event.preventDefault(); void create(); }}>
      <Field label="Project name"><Input autoFocus required maxLength={MAX_PROJECT_NAME} value={name} onChange={event => setName(event.target.value)} placeholder="My project" disabled={creating} /></Field>
      <div className="ide-project-source-field"><span>Project source</span>
        <div ref={zone} className={`ide-source-drop ${dragging ? "is-dragging" : ""}`} data-project-source-drop role="group" aria-label="Project source selection"
          onDragEnter={event => { event.preventDefault(); if (!creating && !browsing) setDragging(true); }}
          onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = creating || browsing ? "none" : "copy"; }}
          onDragLeave={event => { if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setDragging(false); }} onDrop={drop}>
          <Upload size={25} aria-hidden /><strong>Drop your project folder here</strong><p>Choose the folder containing your source code.</p>
          <Button type="button" variant="subtle" disabled={browsing || creating} onClick={() => void browse()}><FolderOpen size={15} aria-hidden />{browsing ? "Choosing folder..." : "Browse folders"}</Button>
        </div>
        {source && <div className="ide-source-selected"><Folder size={19} aria-hidden /><div><strong>{folderName}</strong><code>{source}</code></div><Button type="button" variant="ghost" size="sm" aria-label="Remove selected folder" disabled={creating || browsing} onClick={() => choose("")}><X size={15} aria-hidden /></Button></div>}
        <p className="ide-source-hint">Your source folder stays in place. Chats use independent workspaces.</p>
      </div>
      {error && <p className="ide-create-error" role="alert">{error}</p>}
      <div className="ide-create-actions"><Button type="button" variant="ghost" disabled={creating} onClick={onClose}>Cancel</Button><Button type="submit" variant="primary" loading={creating} disabled={creating || browsing || !name.trim() || !source}>Create project</Button></div>
    </form>
  </Dialog>;
}
