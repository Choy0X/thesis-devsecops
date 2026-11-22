import { ChartFrame, ChartTable } from "../../components/charts/ChartFrame";
import { num } from "../../lib/format";

// How much of the pairs have model-written context, by how much real context they carry (weak, partial, strong): the bar's solid part is what had a draft before the
// campaign, the hatched part is what the campaign added. The numbers are written beside each bar.

const HATCH = "repeating-linear-gradient(135deg, var(--border-strong) 0 2px, transparent 2px 6px)";
type Level = { total: number; enriched: number };
const LEVELS = ["weak", "partial", "strong"];
const WORDS: Record<string, string> = { weak: "Weak context", partial: "Partial context", strong: "Strong context" };

export function CoverageBars({ before, after }: { before: Record<string, Level>; after: Record<string, Level> }) {
  const rows = LEVELS.map((l) => ({ level: l, total: after[l]?.total ?? 0, was: before[l]?.enriched ?? 0, now: after[l]?.enriched ?? 0 }));
  const max = Math.max(1, ...rows.map((r) => r.total));
  const table = <ChartTable caption="Model-written context by level of real context" head={["Level", "Pairs", "Had a draft before", "Have a draft now", "Added"]}
    rows={rows.map((r) => [WORDS[r.level], num(r.total), num(r.was), num(r.now), num(r.now - r.was)])} />;
  return (
    <ChartFrame title="Coverage by level of real context" description="Solid: pairs that already had a model-written draft. Hatched: drafts this campaign added. The grey bar is every pair of that level." table={table}>
      <ul className="space-y-3">
        {rows.map((r) => (
          <li key={r.level} tabIndex={0} aria-label={`${WORDS[r.level]}: ${num(r.now)} of ${num(r.total)} pairs have a draft, ${num(r.now - r.was)} added by this campaign`}
            className="text-[0.8rem] outline-none focus-visible:ring-1 focus-visible:ring-brand-strong">
            <div className="mb-1 flex justify-between gap-3"><span className="font-medium">{WORDS[r.level]}</span><span className="tnum text-muted">{num(r.now)} of {num(r.total)}{r.now > r.was ? ` (+${num(r.now - r.was)})` : ""}</span></div>
            <div aria-hidden className="flex h-3 bg-surface-2" style={{ width: `${(100 * r.total) / max}%` }}>
              <span className="h-full bg-brand" style={{ width: `${r.total ? (100 * r.was) / r.total : 0}%` }} />
              <span className="h-full" style={{ width: `${r.total ? (100 * (r.now - r.was)) / r.total : 0}%`, backgroundImage: HATCH }} />
            </div>
          </li>
        ))}
      </ul>
    </ChartFrame>
  );
}
