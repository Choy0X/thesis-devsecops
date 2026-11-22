import { useEffect, useState } from "react";
import { Button, Panel, Skeleton } from "../components/ui";
import { api, type BuildDetail, type BuildInfo, type SplitCheck } from "../lib/api";
import { num } from "../lib/format";
import { Hint } from "../components/kit";

export const TIERS = ["unreviewed", "auto_checked", "llm_adjudicated", "human_reviewed"];
export const TIER_NOTE: Record<string, string> = {
  unreviewed: "no trusted label, weight 0",
  auto_checked: "deterministic checks passed (grade G0): not a review",
  llm_adjudicated: "a model compared the claim with the evidence",
  human_reviewed: "a person followed the rubric",
};

// The cards of the reviewed datasets (pipeline/build_reviewed.py): what a build holds, the tier mix, and the split check. The Build dataset page makes the builds.
export function AdmissionCounts({counts}: {counts: Record<string, any>}) {
  if (counts.automatically_eligible == null && counts.supported == null && counts.supported_vulnerable == null && counts.protected == null) return null;
  return <div><dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[0.78rem] text-muted">{[
    ...(counts.supported != null || counts.supported_vulnerable != null || counts.protected != null ? [['Supported vulnerable', counts.supported ?? counts.supported_vulnerable], ['Protected', counts.protected]] : [['Automatically eligible', counts.automatically_eligible]]), ['Insufficient context', counts.insufficient_context], ['Excluded', counts.excluded],
  ].map(([label, value]) => <div key={label} className="flex gap-2"><dt>{label}</dt><dd className="tabular-nums text-fg">{value == null ? '-' : num(value)}</dd></div>)}</dl>
    <details className="mt-2 space-y-1 text-[0.76rem] text-muted"><summary className="cursor-pointer">What these counts mean</summary>
      <p>Excluded records produce no training examples. They remain stored for provenance and future evidence repair.</p>
      <p>Protected examples may be derived from supported records' fixed code. These counts are not disjoint source-record totals.</p>
      <p>Insufficient-context examples teach missing-evidence responses, rather than vulnerable or safe labels.</p>
      <p>Raw source records: {num(counts.records)}. Derived views across partitions: {num(counts.views)}. View counts do not establish independent source groups.</p>
    </details></div>;
}
export function BuildCard({ b: source }: { b: Pick<BuildInfo, "tag" | "status"> & Partial<Omit<BuildInfo, "tag" | "status">> }) {
  const b = { ...source, counts: source.counts ?? {}, tier_mix: source.tier_mix ?? {}, blocked: source.blocked ?? {}, versions: source.versions ?? {}, files: source.files ?? {} };
  const [open, setOpen] = useState(false);
  const [d, setD] = useState<BuildDetail | null>(null);
  useEffect(() => { if (open && !d) api.buildDetail(b.tag).then(setD).catch(() => undefined); }, [open]);   // ignored: the card stays collapsed without detail
  const frozen = b.status === "frozen";
  const total = TIERS.reduce((a, t) => a + (b.tier_mix[t] || 0), 0);
  const barClass = ["bg-surface-3", "bg-warn", "bg-brand", "bg-ok"];
  return (
    <Panel title={<span>dataset_{b.tag} <span className={`ml-2 rounded-full border px-2 py-0.5 text-[0.7rem] font-normal ${frozen ? "border-ok-line text-ok" : "border-line text-muted"}`}>{b.status || "preview"}</span></span>}
      right={<Button variant="ghost" onClick={() => setOpen(!open)}>{open ? "Hide details" : "Details"}</Button>}>
      <div className="flex flex-wrap gap-x-8 gap-y-2 text-[0.82rem]">
        <span><span className="text-muted">Records </span><b className="font-medium tnum">{num(b.counts.records)}</b></span>
        <span><span className="text-muted">Groups </span><b className="font-medium tnum">{num(b.counts.groups)}</b></span>
        <span><span className="text-muted">Views </span><b className="font-medium tnum">{num(b.counts.views)}</b></span>
        <span><span className="text-muted">Duplicates set aside </span><b className="font-medium tnum">{num(b.counts.duplicates)}</b></span>
      </div>
      <AdmissionCounts counts={b.counts} />
      <details className="mt-3 text-[0.76rem] text-muted"><summary className="cursor-pointer">Evidence provenance</summary>
        <div className="flex h-2 w-full overflow-hidden rounded-full bg-surface-2">
          {TIERS.map((t, i) => (b.tier_mix[t] || 0) > 0 ? (
            <div key={t} title={`${t}: ${b.tier_mix[t]}`} style={{ width: `${(b.tier_mix[t] / Math.max(total, 1)) * 100}%` }} className={barClass[i]} />
          ) : null)}
        </div>
        <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[0.76rem] text-muted">
          {TIERS.map((t) => <li key={t}><Hint label={TIER_NOTE[t]} className="no-underline">{t.replace(/_/g, " ")}: <b className="font-medium text-fg tnum">{num(b.tier_mix[t] || 0)}</b></Hint></li>)}
        </ul>
      </details>
      <p className="mt-2 text-[0.76rem] text-faint">Training readiness follows the build's automatic evidence, integrity and partition checks. Provenance records how each example was obtained.</p>
      {Object.entries(b.blocked).filter(([, v]) => v.status !== "available").length > 0 && (
        <ul className="mt-3 space-y-0.5 text-[0.74rem] text-faint">
          {Object.entries(b.blocked).filter(([, v]) => v.status !== "available").map(([k, v]) => (
            <li key={k}><span className="text-muted">{k.replace(/_/g, " ")}</span>: {v.status.replace(/_/g, " ")}{v.reason ? ` - ${v.reason}` : ""}</li>
          ))}
        </ul>
      )}
      {open && <Detail d={d} />}
    </Panel>
  );
}

