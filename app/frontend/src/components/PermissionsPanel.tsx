import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, errorText, type AgentRule } from "../lib/api";
import { Button, IconButton, Input, Select, SettingNote, SettingsCard } from "./kit";
import { useToast } from "./Toast";

// What the agent may do without asking, as the owner's own rules. A rule names a kind of call, a pattern for what it is aimed at (a command, a path, a search) and what to do: allow it, ask,
// or refuse it. The last rule that matches wins over the defaults of the mode (Plan reads and stops at your questions, Manual asks before it writes or runs, Auto does not ask). Some things
// are refused whatever is written here (deleting the home folder, writing inside .git, a file of credentials). "Allow for this session" and "Allow for this project" on the approval card add to these.

const TOOLS: { value: string; label: string }[] = [
  { value: "bash", label: "Commands" }, { value: "edit", label: "Writing files" }, { value: "read", label: "Reading files" }, { value: "grep", label: "Searching text" },
  { value: "glob", label: "Finding files" }, { value: "scan", label: "Security scans" }, { value: "websearch", label: "Web search" }, { value: "webfetch", label: "Fetching a page" }, { value: "mcp", label: "An MCP tool (server/tool)" }, { value: "depscan", label: "Dependency scan" },
  { value: "external_directory", label: "A path outside the project" }, { value: "*", label: "Anything" },
];
const ACTIONS: { value: AgentRule["action"]; label: string }[] = [{ value: "allow", label: "Allow" }, { value: "ask", label: "Ask me" }, { value: "deny", label: "Refuse" }];

export function PermissionsPanel() {
  const toast = useToast();
  const [rules, setRules] = useState<AgentRule[] | null>(null);
  const [saved, setSaved] = useState<string>("[]");
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);

  const load = () => { api.agentPermissions().then((d) => { setRules(d.rules); setSaved(JSON.stringify(d.rules)); setError(null); }).catch(setError); };
  useEffect(load, []);

  const change = (i: number, patch: Partial<AgentRule>) => setRules((r) => (r ?? []).map((x, j) => (j === i ? { ...x, ...patch } : x)));
  const save = async () => {
    setSaving(true);
    try { const d = await api.agentPermissionsSave(rules ?? []); setRules(d.rules); setSaved(JSON.stringify(d.rules)); toast.success("Saved the rules."); } catch (e) { toast.error(errorText(e)); } finally { setSaving(false); }
  };
  const dirty = rules !== null && JSON.stringify(rules) !== saved;

  return (
    <SettingsCard title="Agent permissions" desc="Rules apply to every project. The last matching rule takes precedence. Use * as a wildcard in a command or path pattern."
      footer={<>
        <Button variant="subtle" icon={<Plus size={15} aria-hidden />} disabled={!rules || saving} onClick={() => setRules((r) => [...(r ?? []), { tool: "bash", pattern: "", action: "ask" }])}>Add a rule</Button>
        <Button variant="primary" loading={saving} disabled={!dirty} onClick={() => void save()}>Save the rules</Button>
      </>}>
      {error ? <SettingNote role="alert" className="text-crit-soft">{errorText(error)} <button type="button" className="underline" onClick={load}>Try again</button></SettingNote> : null}
      {!error && !rules && <SettingNote role="status">Loading rules...</SettingNote>}
      {rules?.length === 0 && <SettingNote>No rules yet. The defaults of each mode apply.</SettingNote>}
      {!!rules?.length && <ul aria-label="Rules" className="divide-y divide-line">
        {rules.map((r, i) => (
          <li key={i} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-5 py-3 sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)_minmax(0,7rem)_auto]">
            <Select aria-label={`Rule ${i + 1}: kind of call`} value={r.tool} options={TOOLS} onValueChange={(v) => change(i, { tool: v })} />
            <Input aria-label={`Rule ${i + 1}: pattern`} value={r.pattern} placeholder="*" spellCheck={false} onChange={(e) => change(i, { pattern: e.target.value })} />
            <Select aria-label={`Rule ${i + 1}: what to do`} value={r.action} options={ACTIONS} onValueChange={(v) => change(i, { action: v as AgentRule["action"] })} />
            <IconButton label={`Remove rule ${i + 1}`} size="sm" onClick={() => setRules((all) => (all ?? []).filter((_, j) => j !== i))}><Trash2 size={15} aria-hidden /></IconButton>
          </li>))}
      </ul>}
    </SettingsCard>
  );
}
