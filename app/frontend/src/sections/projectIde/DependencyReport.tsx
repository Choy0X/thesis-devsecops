import { useState } from "react";

type Advisory = { id: string; ids?: string[]; summary?: string };
type AdvisoryCheck = {
  status?: "affected" | "no_known_vulnerabilities" | "inconclusive";
  lookup?: { status?: string; reason?: string };
  advisories?: Advisory[];
};
type DependencyVersion = {
  id: string; name: string; ecosystem?: string; resolved?: string[]; installed?: string[]; locked?: string[];
  resolution?: string; checks?: Record<string, AdvisoryCheck>;
  recommendation?: { version: string; compatibility?: string; limitations?: string } | null;
};

function describeCheck(check?: AdvisoryCheck, resolution?: string) {
  if (resolution === "minimum_declared") return {
    label: check?.status === "affected" ? "Advisory at declared minimum" : "Resolved version unknown",
    tone: "pending",
  };
  if (check?.status === "affected") return { label: "Affected", tone: "danger" };
  if (check?.status === "no_known_vulnerabilities" && check.lookup?.status === "fresh") return { label: "No advisory match", tone: "clear" };
  return { label: "Inconclusive", tone: "pending" };
}

const resolutionNames: Record<string, string> = {
  installed: "Installed", vendored: "Vendored", locked: "Locked", minimum_declared: "Declared minimum",
  declared: "Declared", unresolved: "Unresolved",
};

export function DependencyReport({ result }: { result: Record<string, unknown> }) {
  const [visible, setVisible] = useState(12);
  const packages: DependencyVersion[] = Array.isArray(result.packages) ? result.packages : [];
  const rows = packages.flatMap((item, packageIndex) => {
    const versions = Array.from(new Set(item.resolved || []));
    return (versions.length ? versions : [null]).map(version => {
      const check = version ? item.checks?.[version] : undefined;
      return { item, version, check, packageKey: item.id ?? packageIndex, ...describeCheck(check, item.resolution) };
    });
  });
  const affected = rows.filter(row => row.tone === "danger").length;
  const matchedNone = rows.filter(row => row.tone === "clear").length;
  const unknown = rows.length - affected - matchedNone;
  const rank: Record<string, number> = { danger: 0, pending: 1, clear: 2 };
  const ordered = [...rows].sort((left, right) => rank[left.tone] - rank[right.tone]);
  const ecosystems = Array.from(new Set(packages.map(row => row.ecosystem).filter(Boolean))).join(", ");
  const problems: { path?: string; reason?: string }[] = Array.isArray(result.problems) ? result.problems : [];
  return <div className="chat-dependency-report">
    {!rows.length ? <p className="chat-scan-explainer">No dependencies were found in the inspected scope.</p> : <>
      <p className="chat-scan-explainer">{packages.length} {packages.length === 1 ? "package" : "packages"}{ecosystems ? ` across ${ecosystems}` : ""}. Results are grouped by version.</p>
      <dl className="chat-scan-metrics" role="group" aria-label="Advisory check results">
        <div data-tone={affected ? "danger" : undefined}><dt>Affected</dt><dd>{affected}</dd></div>
        <div><dt>No advisory match</dt><dd>{matchedNone}</dd></div>
        <div data-tone={unknown ? "pending" : undefined}><dt>Inconclusive</dt><dd>{unknown}</dd></div>
      </dl>
      <details className="chat-scan-disclosure" open={Boolean(affected || unknown) || undefined}>
        <summary>Dependency versions and advisories</summary>
        <div className="chat-scan-dependencies">{ordered.slice(0, visible).map(({ item, version, check, label, tone, packageKey }) => <section key={packageKey + ":" + version} className="chat-scan-dependency" data-tone={tone}>
          <header><div><strong>{item.name}</strong>{item.ecosystem && <span>{item.ecosystem}</span>}</div><span className="chat-scan-tag" data-tone={tone}>{label}</span></header>
          <p className="chat-scan-version"><code>{version || "Exact version unavailable"}</code>
            <span>{resolutionNames[item.resolution || ""] || (item.installed?.includes(version || "") ? "Installed" : item.locked?.includes(version || "") ? "Locked" : "")}</span>
            {check?.lookup?.status && check.lookup.status !== "fresh" && <span>{check.lookup.status.replace(/_/g, " ")}</span>}</p>
          {tone === "pending" && check?.lookup?.reason && <p className="chat-scan-explainer">{check.lookup.reason}</p>}
          {!!check?.advisories?.length && <ul className="chat-scan-advisories">{check.advisories.map(advisory => <li key={advisory.id}><a href={"https://osv.dev/vulnerability/" + encodeURIComponent(advisory.id)} target="_blank" rel="noreferrer">{advisory.id}</a>
            {advisory.summary && <span>{advisory.summary}</span>}</li>)}</ul>}
          {tone === "danger" && (item.recommendation ? <p className="chat-scan-upgrade">
            <strong>Suggested version</strong> <code>{item.recommendation.version}</code>
            {item.recommendation.compatibility === "requires_constraint_change" && <span> Requires a manifest constraint change.</span>}
            {item.recommendation.compatibility === "needs_compatibility_review" && <span> Review compatibility before upgrading.</span>}
            {item.recommendation.limitations && <small>{item.recommendation.limitations}</small>}
          </p> : <p className="chat-scan-explainer">No confirmed upgrade was found for this dependency.</p>)}
        </section>)}</div>
        {visible < rows.length && <button className="chat-scan-more" type="button" onClick={() => setVisible(visible + 12)}>Show more versions</button>}
      </details>
    </>}
    {problems.length > 0 && <details className="chat-scan-disclosure"><summary>Inventory limitations ({problems.length})</summary>
      <ul className="chat-scan-inventory-issues">{problems.map((problem, index) => <li key={index}>{problem.path && <code>{problem.path} </code>}{problem.reason}</li>)}</ul>
    </details>}
  </div>;
}