function Detail({ d }: { d: BuildDetail | null }) {
  if (!d) return <Skeleton className="mt-4 h-24 w-full" />;
  const all = d.stats.coverage || [];
  const cov = all.slice(0, 60);
  const conc = d.stats.concentration || {};
  const exc = d.stats.exclusions || {};
  return (
    <div className="mt-4 space-y-3 border-t border-line pt-3">
      <p className="text-[0.76rem] text-muted">
        Largest repository share {conc.largest_repository_share !== undefined ? `${(conc.largest_repository_share * 100).toFixed(1)}%` : "-"}
        {exc.data_quality && <> - data quality {Object.entries(exc.data_quality).map(([k, v]) => `${k} ${num(v as number)}`).join(", ")}</>}
      </p>
      <div className="max-h-72 overflow-auto">
        <table className="w-full text-[0.76rem]">
          <thead className="sticky top-0 bg-surface text-left text-faint"><tr>
            {["Ecosystem", "Language", "Year", "Mechanism", "Records", "Auto-checked", "Views", "Train", "Val"].map((h) => <th key={h} className="py-1 pr-3 font-medium">{h}</th>)}
          </tr></thead>
          <tbody>{cov.map((c, i) => (
            <tr key={i} className="border-t border-line">
              <td className="py-1 pr-3">{c.ecosystem}</td><td className="py-1 pr-3">{c.language}</td><td className="py-1 pr-3">{c.year}</td>
              <td className="py-1 pr-3">{c.mechanism}</td><td className="py-1 pr-3 tnum">{num(c.records)}</td>
              <td className="py-1 pr-3 tnum">{num(c.auto_checked)}</td><td className="py-1 pr-3 tnum">{num(c.views)}</td>
              <td className="py-1 pr-3 tnum">{num(c.train)}</td><td className="py-1 tnum">{num(c.val)}</td>
            </tr>))}</tbody>
        </table>
      </div>
      {all.length > cov.length && <p className="text-[0.72rem] text-faint">Showing {cov.length} of {all.length} rows; the full table is in stats.json.</p>}
    </div>
  );
}

export function SplitCheckPanel() {
  const [r, setR] = useState<SplitCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const run = () => { setBusy(true); api.splitCheck().then(setR).finally(() => setBusy(false)); };
  return (
    <Panel title="Split check" desc="The saved split manifests with the state of their validation roles."
      right={<Button onClick={run} loading={busy}>Check</Button>}>
      {!r ? <p className="text-[0.8rem] text-muted">Not checked yet.</p> : (
        <div className="space-y-2 text-[0.8rem]">
          {r.split_versions.length === 0 ? <p className="text-muted">No split manifest is saved yet (a freeze saves one).</p> : (
            <ul className="space-y-1">{r.split_versions.map((s) => (
              <li key={s.version}>
                <span className="font-mono">{s.version}</span>{s.parent ? <span className="text-faint"> (builds on {s.parent})</span> : null}
                <span className="text-muted"> - validation roles: </span>
                <b className="font-medium">{s.val_roles ? String(s.val_roles.status).replace(/_/g, " ") : "-"}</b>
                {s.val_roles && <span className="text-faint"> ({s.val_roles.selection_groups} selection, {s.val_roles.calibration_groups} calibration groups)</span>}
              </li>))}</ul>
          )}
        </div>
      )}
    </Panel>
  );
}
