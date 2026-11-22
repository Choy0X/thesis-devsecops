import { useEffect, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { api, errorText, type AuditEntry, type Capabilities, type FolderLook, type Project } from "../lib/api";
import { Button, EmptyState, Field, Input, Panel, Skeleton } from "../components/ui";
import { Checkbox } from "../components/kit";
import { Dialog } from "../components/hud/Dialog";
import { useConfirm } from "../components/hud/AlertDialog";
import { useToast } from "../components/Toast";
import { num } from "../lib/format";
import { CapabilityTable, DependencyScan } from "./ProjectScan";
import { CodeScan } from "./ProjectCode";
import { ProjectUpdates } from "./ProjectUpdate";
import { DeepScan } from "./ProjectDeepScan";
import { ACTIONS, CODEBASE_DEPTHS, ChoiceCards, DEPENDENCY_MODES } from "./ProjectActions";

// Projects: code the person owns or may test. A project is added only with the confirmation that it is theirs to test, its folders are checked by the server, and
// they are only ever read. The three things to do with a project are chosen on its page: ask about it, scan its dependencies, scan its codebase, and the scans have modes.

export const when = (iso: string) => iso.slice(0, 16).replace("T", " ");
export const languageText = (l: Record<string, number>) => Object.entries(l).slice(0, 4).map(([k, v]) => `${k} (${num(v)})`).join(", ") || "no source files found";

export { BrowseButton, PICK_POLL_MS } from "../components/BrowseButton";
import { BrowseButton } from "../components/BrowseButton";

/** The one source folder of a new project: the path (typed or chosen) and what the server finds in it, which is also where the name suggestion comes from. */
function FolderField({ value, onChange, onLook }: { value: string; onChange: (v: string) => void; onLook: (look: FolderLook | null) => void }) {
  const [look, setLook] = useState<FolderLook | null>(null);
  const latest = useRef(0);
  const report = useRef(onLook);
  report.current = onLook;
  useEffect(() => {
    const settle = (r: FolderLook | null) => { setLook(r); report.current(r); };
    if (!value.trim()) { settle(null); return; }
    const id = ++latest.current;
    const t = window.setTimeout(() => { api.projectLook(value.trim()).then((r) => { if (id === latest.current) settle(r); }).catch(() => { if (id === latest.current) settle(null); }); }, 400);
    return () => window.clearTimeout(t);
  }, [value]);
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <Input aria-label="Source folder" value={value} placeholder="C:\Users\you\code\my-app" onChange={(e) => onChange(e.target.value)} autoComplete="off" />
        <BrowseButton onChosen={onChange} />
      </div>
      {look && (look.ok
        ? <p className="text-[0.76rem] text-muted">{languageText(look.languages)}{look.ecosystems.length ? `; packages: ${look.ecosystems.join(", ")}` : "; no package manifest found"}{look.truncated ? "; only part of a very large folder was looked at" : ""}.</p>
        : <p role="alert" className="text-[0.76rem] text-crit-soft">{look.reason}</p>)}
    </div>
  );
}

export function CreateProject({ open, onClose, onCreated, statement }: { open: boolean; onClose: () => void; onCreated: (p: Project) => void; statement: string }) {
  const [name, setName] = useState("");
  const [typed, setTyped] = useState(false);                        // the person wrote or cleared the name: a suggestion never replaces it
  const [suggestion, setSuggestion] = useState<{ name: string; source: string } | null>(null);
  const [folder, setFolder] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {                                                // Cancel (or any close) forgets what was entered, including a name the person cleared
    if (!open) { setName(""); setTyped(false); setSuggestion(null); setFolder(""); setAuthorized(false); setError(""); }
  }, [open]);
  const ready = folder.trim() !== "" && authorized;                 // the name is optional: left empty, the server suggests one from the folder
  const looked = (look: FolderLook | null) => {
    const next = look && look.ok && look.suggested_name ? { name: look.suggested_name, source: look.name_source ?? "the folder" } : null;
    setSuggestion(next);
    if (!typed) setName(next?.name ?? "");
  };
  const create = async () => {
    setBusy(true); setError("");
    try {
      const p = await api.createProject({ name: name.trim(), folders: [folder.trim()], authorized });
      setName(""); setTyped(false); setSuggestion(null); setFolder(""); setAuthorized(false);
      onCreated(p);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const hint = typed && name.trim() === ""
    ? "Left empty: a name is made from the folder when the project is added."
    : suggestion && name === suggestion.name ? `Suggested from ${suggestion.source}.`
    : suggestion ? <>You changed the suggested name. <button type="button" className="text-brand-strong hover:underline" onClick={() => { setName(suggestion.name); setTyped(false); }}>Use "{suggestion.name}" again</button></>
    : "Optional. A name is suggested once the folder is chosen.";
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }} title="Add a project" description="Code you own or may test. The app reads its folder and never changes it.">
      <div className="space-y-4">
        <div className="space-y-2">
          <span className="text-[0.8rem] font-medium">Source folder</span>
          <FolderField value={folder} onChange={setFolder} onLook={looked} />
        </div>
        <Field label="Name (optional)" hint={hint}><Input aria-label="Project name" value={name} onChange={(e) => { setName(e.target.value); setTyped(true); }} placeholder="My shop" maxLength={80} /></Field>
        <Checkbox checked={authorized} onCheckedChange={setAuthorized} label="I own this code or am authorised to test it" description={statement} />
        {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy} disabled={!ready} onClick={create}>Add the project</Button>
        </div>
      </div>
    </Dialog>
  );
}

