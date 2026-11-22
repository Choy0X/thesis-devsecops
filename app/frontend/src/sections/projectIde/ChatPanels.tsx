import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, FileCode2, Folder, Link, Plus, Trash2, X } from "lucide-react";
import { Markdown } from "../../components/chat/Markdown";
import { Input, IdeSelect } from "./Controls";
import { endpoint, get, write, type Change } from "./client";
import { friendlyRunError } from "./runError";
import { DiffView } from "./DiffView";

export type ChatSource = { id: string; kind: string; label: string; primary?: boolean; path?: string; url?: string; available?: boolean };
export type ChatPanel = "plan" | "subagents" | "sources" | "dependencies";

export function DetailPanel({ name, onClose, children }: { name: string; onClose: () => void; children: ReactNode }) {
  const region = useRef<HTMLElement>(null);
  useEffect(() => { region.current?.focus(); }, [name]);
  return <aside className="chat-detail-panel" aria-label={`${name} panel`} tabIndex={-1} ref={region}>
    <div className="chat-detail-header"><strong>{name}</strong><button className="ide-icon" aria-label={`Close ${name.toLowerCase()}`} onClick={onClose}><X size={17} /></button></div>
    <div className="chat-detail-content">{children}</div>
  </aside>;
}

export function ChangesPanel({ chatId, changes, busy, onIntegrated }: { chatId: string; changes: Change[]; busy: boolean; onIntegrated: () => void }) {
  const [selected, setSelected] = useState("");
  const [reviewed, setReviewed] = useState<Record<string, string | null>>({});
  const [integrating, setIntegrating] = useState(false);
  const [error, setError] = useState("");
  const change = changes.find(item => item.path === selected);
  const review = Object.fromEntries(changes.filter(item => Object.prototype.hasOwnProperty.call(reviewed, item.path) && reviewed[item.path] === (item.after ?? null)).map(item => [item.path, item.after ?? null]));
  if (!changes.length) return <div className="chat-panel-empty"><FileCode2 size={25} /><h3>No changes yet</h3><p>Changes the agent makes appear here. Review the diff before applying them to your project.</p></div>;
  return <div className="chat-changes"><h3>Workspace changes</h3>
    <div className="chat-change-list">{changes.map(item => <button className={selected === item.path ? "is-selected" : ""} key={item.path} onClick={() => setSelected(item.path)}><span>{item.status}</span><code>{item.path}</code>{Object.prototype.hasOwnProperty.call(review, item.path) && <Check size={13} aria-label="Reviewed" />}</button>)}</div>
    {change ? <section className="chat-diff" aria-label={`Workspace diff for ${change.path}`}><div className="ide-toolbar"><code>{change.path}</code><button disabled={busy || !change.diff} onClick={() => setReviewed(items => ({ ...items, [change.path]: change.after ?? null }))}>Mark reviewed</button></div>{change.diff ? <DiffView diff={change.diff} /> : <p>No text diff available.</p>}</section> : <p className="ide-rail-hint">Select a changed file to review its diff.</p>}
    {error && <p role="alert" className="ide-error">{error}</p>}
    <button className="ide-primary" disabled={busy || integrating || !Object.keys(review).length} onClick={() => {
      setIntegrating(true); setError("");
      void write(`${endpoint(chatId)}/integrations`, { paths: Object.keys(review), review }).then(() => { setReviewed({}); onIntegrated(); }).catch(reason => setError(friendlyRunError(reason))).finally(() => setIntegrating(false));
    }}>{integrating ? "Applying changes..." : "Integrate reviewed files"}</button>
  </div>;
}

export function ObservedPanel({ chatId, kind }: { chatId: string; kind: "plan" | "subagents" }) {
  const [data, setData] = useState<{ items?: { content: string; status: string }[]; text?: string; subagents?: { id: string; description: string; status: string; agent?: string }[] }>();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { const result = await get<typeof data>(`${endpoint(chatId)}/${kind}`, controller.signal); if (!controller.signal.aborted) { setData(result); setError(""); } }
      catch (reason) { if (!controller.signal.aborted) setError(friendlyRunError(reason)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2500);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [chatId, kind, attempt]);
  if (error) return <div role="alert"><p className="ide-error">{error}</p><button onClick={() => setAttempt(value => value + 1)}>Retry</button></div>;
  if (!data) return <p role="status">Loading {kind}...</p>;
  if (kind === "plan") return data.items?.length || data.text ? <><ol className="chat-plan-list">{data.items?.map((item, index) => <li key={index} data-state={item.status}><span>{item.status === "completed" ? <Check size={15} /> : index + 1}</span><div><p>{item.content}</p><small>{item.status.replace(/_/g, " ")}</small></div></li>)}</ol>{data.text && <Markdown text={data.text} />}</> : <div className="chat-panel-empty"><h3>No plan yet</h3><p>Ask the agent to plan a change. Its recorded plan and progress will appear here.</p></div>;
  return data.subagents?.length ? <ul className="chat-subagent-list">{data.subagents.map(agent => <li key={agent.id}><strong>{agent.description}</strong><small>{agent.agent || "Subagent"}: {agent.status}</small></li>)}</ul> : <div className="chat-panel-empty"><h3>No subagents yet</h3><p>Delegated tasks appear here when the agent starts them.</p></div>;
}

