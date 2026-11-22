import type { CodeSummary } from "../lib/api";
import { Skeleton } from "../components/ui";
import { num } from "../lib/format";

// The numbers at the top of the Code pairs page: fix commits, mined, usable pairs, and the languages and exclusions.

export function Stats({ sum }: { sum: CodeSummary | null }) {
  if (!sum) return <Skeleton className="h-20 w-full" />;
  const f = sum.fix_commits, p = sum.pairs, d = sum.dataset;
  const tiles: [string, number | string, string?][] = [
    ["fix commits of records", f.total - (f.by_status.unlinked || 0), `${num(f.cves_with_fix)} CVEs`],
    ["mined", f.by_status.mined || 0, [`${num(f.by_status.pending || 0)} pending`,
      f.by_status.unlinked ? `${num(f.by_status.unlinked)} unlinked` : "",
      `${num(f.by_status.failed || 0)} failed`].filter(Boolean).join(" · ")],
    ["usable pairs", p.usable, `${num(p.functions)} function · ${num(p.by_granularity?.hunk || 0)} hunk`],
    ["CVEs with code", p.cves, `${num(p.commits)} commits`],
  ];
  const langs = Object.entries(p.by_language).slice(0, 6);
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-4">
        {tiles.map(([k, v, sub]) => (
          <div key={k} className="bg-surface px-4 py-3">
            <div className="text-[0.72rem] text-muted">{k}</div>
            <div className="tnum mt-0.5 text-[1.1rem] font-semibold">{typeof v === "number" ? num(v) : v}</div>
            {sub && <div className="tnum text-[0.7rem] text-faint">{sub}</div>}
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.74rem] text-muted">
        {langs.length > 0 && <span>languages: {langs.map(([l, n]) => `${l} ${num(n)}`).join(" · ")}</span>}
        {p.tangled > 0 && <span className="tnum">tangled (excluded): {num(p.tangled)}</span>}
        {p.tests > 0 && <span className="tnum">test files (excluded): {num(p.tests)}</span>}
        {Object.keys(p.by_skip_reason).length > 0 &&
          <span>code unavailable: {Object.entries(p.by_skip_reason).map(([r, n]) => `${r} ${num(n)}`).join(" · ")}</span>}
        {p.enriched > 0 && <span className="tnum">LLM-enriched: {num(p.enriched)}</span>}
        {d && <span className="tnum text-fg">dataset ({d.mode}): {Object.entries(d.split_counts || {}).map(([s, n]) => `${s} ${num(n as number)}`).join(" · ")}</span>}
      </div>
    </div>
  );
}