function AuditLog({ id }: { id: string }) {
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.projectAudit(id).then((r) => setRows(r.entries)).catch((e) => setError(errorText(e))); }, [id]);
  return (
    <Panel title="Audit log" desc="Every tool run on this project: what, on which target, when and how it ended. Only ever added to."
      right={<a href={`/api/projects/${encodeURIComponent(id)}/audit/export`} download><Button variant="ghost">Export</Button></a>}>
      {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
      {!rows && !error && <Skeleton className="h-16 w-full" />}
      {rows && rows.length === 0 && <p className="text-[0.8rem] text-muted">Nothing has been run yet.</p>}
      {rows && rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[0.8rem]">
            <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-4 font-medium">When</th><th className="py-2 pr-4 font-medium">Tool</th>
              <th className="py-2 pr-4 font-medium">What</th><th className="py-2 pr-4 font-medium">Ended</th><th className="py-2 font-medium">Exit</th></tr></thead>
            <tbody className="tnum text-muted">
              {rows.map((e, i) => (
                <tr key={`${e.at}-${i}`} className="border-b border-line/60">
                  <td className="py-2 pr-4">{when(e.at)}</td><td className="py-2 pr-4 font-mono text-[0.74rem] text-fg">{e.tool}</td><td className="py-2 pr-4">{e.summary}</td>
                  <td className="py-2 pr-4">{e.ended ? when(e.ended) : ""}</td><td className="py-2">{e.exit_status ?? ""}</td>
                </tr>))}
            </tbody>
          </table>
        </div>)}
    </Panel>
  );
}

export function ProjectPage({ project, onBack, onRemoved, onScanned, onFocusChat }: { project: Project; onBack: () => void; onRemoved: () => void; onScanned?: () => void; onFocusChat?: () => void }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [action, setAction] = useState("dependencies");
  const [dependencyMode, setDependencyMode] = useState("only");
  const [depth, setDepth] = useState("quick");
  useEffect(() => { api.projectCapabilities().then(setCaps).catch(() => undefined); }, []);                // ignored: the table is an addition; the page works without it
  const remove = async () => {
    const ok = await confirm({ title: `Remove ${project.name}?`, description: "The project's record, its audit log and its results are removed from the app. Its source folders are not touched.",
      confirmLabel: "Remove the project", cancelLabel: "Keep it", tone: "critical" });
    if (!ok) return;
    try { await api.deleteProject(project.id); toast.success("The project was removed."); onRemoved(); } catch (e) { toast.error(errorText(e)); }
  };
  const chooseAction = (id: string) => { if (id === "chat") onFocusChat?.(); else setAction(id); };
  return (
    <div className="space-y-5">
      <Panel title={project.name} desc={`Added ${when(project.created_at)}. Confirmed on ${when(project.authorization.at)} that you own the code or may test it.`}
        right={<div className="flex gap-2"><Button variant="ghost" onClick={onBack}>All projects</Button><Button variant="ghost" onClick={remove}>Remove</Button></div>}>
        <div className="space-y-3 text-[0.82rem]">
          <ul className="space-y-1" aria-label="Source folders">
            {project.folders.map((f) => <li key={f.path} className="break-all font-mono text-[0.76rem]">{f.path} <span className="font-sans text-muted">- {languageText(f.languages)}</span></li>)}
          </ul>
          <p className="text-muted">Packages: {project.ecosystems.length ? project.ecosystems.join(", ") : "no package manifest was found"}.</p>
        </div>
      </Panel>
      <ChoiceCards legend="What to do with this project" choices={ACTIONS} value={action} onChange={chooseAction} />
      {action === "dependencies" && (
        <>
          <ChoiceCards legend="How far to go" choices={DEPENDENCY_MODES} value={dependencyMode} onChange={setDependencyMode} />
          <DependencyScan project={project} onScanned={onScanned} />
          {dependencyMode === "update" && <ProjectUpdates project={project} onChanged={onScanned} />}
        </>)}
      {action === "codebase" && (
        <>
          <ChoiceCards legend="How deep to go" choices={CODEBASE_DEPTHS} value={depth} onChange={setDepth} />
          {depth === "quick" ? <CodeScan project={project} onScanned={onScanned} /> : <DeepScan project={project} onScanned={onScanned} />}
        </>)}
      {caps && <Panel title="What the app can check for this project" desc="Per package ecosystem. A cell that says Not yet is not checked at all: an empty result there is not a clean one."><CapabilityTable data={caps} mine={project.ecosystems} /></Panel>}
      <AuditLog id={project.id} />
    </div>
  );
}

