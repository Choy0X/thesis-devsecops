import { useRef, useState } from "react";
import { api, errorText, type HubDataset, type HubDatasetFiles } from "../lib/api";
import { Button } from "./ui";
import { Input, Radio } from "./kit";
import { RunConsole } from "./RunConsole";
import { num } from "../lib/format";

// Finding training data on Hugging Face. Looking only reads the Hub's metadata (names, licences, file sizes); a file is fetched only when the person presses the button next to it, after
// seeing its name, size and licence, and it then goes through the same checks as a file of their own. Nothing is fetched by browsing.

const mb = (n: number | null) => (n === null ? "size not stated" : n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`);
const base = (path: string) => path.split("/").pop() || path;

export function HubDatasets({ onFetched }: { onFetched: (up: { upload_id: string; filename: string; size_bytes: number }) => void }) {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [found, setFound] = useState<HubDataset[] | null>(null);
  const [repo, setRepo] = useState<HubDatasetFiles | null>(null);
  const [path, setPath] = useState("");
  const fetching = useRef<{ upload_id: string; path: string; size: number | null } | null>(null);

  const search = async () => {
    setBusy(true); setError(""); setRepo(null); setPath("");
    try { setFound((await api.hubDatasetSearch(query)).datasets); } catch (e) { setError(errorText(e)); setFound(null); }
    finally { setBusy(false); }
  };
  const open = async (id: string) => {
    setBusy(true); setError(""); setPath("");
    try { setRepo(await api.hubDatasetFiles(id)); } catch (e) { setError(errorText(e)); setRepo(null); }
    finally { setBusy(false); }
  };
  const chosen = repo?.files.find((f) => f.path === path) ?? null;
  const canFetch = !!repo && !!repo.revision && !repo.gated && !!chosen && chosen.within_limit;

  return (
    <div className="space-y-3 border border-line bg-surface-2 p-3" aria-label="Hugging Face datasets">
      <p className="text-muted">Search the datasets on Hugging Face. Looking fetches nothing; a file is fetched only when you press the button next to it. The licence is the one the Hub lists: check that it allows your use.</p>
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void search(); }}>
        <Input aria-label="Search Hugging Face datasets" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="for example: code vulnerability" className="min-w-0 flex-1" />
        <Button variant="ghost" type="submit" loading={busy}>Search</Button>
      </form>
      {error && <p role="alert" className="text-crit-soft">{error}</p>}
      {found && found.length === 0 && <p className="text-muted">No dataset matches.</p>}
      {found && found.length > 0 && !repo && (
        <ul aria-label="Datasets found" className="max-h-56 space-y-1 overflow-y-auto">
          {found.map((d) => (
            <li key={d.id} className="flex items-center justify-between gap-3 border-b border-line/60 py-1">
              <span className="min-w-0"><span className="font-mono text-[0.76rem]">{d.id}</span>
                <span className="ml-2 text-[0.74rem] text-faint">{d.downloads === null ? "" : `${num(d.downloads)} downloads, `}{d.license ?? "no licence listed"}{d.gated ? ", gated" : ""}</span></span>
              <Button variant="ghost" aria-label={`Show the files of ${d.id}`} onClick={() => void open(d.id)}>Files</Button>
            </li>))}
        </ul>)}
      {repo && (
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <p><span className="font-mono text-[0.76rem]">{repo.id}</span> <span className="text-[0.74rem] text-faint">at {repo.revision ? repo.revision.slice(0, 12) : "no commit"}, {repo.license ?? "no licence listed"}</span></p>
            <Button variant="ghost" onClick={() => { setRepo(null); setPath(""); }}>Back to the list</Button>
          </div>
          {repo.gated && <p role="alert" className="text-warn">This dataset is gated: the Hub requires you to accept its terms with your own account, which this app does not do. Download the file yourself and import it as a file.</p>}
          {!repo.license && <p className="text-warn">The Hub lists no licence for this dataset. Check where the data comes from before you use it.</p>}
          {repo.files.length === 0 && <p className="text-muted">It has no JSONL, Parquet or CSV file that can be imported.</p>}
          {repo.files.length > 0 && (
            <ul aria-label="Files that can be imported" className="max-h-48 space-y-1 overflow-y-auto">
              {repo.files.map((f) => (
                <li key={f.path} className="flex items-center gap-2 py-0.5">
                  <label className="flex min-w-0 flex-1 items-center gap-2">
                    <Radio name="hub-file" aria-label={f.path} checked={path === f.path} disabled={!f.within_limit} onChange={() => setPath(f.path)} />
                    <span className="min-w-0 truncate font-mono text-[0.76rem]">{f.path}</span>
                  </label>
                  <span className={`shrink-0 text-[0.74rem] ${f.within_limit ? "text-faint" : "text-warn"}`}>{mb(f.size_bytes)}{f.within_limit ? "" : f.size_bytes === null ? ", cannot be checked against the limit" : `, over the ${Math.round(repo.limit_bytes / 1048576)} MB limit`}</span>
                </li>))}
            </ul>)}
          {chosen && canFetch && (
            <div className="space-y-1">
              <p>Fetch <b className="font-medium">{chosen.path}</b> ({mb(chosen.size_bytes)}) from {repo.id}, licence {repo.license ?? "not listed"}. It is kept on this computer only to make a dataset from it, and checked row by row before anything is imported.</p>
              <RunConsole stage="fetch_hub_dataset" label="Fetch this file" params={{ repo: repo.id, path: chosen.path }}
                launch={async () => {
                  const job = await api.hubDatasetFetch(repo.id, repo.revision as string, chosen.path);
                  fetching.current = { upload_id: job.upload_id, path: chosen.path, size: chosen.size_bytes };
                  return job;
                }}
                onDone={() => { const f = fetching.current; if (f) onFetched({ upload_id: f.upload_id, filename: base(f.path), size_bytes: f.size ?? 0 }); }} />
            </div>)}
        </div>)}
    </div>
  );
}
