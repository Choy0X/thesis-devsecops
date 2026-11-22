import { useState, type ReactNode } from "react";
import { api, errorText, type Settings as S, type TestResult } from "../lib/api";
import { Button } from "./ui";
import { FormRow, Input, SecretField, Select, SettingsCard, TextField } from "./kit";
import { useToast } from "./Toast";
import { useConfirm } from "./hud/AlertDialog";
import { Dialog } from "./hud/Dialog";
import { Dev } from "../lib/devMode";

// A group of settings that are typed, saved together and tried out together: a few fields (text, a secret, a model that can be discovered), a Save and a Test connection. The form keeps
// its own unsaved text, so an edit in one panel is never lost or overwritten by saving another. `useCredForm` returns the fields as rows, the buttons for the footer of the card and the
// test dialog, so that a card of any kind (a data source, the email server, the external language model) puts them where every card has them. A saved secret is not on the page: the Eye
// of a SecretField asks for it only when it is pressed, and a secret is sent only when a new one was typed.

type Discover = { baseKey: string; apiKeyKey: string };
export type FieldDef = {
  k: string; label: string; placeholder?: string;
  secret?: boolean; hint?: string;
  kind?: "text" | "model"; discover?: Discover;
};

const initialDraft = (fields: FieldDef[], saved: S): Record<string, string> =>
  Object.fromEntries(fields.map((f) => [f.k, f.secret ? "" : String(saved[f.k] ?? "")]));

export type CredFieldsProps = {
  /** What the panel is called in the messages ("NVD saved."). */
  title: string;
  /** What the test dialog is called after "Test" ("Test the language model"); the title when not given. */
  testTitle?: string;
  /** The target of the connection test (POST /api/settings/test/<target>). */
  test: string;
  fields: FieldDef[];
  saved: S;
  onUpdated: (v: S) => void;
  /** Values sent with the test and with a save that are not fields of the form (the engine, for the language model). */
  extra?: Record<string, string>;
  /** The text of the test button ("Test connection"). */
  testLabel?: string;
};

export function useCredForm({ title, testTitle, test, fields, saved, onUpdated, extra, testLabel = "Test connection" }: CredFieldsProps): { rows: ReactNode; footer: ReactNode; dialog: ReactNode } {
  const toast = useToast();
  const confirm = useConfirm();
  const [draft, setDraft] = useState<Record<string, string>>(() => initialDraft(fields, saved));
  const [saving, setSaving] = useState(false);
  const [testOpen, setTestOpen] = useState(false);
  const set = (k: string, v: string) => setDraft((d) => ({ ...d, [k]: v }));

  const body = () => ({ ...(extra ?? {}), ...Object.fromEntries(fields.map((f) => [f.k, draft[f.k] ?? ""])) });
  const save = async () => {
    setSaving(true);
    try {
      const patch: Record<string, string> = { ...(extra ?? {}) };
      for (const f of fields) {
        if (f.secret && !(draft[f.k] ?? "")) continue;           // a secret is sent only when a new one was typed (the saved one is never shown)
        patch[f.k] = draft[f.k] ?? "";
      }
      const v = await api.putSettings(patch);
      onUpdated(v);
      setDraft(initialDraft(fields, v));
      toast.success(`${title} saved.`);
    } catch (e) {
      toast.error(`Could not save ${title}: ${errorText(e)}`);
    } finally { setSaving(false); }
  };
  const remove = async (f: FieldDef) => {
    if (!(await confirm({ title: `Remove the saved ${f.label.toLowerCase()}?`, confirmLabel: "Remove it",
      description: "It is deleted from the settings file on this computer. You can enter it again later." }))) return;
    try { onUpdated(await api.putSettings({ [f.k]: "" })); toast.success(`${f.label} removed.`); }
    catch (e) { toast.error(`Could not remove it: ${errorText(e)}`); }
  };

  const rows = fields.map((f) =>
    f.kind === "model"
      ? <ModelField key={f.k} f={f} draft={draft} set={set} />
      : f.secret
        ? <SecretField key={f.k} label={f.label} hint={f.hint} placeholder={f.placeholder} value={draft[f.k] ?? ""} onChange={(v) => set(f.k, v)}
            saved={!!saved[`${f.k}_set`]} savedHint={saved[`${f.k}_hint`] || ""} onReveal={() => api.revealSecret(f.k)} onRemove={() => remove(f)} />
        : <TextField key={f.k} label={f.label} hint={f.hint} value={draft[f.k] ?? ""} placeholder={f.placeholder} onChange={(e) => set(f.k, e.target.value)} autoComplete="off" />);
  const footer = (
    <>
      <Button variant="primary" onClick={save} loading={saving}>Save</Button>
      <Button variant="subtle" onClick={() => setTestOpen(true)}>{testLabel}</Button>
    </>);
  const dialog = testOpen
    ? <TestModal target={test} title={testTitle ?? title} getBody={body} defaultInput={test === "smtp" ? (draft.smtp_to || "") : undefined} onClose={() => setTestOpen(false)} />
    : null;
  return { rows, footer, dialog };
}