export function SourcesPanel({ chatId, sources, onChange, autoAdd = false }: { chatId: string; sources: ChatSource[]; onChange: () => Promise<void>; autoAdd?: boolean }) {
  const [adding, setAdding] = useState(autoAdd);
  const [kind, setKind] = useState("project_file");
  const [files, setFiles] = useState<{ path: string }[]>([]);
  const [selected, setSelected] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController>();
  useEffect(() => { if (autoAdd) setAdding(true); }, [autoAdd]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!adding || kind !== "project_file") return;
    const active = new AbortController();
    void get<{ files: { path: string }[] }>(`${endpoint(chatId)}/files`, active.signal).then(result => { if (!active.signal.aborted) { setFiles(result.files); setError(""); } }).catch(reason => { if (!active.signal.aborted) setError(friendlyRunError(reason)); });
    return () => active.abort();
  }, [chatId, adding, kind]);
  const add = async (body: unknown) => { await write(`${endpoint(chatId)}/sources`, body); await onChange(); setAdding(false); setSelected(""); setUrl(""); };
  const browse = async () => {
    const active = new AbortController(); controller.current = active; setBusy(true); setError("");
    try {
      const picker = await write<{ ok: boolean; id?: string }>("/api/shell/pick-folder", { title: "Choose a read-only source folder" });
      if (!picker.ok || !picker.id) throw new Error("Open the desktop app to browse local source folders.");
      const deadline = Date.now() + 600000;
      while (!active.signal.aborted && Date.now() < deadline) {
        const result = await get<{ state: string; path?: string }>(`/api/shell/pick-folder/${encodeURIComponent(picker.id)}`, active.signal);
        if (active.signal.aborted || result.state === "cancelled") return;
        if (result.state === "chosen" && result.path) { await add({ kind: "local", path: result.path }); return; }
        await new Promise(resolve => setTimeout(resolve, 300));
      }
      if (!active.signal.aborted) throw new Error("Folder selection expired. Browse again.");
    } catch (reason) { if (!active.signal.aborted) setError(friendlyRunError(reason)); }
    finally { if (!active.signal.aborted) setBusy(false); }
  };
  return <div className="chat-sources"><p className="ide-rail-hint">Read-only context available to this chat. Sources are included with future messages.</p><ul>{sources.map(source => <li key={source.id}><div>{source.kind === "url" ? <Link size={16} /> : source.primary || source.kind === "local" ? <Folder size={16} /> : <FileCode2 size={16} />}<div><strong>{source.label}</strong><small>{source.primary ? "Project source" : "Added source"}{source.available === false ? ", unavailable" : ""}</small><code>{source.path || source.url}</code></div></div>{!source.primary && <button className="ide-icon" aria-label={`Remove source ${source.label}`} disabled={busy} onClick={() => { setBusy(true); void write(`${endpoint(chatId)}/sources/${encodeURIComponent(source.id)}`, undefined, "DELETE").then(onChange).catch(reason => setError(friendlyRunError(reason))).finally(() => setBusy(false)); }}><Trash2 size={14} /></button>}</li>)}</ul>
    {!adding ? <button onClick={() => setAdding(true)}><Plus size={15} />Add source</button> : <form className="chat-add-source" onSubmit={event => { event.preventDefault(); setBusy(true); setError(""); void add(kind === "url" ? { kind: "url", url } : { kind: "project_file", path: selected }).catch(reason => setError(friendlyRunError(reason))).finally(() => setBusy(false)); }}>
      <h3>Add a source</h3><IdeSelect aria-label="Source type" value={kind} disabled={busy} onChange={event => { setKind(event.target.value); setError(""); }}><option value="project_file">Project file</option><option value="local">Local folder</option><option value="url">URL reference</option></IdeSelect>
      {kind === "project_file" && <label>Project file<IdeSelect aria-label="Project file" value={selected} disabled={busy} onChange={event => setSelected(event.target.value)}><option value="">Choose a file</option>{files.map(file => <option key={file.path} value={file.path}>{file.path}</option>)}</IdeSelect></label>}
      {kind === "url" && <><label>Source URL<Input aria-label="Source URL" type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://example.com/documentation" required /></label><small>Saved as a reference. Page contents are not downloaded automatically.</small></>}
      {kind === "local" && <><p>Choose a folder to add bounded, read-only source excerpts. Credential and generated files are excluded.</p><button type="button" disabled={busy} onClick={() => void browse()}><Folder size={15} />{busy ? "Choosing folder..." : "Browse folders"}</button></>}
      <div className="ide-toolbar">{kind !== "local" && <button className="ide-primary" disabled={busy || (kind === "project_file" ? !selected : !url.trim())}>Add source</button>}<button type="button" disabled={busy} onClick={() => setAdding(false)}>Cancel</button></div>
    </form>}
    {error && <p role="alert" className="ide-error">{error}</p>}
  </div>;
}
