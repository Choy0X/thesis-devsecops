import { useEffect, useState, type ReactNode } from "react";
import { Dialog } from "../components/hud/Dialog";
import { Badge, SeverityBadge, type Severity } from "../components/hud/Badge";
import { ErrorState, Skeleton } from "../components/ui";
import { api, type CveDetail, type CveHit } from "../lib/api";
import { webUrl, commitUrl } from "../lib/links";

// One advisory of a downloaded dump in full: what it says, how severe it is and how that was measured, which weaknesses and packages it names, where the fix is, where it came from and whether the
// store holds it. It opens at once with what the list already knew, and fills in as the dump is read (the biggest dump takes a second or two); a second request for another advisory never lets
// the earlier answer overwrite it.

const SEVERITIES: Record<string, Severity> = { low: "low", moderate: "medium", medium: "medium", high: "high", critical: "critical" };
const STATE_WORDS = { in_store: "In your store", downloaded: "Downloaded, not in your store" } as const;
const day = (iso: string | null | undefined) => (iso ? iso.slice(0, 10) : "");

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section><h3 className="mb-1 text-[0.7rem] font-semibold uppercase tracking-wider text-faint">{title}</h3><div className="text-[0.82rem]">{children}</div></section>;
}

function Link({ url, children }: { url: string; children: ReactNode }) {
  const safe = webUrl(url);
  return safe ? <a href={safe} target="_blank" rel="noreferrer" className="break-all text-brand-strong hover:underline">{children}</a> : <span className="break-all text-muted">{children}</span>;
}

/** The range events of one affected package in words: ``introduced 0, fixed 4.17.21``. */
export const eventWords = (events: Record<string, string>[]) => events.map((e) => Object.entries(e).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(", ")).join("; ");