export function CredPanel({ title, desc, ...rest }: CredFieldsProps & { desc: string }) {
  const form = useCredForm({ title, ...rest });
  return <SettingsCard title={title} desc={desc} footer={form.footer}>{form.rows}{form.dialog}</SettingsCard>;
}

// -- Test connection: send an input, wait, show the response ---------------------------------------------------------------------------------------------
const PROBE: Record<string, { label: string; placeholder: string; def: string; cta: string }> = {
  llm: { label: "Prompt to send", placeholder: "Ask the model something...", def: "Reply with exactly: ok", cta: "Send prompt" },
  nvd: { label: "CVE id (optional)", placeholder: "CVE-2023-32681", def: "CVE-2023-32681", cta: "Query NVD" },
  github: { label: "Repository to read (optional)", placeholder: "owner/repo", def: "psf/requests", cta: "Check access" },
  huggingface: { label: "Model to check access to (optional)", placeholder: "org/model", def: "", cta: "Check access" },
  smtp: { label: "Send test email to", placeholder: "you@example.com", def: "", cta: "Send test email" },
};

export function TestModal({ target, title, getBody, defaultInput, onClose }:
  { target: string; title: string; getBody: () => Record<string, string>; defaultInput?: string; onClose: () => void }) {
  const cfg = PROBE[target] ?? { label: "Input", placeholder: "", def: "", cta: "Run test" };
  const [input, setInput] = useState(defaultInput ?? cfg.def);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);

  const run = async () => {
    setRunning(true); setResult(null);
    try { setResult(await api.testConnection(target, { ...getBody(), input })); }
    catch (e) { setResult({ ok: false, detail: errorText(e) }); }
    finally { setRunning(false); }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }} title={`Test ${title}`} description="Sends one small request with the values on this page.">
      <div className="space-y-3">
        <TextField label={cfg.label} value={input} placeholder={cfg.placeholder} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") run(); }} autoFocus />
        <div className="flex items-center gap-2">
          <Button variant="primary" onClick={run} loading={running}>{cfg.cta}</Button>
          <Button variant="ghost" onClick={onClose}>Close</Button>
        </div>
        {running && <p className="text-[0.82rem] text-muted">Waiting for a response... A model that is not loaded yet is loaded first; there is no time limit.</p>}
        {result && (
          <div className={`chamfer-sm border p-3 ${result.ok ? "border-ok-line bg-ok-tint" : "border-crit-line bg-crit-tint"}`} role="status">
            <div className={`text-[0.82rem] font-medium ${result.ok ? "text-ok" : "text-crit-soft"}`}>
              {result.ok ? "It works." : "It did not work."} {result.detail}
            </div>
            {result.response && (
              <Dev>
                <details className="mt-2"><summary className="cursor-pointer text-[0.76rem] text-muted">Technical details</summary>
                  <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap bg-terminal p-2.5 font-mono text-[0.76rem] text-muted">{result.response}</pre>
                </details>
              </Dev>
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}

// -- A model name: typed, or picked from what the endpoint lists ---------------------------------------------------------------------------------------
const MANUAL = "__manual__";

function ModelField({ f, draft, set }: { f: FieldDef; draft: Record<string, string>; set: (k: string, v: string) => void }) {
  const [options, setOptions] = useState<string[] | null>(null);
  const [manual, setManual] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const discover = async () => {
    if (!f.discover) return;
    setBusy(true); setMsg(null);
    try {
      const r = await api.discoverModels({ base_url: draft[f.discover.baseKey] || undefined, api_key: draft[f.discover.apiKeyKey] || undefined });
      if (r.ok && r.models.length) {
        setOptions(r.models); setManual(false);
        setMsg({ ok: true, text: r.detail || `${r.models.length} model(s) found.` });
      } else {
        setOptions(null); setManual(true);
        setMsg({ ok: false, text: r.detail || "No models found. Type the model name." });
      }
    } catch (e) {
      setOptions(null); setManual(true);
      setMsg({ ok: false, text: errorText(e) });
    } finally { setBusy(false); }
  };

  const cur = draft[f.k] ?? "";
  const useSelect = !!options && options.length > 0 && !manual;
  const names = useSelect && cur && !options!.includes(cur) ? [cur, ...options!] : options || [];

  return (
    <FormRow label={f.label} hint={f.hint}>
      <div className="flex gap-2">
        <div className="min-w-0 flex-1">
          {useSelect ? (
            <Select value={cur} placeholder="Select a model" onValueChange={(v) => { if (v === MANUAL) setManual(true); else set(f.k, v); }}
              options={[...names.map((m) => ({ value: m, label: m })), { value: MANUAL, label: "Type manually" }]} />
          ) : (
            <Input value={cur} placeholder={f.placeholder} onChange={(e) => set(f.k, e.target.value)} autoComplete="off" />
          )}
        </div>
        <Button variant="subtle" onClick={discover} loading={busy}>Discover</Button>
      </div>
      {msg && <span className={`mt-1 block text-[0.72rem] ${msg.ok ? "text-ok" : "text-crit-soft"}`}>{msg.text}</span>}
    </FormRow>
  );
}
