import { useEffect, useRef, useState } from "react";
import { Plus, Search, Trash2 } from "lucide-react";
import { Button, ErrorState, Skeleton } from "../components/ui";
import { IconButton, Input, SettingNote, SettingsCard, TextareaField, TextField } from "../components/kit";
import { useConfirm } from "../components/hud/AlertDialog";
import { Dialog } from "../components/hud/Dialog";
import { useToast } from "../components/Toast";
import { useOnActivate } from "../components/ActivePage";
import { api, errorText, type Skill, type SkillSummary } from "../lib/api";

export function Skills() {
  const [library, setLibrary] = useState<Awaited<ReturnType<typeof api.skills>> | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const addButton = useRef<HTMLButtonElement>(null);
  const load = () => { api.skills().then((r) => { setLibrary(r); setErr(null); }).catch(setErr); };
  useEffect(() => { load(); }, []);
  useOnActivate(load);
  if (err && !library) return <ErrorState error={err} onRetry={load} title="Could not read the skills" />;
  if (!library) return <Skeleton className="h-40 w-full" />;

  const filter = query.trim().toLowerCase();
  const matches = (skill: { name: string; description: string }) => `${skill.name} ${skill.description}`.toLowerCase().includes(filter);
  const saved = library.skills.filter(matches);
  const builtIn = (library.bundled ?? []).filter(matches);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Input type="search" aria-label="Search skills" placeholder="Search skills" value={query}
          prefix={<Search size={16} aria-hidden />} className="min-w-48 flex-1 sm:max-w-sm" onChange={(event) => setQuery(event.target.value)} />
        <Button ref={addButton} variant="primary" icon={<Plus size={16} aria-hidden />} onClick={() => setCreating(true)}>Add skill</Button>
      </div>
      {creating && <CreateSkill close={() => {
        setCreating(false);
        window.requestAnimationFrame(() => addButton.current?.focus());
      }} onCreated={load} />}
      {err ? <ErrorState error={err} onRetry={load} title="Could not refresh the skills" /> : null}
      <SettingsCard title="Built-in project skills" desc="The project agent can load these instructions when a task calls for them.">
        {!builtIn.length ? <SettingNote>{filter ? "No built-in skills match your search." : "No built-in skills are available."}</SettingNote> : (
          <ul className="divide-y divide-line" aria-label="Built-in project skills">
            {builtIn.map((skill) => (
              <li key={skill.name} className="grid gap-1 px-5 py-3 text-sm sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)] sm:gap-4">
                <span className="break-words font-mono text-[0.82rem] text-fg">{skill.name}</span>
                <span className="leading-relaxed text-muted">{skill.description}</span>
              </li>
            ))}
          </ul>
        )}
      </SettingsCard>
      <SettingsCard title="Saved instructions" desc="Review and approve your saved instruction blocks here. Import project-specific skills from the project's extensions panel.">
        {!saved.length ? <SettingNote>{filter ? "No saved skills match your search." : "No saved skills yet."}</SettingNote> : (
          <ul className="divide-y divide-line" aria-label="Saved skills">{saved.map((skill) => <SkillRow key={skill.name} s={skill} reload={load} />)}</ul>
        )}
      </SettingsCard>
    </div>
  );
}

function CreateSkill({ close, onCreated }: { close: () => void; onCreated: () => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const complete = !!(name.trim() && description.trim() && prompt.trim());

  const create = async () => {
    if (!complete || busy) return;
    setBusy(true);
    setError("");
    try {
      const made = await api.skillCreate(name.trim(), description.trim(), prompt);
      toast.success(`"${made.name}" added for review.`);
      onCreated();
      close();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) close(); }} title="Add skill" dismissible={!busy}
      description="Save an instruction block for review.">
      <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void create(); }}>
        <TextField label="Name" hint="Start with a lowercase letter. Use up to 40 lowercase letters, digits, hyphens or underscores." value={name} autoComplete="off" onChange={(e) => setName(e.target.value)} />
        <TextField label="Description" value={description} autoComplete="off" onChange={(e) => setDescription(e.target.value)} />
        <TextareaField label="Prompt" value={prompt} rows={6} onChange={(e) => setPrompt(e.target.value)} />
        {error ? <p role="alert" className="text-sm text-crit">{error}</p> : null}
        <div className="flex justify-end gap-2 border-t border-line pt-4">
          <Button variant="subtle" type="button" disabled={busy} onClick={close}>Cancel</Button>
          <Button variant="primary" type="submit" loading={busy} disabled={!complete}>Add</Button>
        </div>
      </form>
    </Dialog>
  );
}

function SkillRow({ s, reload }: { s: SkillSummary; reload: () => void }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [full, setFull] = useState<Skill | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [busy, setBusy] = useState("");

  const togglePreview = async () => {
    if (full) { setFull(null); return; }
    setPreviewError("");
    try { setFull(await api.skillGet(s.name)); }
    catch (e) { setPreviewError(errorText(e)); }
  };
  const approve = async () => {
    setBusy("approve");
    try { await api.skillApprove(s.name); toast.success(`"${s.name}" approved.`); reload(); }
    catch (e) { toast.error(errorText(e)); }
    finally { setBusy(""); }
  };
  const remove = async () => {
    if (!(await confirm({ title: `Remove "${s.name}"?`, confirmLabel: "Remove it",
      description: "This removes the saved instruction block from your library." }))) return;
    setBusy("delete");
    try { await api.skillDelete(s.name); toast.success(`"${s.name}" removed.`); reload(); }
    catch (e) { toast.error(errorText(e)); }
    finally { setBusy(""); }
  };

  return (
    <li className="px-5 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-medium">
            <span className="font-mono text-[0.85rem]">{s.name}</span>
            <span className={s.approved ? "text-xs text-ok" : "text-xs text-warn"}>{s.approved ? "Approved" : "Needs review"}</span>
          </p>
          <p className="text-[0.8rem] text-muted">{s.description}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="subtle" size="sm" aria-expanded={!!full} onClick={togglePreview}>{full ? "Hide" : "Preview"}</Button>
          {!s.approved && <Button variant="primary" size="sm" loading={busy === "approve"} onClick={approve}>Approve</Button>}
          <IconButton label={`Remove ${s.name}`} tone="danger" onClick={remove}><Trash2 size={15} aria-hidden /></IconButton>
        </div>
      </div>
      {previewError && <p role="alert" className="mt-2 text-sm text-crit">{previewError}</p>}
      {full && <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-line bg-surface-3 p-3 text-[0.78rem]">{full.prompt}</pre>}
    </li>
  );
}
