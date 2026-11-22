import { Check, Copy, FolderOpen, Plus, Puzzle, ScrollText, Trash2 } from "lucide-react";
import { useId, useRef, useState, type RefObject } from "react";
import { useConfirm } from "../../components/hud/AlertDialog";
import { Dialog } from "../../components/hud/Dialog";
import { Button } from "../../components/kit/Button";
import { IconButton } from "../../components/kit/IconButton";
import { TextareaField, TextField } from "../../components/kit/TextField";
import { Extensions } from "./Extensions";
import { write, type ContextUsage, type Project } from "./client";
import "./projectSettings.css";

const sections = [
  { id: "general", label: "General", icon: FolderOpen },
  { id: "context", label: "Agent context", icon: ScrollText },
  { id: "extensions", label: "Extensions", icon: Puzzle },
] as const;

const contextLimit = 8000;
const memoryLimit = 100;
const characters = (text: string) => Array.from(text).length;
const projectContext = (project: Project) => ({
  instructions: project.settings?.instructions ?? project.instructions ?? "",
  memory: project.settings?.memory ?? project.memory ?? [],
});
const memoryRows = (memory: string[]) => memory.map((text, id) => ({ id, text }));

export function ProjectSettingsDialog({ project, contextUsage, savedMessages, turnActive, onClose, returnFocus, onSaved }: {
  project: Project;
  contextUsage?: ContextUsage | null;
  savedMessages?: number;
  turnActive: boolean;
  onClose: () => void;
  returnFocus: RefObject<HTMLButtonElement>;
  onSaved: (updated: Project) => void;
}) {
  const confirm = useConfirm();
  const formId = useId();
  const scroller = useRef<HTMLDivElement>(null);
  const addMemory = useRef<HTMLButtonElement>(null);
  const [section, setSection] = useState<(typeof sections)[number]["id"]>("general");
  const [visitedExtensions, setVisitedExtensions] = useState(false);
  const [saved, setSaved] = useState(() => ({ name: project.name, ...projectContext(project) }));
  const [name, setName] = useState(saved.name);
  const [instructions, setInstructions] = useState(saved.instructions);
  const [entries, setEntries] = useState(() => memoryRows(saved.memory));
  const nextEntry = useRef(entries.length);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedMessage, setSavedMessage] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"copied" | "failed">();

  const memory = entries.filter(entry => entry.text.trim() || entry.text === saved.memory[entry.id]).map(entry => entry.text);
  const memoryChanged = memory.length !== saved.memory.length || memory.some((text, index) => text !== saved.memory[index]);
  const patch = {
    ...(name.trim() !== saved.name ? { name: name.trim() } : {}),
    ...(instructions !== saved.instructions ? { instructions } : {}),
    ...(memoryChanged ? { memory } : {}),
  };
  const used = characters(instructions) + memory.reduce((sum, text) => sum + characters(text), 0);
  const overBudget = used > contextLimit;
  const nameError = !name.trim() ? "Enter a project name." : characters(name.trim()) > 120 ? "Use 120 characters or fewer." : "";
  const dirty = Object.keys(patch).length > 0;
  const canSave = dirty && !turnActive && !saving && !nameError && !overBudget;

  function edit() {
    setSavedMessage(false);
    setSaveError("");
  }

  async function save() {
    if (!canSave) return;
    setSaving(true);
    setSaveError("");
    try {
      const updated = await write<Project>(`/api/ide/projects/${encodeURIComponent(project.id)}`, patch, "PATCH");
      const context = projectContext(updated);
      setSaved({ name: updated.name, ...context });
      setName(updated.name);
      setInstructions(context.instructions);
      setEntries(memoryRows(context.memory));
      nextEntry.current = context.memory.length;
      onSaved(updated);
      setSavedMessage(true);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  async function close() {
    if (saving) return;
    if (dirty && !await confirm({
      title: "Discard changes?",
      description: "Your unsaved edits to this project will be lost.",
      confirmLabel: "Discard changes",
      cancelLabel: "Keep editing",
    })) return;
    onClose();
  }

  async function copyPath(path: string) {
    try {
      await navigator.clipboard.writeText(path);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  }

  function focusEntry(id?: number) {
    requestAnimationFrame(() => {
      if (id === undefined) addMemory.current?.focus();
      else document.getElementById(`${formId}-memory-${id}`)?.focus();
    });
  }

  return (
    <Dialog open title="Project settings" description={project.name}
      className="project-settings-dialog w-[min(52rem,100%)]"
      bodyClassName="project-settings-body overflow-hidden p-0"
      onOpenChange={open => { if (!open) void close(); }}
      onCloseAutoFocus={event => {
        if (!returnFocus.current?.isConnected) return;
        event.preventDefault();
        returnFocus.current.focus({ preventScroll: true });
      }}>
      <div className="project-settings-layout">
        <nav className="project-settings-nav" aria-label="Project settings sections">
          {sections.map(({ id, label, icon: Icon }) => (
            <Button key={id} variant="ghost" aria-current={section === id ? "true" : undefined}
              icon={<Icon size={16} aria-hidden />} onClick={() => {
                if (id === section) return;
                setSection(id);
                if (id === "extensions") setVisitedExtensions(true);
                if (scroller.current) scroller.current.scrollTop = 0;
              }}>{label}</Button>
          ))}
        </nav>
        <div className="project-settings-scroll" ref={scroller}>
          <form id={formId} hidden={section === "extensions"} noValidate onSubmit={event => { event.preventDefault(); void save(); }}>
            {section === "general" && <section className="project-settings-panel" aria-label="General">
              <div className="project-settings-heading"><h3>General</h3><p>Manage the project name and find its source folder.</p></div>
              <TextField label="Project name" value={name} disabled={saving} error={nameError}
                onChange={event => { setName(event.target.value); edit(); }} />
              <div className="project-settings-source">
                <h4>Source folder</h4>
                {project.source ? <>
                  <div className="project-settings-path"><code>{project.source}</code>
                    <Button size="sm" icon={copyStatus === "copied" ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
                      onClick={() => { if (project.source) void copyPath(project.source); }}>Copy path</Button>
                  </div>
                  {copyStatus && <p role="status">{copyStatus === "copied" ? "Source path copied." : "Clipboard unavailable. Select and copy the path above."}</p>}
                </> : <p>Source path unavailable.</p>}
              </div>
            </section>}
            {section === "context" && <section className="project-settings-panel" aria-label="Agent context">
              <div className="project-settings-heading"><h3>Agent context</h3><p>Set guidance shared by the project and see this conversation's context window.</p></div>
              <div className="project-settings-budget" data-over={overBudget || undefined}>
                <div><span>Project guidance limit</span><span>{used.toLocaleString()} / {contextLimit.toLocaleString()} characters</span></div>
                <progress aria-label="Project guidance usage" max={contextLimit} value={Math.min(used, contextLimit)} />
                {overBudget && <p role="alert">Remove {(used - contextLimit).toLocaleString()} characters to save.</p>}
              </div>
              <div className="project-settings-budget">
                <div><span>Context window</span>{contextUsage && <span>{contextUsage.input_tokens.toLocaleString()} / {contextUsage.window_tokens.toLocaleString()} tokens</span>}</div>
                {contextUsage ? <>
                  <progress aria-label="Model context usage" max={contextUsage.window_tokens} value={Math.min(contextUsage.input_tokens, contextUsage.window_tokens)} />
                  <p>{contextUsage.estimated ? "Estimated input for the last request." : "Input tokens reported by the model for the last request."} {(contextUsage.compacted_messages > 0 || contextUsage.compacted_tool_exchanges > 0) && "Context was automatically compacted. The full conversation is saved."}</p>
                </> : <p>{savedMessages ? `${savedMessages.toLocaleString()} saved messages. Model usage appears after the next request.` : "Model usage appears after the first request."}</p>}
              </div>
              <TextareaField label="Instructions" rows={4} value={instructions} disabled={saving}
                hint="Tell the agent how to work in this project."
                onChange={event => { setInstructions(event.target.value); edit(); }} />
              <div className="project-settings-memory">
                <div className="project-settings-section-head">
                  <div><h4>Project memory</h4><p>Keep decisions and facts the agent should carry between chats.</p></div>
                  <Button size="sm" ref={addMemory} icon={<Plus size={14} aria-hidden />} disabled={saving || entries.length >= memoryLimit}
                    onClick={() => {
                      const id = nextEntry.current++;
                      setEntries(items => [...items, { id, text: "" }]);
                      edit();
                      focusEntry(id);
                    }}>Add memory entry</Button>
                </div>
                {entries.length ? <ol className="project-settings-memory-list">{entries.map((entry, index) => (
                  <li key={entry.id}>
                    <TextareaField id={`${formId}-memory-${entry.id}`} label={`Memory entry ${index + 1}`} rows={2} value={entry.text} disabled={saving}
                      onChange={event => { const text = event.target.value; setEntries(items => items.map(item => item.id === entry.id ? { ...item, text } : item)); edit(); }} />
                    <IconButton label={`Remove memory entry ${index + 1}`} size="sm" tip={false} disabled={saving} onClick={() => {
                      setEntries(items => items.filter(item => item.id !== entry.id));
                      edit();
                      focusEntry((entries[index + 1] ?? entries[index - 1])?.id);
                    }}><Trash2 size={15} aria-hidden /></IconButton>
                  </li>
                ))}</ol> : <p className="project-settings-empty">No memory entries yet.</p>}
              </div>
            </section>}
          </form>
          {visitedExtensions && <div hidden={section !== "extensions"}><Extensions projectId={project.id} turnActive={turnActive} /></div>}
        </div>
      </div>
      <footer className="project-settings-footer">
        <div className="project-settings-status">
          {turnActive && <p>An agent turn is active. You can save settings or change extension access when it ends.</p>}
          {saveError && <p role="alert" className="text-crit-soft">{saveError}</p>}
          {dirty && overBudget && section !== "context" && <p>Reduce the shared context to save.</p>}
          {dirty && nameError && section !== "general" && <p>Enter a valid project name to save.</p>}
          {savedMessage ? <p role="status" className="project-settings-saved"><Check size={14} aria-hidden /> Project settings saved.</p> : dirty && !saveError && <p>Unsaved changes</p>}
        </div>
        <div className="project-settings-actions">
          <Button variant="ghost" disabled={saving} onClick={() => void close()}>{dirty ? "Cancel" : "Done"}</Button>
          {(section !== "extensions" || dirty) && <Button variant={section === "extensions" ? "subtle" : "primary"}
            type="submit" form={formId} loading={saving} disabled={!canSave}>Save project settings</Button>}
        </div>
      </footer>
    </Dialog>
  );
}
