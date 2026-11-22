import { useWorkspaceState } from "../lib/workspaceState";
import { useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { Button, EmptyState, ErrorState, Panel, Skeleton } from "../components/ui";
import { Input, Select } from "../components/kit";
import { api, errorText, type CveHit, type CveSearch as Found } from "../lib/api";
import { bytes, num } from "../lib/format";
import { AdvisoryDialog } from "./AdvisoryDialog";
import { InstallDialog } from "./InstallDialog";

// Looking an advisory up in the dumps that were downloaded: by CVE id, advisory id, alias or package. The search reads the tables made from the dumps, so it needs no network; the newest
// advisories of every downloaded ecosystem are listed as soon as the page opens, each says whether the store already holds it, and a row opens the advisory in full. Typing waits a moment
// before asking, and a state (only what is in the store, only what is still just downloaded) needs a narrow enough search. A dump that was downloaded but not read yet is named, with a button
// to read it.

const PAGE = 100;
const STATE_WORDS = { in_store: "In your store", downloaded: "Downloaded, not in your store" } as const;
const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "");

export function CveSearch() {
  const [q, setQ] = useWorkspaceState("cves.q", "");
  const [typed, setTyped] = useWorkspaceState("cves.typed", "");
  const [ecosystem, setEcosystem] = useWorkspaceState("cves.ecosystem", "");
  const [state, setState] = useWorkspaceState<"" | "in_store" | "downloaded">("cves.state", "");
  const [offset, setOffset] = useWorkspaceState("cves.offset", 0);
  const [found, setFound] = useState<Found | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<CveHit | null>(null);                   // the advisory shown in full
  const [reading, setReading] = useState<{ name: string; percent: number | null } | null>(null);             // the dump being read, and how far an earlier cut read had got (null: it was not started)
  const [nonce, setNonce] = useState(0);                                   // bumped when a dump was read: the list is asked for again
  const options = useRef<string[]>([]);                                   // the ecosystems that were downloaded, from the first answer, so the choice does not shrink as results narrow
  const optionsAt = useRef(0);                                            // the value of `nonce` the options were taken at: reading a dump adds an ecosystem, so they are taken again

  useEffect(() => {
    if (typed.trim() === q) return;
    const t = window.setTimeout(() => { setQ(typed.trim()); setOffset(0); }, 300);
    return () => window.clearTimeout(t);
  }, [typed]);                                                       // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let on = true;
    setBusy(true);
    api.cveSearch({ q, ecosystem, state, limit: PAGE, offset })
      .then((r) => {
        if (!on) return;
        if ((!options.current.length || optionsAt.current !== nonce) && r.ecosystems_searched.length) { options.current = r.ecosystems_searched; optionsAt.current = nonce; }
        setFound(r); setError(null);
      })
      .catch((e) => { if (on) { setError(e); setFound(null); } })
      .finally(() => { if (on) setBusy(false); });
    return () => { on = false; };
  }, [q, ecosystem, state, offset, nonce]);

  const from = found && found.total ? found.offset + 1 : 0;
  const to = found ? found.offset + found.results.length : 0;
  const unread = found?.unread ?? [];
  return (
    <Panel title="Find an advisory" desc="Search the dumps you downloaded by CVE id, advisory id, alias or package. Nothing is fetched from the internet. Open a row to read the advisory in full.">
      <form role="search" className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); setQ(typed.trim()); setOffset(0); }}>
        <div className="min-w-[16rem] flex-1">
          <Input aria-label="Search advisories" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="CVE-2021-23337, GHSA-..., lodash"
            prefix={<Search size={14} aria-hidden />} />
        </div>
        <Select aria-label="Ecosystem" value={ecosystem} onValueChange={(v) => { setEcosystem(v); setOffset(0); }} className="w-auto min-w-[12rem]"
          options={[{ value: "", label: "All downloaded ecosystems" }, ...options.current.map((o) => ({ value: o, label: o }))]} />
        <Select aria-label="State" value={state} onValueChange={(v) => { setState(v as typeof state); setOffset(0); }} className="w-auto min-w-[10rem]"
          options={[{ value: "", label: "Any state" }, { value: "in_store", label: STATE_WORDS.in_store }, { value: "downloaded", label: STATE_WORDS.downloaded }]} />
      </form>

      {unread.length > 0 && (
        <div role="status" className="mt-3 border border-warn-line bg-warn-tint px-3 py-2 text-[0.78rem]">
          <p className="text-warn">{unread.length === 1 ? "A downloaded dump has" : `${unread.length} downloaded dumps have`} not been read yet, so {unread.length === 1 ? "it is" : "they are"} not searched.</p>
          <ul className="mt-1 space-y-1">
            {unread.map((u) => (
              <li key={u.ecosystem} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{u.ecosystem}</span><span className="text-muted">{bytes(u.dump_bytes)}</span>
                {u.partial && <span className="text-warn">{u.partial.phase === "assembling" ? "finishing the read" : `stopped at ${u.partial.percent}%`}</span>}
                <Button variant="ghost" aria-label={u.partial ? `Continue reading the ${u.ecosystem} dump` : `Read the ${u.ecosystem} dump`} disabled={u.partial?.active}
                  onClick={() => setReading({ name: u.ecosystem, percent: u.partial ? u.partial.percent : null })}>{u.partial ? "Continue reading" : "Read this dump"}</Button>
              </li>))}
          </ul>
        </div>
      )}

      <div className="mt-3" aria-busy={busy}>
        {error != null ? <ErrorState error={error} title={`The search failed: ${errorText(error)}`} /> : !found ? (
          <div role="status" aria-label="Loading advisories" className="space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /></div>
        ) : found.downloaded_ecosystems === 0 ? (
          <EmptyState icon={<Search size={20} />} title={unread.length ? "No dump has been read yet" : "No ecosystem is downloaded yet"}
            hint={unread.length ? "Read a downloaded dump above to search it here." : "Download an ecosystem in the table above, then search it here."} />
        ) : found.results.length === 0 ? (
          <p className="text-[0.82rem] text-muted">{found.query ? `No advisory in the downloaded dumps matches "${found.query}"` : "Nothing to show"}{state ? ` that is ${state === "in_store" ? "in your store" : "only downloaded"}` : ""}.</p>
        ) : (
          <>
            <ul aria-label="Advisories found" className="divide-y divide-line/60">
              {found.results.map((r) => {
                const id = r.cve_id ?? r.osv_id;
                return (
                  <li key={`${r.ecosystem}:${r.osv_id}`}>
                    <button type="button" aria-label={`Open ${id} in ${r.ecosystem}`} onClick={() => setOpen(r)}
                      className="grid w-full gap-x-4 gap-y-0.5 py-2 text-left text-[0.82rem] hover:bg-surface-2 sm:grid-cols-[minmax(0,1.3fr)_minmax(0,1.5fr)_auto]">
                      <div className="min-w-0">
                        <span className="font-mono text-[0.78rem]">{id}</span>
                        {r.cve_id && r.cve_id !== r.osv_id && <span className="ml-2 font-mono text-[0.72rem] text-faint">{r.osv_id}</span>}
                        {r.summary && <p className="truncate text-[0.76rem] text-muted" title={r.summary}>{r.summary}</p>}
                      </div>
                      <div className="min-w-0 text-[0.78rem] text-muted">
                        <span className="font-medium text-fg">{r.ecosystem}</span> {r.packages.join(", ")}{r.more_packages ? ` and ${r.more_packages} more` : ""}
                        <p className="text-faint">{[r.severity, day(r.published)].filter(Boolean).join(" · ")}</p>
                      </div>
                      <span className={`self-center text-[0.74rem] ${r.state === "in_store" ? "text-ok" : "text-muted"}`}>{STATE_WORDS[r.state]}</span>
                    </button>
                  </li>);
              })}
            </ul>
            <div className="mt-3 flex items-center justify-between text-[0.78rem] text-muted">
              <span className="tnum">{num(from)} to {num(to)} of {num(found.total)} advisories in {found.ecosystems_searched.length} downloaded ecosystem{found.ecosystems_searched.length === 1 ? "" : "s"}</span>
              <span className="flex gap-2">
                <Button variant="ghost" disabled={found.offset === 0} onClick={() => setOffset(Math.max(0, found.offset - PAGE))}>Previous</Button>
                <Button variant="ghost" disabled={to >= found.total} onClick={() => setOffset(found.offset + PAGE)}>Next</Button>
              </span>
            </div>
          </>)}
      </div>
      {open && <AdvisoryDialog hit={open} onClose={() => setOpen(null)} />}
      {reading && <InstallDialog name={reading.name} mode="read" nonce={0} stopped={reading.percent == null ? undefined : { kind: "read", percent: reading.percent }} onClose={() => setReading(null)} onDone={() => setNonce((n) => n + 1)} />}
    </Panel>
  );
}
