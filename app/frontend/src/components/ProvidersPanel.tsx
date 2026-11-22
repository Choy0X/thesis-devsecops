import { FlaskConical, Pencil, Plug, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, errorText, type AgentModel, type AgentProvider, type AgentProviderBody, type ModelProfileSummary, type ProviderModel } from "../lib/api";
import { useConfirm } from "./hud/AlertDialog";
import { Dialog } from "./hud/Dialog";
import { Button, IconButton, SecretField, SettingsCard, TextField, TextareaField } from "./kit";
import { useToast } from "./Toast";

// The model servers an agent session can use besides the one the settings name: a name, an address, a key and the models the owner wants to say something about (how long its context is, how
// much it may write, which protocol to ask it with). A key is never shown again, only that one is set; an edit that types no key keeps it. "Test tools" asks a model a few small questions
// to find out how it takes tools, and keeps the answer: what a chat template promises and what a model does are different things.

const PROTOCOL_WORDS: Record<ModelProfileSummary["protocol"], string> = { native: "native tools", schema: "a schema for the reply", text: "calls written in its reply" };
const TRUST_WORDS = { local: "This computer", lan: "Another computer on the network", remote: "A hosted service" } as const;

/** What is known of a model, in a sentence: how it is asked, and whether that was tested. */
export function profileSaid(p: ModelProfileSummary | null, fallback = "Not tested yet."): string {
  if (!p) return fallback;
  const how = `asked with ${PROTOCOL_WORDS[p.protocol]}`;
  const thinks = p.reasoning === "none" ? "" : p.reasoning === "always" ? "; thinks before every answer" : "; can think";
  return p.source === "probed" ? `Tested: ${how}${thinks}.` : p.source === "manual" ? `Set by you: ${how}${thinks}.` : `Not tested yet: ${how}${thinks}.`;
}

/** The models of a provider as the lines of a text box: an id, then context=, max_output= and protocol= where something was said. */
export function modelsToText(models: ProviderModel[]): string {
  return models.map((m) => [m.id, m.context ? `context=${m.context}` : "", m.max_output ? `max_output=${m.max_output}` : "", m.protocol ? `protocol=${m.protocol}` : ""].filter(Boolean).join(" ")).join(String.fromCharCode(10));
}

/** The lines of that text box as what the server keeps, and what is wrong with a line (it is skipped, never guessed at). */
export function textToModels(text: string): { models: Record<string, { context?: number; max_output?: number; protocol?: "native" | "schema" | "text" }>; errors: string[] } {
  const models: Record<string, { context?: number; max_output?: number; protocol?: "native" | "schema" | "text" }> = {};
  const errors: string[] = [];
  text.split(String.fromCharCode(10)).forEach((raw, i) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    const [id, ...rest] = line.split(/\s+/);
    const entry: { context?: number; max_output?: number; protocol?: "native" | "schema" | "text" } = {};
    for (const token of rest) {
      const [key, value = ""] = token.split("=");
      if ((key === "context" || key === "max_output") && /^\d+$/.test(value) && Number(value) >= 256) entry[key] = Number(value);
      else if (key === "protocol" && (value === "native" || value === "schema" || value === "text")) entry.protocol = value;
      else errors.push(`Line ${i + 1}: ${token} is not context=, max_output= (a number of tokens, at least 256) or protocol=native, protocol=schema or protocol=text.`);
    }
    models[id] = entry;
  });
  return { models, errors };
}

type Found = { ok: boolean; text: string; models: string[] };

