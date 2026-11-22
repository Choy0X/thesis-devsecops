import { num } from "../../lib/format";

// Where the rows of a draft dataset went: each filter of the build in order, what it started from and what it dropped. The build step reports this list; without it a
// campaign could say "22 rows" and not why 85 mined pairs became 22.

export type FunnelStep = { step: string; rows: number };

type Detail = { funnel?: FunnelStep[]; duplicates_removed?: number; leak_guard_removed?: number; rows?: number; warnings?: string[]; unscored?: number };

/** The funnel with the two steps the split adds (copies that survived the filters, and rows a leak guard drops), as ``[step, rows left]``. */
export function funnelRows(d: Detail): FunnelStep[] {
  const rows = [...(d.funnel ?? [])];
  const last = () => (rows.length ? rows[rows.length - 1].rows : 0);
  if ((d.duplicates_removed ?? 0) > 0) rows.push({ step: "exact copies that are left are removed (the split)", rows: last() - (d.duplicates_removed ?? 0) });
  if ((d.leak_guard_removed ?? 0) > 0) rows.push({ step: "validation rows that repeat training code are removed (leak guard)", rows: last() - (d.leak_guard_removed ?? 0) });
  return rows;
}

export function DatasetFunnel({ detail }: { detail: Detail }) {
  const rows = funnelRows(detail);
  if (!rows.length) return null;
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <table className="w-full text-[0.8rem]">
          <caption className="sr-only">Where the rows of the draft dataset went</caption>
          <thead><tr className="text-left text-faint"><th className="py-1.5 pr-4 font-medium">Filter</th><th className="py-1.5 pr-4 text-right font-medium">Rows left</th><th className="py-1.5 text-right font-medium">Dropped</th></tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const dropped = i === 0 ? null : rows[i - 1].rows - r.rows;
              return (
                <tr key={r.step} className="border-t border-line">
                  <td className="py-1.5 pr-4">{r.step}</td>
                  <td className="tnum py-1.5 pr-4 text-right">{num(r.rows)}</td>
                  <td className={`tnum py-1.5 text-right ${dropped ? "text-muted" : "text-faint"}`}>{dropped == null ? "start" : dropped ? `-${num(dropped)}` : "0"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!!detail.unscored && <p className="text-[0.78rem] text-muted">{num(detail.unscored)} {detail.unscored === 1 ? "row has" : "rows have"} no severity score.</p>}
      {!!detail.warnings?.length && (
        <ul className="list-disc space-y-1 pl-5 text-[0.78rem] text-warn">
          {detail.warnings.map((w) => <li key={w}>{w}</li>)}
        </ul>
      )}
    </div>
  );
}
