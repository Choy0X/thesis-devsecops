import { useEffect, useMemo, useRef, useState } from "react";
import { Button, EmptyState, ErrorState, Input, Panel, Skeleton } from "../components/ui";
import { DataTable, type Column } from "../components/hud/DataTable";
import { Dev } from "../lib/devMode";
import { api, type EcosystemCatalog, type EcosystemRow, type Job } from "../lib/api";
import { useJobs } from "../lib/jobsList";
import { amountOf, num } from "../lib/format";
import { InstallDialog, VERB, type Mode, type Stopped } from "./InstallDialog";
import { Hint } from "../components/kit";

// The ecosystems OSV publishes and what of each is on this computer: whether its dump was downloaded, whether it was read into the table that Find an advisory
// searches, whether OSV has a newer copy, and whether the app collects it. A dump is installed from here in two steps with real numbers (download, then read);
// the working store is not touched by that (a campaign's ingest does it). Removing a dump is not offered.

const megabytes = (b: number | null) => (b == null ? "" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`);
const STALE_MS = 6 * 3600 * 1000;                           // a look at OSV older than this is repeated when the page opens

/** A step that was cut: the download (a first one, or the fetch of a newer copy) or the read. */
const stoppedOf = (r: EcosystemRow): Stopped | undefined => (r.download_partial ? { kind: "download", percent: r.download_partial.percent }
  : r.read_partial ? { kind: "read", percent: r.read_partial.percent } : undefined);
const busyElsewhere = (r: EcosystemRow) => !!(r.download_partial?.active || r.read_partial?.active);      // some process (a campaign, another job) is working on it right now
const modeOf = (r: EcosystemRow): Mode => (r.download_partial ? (r.downloaded ? "update" : "download") : r.read_partial ? "read"
  : !r.downloaded ? "download" : !r.ingested ? "read" : r.update_state === "update_available" ? "update" : "check");
const INSTALL_STAGES = new Set(["osv_install", "osv_download"]);

/** What a running install is doing, in a few words, for the row. */
function activeWords(job: Job): string {
  const p = job.progress;
  if (!p) return "Starting";
  if (p.phase === "download") return p.total ? `Downloading ${Math.min(100, Math.round((100 * (p.processed ?? 0)) / p.total))}%` : p.processed ? `Downloading ${amountOf(p.processed, "bytes")}` : "Downloading";
  if (p.phase === "read") return p.total ? `Reading ${num(p.processed ?? 0)} of ${num(p.total)} files` : "Reading";
  return p.label || "Working";
}

function State({ r, active }: { r: EcosystemRow; active?: Job }) {
  const chip = (t: string, c: string) => <span className={`whitespace-nowrap border px-2 py-0.5 text-[0.7rem] ${c}`}>{t}</span>;
  const dp = r.download_partial, rp = r.read_partial;
  const stoppedWords = dp ? `${r.downloaded ? "Update" : "Download"} stopped${dp.percent == null ? "" : ` at ${dp.percent}%`}`
    : rp ? (rp.phase === "assembling" ? "Finishing the read" : `Reading stopped at ${rp.percent}%`) : "";
  const main = busyElsewhere(r) ? chip("Being worked on by another job", "border-brand-line text-brand-strong")
    : stoppedWords ? chip(stoppedWords, "border-warn-line text-warn")
    : r.ingested ? chip("Downloaded and read", "border-ok-line text-ok")
    : r.downloaded ? chip("Downloaded, not read yet", "border-warn-line text-warn")
    : chip("Not downloaded", "border-line text-faint");
  const checked = r.update_checked_at ? ` (looked at ${r.update_checked_at.slice(0, 16).replace("T", " ")})` : "";
  return (
    <span className="flex items-center gap-2">
      {main}
      {active ? <span role="status" className="whitespace-nowrap text-[0.72rem] text-brand-strong">{activeWords(active)}</span>
        : r.downloaded && r.update_state === "update_available" ? <Hint label={`OSV has a newer copy${checked}`} className="whitespace-nowrap text-[0.72rem] text-warn no-underline">Update available</Hint>
        : r.downloaded && r.update_state === "current" ? <Hint label={`The newest copy${checked}`} className="whitespace-nowrap text-[0.72rem] text-muted no-underline">Up to date</Hint> : null}
    </span>
  );
}

const date = (iso: string | null | undefined) => (iso ? iso.slice(0, 10) : "");

export function EcosystemTable() {
  const [cat, setCat] = useState<EcosystemCatalog | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [filter, setFilter] = useState("");
  const [only, setOnly] = useState<"all" | "mine">("all");
  const [dl, setDl] = useState<{ name: string; mode: Mode; stopped?: Stopped } | null>(null);
  const [nonce, setNonce] = useState(0);                       // bumped when a job ends: the dialog asks OSV again
  const jobs = useJobs();
  const active = useMemo(() => {
    const m = new Map<string, Job>();
    for (const j of jobs) if (j.status === "running" && INSTALL_STAGES.has(j.stage) && j.params?.ecosystem) m.set(String(j.params.ecosystem), j);
    return m;
  }, [jobs]);

  // Looking at OSV for each downloaded dump whose last look is missing or old, one at a time, so the table says which dumps have an update without anyone asking.
  const checked = useRef(new Set<string>());
  const queue = useRef<string[]>([]);
  const draining = useRef(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  const enqueue = (names: string[]) => { for (const n of names) if (!checked.current.has(n)) { checked.current.add(n); queue.current.push(n); } };
  const drain = () => {
    if (draining.current) return;
    draining.current = true;
    (async () => {
      try {
        while (queue.current.length && mounted.current) {
          const name = queue.current.shift()!;
          try {
            const res = await api.ecosystemUpdate(name);
            if (res && (res.state === "current" || res.state === "update_available") && mounted.current)
              setCat((prev) => prev && { ...prev, ecosystems: prev.ecosystems.map((x) => (x.name === name
                ? { ...x, update_state: res.state as "current" | "update_available", update_checked_at: new Date().toISOString(), remote_modified: res.remote_modified ?? x.remote_modified } : x)) });
          } catch { /* ignored: an ecosystem that could not be looked at keeps its state; "Check" asks again by hand */ }
        }
      } finally { draining.current = false; }
    })();
  };
  useEffect(() => {
    if (!cat) return;
    const old = (iso?: string | null) => !iso || Date.now() - Date.parse(iso) > STALE_MS;
    enqueue(cat.ecosystems.filter((r) => r.remote && r.downloaded && (r.update_state === "unknown" || (r.update_state && r.update_state !== "not_downloaded" && old(r.update_checked_at)))).map((r) => r.name));
    drain();
  }, [cat]);   // eslint-disable-line react-hooks/exhaustive-deps

  const load = (again = false) => {
    setErr(null);
    if (again) checked.current.clear();
    api.ecosystems().then((c) => { setCat(c); if (again) { enqueue(c.ecosystems.filter((r) => r.remote && r.downloaded).map((r) => r.name)); drain(); } }).catch(setErr);
  };
  useEffect(() => { load(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const columns = useMemo<Column<EcosystemRow>[]>(() => [
    { key: "name", label: "Ecosystem", render: (r) => <span>{r.name}{!r.remote && <span className="ml-2 text-[0.7rem] text-faint">(no longer listed by OSV)</span>}</span>, sortValue: (r) => r.name },
    { key: "state", label: "On this computer", width: "20rem", render: (r) => <State r={r} active={active.get(r.name)} />, sortValue: (r) => (r.ingested && !r.read_partial ? 2 : r.downloaded || r.read_partial ? 1 : 0) },
    { key: "size", label: "Dump size", width: "7rem", align: "right", render: (r) => megabytes(r.dump_bytes), sortValue: (r) => r.dump_bytes ?? 0 },
    { key: "snapshot", label: "OSV copy of", width: "9rem", render: (r) => <Hint label={r.remote_modified ? "The date OSV gives its copy" : "The day it was downloaded here"} className="no-underline">{date(r.remote_modified) || date(r.snapshot)}</Hint>, sortValue: (r) => r.remote_modified ?? r.snapshot ?? "" },
    { key: "enabled", label: "Collected by the app", width: "10rem", render: (r) => (r.enabled ? "Yes" : "No"), sortValue: (r) => (r.enabled ? 1 : 0) },
    {
      key: "action", label: "Dump", width: "8rem", align: "right",
      render: (r) => {
        const stopped = stoppedOf(r);
        if (!r.remote && !(r.downloaded && !r.ingested) && !stopped) return null;          // OSV no longer has it: it can still be read from disk, but not fetched or checked
        const mode = modeOf(r);
        const word = stopped ? "Continue" : VERB[mode];
        return <Button variant="ghost" aria-label={`${word} the ${r.name} dump`} disabled={active.has(r.name) || busyElsewhere(r)}
          onClick={(e) => { e.stopPropagation(); setDl({ name: r.name, mode, stopped }); setNonce((n) => n + 1); }}>{word}</Button>;
      },
    },
  ], [active]);
  const rows = useMemo(() => (cat?.ecosystems ?? []).filter((r) => r.name.toLowerCase().includes(filter.trim().toLowerCase()) && (only === "all" || r.downloaded || r.enabled)), [cat, filter, only]);

  return (
    <Panel title="Ecosystems" desc="What OSV publishes, and what of it is on this computer."
      right={<Button variant="ghost" onClick={() => load(true)}>Ask OSV again</Button>}>
      {!cat ? (err != null ? <ErrorState error={err} title="Could not read the ecosystems" onRetry={() => load()} /> : <Skeleton className="h-40 w-full" />) : (
        <>
          {cat.note && <p role="status" className="mb-3 border border-warn-line bg-warn-tint px-3 py-2 text-[0.78rem] text-warn">{cat.note}</p>}
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="min-w-[12rem] flex-1"><Input placeholder="Filter ecosystems" aria-label="Filter ecosystems" value={filter} onChange={(e) => setFilter(e.target.value)} /></div>
            <div role="radiogroup" aria-label="Which ecosystems" className="flex gap-1">
              {([["all", "All"], ["mine", "On this computer or collected"]] as const).map(([id, label]) => (
                <button key={id} type="button" role="radio" aria-checked={only === id} onClick={() => setOnly(id)}
                  className={`border px-3 py-1.5 text-[0.78rem] ${only === id ? "border-brand bg-brand-tint text-fg" : "border-line text-muted hover:text-fg"}`}>{label}</button>))}
            </div>
          </div>
          <DataTable caption="Ecosystems" columns={columns} rows={rows} rowKey={(r) => r.name} height={360}
            empty={<EmptyState title="No ecosystem to show" hint={cat.ecosystems.length ? "No ecosystem matches this filter." : "OSV could not be reached and nothing is saved yet."} />} />
          <p className="mt-3 text-[0.76rem] text-muted">{num(rows.length)} of {num(cat.ecosystems.length)} ecosystems.</p>
          <Dev><p className="mt-1 font-mono text-[0.7rem] text-faint">list source: {cat.source}</p></Dev>
        </>
      )}
      {dl && <InstallDialog name={dl.name} mode={dl.mode} stopped={dl.stopped} nonce={nonce} onClose={() => setDl(null)} onDone={() => { load(); setNonce((n) => n + 1); }} />}
    </Panel>
  );
}
