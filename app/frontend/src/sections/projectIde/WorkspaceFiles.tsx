import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, FileCode2, Folder, FolderOpen, RefreshCw, Save, X } from "lucide-react";
import type { WorkspaceReference } from "../../components/chat/Markdown";
import { Input } from "./Controls";
import { endpoint, get, write } from "./client";
import { friendlyRunError } from "./runError";

const Editor = lazy(() => import("./Editor").then(module => ({ default: module.Editor })));
const nameOf = (path: string) => path.slice(path.lastIndexOf("/") + 1);
const folderOf = (path: string) => path.slice(0, Math.max(0, path.lastIndexOf("/")));

type FileVersion = { path: string; text: string; version: string };
export type FileDrafts = Map<string, { file: FileVersion; text: string }>;
type Entry = { path: string; directory: boolean };
type Selection = { folder: string; path?: string; line?: number };

function indexFiles(files: { path: string }[]) {
  const entries = new Map<string, Entry>();
  const children = new Map<string, Entry[]>();
  for (const file of files) {
    let path = file.path;
    let directory = false;
    while (path && !entries.has(path)) {
      const entry = { path, directory };
      entries.set(path, entry);
      const folder = folderOf(path);
      const siblings = children.get(folder) || [];
      siblings.push(entry);
      children.set(folder, siblings);
      path = folder;
      directory = true;
    }
  }
  for (const siblings of children.values()) {
    siblings.sort((a, b) => Number(b.directory) - Number(a.directory) || nameOf(a.path).localeCompare(nameOf(b.path)));
  }
  return { entries, children };
}

function resolveReference(entries: Map<string, Entry>, reference: WorkspaceReference) {
  const exact = entries.get(reference.path);
  if (exact && (!reference.directory || exact.directory)) return exact;
  if (reference.path.includes("/")) return;
  const matches = [...entries.values()].filter(entry => nameOf(entry.path) === reference.path && (!reference.directory || entry.directory));
  if (matches.length === 1) return matches[0];
}