export function AdvisoryDialog({ hit, onClose }: { hit: CveHit; onClose: () => void }) {
  const [d, setD] = useState<CveDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [again, setAgain] = useState(0);
  useEffect(() => {
    let on = true;                                                            // a later advisory's request must not be overwritten by this one's late answer
    setD(null); setError(null);
    api.cveDetail(hit.ecosystem, hit.osv_id).then((r) => { if (on) setD(r); }).catch((e) => { if (on) setError(e); });
    return () => { on = false; };
  }, [hit.ecosystem, hit.osv_id, again]);

  const id = hit.cve_id ?? hit.osv_id;
  const level = SEVERITIES[String(d?.severity_label ?? hit.severity ?? "").toLowerCase()];
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={id}
      description={`${hit.ecosystem}${hit.cve_id && hit.cve_id !== hit.osv_id ? ` · ${hit.osv_id}` : ""}${hit.published ? ` · published ${day(hit.published)}` : ""}`} className="w-[min(52rem,96vw)] max-h-[92vh]">
      <div className="space-y-4" aria-busy={!d && !error}>
        <div className="flex flex-wrap items-center gap-2">
          {level && <SeverityBadge level={level} />}
          <span className={`text-[0.78rem] ${(d?.state ?? hit.state) === "in_store" ? "text-ok" : "text-muted"}`}>{STATE_WORDS[d?.state ?? hit.state]}</span>
        </div>
        {error != null ? <ErrorState error={error} title="Could not open this advisory" onRetry={() => setAgain((n) => n + 1)} />
          : !d ? (
            <div role="status" aria-label="Loading the advisory" className="space-y-3">
              {hit.summary && <p className="text-[0.84rem]">{hit.summary}</p>}
              <Skeleton className="h-4 w-1/3" /><Skeleton className="h-20 w-full" /><Skeleton className="h-4 w-1/2" /><Skeleton className="h-16 w-full" />
            </div>
          ) : (
            <>
              <Section title="Summary"><p>{d.summary || <span className="text-muted">The advisory has no summary.</span>}</p></Section>
              {d.details && <Section title="Details"><p className="max-h-64 overflow-auto whitespace-pre-wrap text-muted">{d.details.trim()}</p></Section>}
              <Section title="Dates">
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-muted">
                  <dt className="text-faint">Published</dt><dd>{day(d.published) || "not given"}</dd>
                  <dt className="text-faint">Modified</dt><dd>{day(d.modified) || "not given"}</dd>
                  {d.withdrawn && <><dt className="text-faint">Withdrawn</dt><dd className="text-warn">{day(d.withdrawn)}</dd></>}
                  {d.nvd_published_at && <><dt className="text-faint">NVD published</dt><dd>{day(d.nvd_published_at)}</dd></>}
                </dl>
              </Section>
              <Section title="Severity">
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-muted">
                  <dt className="text-faint">Label</dt><dd>{d.severity_label ?? "not given"}{d.github_reviewed ? " (reviewed by GitHub)" : ""}</dd>
                  <dt className="text-faint">CVSS v3</dt><dd className="break-all font-mono text-[0.76rem]">{d.cvss_v3 ?? "not given"}</dd>
                  {d.cvss_v4 && <><dt className="text-faint">CVSS v4</dt><dd className="break-all font-mono text-[0.76rem]">{d.cvss_v4}</dd></>}
                </dl>
              </Section>
              {d.cwe.length > 0 && <Section title="Weaknesses"><div className="flex flex-wrap gap-1.5">{d.cwe.map((c) => <Badge key={c}>{c}</Badge>)}</div></Section>}
              <Section title={`Affected packages (${d.affected.length})`}>
                {d.affected.length === 0 ? <p className="text-muted">No package is named.</p> : (
                  <ul className="divide-y divide-line/60">
                    {d.affected.slice(0, 40).map((a, i) => (
                      <li key={`${a.package}:${i}`} className="py-1.5">
                        <span className="font-mono text-[0.78rem]">{a.package ?? "(no package)"}</span>{a.ecosystem && <span className="ml-2 text-faint">{a.ecosystem}</span>}
                        {a.ranges.map((r, j) => <p key={j} className="text-[0.76rem] text-muted">{r.type ? `${r.type}: ` : ""}{eventWords(r.events)}{r.repo ? ` (${r.repo})` : ""}</p>)}
                        {a.versions.length > 0 && <p className="text-[0.76rem] text-faint">versions: {a.versions.join(", ")}{a.versions_more ? ` and ${a.versions_more} more` : ""}</p>}
                      </li>))}
                  </ul>)}
                {d.affected.length > 40 && <p className="mt-1 text-[0.76rem] text-faint">and {d.affected.length - 40} more packages (all of them are in the raw record).</p>}
              </Section>
              <Section title={`Fix commits (${d.fix_commits.length})`}>
                {d.fix_commits.length === 0 ? <p className="text-muted">No fix commit is named.</p> : (
                  <ul className="space-y-0.5">{d.fix_commits.map((c, i) => {
                    const link = commitUrl(c.repo_url, c.sha);
                    return <li key={c.commit_key ?? i} className="font-mono text-[0.76rem]">{link ? <Link url={link}>{c.repo_url?.replace(/^https?:\/\//, "")} @ {c.sha?.slice(0, 10)}</Link> : <span className="text-muted">{c.repo_url ?? ""} {c.sha?.slice(0, 10)}</span>}{c.via && <span className="ml-2 font-sans text-faint">{c.via.replace(/_/g, " ")}</span>}</li>;
                  })}</ul>)}
              </Section>
              <Section title={`References (${d.references.length})`}>
                {d.references.length === 0 ? <p className="text-muted">No reference is given.</p> : (
                  <ul className="space-y-0.5">{d.references.slice(0, 40).map((r, i) => <li key={i}><span className="mr-2 text-[0.7rem] uppercase text-faint">{r.type ?? "web"}</span><Link url={r.url}>{r.url}</Link></li>)}</ul>)}
                {d.references.length > 40 && <p className="mt-1 text-[0.76rem] text-faint">and {d.references.length - 40} more.</p>}
              </Section>
              <Section title="Identifiers">
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-muted">
                  <dt className="text-faint">Advisory</dt><dd className="font-mono text-[0.76rem]">{d.osv_id}</dd>
                  {d.cve_id && <><dt className="text-faint">CVE</dt><dd className="font-mono text-[0.76rem]">{d.cve_id}</dd></>}
                  {d.aliases.length > 0 && <><dt className="text-faint">Aliases</dt><dd className="font-mono text-[0.76rem]">{d.aliases.join(", ")}</dd></>}
                </dl>
              </Section>
              <p className="text-[0.74rem] text-faint">
                {d.source === "dump" ? "Read from the downloaded dump." : "Rebuilt from the table read from the dump, because the dump itself is not on this computer."}
                {d.source_record_hash ? ` Record hash ${d.source_record_hash.slice(0, 12)}.` : ""}
              </p>
              {d.raw ? (
                <details><summary className="cursor-pointer text-[0.78rem] text-muted hover:text-fg">Raw record (JSON)</summary>
                  <pre aria-label="Raw record" className="mt-2 max-h-72 overflow-auto border border-line bg-terminal p-3 font-mono text-[0.72rem] leading-relaxed">{JSON.stringify(d.raw, null, 2)}</pre></details>
              ) : d.raw_omitted ? <p className="text-[0.76rem] text-faint">The raw record is too large to show here.</p> : null}
            </>)}
      </div>
    </Dialog>
  );
}