export function ProvidersPanel() {
  const toast = useToast();
  const confirm = useConfirm();
  const [data, setData] = useState<{ providers: AgentProvider[]; default: AgentModel } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState<AgentProvider | "new" | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [found, setFound] = useState<Record<string, Found>>({});

  const load = () => { api.agentProviders().then((d) => { setData(d); setError(null); }).catch(setError); };
  useEffect(load, []);

  const probe = async (ref: string) => {
    setBusy(`tools:${ref}`);
    try { await api.agentModelProbe(ref); load(); toast.success("Asked how the model takes tools, and kept the answer."); } catch (e) { toast.error(errorText(e)); } finally { setBusy(null); }
  };
  const testConnection = async (p: AgentProvider) => {
    setBusy(`test:${p.id}`);
    try {
      const r = await api.agentProviderTest(p.id);
      setFound((f) => ({ ...f, [p.id]: r.ok ? { ok: true, text: `Reached it: ${r.count} model${r.count === 1 ? "" : "s"}${r.models?.length ? `, ${r.models.slice(0, 6).join(", ")}${(r.count ?? 0) > 6 ? " ..." : ""}` : ""}.`, models: r.models ?? [] }
        : { ok: false, text: r.error || "It did not answer.", models: [] } }));
    } catch (e) { setFound((f) => ({ ...f, [p.id]: { ok: false, text: errorText(e), models: [] } })); } finally { setBusy(null); }
  };
  const listFound = async (p: AgentProvider) => {
    const models = Object.fromEntries((found[p.id]?.models ?? []).map((m) => { const old = p.models.find((x) => x.id === m); return [m, old ? { name: old.name, context: old.context, max_output: old.max_output, protocol: old.protocol } : {}]; }));
    try { await api.agentProviderPut(p.id, { name: p.name, base_url: p.base_url, models }); load(); toast.success("Listed the models."); } catch (e) { toast.error(errorText(e)); }
  };
  const remove = async (p: AgentProvider) => {
    if (!(await confirm({ title: `Delete ${p.name}?`, description: "Its address and its key are removed. A session that was given one of its models goes back to asking for another.", confirmLabel: "Delete", tone: "critical" }))) return;
    try { await api.agentProviderDelete(p.id); load(); } catch (e) { toast.error(errorText(e)); }
  };

  const d = data?.default;
  return (
    <SettingsCard title="Model providers" desc="Other model servers an agent session can use, beside the model of the engine above. A session picks its model next to the mode."
      footer={<Button variant="subtle" icon={<Plus size={15} aria-hidden />} onClick={() => setEditing("new")}>Add a provider</Button>}>
      {error ? <p role="alert" className="text-[0.82rem] text-crit-soft">{errorText(error)} <button type="button" className="underline" onClick={load}>Try again</button></p> : null}
      {d && (
        <div className="flex flex-wrap items-center justify-between gap-3 border border-line bg-surface-2 px-3 py-2">
          <div className="min-w-0">
            <p className="text-[0.84rem] font-medium">{d.provider_name}: {d.label}</p>
            <p className="text-[0.78rem] text-muted">{d.configured ? profileSaid(d.profile, "Not tested yet: it is tested once, the first time an agent session uses it.") : "No model is chosen above yet."}</p>
          </div>
          <Button variant="subtle" size="sm" icon={<FlaskConical size={14} aria-hidden />} loading={busy === "tools:"} disabled={!d.configured || busy !== null} onClick={() => void probe("")}>Test tools</Button>
        </div>)}
      <ul aria-label="Providers" className="space-y-3">
        {data?.providers.map((p) => (
          <li key={p.id} className="space-y-2 border border-line px-3 py-2.5">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-[0.88rem] font-medium">{p.name} <span className="font-mono text-[0.74rem] text-faint">{p.id}</span></p>
                <p className="break-all font-mono text-[0.76rem] text-muted">{p.base_url}</p>
                <p className="text-[0.76rem] text-faint">{TRUST_WORDS[p.trust]}. {p.api_key_set ? `A key is set${p.api_key_hint ? ` (ends ${p.api_key_hint})` : ""}.` : "No key."}</p>
              </div>
              <div className="flex items-center gap-1">
                <Button variant="subtle" size="sm" icon={<Plug size={14} aria-hidden />} loading={busy === `test:${p.id}`} disabled={busy !== null} onClick={() => void testConnection(p)}>Test connection</Button>
                <IconButton label={`Edit ${p.name}`} size="sm" onClick={() => setEditing(p)}><Pencil size={15} aria-hidden /></IconButton>
                <IconButton label={`Delete ${p.name}`} size="sm" onClick={() => void remove(p)}><Trash2 size={15} aria-hidden /></IconButton>
              </div>
            </div>
            {found[p.id] && (
              <p role="status" className={`text-[0.78rem] ${found[p.id].ok ? "text-muted" : "text-crit-soft"}`}>
                {found[p.id].text}
                {found[p.id].ok && found[p.id].models.some((m) => !p.models.some((x) => x.id === m)) && <> <button type="button" className="underline" onClick={() => void listFound(p)}>List these models</button></>}
              </p>)}
            {p.models.length > 0 ? (
              <ul aria-label={`Models of ${p.name}`} className="space-y-1">
                {p.models.map((m) => (
                  <li key={m.id} className="flex flex-wrap items-center justify-between gap-2 text-[0.78rem]">
                    <span className="min-w-0 break-all"><span className="font-mono">{m.id}</span>{m.context ? <span className="text-faint">, context {m.context.toLocaleString()}</span> : null}{m.max_output ? <span className="text-faint">, writes up to {m.max_output.toLocaleString()}</span> : null}</span>
                    <Button variant="ghost" size="sm" icon={<FlaskConical size={13} aria-hidden />} loading={busy === `tools:${p.id}/${m.id}`} disabled={busy !== null} onClick={() => void probe(`${p.id}/${m.id}`)}
                      title="Asks the model a few small questions to see how it takes tools. It costs a few requests.">Test tools</Button>
                  </li>))}
              </ul>) : <p className="text-[0.78rem] text-faint">No models listed. A session can still name one by hand, or you can test the connection and list what the server has.</p>}
          </li>))}
        {data && data.providers.length === 0 && <li className="text-[0.82rem] text-muted">No provider added yet.</li>}
      </ul>
      {editing && <ProviderDialog provider={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </SettingsCard>
  );
}

function ProviderDialog({ provider, onClose, onSaved }: { provider: AgentProvider | null; onClose: () => void; onSaved: () => void }) {
  const [id, setId] = useState(provider?.id ?? "");
  const [name, setName] = useState(provider?.name ?? "");
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? "");
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [models, setModels] = useState(modelsToText(provider?.models ?? []));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const parsed = textToModels(models);

  const save = async () => {
    if (parsed.errors.length) { setError(parsed.errors[0]); return; }
    setSaving(true);
    setError("");
    const body: AgentProviderBody = { name: name.trim() || undefined, base_url: baseUrl.trim(), models: parsed.models, ...(key ? { api_key: key } : clearKey ? { api_key: "" } : {}) };
    try { await api.agentProviderPut(provider?.id ?? id.trim().toLowerCase(), body); onSaved(); } catch (e) { setError(errorText(e)); } finally { setSaving(false); }
  };

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={provider ? `Edit ${provider.name}` : "Add a provider"} description="An OpenAI-compatible server: a /v1 address, and a key if it asks for one.">
      <div className="space-y-4">
        <TextField label="Id" value={id} disabled={!!provider} onChange={(e) => setId(e.target.value)} placeholder="lab-server" autoComplete="off" hint="Lowercase letters, digits and hyphens. A model is named id/model-name." />
        <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Lab server" autoComplete="off" />
        <TextField label="Address" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://192.168.1.20:8000/v1" autoComplete="off"
          hint="The /v1 root. A full endpoint path such as /v1/chat/completions is accepted and corrected." />
        <SecretField label="API key" value={key} onChange={(v) => { setKey(v); setClearKey(false); }} saved={!!provider?.api_key_set && !clearKey} savedHint={provider?.api_key_hint ?? ""}
          onRemove={provider?.api_key_set ? () => { setKey(""); setClearKey(true); } : undefined} hint="A key that is not typed stays as it is." />
        <TextareaField label="Models" value={models} onChange={(e) => setModels(e.target.value)} rows={5} spellCheck={false} placeholder={"qwen3-8b context=32768 max_output=4096 protocol=native"}
          hint="One per line: the model's id on that server, then what you know of it. context= and max_output= are numbers of tokens; protocol= is native or schema. Leave a line with the id alone if there is nothing to say." />
        {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={() => void save()} loading={saving} disabled={!baseUrl.trim() || (!provider && !id.trim())}>Save</Button>
        </div>
      </div>
    </Dialog>
  );
}
