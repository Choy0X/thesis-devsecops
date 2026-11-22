import { useEffect, useState } from "react";
import { api, errorText, type HubModel, type LocalModel, type ShelfModel } from "../lib/api";
import { gigabytes } from "../lib/fitWords";
import { Button, EmptyState, Input, Skeleton } from "./ui";

// Choosing the base model of a run. Three places to choose from, none of which sends anything to the internet until asked: the models already downloaded (in the Hugging Face cache),
// the models in the folders the person pointed the app to (listed, but a model in a folder has no Hub commit to pin, so it cannot be a base model), and the Hub itself (a search, and the
// two models this project uses). A model is named by its Hub repository id; its exact version is pinned from the Hub when the run is prepared.

const REPO_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;

/** The models that can be trained as they are: stored as safetensors with their configuration, named like a Hub repository (these are the downloads of the Hugging Face cache). */
export const trainable = (models: LocalModel[]): LocalModel[] => models.filter((m) => m.training_eligible === true && m.source === "hub_cache" && m.safetensors && m.config && REPO_ID.test(m.id));
/** The models in the person's folders: listed with the reason they cannot be a base model. */
export const inFolders = (models: LocalModel[]): LocalModel[] => models.filter((m) => m.source === "folder" && m.safetensors);

type Place = "downloaded" | "hub";

export function ModelPicker({ value, contractDefault, onChange }: { value: string; contractDefault: string; onChange: (id: string) => void }) {
  const [place, setPlace] = useState<Place>("downloaded");
  const [local, setLocal] = useState<LocalModel[] | null>(null);
  const [localError, setLocalError] = useState("");
  const [shelf, setShelf] = useState<ShelfModel[] | null>(null);
  const [shelfBusy, setShelfBusy] = useState(false);
  const [shelfError, setShelfError] = useState("");
  const [q, setQ] = useState("");
  const [found, setFound] = useState<HubModel[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");

  useEffect(() => { api.localModels().then((r) => setLocal(r.models)).catch((e) => { setLocal([]); setLocalError(errorText(e)); }); }, []);

  const downloaded = local ? trainable(local) : [];
  const isDownloaded = (id: string) => downloaded.some((m) => m.id === id);
  const lookUpShelf = () => { setShelfBusy(true); setShelfError(""); api.modelShelf().then((r) => setShelf(r.models)).catch((e) => setShelfError(errorText(e))).finally(() => setShelfBusy(false)); };
  const search = () => { setSearching(true); setSearchError(""); api.searchModels(q.trim(), "generation").then((r) => setFound(r.models)).catch((e) => { setFound(null); setSearchError(errorText(e)); }).finally(() => setSearching(false)); };

  const choice = (id: string, extra?: string) => (
    <li key={id} className="flex flex-wrap items-center gap-x-3 gap-y-1 border border-line bg-surface-2 px-3 py-1.5 text-[0.8rem]">
      <span className="font-mono text-[0.76rem]">{id}</span>
      {extra && <span className="text-muted">{extra}</span>}
      <Button variant={value === id ? "primary" : "ghost"} className="ml-auto" aria-pressed={value === id} aria-label={`Use ${id}`} onClick={() => onChange(id)}>{value === id ? "Chosen" : "Use"}</Button>
    </li>);

  return (
    <div className="space-y-3 text-[0.82rem]">
      <p>
        <b className="font-medium">Base model:</b> <span className="font-mono" data-testid="chosen-model">{value}</span>
        {value === contractDefault ? " (the contract's)" : <> {" "}<Button variant="ghost" onClick={() => onChange(contractDefault)}>Use default model</Button></>}
      </p>
      <p className="text-[0.78rem] text-muted">{isDownloaded(value) ? "Downloaded on this computer. " : "Not downloaded on this computer. "}The run pins the model version from Hugging Face.</p>
      <div role="tablist" aria-label="Where to choose from" className="flex gap-1.5">
        {([["downloaded", "On this computer"], ["hub", "Hugging Face"]] as [Place, string][]).map(([id, label]) => (
          <button key={id} role="tab" type="button" aria-selected={place === id} onClick={() => setPlace(id)}
            className={`border px-3 py-1 text-[0.78rem] ${place === id ? "border-brand bg-brand-tint" : "border-line hover:border-line-strong"}`}>{label}</button>))}
      </div>

      {place === "downloaded" && (
        local === null ? <Skeleton className="h-16 w-full" /> : (
          <div className="space-y-3">
            {localError && <p role="alert" className="text-crit-soft">{localError}</p>}
            <section aria-label="Downloaded models">
              <h3 className="mb-1 text-[0.72rem] font-semibold uppercase tracking-wide text-faint">Downloaded</h3>
              {downloaded.length === 0 ? <EmptyState title="No supported generative model downloaded" hint="Search Hugging Face for a trainable generative model." />
                : <ul className="space-y-1">{downloaded.map((m) => choice(m.id, gigabytes(m.bytes)))}</ul>}
            </section>
          </div>))}

      {place === "hub" && (
        <div className="space-y-3">
          <section aria-label="Recommended models">
            <div className="mb-1 flex items-center justify-between">
              <h3 className="text-[0.72rem] font-semibold uppercase tracking-wide text-faint">Recommended</h3>
              <Button variant="ghost" loading={shelfBusy} onClick={lookUpShelf}>{shelf ? "Check again" : "Show supported models"}</Button>
            </div>
            {shelfError && <p role="alert" className="text-crit-soft">{shelfError}</p>}
            {shelf === null ? <p className="text-[0.78rem] text-muted">Showing them sends the model names to huggingface.co.</p>
              : <ul className="space-y-1" aria-label="Project models">{shelf.filter((m) => m.training_eligible === true && REPO_ID.test(m.id) && !m.error).map((m) => choice(m.id, m.role))}</ul>}
          </section>
          <section aria-label="Search the Hub">
            <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); search(); }}>
              <div className="min-w-[14rem] flex-1"><Input aria-label="Search models" placeholder="for example qwen coder" value={q} onChange={(e) => setQ(e.target.value)} /></div>
              <Button type="submit" variant="primary" loading={searching}>Search</Button>
            </form>
            <p className="mt-1 text-[0.76rem] text-muted">The search sends your words to huggingface.co. Nothing is downloaded.</p>
            {searchError && <p role="alert" className="mt-1 text-crit-soft">{searchError}</p>}
            {found && (found.length === 0 ? <p className="mt-2 text-muted">No model matches this search.</p>
              : <ul className="mt-2 space-y-1" aria-label="Search results">{found.filter((m) => m.training_eligible === true && REPO_ID.test(m.id)).map((m) => choice(m.id, `${m.gated ? "gated, " : ""}${m.downloads ?? 0} downloads`))}</ul>)}
          </section>
        </div>)}
    </div>
  );
}
