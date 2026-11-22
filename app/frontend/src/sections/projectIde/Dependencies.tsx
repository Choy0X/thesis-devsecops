import { Fragment } from "react";
import { Checkbox } from "./Controls";
import { describe, type Dependency } from "./client";

type Advisory = { id: string; ids?: string[]; severity?: unknown; affected?: unknown; summary?: string; modified?: string };
type Check = { status?: string; lookup?: { status?: string; fetched_at?: number; age_seconds?: number }; advisories?: Advisory[]; statement?: string };
export function Dependencies({ packages, selection, onSelection }: { packages: Dependency[]; selection: string[] | null; onSelection: (value: string[]) => void }) {
  return <table><thead><tr>{selection !== null && <th>Select</th>}<th>Package</th><th>Installed / resolved</th><th>Latest stable</th><th>Update status</th><th>Known advisories</th><th>Recommendation</th></tr></thead><tbody>{packages.map(pkg => {
    const checks = (pkg.checks || {}) as Record<string, Check>;
    const recommendation = pkg.recommendation as { version?: string; limitations?: string } | undefined;
    const installed = (pkg.installed as string[] | undefined) || [];
    const resolved = (pkg.resolved as string[] | undefined) || [];
    const displayVersions = installed.length ? installed : resolved.length ? resolved : (pkg.locked as string[] | undefined) || [];
    const currentChecks = Object.entries(checks).filter(([version]) => displayVersions.includes(version));
    const advisoryIds = [...new Set(currentChecks.flatMap(([, check]) => (check.advisories || []).map(advisory => advisory.id)))];
    const advisoryStatus = !currentChecks.length ? "Not checked" : currentChecks.some(([, check]) => check.status === "affected") ? `${advisoryIds.length} known ${advisoryIds.length === 1 ? "advisory" : "advisories"}` : currentChecks.some(([, check]) => check.status === "inconclusive") ? "Inconclusive" : currentChecks.every(([, check]) => check.status === "no_known_vulnerabilities") ? "No known vulnerabilities" : "Unknown";
    return <Fragment key={pkg.id}><tr>{selection !== null && <td><Checkbox aria-label={`Review source for ${pkg.name}`} checked={selection.includes(pkg.id)} onCheckedChange={checked => onSelection(checked ? [...selection, pkg.id] : selection.filter(id => id !== pkg.id))} /></td>}<td><code>{pkg.name}</code><small>{pkg.ecosystem}</small></td><td>{pkg.version || displayVersions.join(", ") || "Unknown"}</td><td>{typeof pkg.latest_stable === "string" ? pkg.latest_stable : "Unknown"}</td><td>{pkg.outdated === true ? "Outdated" : pkg.outdated === false ? "Current" : "Unknown"}</td><td>{advisoryStatus}{advisoryIds.length > 0 && <small>{advisoryIds.join(", ")}</small>}</td><td>{recommendation?.version || "None established"}</td></tr>
      <tr><td colSpan={selection !== null ? 8 : 7}><details className="ide-package-evidence"><summary>Evidence and provenance for {pkg.name}</summary>
        <p>Inventory: {describe(pkg.status || "unknown")}. Source availability: {describe(pkg.source_available ?? "unknown")}. Estimated source size: {describe(pkg.estimated_source_bytes ?? "unknown")}.</p>
        {Object.entries(checks).map(([version, check]) => <section key={version}><strong>Version {version}: {(check.status || "unknown").replace(/_/g, " ")}</strong><p>Advisory data: {check.lookup?.status || "unknown"}{check.lookup?.fetched_at ? `, fetched ${new Date(check.lookup.fetched_at * 1000).toLocaleString()}` : ""}{typeof check.lookup?.age_seconds === "number" ? `, age ${Math.round(check.lookup.age_seconds)} seconds` : ""}.</p>{check.statement && <p>{check.statement}</p>}{check.advisories?.map(advisory => <div key={advisory.id}><strong>{(advisory.ids || [advisory.id]).join(", ")}</strong><p>{advisory.summary}</p><p>Severity: {describe(advisory.severity || "unknown")}</p><details><summary>Affected and fixed ranges</summary><pre>{describe(advisory.affected)}</pre></details></div>)}</section>)}
        {recommendation?.limitations && <p>{recommendation.limitations}</p>}
        {typeof pkg.recommendation_limits === "string" && <p>{pkg.recommendation_limits}</p>}
        <details><summary>Full inventory and registry evidence</summary><pre>{describe(pkg)}</pre></details>
      </details></td></tr>
    </Fragment>;
  })}</tbody></table>;
}