/** ``initialOpen`` opens the page of that project (an address that names it); ``onFocusChat`` lets the chat of a project (Agentic mode is one of its own tabs) take the whole
 * window (the page that holds this decides how). */
export function Projects({ initialOpen, onFocusChat }: { initialOpen?: string | null; onFocusChat?: (projectId: string) => void } = {}) {
  const [data, setData] = useState<{ projects: Project[]; statement: string } | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState<string | null>(initialOpen ?? null);
  useEffect(() => { if (initialOpen) setOpenId(initialOpen); }, [initialOpen]);
  const load = () => api.projects().then((r) => { setData(r); setError(""); }).catch((e) => setError(errorText(e)));
  useEffect(() => { load(); }, []);
  const opened = data?.projects.find((p) => p.id === openId);
  if (opened) return <ProjectPage project={opened} onBack={() => setOpenId(null)} onRemoved={() => { setOpenId(null); load(); }} onScanned={load}
    onFocusChat={onFocusChat ? () => onFocusChat(opened.id) : undefined} />;
  return (
    <div className="space-y-5">
      <Panel title="Projects" desc="Code you own or may test. Add one to look at its dependencies and its code in a sandboxed copy." right={<Button variant="primary" icon={<Plus size={14} aria-hidden />} onClick={() => setAdding(true)}>Add a project</Button>}>
        {error && <p role="alert" className="text-[0.82rem] text-crit-soft">The projects could not be read: {error}</p>}
        {!data && !error && <Skeleton className="h-24 w-full" />}
        {data && data.projects.length === 0 && <EmptyState title="No project yet" hint="Add the folder of a project you own or may test, and confirm that it is yours to test." />}
        {data && data.projects.length > 0 && (
          <ul className="grid gap-3 sm:grid-cols-2" aria-label="Projects">
            {data.projects.map((p) => (
              <li key={p.id} className="border border-line bg-surface-2 p-3.5">
                <button type="button" onClick={() => setOpenId(p.id)} className="block w-full text-left outline-none focus-visible:ring-1 focus-visible:ring-brand">
                  <span className="block text-[0.92rem] font-semibold">{p.name}</span>
                  <span className="mt-1 block truncate font-mono text-[0.72rem] text-muted" title={p.folders.map((f) => f.path).join("\n")}>{p.folders.map((f) => f.path).join(", ")}</span>
                  <span className="mt-1.5 block text-[0.78rem] text-muted">{languageText(p.languages)}</span>
                  <span className="mt-0.5 block text-[0.76rem] text-faint">{p.ecosystems.length ? `Packages: ${p.ecosystems.join(", ")}` : "No package manifest"} · {p.last_scan ? `last scan ${when(p.last_scan.at)}` : "not scanned yet"}</span>
                </button>
              </li>))}
          </ul>)}
      </Panel>
      {data && <CreateProject open={adding} statement={data.statement} onClose={() => setAdding(false)} onCreated={(p) => { setAdding(false); load(); setOpenId(p.id); }} />}
    </div>
  );
}