function OpenFile({ chatId, path, line, drafts, readOnly, onSaved }: {
  chatId: string; path: string; line?: number; drafts: FileDrafts; readOnly: boolean; onSaved: () => void;
}) {
  const draft = drafts.get(path);
  const [file, setFile] = useState(draft?.file);
  const [text, setText] = useState(draft?.text ?? "");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(!draft);
  const [request, setRequest] = useState(0);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (request === 0 && drafts.has(path)) return;
    const abort = new AbortController();
    const query = new URLSearchParams({ path });
    void get<FileVersion>(`${endpoint(chatId)}/file?${query}`, abort.signal).then(result => {
      if (abort.signal.aborted) return;
      drafts.delete(path);
      setFile(result); setText(result.text); setError(""); setSaved(false);
    }).catch(reason => { if (!abort.signal.aborted) setError(friendlyRunError(reason)); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [chatId, drafts, path, request]);

  const dirty = !!file && text !== file.text;
  const useCrlf = useMemo(() => !!file?.text.includes("\r\n") && !file.text.replace(/\r\n/g, "").includes("\n"), [file]);
  const save = async () => {
    if (!file || !dirty || readOnly || loading || savingRef.current) return;
    savingRef.current = true; setSaving(true); setError("");
    try {
      const result = await write<FileVersion>(`${endpoint(chatId)}/file`, { path, text, version: file.version }, "PUT");
      const latest = drafts.get(path)?.text ?? text;
      if (latest === result.text) drafts.delete(path); else drafts.set(path, { file: result, text: latest });
      setFile(result); setText(latest); setSaved(true); onSaved();
    } catch (reason) {
      setError(friendlyRunError(reason));
    } finally { savingRef.current = false; setSaving(false); }
  };

  return <>
    <div className="ide-editor-toolbar workspace-file-toolbar">
      <code title={path}>{path}</code>
      <span role="status">{saving ? "Saving..." : loading ? "Opening..." : dirty ? "Unsaved" : saved ? "Saved" : readOnly && file ? "Read-only" : ""}</span>
      <button type="button" disabled={!dirty || readOnly || saving || loading} onClick={() => void save()} title="Save file (Ctrl+S)"><Save size={14} aria-hidden />Save file</button>
    </div>
    {error && <div className="workspace-file-feedback" role="alert"><p className="ide-error">{error}</p>
      <button type="button" onClick={() => { setLoading(true); setError(""); setRequest(value => value + 1); }}>{dirty ? "Discard edits and reload" : "Retry file"}</button>
    </div>}
    {!file && !error && <p className="workspace-file-feedback" role="status">Opening {nameOf(path)}...</p>}
    {file && <Suspense fallback={<p className="workspace-file-feedback" role="status">Loading editor...</p>}>
      <Editor path={path} text={text} line={line} readOnly={readOnly || saving || loading} onChange={value => {
        const updated = useCrlf ? value.replace(/\n/g, "\r\n") : value;
        setText(updated); setSaved(false);
        if (updated === file.text) drafts.delete(path); else drafts.set(path, { file, text: updated });
      }} onSave={() => void save()} />
    </Suspense>}
  </>;
}

export function WorkspaceFiles({ chatId, reference, drafts, readOnly, onClose, onSaved }: {
  chatId: string; reference?: WorkspaceReference; drafts: FileDrafts; readOnly: boolean; onClose: () => void; onSaved: () => void;
}) {
  const [files, setFiles] = useState<{ path: string }[]>();
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [filter, setFilter] = useState("");
  const [navigation, setNavigation] = useState<{ reference?: WorkspaceReference; selection: Selection }>();
  const region = useRef<HTMLElement>(null);
  useEffect(() => { region.current?.focus(); setFilter(""); }, [reference]);
  useEffect(() => {
    const abort = new AbortController();
    void get<{ files: { path: string }[] }>(`${endpoint(chatId)}/files`, abort.signal).then(result => {
      if (!abort.signal.aborted) { setFiles(result.files); setError(""); }
    }).catch(reason => { if (!abort.signal.aborted) setError(friendlyRunError(reason)); });
    return () => abort.abort();
  }, [chatId, reload]);

  const index = useMemo(() => indexFiles(files ?? []), [files]);
  const cited = reference ? resolveReference(index.entries, reference) : undefined;
  const hasNavigation = navigation && navigation.reference === reference;
  const { folder, path, line }: Selection = hasNavigation ? navigation.selection : cited?.directory ? { folder: cited.path }
    : cited ? { folder: folderOf(cited.path), path: cited.path, line: reference?.line } : { folder: "" };
  const navigate = (selection: Selection) => { setFilter(""); setNavigation({ reference, selection }); };
  const segments = folder ? folder.split("/") : [];
  const rows = (index.children.get(folder) || []).filter(entry => nameOf(entry.path).toLowerCase().includes(filter.trim().toLowerCase()));

  return <aside className="chat-detail-panel workspace-files" aria-label="Workspace files" tabIndex={-1} ref={region} onKeyDown={event => {
    if (event.key === "Escape" && !event.defaultPrevented) { event.stopPropagation(); onClose(); }
  }}>
    <header className="chat-detail-header workspace-files-header"><strong><FolderOpen size={16} aria-hidden />Workspace files</strong><div>
      <button type="button" className="ide-icon" aria-label="Refresh files" title="Refresh files" onClick={() => { setError(""); setReload(value => value + 1); }}><RefreshCw size={15} aria-hidden /></button>
      <button type="button" className="ide-icon" aria-label="Close workspace files" title="Close workspace files" onClick={onClose}><X size={17} aria-hidden /></button>
    </div></header>
    <nav className="workspace-file-breadcrumbs" aria-label="Workspace folder">
      <button type="button" onClick={() => navigate({ folder: "" })} aria-current={!folder && !path ? "location" : undefined}>Root</button>
      {segments.map((segment, index) => {
        const destination = segments.slice(0, index + 1).join("/");
        return <span key={destination}><ChevronRight size={13} aria-hidden /><button type="button" title={destination} onClick={() => navigate({ folder: destination })} aria-current={destination === folder && !path ? "location" : undefined}>{segment}</button></span>;
      })}
    </nav>
    {error && <p className="workspace-file-feedback ide-error" role="alert">{error}</p>}
    {files && reference && !cited && !hasNavigation && <p className="workspace-file-feedback" role="status">Could not find a unique workspace match for <code>{reference.path}</code>. Browse files below.</p>}
    {!files && !error && <p className="workspace-file-feedback" role="status">Loading workspace files...</p>}
    {path ? <OpenFile key={path} chatId={chatId} path={path} line={line} drafts={drafts} readOnly={readOnly} onSaved={onSaved} /> : files && <>
      <div className="workspace-files-filter"><Input type="search" aria-label="Filter files in this folder" placeholder="Filter this folder" value={filter} onChange={event => setFilter(event.target.value)} /></div>
      <ul className="workspace-file-list" aria-label={folder ? `${folder} contents` : "Root contents"}>
        {rows.map(entry => <li key={entry.path}><button type="button" title={entry.path} onClick={() => navigate(entry.directory ? { folder: entry.path } : { folder, path: entry.path })}>
          {entry.directory ? <Folder size={16} aria-hidden /> : <FileCode2 size={16} aria-hidden />}
          <span>{nameOf(entry.path)}</span>{entry.directory ? <ChevronRight size={14} aria-hidden /> : drafts.has(entry.path) && <small>Unsaved</small>}
        </button></li>)}
        {!rows.length && <li className="workspace-file-feedback">{filter ? "No matching files or folders." : "No accessible files here."}</li>}
      </ul>
    </>}
  </aside>;
}
