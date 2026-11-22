import { useEffect, useState } from "react";
import { api, errorText, type NotReadyModel, type RunAdapter, type ServingAdapter, type ServingFile, type Settings } from "../lib/api";
import { noAdapterReason, noModelReason } from "../lib/emptyModels";
import { RefreshCw } from "lucide-react";
import { Button } from "./ui";
import { IconButton, Input, RadioGroup, SelectField, SettingRow, SettingsCard } from "./kit";
import { TestModal, useCredForm, type FieldDef } from "./CredPanel";
import { useOnChanged } from "../lib/changes";
import { useOnActivate } from "./ActivePage";
import { GatewayNote, useGatewayStatus } from "./GatewayNote";
import { useToast } from "./Toast";

// The language model: classification, detection, patching, the project chat and the agent send their questions to it. Local runs a GGUF model on this computer through the model gateway
// (the model is chosen here and loaded when it is first asked); External sends them to an OpenAI-compatible endpoint. Choosing saves at once; the endpoint form has a Save.

const ENGINES = [{ value: "local", label: "Local" }, { value: "external", label: "External" }];
const BASE_HINT = "The /v1 root of an OpenAI-compatible server. A full endpoint path such as /v1/chat/completions is accepted and corrected automatically.";
const EXTERNAL_FIELDS: FieldDef[] = [
  { k: "llm_base_url", label: "Base URL", placeholder: "http://localhost:8080/v1", hint: BASE_HINT },
  { k: "llm_api_key", label: "API key", secret: true },
  { k: "llm_model", label: "Model", placeholder: "the model's name on that server", kind: "model", discover: { baseKey: "llm_base_url", apiKeyKey: "llm_api_key" } },
];

/** The engine in effect: the one chosen, else external when an endpoint is saved and local when nothing is (the rule of the backend). */
export const llmEngineOf = (s: Settings): "local" | "external" => (s.llm_engine === "local" || s.llm_engine === "external" ? s.llm_engine : s.llm_base_url ? "external" : "local");

const baseName = (path: string) => path.split(/[\\/]/).pop() || path;
const isEmbedder = (file: ServingFile) => !!file.info && (file.role ? file.role === "embedding" : /embed/i.test(file.name));

