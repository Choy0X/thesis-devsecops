import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, errorText, type McpCheck, type McpServer, type McpServerIn, type Project } from "../lib/api";
import { Button, IconButton, Input, Select, SettingNote, SettingsCard, Switch, Textarea } from "./kit";
import { useToast } from "./Toast";

// The MCP servers: programs somebody wrote that give the agent more tools. Each runs inside a project's sandbox (so only in Manual and Auto), as a different user from the agent's own commands, with the
// variables written here (an API key, usually: the value is kept on this computer and never shown again, only its name). Every call to one of their tools is asked about. A server that has to install
// something or reach a service needs the sandbox network switched on (the card above). The command is the program and its arguments, one to a line, so a path with a space in it is one word.

type Draft = { key: number; name: string; command: string; env: string; enabled: boolean; timeout: string; saved: boolean };

let counter = 0;
const draftOf = (s: McpServer): Draft => ({ key: ++counter, name: s.name, command: s.command.join("\n"), env: s.env_names.join("\n"), enabled: s.enabled, timeout: String(s.timeout), saved: true });
const blank = (): Draft => ({ key: ++counter, name: "", command: "", env: "", enabled: true, timeout: "60", saved: false });

/** The lines of the variables box as the server wants them: ``NAME=value`` sets one, a line with only ``NAME`` keeps the value that is saved. */
export function parseEnv(text: string): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf("=");
    if (at < 0) out[line] = null;
    else out[line.slice(0, at).trim()] = line.slice(at + 1);
  }
  return out;
}

export function McpServersPanel() {
  const toast = useToast();
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [saved, setSaved] = useState("[]");
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState<McpCheck | null>(null);

  const snapshot = (list: Draft[]) => JSON.stringify(list.map(({ key: _k, ...rest }) => rest));
  const load = () => {
    api.agentMcp().then((d) => { const list = d.servers.map(draftOf); setDrafts(list); setSaved(snapshot(list)); setError(null); }).catch(setError);
  };
  useEffect(load, []);
  useEffect(() => { api.projects().then((d) => { setProjects(d.projects); setProjectId((p) => p || d.projects[0]?.id || ""); }).catch(() => undefined); }, []);   // ignored: without the list the check just has nothing to pick

  const change = (key: number, patch: Partial<Draft>) => setDrafts((all) => (all ?? []).map((d) => (d.key === key ? { ...d, ...patch } : d)));
  const dirty = drafts !== null && snapshot(drafts) !== saved;

  const save = async () => {
    setSaving(true);
    try {
      const body: Record<string, McpServerIn> = {};
      for (const d of drafts ?? []) {
        body[d.name.trim()] = { command: d.command.split("\n").map((w) => w.trim()).filter(Boolean), env: parseEnv(d.env), enabled: d.enabled, timeout: Number(d.timeout) || 60 };
      }
      const done = await api.agentMcpSave(body);
      const list = done.servers.map(draftOf);
      setDrafts(list); setSaved(snapshot(list)); setChecked(null);
      toast.success(list.length ? "Saved the servers." : "No server is set up.");
    } catch (e) { toast.error(`Not saved: ${errorText(e)}`); } finally { setSaving(false); }
  };

  const check = async () => {
    setChecking(true);
    try { setChecked(await api.agentMcpCheck(projectId)); } catch (e) { toast.error(errorText(e)); } finally { setChecking(false); }
  };

  return (
    <SettingsCard title="MCP servers"
      desc="Programs that give the agent more tools. They run in a project's sandbox, as another user, and every call to one of their tools is asked about. They need the sandbox network switched on to install anything or reach a service. A server starts in an empty folder: if it needs the project's files, give it their path (a folder under /work) as an argument. All servers run as one user, so do not give a server a key that another server you do not trust should not see."
      footer={<>
        <Button variant="subtle" icon={<Plus size={15} aria-hidden />} onClick={() => setDrafts((all) => [...(all ?? []), blank()])}>Add a server</Button>
        <Button variant="primary" loading={saving} disabled={!dirty} onClick={() => void save()}>Save the servers</Button>
      </>}>
      {error ? <p role="alert" className="px-5 py-3 text-[0.82rem] text-crit-soft">{errorText(error)} <button type="button" className="underline" onClick={load}>Try again</button></p> : null}
      <ul aria-label="MCP servers" className="divide-y divide-line">
        {(drafts ?? []).map((d, i) => (
          <li key={d.key} className="space-y-2 px-5 py-4">
            <div className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)_auto_auto] items-center gap-2">
              <Input aria-label={`Server ${i + 1}: name`} value={d.name} placeholder="name" spellCheck={false} maxLength={24} onChange={(e) => change(d.key, { name: e.target.value })} />
              <Input aria-label={`Server ${i + 1}: time limit for a call, in seconds`} value={d.timeout} inputMode="numeric" placeholder="60" onChange={(e) => change(d.key, { timeout: e.target.value })} />
              <Switch checked={d.enabled} onCheckedChange={(on) => change(d.key, { enabled: on })} aria-label={`Server ${i + 1}: on`} />
              <IconButton label={`Remove server ${i + 1}`} size="sm" onClick={() => setDrafts((all) => (all ?? []).filter((x) => x.key !== d.key))}><Trash2 size={15} aria-hidden /></IconButton>
            </div>
            <Textarea aria-label={`Server ${i + 1}: command, one word to a line`} value={d.command} rows={3} spellCheck={false} placeholder={"npx\n-y\n@scope/some-mcp-server\n/work"} onChange={(e) => change(d.key, { command: e.target.value })} />
            <Textarea aria-label={`Server ${i + 1}: variables`} value={d.env} rows={2} spellCheck={false} autoComplete="off" placeholder="API_KEY=the value" onChange={(e) => change(d.key, { env: e.target.value })} />
            {d.saved && <p className="text-[0.72rem] text-faint">A line with only a name keeps the value that is saved. A value is never shown again once it is saved.</p>}
          </li>))}
        {drafts && drafts.length === 0 && <li className="px-5 py-4 text-[0.82rem] text-muted">No server is set up, so the agent has only its own tools.</li>}
      </ul>
      {drafts && drafts.length > 0 && (
        <SettingNote className="space-y-2">
          <p>Start the saved servers in a project's sandbox to see which come up, how many tools each offers and why one did not. This makes the sandbox first if the project has none.</p>
          <div className="flex flex-wrap items-center gap-2">
            <Select aria-label="Project to check in" value={projectId} onValueChange={setProjectId} options={projects.map((p) => ({ value: p.id, label: p.name }))} />
            <Button variant="subtle" loading={checking} disabled={!projectId || dirty} onClick={() => void check()}>Check the servers</Button>
            {dirty && <span className="text-[0.74rem] text-faint">Save first.</span>}
          </div>
          {checked && (
            <ul aria-label="Check result" className="space-y-1 text-[0.8rem]">
              {checked.servers.map((s) => <li key={s.name} className={s.ok ? "text-fg" : "text-crit-soft"}>{s.name}: {s.ok ? `${s.tools} tool${s.tools === 1 ? "" : "s"}` : s.error}</li>)}
              {checked.servers.length === 0 && <li className="text-muted">No server is turned on.</li>}
            </ul>)}
        </SettingNote>)}
    </SettingsCard>
  );
}
