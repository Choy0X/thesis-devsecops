import { useState } from "react";
import { api, errorText, type Settings } from "../lib/api";
import { Button } from "./ui";
import { Input, SettingFacts, SettingRow, SettingsCard } from "./kit";
import { BrowseButton } from "./BrowseButton";
import { useOpenFolder } from "./useOpenFolder";
import { useToast } from "./Toast";

// The one folder that holds every model: GGUF files, converted models, adapters and, in its hub folder, the Hugging Face cache. Empty means the default folder, whose real path is shown.
export function ModelsFolderPanel({ s, onUpdated }: { s: Settings; onUpdated: (v: Settings) => void }) {
  const toast = useToast();
  const open = useOpenFolder("models");
  const [path, setPath] = useState<string>(s.models_dir || "");
  const [busy, setBusy] = useState(false);
  const facts = s.models as { default: string; in_effect: string; saved: string; exists: boolean } | undefined;
  const dirty = path.trim() !== (s.models_dir || "");
  const save = async (value: string, done: string) => {
    setBusy(true);
    try { const v = await api.putSettings({ models_dir: value } as Partial<Settings>); onUpdated(v); setPath(v.models_dir || ""); toast.success(done); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
    finally { setBusy(false); }
  };
  return (
    <SettingsCard title="Models folder" desc="The one folder that holds every model on this computer: GGUF files, converted models, adapters and the Hugging Face cache (the hub folder inside it)."
      footer={<>
        <Button variant="primary" loading={busy} disabled={!dirty} onClick={() => save(path.trim(), path.trim() ? "Models folder saved." : "The default models folder is used.")}>Save</Button>
        <Button variant="subtle" disabled={busy || !(s.models_dir || path)} onClick={() => save("", "The default models folder is used.")}>Reset to default</Button>
      </>}>
      <SettingRow label="Folder" hint="Empty means the default folder. Folders with a model (a config.json next to weights, or a GGUF file) up to two levels down are listed in the Model Hub. Models already in the folder stay where they are when it is changed.">
        <div className="space-y-2">
          <Input aria-label="Folder" value={path} onChange={(e) => setPath(e.target.value)} autoComplete="off" placeholder={facts?.default ?? "data/models"} />
          <div className="flex flex-wrap items-center gap-2">
            <BrowseButton title="Choose the models folder" onChosen={setPath} />
            <Button variant="ghost" onClick={open}>Open the folder in use</Button>
          </div>
        </div>
      </SettingRow>
      {facts && (
        <SettingFacts label="The models folder">
          <dt>Default</dt><dd className="break-all font-mono">{facts.default}</dd>
          <dt>In use now</dt><dd className="break-all font-mono">{facts.in_effect}{facts.exists ? "" : " (not made yet)"}</dd>
        </SettingFacts>)}
    </SettingsCard>
  );
}
