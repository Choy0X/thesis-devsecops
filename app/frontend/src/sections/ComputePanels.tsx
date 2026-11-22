import { useEffect, useState } from "react";
import { api, errorText, type ComputeInfo, type ComputePython, type ComputePythons, type Settings as S, type StorageInfo } from "../lib/api";
import { RefreshCw } from "lucide-react";
import { Button } from "../components/ui";
import { IconButton, Input, SelectField, SettingFacts, SettingNote, SettingRow, SettingsCard } from "../components/kit";
import { BrowseButton } from "../components/BrowseButton";
import { useToast } from "../components/Toast";
import { useOpenFolder } from "../components/useOpenFolder";

const gb = (v: number | null) => (v === null ? "unknown" : `${v.toLocaleString(undefined, { maximumFractionDigits: 1 })} GB`);
const baseName = (path: string) => path.split(/[\\/]/).slice(-3).join("/");

// Where the databases live. The stores are read in large scans, so the folder is worth putting on the fastest disk. A change is saved
// now and used after a restart (the server and the jobs it starts must use one folder); the files are moved by hand (README).
export function StoragePanel({ s, onUpdated }: { s: S; onUpdated: (v: S) => void }) {
  const toast = useToast();
  const open = useOpenFolder("interim");
  const [path, setPath] = useState<string>(s.interim_dir || "");
  const [facts, setFacts] = useState<StorageInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const cur: StorageInfo = facts || s.storage;

  const check = async () => {
    setBusy(true);
    try { setFacts(await api.storageInfo(path)); } catch (e) { toast.error(`Could not check the folder: ${errorText(e)}`); }
    finally { setBusy(false); }
  };
  const save = async () => {
    setBusy(true);
    try { onUpdated(await api.putSettings({ interim_dir: path } as Partial<S>)); setFacts(null); toast.success("Data folder saved. Restart the app to use it."); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
    finally { setBusy(false); }
  };

  return (
    <SettingsCard title="Parsed data and backups" desc="The parsed advisory files, derived tables and database backups (several GB). The database records use the separate folder below."
      footer={<><Button variant="subtle" onClick={check} loading={busy}>Check folder</Button><Button variant="primary" onClick={save} loading={busy} disabled={busy}>Save</Button></>}>
      <SettingRow label="Folder" hint={<>Empty means <code className="font-mono">data/interim</code> next to the code. An absolute path such as <code className="font-mono">C:\ztsa\interim</code> works.</>}>
        <div className="space-y-2">
          <Input aria-label="Folder" value={path} placeholder="data/interim" onChange={(e) => { setPath(e.target.value); setFacts(null); }} autoComplete="off" />
          <div className="flex flex-wrap items-center gap-2">
            <BrowseButton title="Choose the parsed data folder" onChosen={(p) => { setPath(p); setFacts(null); }} />
            <Button variant="ghost" onClick={open}>Open the folder in use</Button>
          </div>
        </div>
      </SettingRow>
      <SettingFacts label="The database folder">
        <dt>In use now</dt><dd className="font-mono">{s.storage.in_effect}</dd>
        <dt>Checked</dt><dd className="font-mono">{cur.path}</dd>
        <dt>Free space</dt><dd>{gb(cur.free_gb)}</dd>
      </SettingFacts>
      {cur.problems.length > 0 && <SettingNote className="text-crit">{cur.problems.join("; ")}</SettingNote>}
      {(s.storage.restart_required || (facts && facts.restart_required)) && !cur.problems.length && (
        <SettingNote className="text-warn">
          A different folder is saved but not in use yet. Close the app, copy the old folder's contents there (README, "Moving the stores"), and start the app again.
          Until you copy them the app would start with an empty folder.
        </SettingNote>
      )}
    </SettingsCard>
  );
}


export function DatabaseStoragePanel({ s, onUpdated }: { s: S; onUpdated: (v: S) => void }) {
  const toast = useToast();
  const [path, setPath] = useState(s.database_dir || "");
  const [facts, setFacts] = useState<StorageInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const current = facts || s.database_storage;
  const check = async () => {
    setBusy(true);
    try { setFacts(await api.databaseStorageInfo(path)); }
    catch (e) { toast.error(`Could not check the folder: ${errorText(e)}`); }
    finally { setBusy(false); }
  };
  const save = async () => {
    setBusy(true);
    try {
      onUpdated(await api.putSettings({ database_dir: path.trim() }));
      setFacts(null);
      toast.success("Database records folder saved. Restart the app to use it.");
    } catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
    finally { setBusy(false); }
  };
  return (
    <SettingsCard title="Database records folder" desc="The local SQLite files that hold records, campaigns, reviews and conversations. A saved folder change takes effect after a restart."
      footer={<><Button variant="subtle" onClick={check} loading={busy}>Check folder</Button><Button variant="primary" onClick={save} loading={busy} disabled={busy || path.trim() === (s.database_dir || "")}>Save</Button></>}>
      <SettingRow label="Database records folder" hint="Leave empty to use the database subfolder of the parsed data folder. Choose a local disk.">
        <div className="space-y-2">
          <Input aria-label="Database records folder" value={path} placeholder="data/interim/database" onChange={(e) => { setPath(e.target.value); setFacts(null); }} autoComplete="off" />
          <BrowseButton title="Choose the database records folder" onChosen={(p) => { setPath(p); setFacts(null); }} />
        </div>
      </SettingRow>
      {current && <SettingFacts label="The database records folder">
        <dt>In use now</dt><dd className="break-all font-mono">{s.database_storage?.in_effect}</dd>
        <dt>Saved or checked</dt><dd className="break-all font-mono">{current.path}</dd>
        <dt>Free space</dt><dd>{gb(current.free_gb)}</dd>
      </SettingFacts>}
      {!!current?.problems.length && <SettingNote className="text-crit">{current.problems.join("; ")}</SettingNote>}
      {current?.restart_required && <SettingNote className="text-warn">Close the app and copy the complete old database folder to the saved location before starting again. The app does not move files automatically.</SettingNote>}
    </SettingsCard>
  );
}

/** The options of the worker count: Automatic with the number it comes to, then 1 up to the logical cores. A saved value outside that range stays, so it is never silently changed. */
export function workerOptions(info: ComputeInfo | null, saved: string) {
  const top = Math.max(info?.logical_cores ?? 0, /^\d+$/.test(saved) ? Number(saved) : 0, 1);
  return [{ value: "auto", label: info ? `Automatic (${info.auto_cpu_workers})` : "Automatic" },
    ...Array.from({ length: top }, (_, i) => ({ value: String(i + 1), label: String(i + 1) }))];
}

/** The options of the GPU interpreter: Automatic first, then each interpreter found; one that cannot drive the card is listed but cannot be chosen, with the reason. A saved path that was not found stays. */
export function pythonOptions(found: ComputePythons | null, saved: string) {
  const auto = found?.automatic ? found.pythons.find((p) => p.path === found.automatic) : undefined;
  const automatic = found ? (found.automatic ? `Automatic: ${auto?.label ?? baseName(found.automatic)}` : "Automatic: none found next to the code") : "Automatic";
  const rows = (found?.pythons ?? []).map((p: ComputePython) => ({
    value: p.path, disabled: !p.cuda,
    label: p.label,
    hint: p.cuda ? [p.torch ? `torch ${p.torch}` : "", p.cuda_build ? `CUDA ${p.cuda_build}` : "", p.name ?? ""].filter(Boolean).join(", ") : (p.reason ?? "cannot use the card"),
  }));
  const extra = saved && found && !found.pythons.some((p) => p.path === saved) ? [{ value: saved, label: `${baseName(saved)} (not found)`, hint: undefined as string | undefined, disabled: false }] : [];
  return [{ value: "", label: automatic, hint: undefined as string | undefined, disabled: false }, ...rows, ...extra];
}

// How many CPU worker processes the heavy stages use, and which Python drives the local GPU jobs. The CPU stages (building the dataset,
// splitting code, scanning for duplicates) are pure Python, so they scale with cores; collection and mining wait on the network and on
// GitHub's quota, so they do not, and the GPU cannot speed them up. A choice is saved at once.
export function ComputePanel({ s, onUpdated }: { s: S; onUpdated: (v: S) => void }) {
  const toast = useToast();
  const [info, setInfo] = useState<ComputeInfo | null>(null);
  const [found, setFound] = useState<ComputePythons | null>(null);
  const [looking, setLooking] = useState(false);
  const load = () => api.compute().then(setInfo).catch(() => undefined);   // ignored: the panel keeps its last numbers
  const look = (refresh = false) => {
    setLooking(true);
    api.computePythons(refresh).then(setFound).catch(() => undefined).finally(() => setLooking(false));        // ignored: without the list only Automatic and the saved choice are offered
  };
  useEffect(() => { load(); look(); }, []);          // eslint-disable-line react-hooks/exhaustive-deps; ignored: read once when the panel opens, then on Refresh

  const workers = (s.cpu_workers || "auto").trim().toLowerCase() || "auto";
  const gpuPy = s.gpu_python || "";
  const save = async (patch: Partial<S>, done: string) => {
    try { onUpdated(await api.putSettings(patch)); toast.success(done); load(); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
  };
  const gpu = info?.gpu;

  return (
    <SettingsCard title="Compute" desc="Worker processes for the CPU-bound stages, and the Python for local GPU jobs."
      action={<IconButton label="Refresh" onClick={() => { load(); look(true); }}><RefreshCw size={15} aria-hidden /></IconButton>}>
      <SelectField label="Worker processes" value={workers} options={workerOptions(info, workers)}
        hint="Used by the stages that have no workers field of their own. Automatic leaves a core for the window and keeps within the free memory."
        onValueChange={(v) => save({ cpu_workers: v }, "The number of worker processes is saved.")} />
      <SelectField label="GPU Python" value={gpuPy} options={pythonOptions(found, gpuPy)} placeholder="Automatic"
        hint={looking && !found ? "Looking for interpreters with CUDA torch..." : "The interpreter with CUDA torch that runs local training and evaluation. Found next to the code and in the app's own environment."}
        onValueChange={(v) => save({ gpu_python: v }, v ? "The GPU Python is saved." : "The GPU Python is chosen automatically.")} />
      <SettingFacts label="This machine">
        <dt>This machine</dt>
        <dd>{info ? `${info.physical_cores} cores (${info.logical_cores} threads), ${info.ram_gb ?? "?"} GB RAM, ${info.ram_free_gb ?? "?"} GB free` : "reading..."}</dd>
        <dt>Workers in use</dt>
        <dd>{info ? `${info.cpu_workers}${workers === "auto" ? ` (automatic: ${info.auto_cpu_workers})` : ""}` : "..."}</dd>
        <dt>GPU</dt>
        <dd>{!info ? "reading..." : gpu?.available ? `${gpu.name}, ${gpu.free_mb} of ${gpu.total_mb} MB free` : `not available${gpu?.reason ? ` (${gpu.reason})` : ""}`}</dd>
      </SettingFacts>
    </SettingsCard>
  );
}
