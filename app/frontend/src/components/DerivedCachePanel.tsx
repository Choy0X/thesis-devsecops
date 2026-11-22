import { useEffect, useState } from "react";
import { api, errorText, type DerivedCache, type DerivedEntry } from "../lib/api";
import { useOnChanged } from "../lib/changes";
import { gigabytes } from "../lib/fitWords";
import { whenWords } from "../lib/format";
import { Button, EmptyState, Panel, Skeleton } from "./ui";
import { Input } from "./kit";
import { useConfirm } from "./hud/AlertDialog";
import { useToast } from "./Toast";

// The models the app made from the ones that were downloaded (the GGUF files in data/models/converted). They can always be made again from the model they came from, which is kept, so they
// are a cache: with a budget, the one loaded least recently is deleted when a new one needs the room, except a pinned one, a loaded one and one that Settings chooses.

const keptBecause = (e: DerivedEntry): string => (e.pinned ? "Pinned" : e.in_use ? "Loaded now" : e.settings_roles.length ? `Chosen for ${e.settings_roles.join(" and ")}` : "");

export function DerivedCachePanel() {
  const toast = useToast();
  const confirm = useConfirm();
  const [cache, setCache] = useState<DerivedCache | null>(null);
  const [budget, setBudget] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = () => api.servingDerived().then((c) => { setCache(c); setBudget(c.budget_gb ? String(c.budget_gb) : ""); setError(""); }).catch((e) => setError(errorText(e)));
  useEffect(() => { load(); }, []);          // eslint-disable-line react-hooks/exhaustive-deps; ignored: read when the tab opens, and again when a model is installed or deleted
  useOnChanged(["models"], load);

  const saved = cache ? (cache.budget_gb ? String(cache.budget_gb) : "") : "";
  const dirty = budget !== saved;
  const over = !!cache && cache.budget_gb > 0 && cache.used_bytes > cache.budget_gb * 1024 ** 3;

  const saveBudget = async () => {
    setBusy("budget");
    try { await api.putSettings({ derived_cache_gb: budget }); toast.success(budget && Number(budget) > 0 ? "The budget is saved." : "There is no limit now: nothing is deleted."); await load(); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
    finally { setBusy(""); }
  };
  const pin = async (e: DerivedEntry) => {
    setBusy(e.name);
    try { await api.servingDerivedPin(e.name, !e.pinned); await load(); }
    catch (err) { toast.error(errorText(err)); }
    finally { setBusy(""); }
  };
  const trim = async () => {
    const ok = await confirm({ title: "Free space now?", confirmLabel: "Delete the oldest", cancelLabel: "Keep them",
      description: "The converted models used least recently are deleted until the cache is within its budget. A pinned model, a loaded one and one that Settings chooses are kept. They can be made again from the models they came from, which are not touched." });
    if (!ok) return;
    setBusy("trim");
    try {
      const r = await api.servingDerivedTrim();
      toast.success(r.removed.length ? `Deleted ${r.removed.length} converted model${r.removed.length === 1 ? "" : "s"}, ${gigabytes(r.freed_bytes)}.` : "Nothing needed to be deleted.");
      await load();
    } catch (err) { toast.error(errorText(err)); }
    finally { setBusy(""); }
  };

  return (
    <Panel title="Converted models" desc="Made from the models you downloaded, so that Ollama can run them. They can be made again, so they are a cache with a size you choose.">
      {error && <p role="alert" className="mb-2 text-[0.82rem] text-crit-soft">{error}</p>}
      {!cache && !error ? <Skeleton className="h-16 w-full" /> : cache && (
        <div className="space-y-3 text-[0.82rem]">
          <div className="flex flex-wrap items-end gap-2">
            <label className="block">
              <span className="caps block pb-1 text-muted">Most they may take, in GB</span>
              <Input aria-label="Most the converted models may take, in GB" value={budget} inputMode="decimal" placeholder="no limit" autoComplete="off"
                onChange={(e) => setBudget(e.target.value.replace(/[^\d.]/g, ""))} />
            </label>
            <Button variant="subtle" disabled={!dirty || busy === "budget"} loading={busy === "budget"} onClick={saveBudget}>Save</Button>
            {cache.budget_gb > 0 && <Button variant="ghost" disabled={!over || busy === "trim"} loading={busy === "trim"} onClick={trim}>Free space now</Button>}
          </div>
          <p className="text-muted">
            {cache.entries.length === 0 ? "Nothing has been converted here yet." : `${gigabytes(cache.used_bytes)} in ${cache.entries.length} model${cache.entries.length === 1 ? "" : "s"}.`}
            {cache.budget_gb > 0 ? ` The limit is ${cache.budget_gb} GB.` : " With no limit, nothing is ever deleted."}
          </p>
          {over && <p role="status" className="text-warn">The cache holds more than its limit. The next conversion makes room by itself; Free space now does it at once.</p>}
          {cache.entries.length === 0 ? <EmptyState title="No converted model" hint="Installing a model that comes as safetensors converts it, and the result is listed here." />
            : (
              <div className="overflow-x-auto">
                <table className="w-full text-[0.8rem]" aria-label="Converted models">
                  <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint">
                    <th className="py-2 pr-3 font-medium">Model</th><th className="py-2 pr-3 font-medium">Size</th><th className="py-2 pr-3 font-medium">Last loaded</th>
                    <th className="py-2 pr-3 font-medium">Adapters</th><th className="py-2 pr-3 font-medium">Kept</th><th className="py-2 font-medium"><span className="sr-only">Pin</span></th></tr></thead>
                  <tbody>
                    {cache.entries.map((e) => (
                      <tr key={e.name} className="border-b border-line/60 align-top">
                        <td className="py-2 pr-3"><span className="font-medium">{e.label ?? e.name}</span>{e.label && <span className="block text-[0.72rem] text-muted">{e.name}</span>}</td>
                        <td className="tnum py-2 pr-3">{gigabytes(e.bytes)}</td>
                        <td className="py-2 pr-3">{e.used ? whenWords(e.last_used) : "not yet"}</td>
                        <td className="tnum py-2 pr-3">{e.adapters || ""}</td>
                        <td className="py-2 pr-3">{keptBecause(e)}</td>
                        <td className="py-2 text-right">
                          <Button variant="ghost" size="sm" disabled={busy === e.name} onClick={() => pin(e)} aria-pressed={e.pinned} aria-label={`${e.pinned ? "Unpin" : "Pin"} ${e.label ?? e.name}`}>{e.pinned ? "Unpin" : "Pin"}</Button>
                        </td>
                      </tr>))}
                  </tbody>
                </table>
              </div>)}
        </div>)}
    </Panel>
  );
}