export function LlmEnginePanel({ s, onUpdated }: { s: Settings; onUpdated: (v: Settings) => void }) {
  const toast = useToast();
  const gateway = useGatewayStatus();
  const [files, setFiles] = useState<ServingFile[] | null>(null);
  const [notReady, setNotReady] = useState<NotReadyModel[]>([]);
  const [adapters, setAdapters] = useState<ServingAdapter[]>([]);
  const [runs, setRuns] = useState<RunAdapter[]>([]);                      // the fine-tuning runs with an adapter: what is waiting, for the reason the adapter list is empty
  const [context, setContext] = useState<string>(s.llm_local_context || "");
  const [testOpen, setTestOpen] = useState(false);
  const engine = llmEngineOf(s);

  const load = () => {
    api.servingModels().then((r) => { setFiles(r.files); setNotReady(r.not_ready ?? []); }).catch(() => setFiles([]));                 // ignored: without the list only the saved choice is shown
    api.servingAdapters().then((r) => setAdapters(r.adapters)).catch(() => setAdapters([]));
    api.convertAdapterRuns().then((r) => setRuns(r.adapters)).catch(() => setRuns([]));                    // ignored: without it the reason says only what is true of every empty list
    gateway.reload();
  };
  useEffect(load, []);          // eslint-disable-line react-hooks/exhaustive-deps; ignored: load once when the panel opens, then on "Look again", when a download or install ended, and when the page is brought back
  useOnChanged(["models", "program"], load);
  useOnActivate(load);

  const save = async (patch: Partial<Settings>, done: string) => {
    try { onUpdated(await api.putSettings(patch)); toast.success(done); gateway.reload(); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
  };

  const chat = (files ?? []).filter((f) => !!f.info && !isEmbedder(f));
  const chosen = s.llm_local_model || "";
  const modelOptions = [...chat.map((f) => ({ value: f.path, label: f.label ?? f.name, hint: f.label && f.label !== f.name ? undefined : f.info?.quantisation ?? undefined })),          // a label says the format already
    ...(chosen && !chat.some((f) => f.path === chosen) ? [{ value: chosen, label: `${baseName(chosen)} (not in the models folder any more)` }] : [])];
  const chosenAdapter = s.llm_local_adapter || "";
  const adapterOptions = [{ value: "", label: "None" }, ...adapters.map((a) => ({ value: a.path, label: a.name, hint: a.base ?? undefined })),
    ...(chosenAdapter && !adapters.some((a) => a.path === chosenAdapter) ? [{ value: chosenAdapter, label: `${baseName(chosenAdapter)} (not found)` }] : [])];
  const contextDirty = context.trim() !== (s.llm_local_context || "");

  const form = useCredForm({ title: "Language model", testTitle: "the language model", test: "llm", fields: EXTERNAL_FIELDS, saved: s, onUpdated, extra: { llm_engine: "external" } });
  const slotLoading = gateway.status?.slots.llm?.state === "loading";

  return (
    <>
      <SettingsCard title="Language model" desc="Classification, detection, patching, the project chat and the agent send their questions here."
        action={<IconButton label="Look again" onClick={load}><RefreshCw size={15} aria-hidden /></IconButton>}
        footer={engine === "local" ? <Button variant="subtle" onClick={() => setTestOpen(true)} disabled={!chosen && !gateway.status?.choices.llm.model}>Test the model</Button> : form.footer}>
        <SettingRow label="Engine" hint={engine === "local" ? "A model on this computer, run by Ollama. It loads when it is first asked and unloads after a time without use."
          : "An OpenAI-compatible endpoint: a server of your own or a provider's."}>
          <RadioGroup legend="Engine" hideLegend variant="segmented" value={engine} options={ENGINES}
            onValueChange={(v) => save({ llm_engine: v }, v === "local" ? "The language model runs on this computer." : "The language model is the external endpoint.")} />
        </SettingRow>

        {engine === "local" ? (
          <>
            <SelectField label="Model" value={chosen} placeholder={files === null ? "Reading the models folder" : "Choose a model"} options={modelOptions} disabled={slotLoading}
              emptyReason={files !== null ? noModelReason(notReady) : undefined}
              hint={slotLoading ? "The model is loading. The choice unlocks when the load finishes." : "Choose a chat or instruct GGUF model from the models folder."}
              onValueChange={(v) => save({ llm_local_model: v }, "The language model is chosen. It loads when it is first used.")} />
            <GatewayNote status={gateway.status} failed={gateway.failed} role="llm" onChanged={gateway.reload} />
            <details className="group">
              <summary className="caps cursor-pointer px-5 py-3 text-muted hover:text-fg">Advanced</summary>
              <div className="divide-y divide-line border-t border-line">
                <SelectField label="Adapter" value={chosenAdapter} options={adapterOptions} emptyReason={files !== null ? noAdapterReason(runs) : undefined} disabled={slotLoading}
                  hint="A LoRA adapter converted to GGUF (that happens by itself once the model it was trained on is installed), loaded with the model. It must have been trained on this model."
                  onValueChange={(v) => save({ llm_local_adapter: v }, v ? "The adapter is chosen." : "No adapter is loaded.")} />
                <SettingRow label="Context size" hint="Tokens the model holds at once, the question and the answer together. Empty means 4096. More needs more memory on the card.">
                  <div className="flex gap-2">
                    <Input aria-label="Context size" value={context} inputMode="numeric" placeholder="4096" autoComplete="off" disabled={slotLoading} onChange={(e) => setContext(e.target.value.replace(/\D/g, ""))} />
                    <Button variant="subtle" disabled={!contextDirty || slotLoading} onClick={() => save({ llm_local_context: context.trim() }, "The context size is saved.")}>Save</Button>
                  </div>
                </SettingRow>
              </div>
            </details>
          </>
        ) : form.rows}
      </SettingsCard>
      {testOpen && <TestModal target="llm" title="the language model" getBody={() => ({ llm_engine: "local", llm_local_model: chosen })} onClose={() => setTestOpen(false)} />}
      {engine !== "local" && form.dialog}
    </>
  );
}
