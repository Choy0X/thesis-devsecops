import { useState } from "react";
import { api, errorText, type Settings as S } from "../lib/api";
import { Button } from "./ui";
import { RadioGroup, SecretField, SettingNote, SettingRow, SettingsCard, TextField } from "./kit";
import { useConfirm } from "./hud/AlertDialog";
import { useToast } from "./Toast";

// Lets a conversation search the web, when turned on for it (the chat's own "Search the web" switch). A provider here only ever finds candidate
// pages; the backend fetches and reads them itself, so nothing about the project reaches the provider or a fetched page, and only the retrieved
// excerpts -- not raw search results or whole pages -- are handed to the model. Off until a provider is chosen below.

const PROVIDERS = [
  { value: "", label: "Off" },
  { value: "brave", label: "Brave Search" },
  { value: "google", label: "Google Custom Search" },
  { value: "duckduckgo", label: "DuckDuckGo" },
];

export function WebSearchPanel({ s, onUpdated }: { s: S; onUpdated: (v: S) => void }) {
  const toast = useToast();
  const confirm = useConfirm();
  const provider = s.web_search_provider || "";
  const [braveKey, setBraveKey] = useState("");
  const [googleKey, setGoogleKey] = useState("");
  const [cseId, setCseId] = useState(s.google_cse_id || "");
  const [savingGoogle, setSavingGoogle] = useState(false);

  const save = async (patch: Record<string, string>, done: string) => {
    try { onUpdated(await api.putSettings(patch)); toast.success(done); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
  };
  const removeKey = async (field: "brave_api_key" | "google_api_key", label: string) => {
    if (!(await confirm({ title: `Remove the saved ${label}?`, confirmLabel: "Remove it",
      description: "It is deleted from the settings file on this computer. You can enter it again later." }))) return;
    await save({ [field]: "" }, `${label} removed.`);
  };
  const saveGoogle = async () => {
    setSavingGoogle(true);
    try { await save({ ...(googleKey ? { google_api_key: googleKey } : {}), google_cse_id: cseId.trim() }, "The Google Custom Search settings are saved."); setGoogleKey(""); }
    finally { setSavingGoogle(false); }
  };

  return (
    <SettingsCard title="Web search"
      desc="Lets a conversation search the web when turned on for it. A fetched page is never handed to the model directly: it is read, masked for credentials and retrieved the same way the project's own files are.">
      <SettingRow label="Provider" hint="Which search engine looks up pages. DuckDuckGo needs no key, but it is an unofficial endpoint and more likely to break.">
        <RadioGroup legend="Provider" hideLegend value={provider} options={PROVIDERS}
          onValueChange={(v) => save({ web_search_provider: v }, v ? `Web search uses ${PROVIDERS.find((p) => p.value === v)?.label}.` : "Web search is off.")} />
      </SettingRow>
      {provider === "brave" && (
        <>
          <SecretField label="Brave API key" value={braveKey} onChange={setBraveKey} saved={!!s.brave_api_key_set} savedHint={(s.brave_api_key_hint as string) || ""}
            onReveal={() => api.revealSecret("brave_api_key")} onRemove={() => removeKey("brave_api_key", "The Brave API key")} />
          {braveKey && <Button variant="primary" onClick={() => save({ brave_api_key: braveKey }, "The Brave API key is saved.").then(() => setBraveKey(""))}>Save the key</Button>}
        </>
      )}
      {provider === "google" && (
        <>
          <SecretField label="Google API key" value={googleKey} onChange={setGoogleKey} saved={!!s.google_api_key_set} savedHint={(s.google_api_key_hint as string) || ""}
            onReveal={() => api.revealSecret("google_api_key")} onRemove={() => removeKey("google_api_key", "The Google API key")} />
          <TextField label="Custom Search Engine id" hint="From the Google Programmable Search Engine made for this key." value={cseId} autoComplete="off" onChange={(e) => setCseId(e.target.value)} />
          {(googleKey || cseId.trim() !== (s.google_cse_id || "")) && <Button variant="primary" loading={savingGoogle} onClick={saveGoogle}>Save</Button>}
        </>
      )}
      {provider === "duckduckgo" && <SettingNote>DuckDuckGo needs no key. It is an unofficial endpoint, not a published API: it can break or rate-limit without notice.</SettingNote>}
    </SettingsCard>
  );
}
