import { useMemo } from "react";
import { lttb, type Pt } from "../../lib/lttb";
import { ChartFrame, ChartTable } from "./ChartFrame";

// A line chart for long series (loss, accuracy and so on): each series is cut to a few hundred points that keep its shape (largest triangle, three
// buckets), so 20,000 points cost no more than 600. Up to four series can be laid over each other; they differ by dash pattern as well as colour.

export type Series = { name: string; points: Pt[] };

const STROKE = ["text-chart-1", "text-chart-2", "text-chart-3", "text-chart-4"];
const DASH = [undefined, "6 3", "2 3", "8 3 2 3"];
export const MAX_SERIES = 4;

const fmt = (n: number) => (Math.abs(n) >= 1000 || (n !== 0 && Math.abs(n) < 0.01) ? n.toExponential(2) : String(Math.round(n * 1000) / 1000));

export function Curve({ title, description, series, xLabel = "step", yLabel = "value", maxPoints = 600, height = 220 }:
  { title: string; description?: string; series: Series[]; xLabel?: string; yLabel?: string; maxPoints?: number; height?: number }) {
  const shown = useMemo(() => series.slice(0, MAX_SERIES), [series]);
  const w = 640, padL = 44, padR = 10, padT = 10, padB = 24;
  const cut = useMemo(() => shown.map((s) => lttb(s.points, maxPoints)), [shown, maxPoints]);
  const all = cut.flat();
  const finite = all.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  const x0 = Math.min(...finite.map((p) => p.x)), x1 = Math.max(...finite.map((p) => p.x));
  const y0 = Math.min(...finite.map((p) => p.y)), y1 = Math.max(...finite.map((p) => p.y));
  const sx = (x: number) => padL + ((x - x0) / (x1 - x0 || 1)) * (w - padL - padR);
  const sy = (y: number) => height - padB - ((y - y0) / (y1 - y0 || 1)) * (height - padT - padB);
  const last = (s: Series) => s.points[s.points.length - 1];
  const table = <ChartTable caption={title} head={["Series", "Points", `First ${yLabel}`, `Last ${yLabel}`, `Lowest ${yLabel}`, `Highest ${yLabel}`]}
    rows={shown.map((s) => { const ys = s.points.map((p) => p.y); return [s.name, s.points.length.toLocaleString("en"), s.points.length ? fmt(s.points[0].y) : "-", s.points.length ? fmt(last(s).y) : "-",
      ys.length ? fmt(Math.min(...ys)) : "-", ys.length ? fmt(Math.max(...ys)) : "-"]; })} />;
  if (!finite.length) return <ChartFrame title={title} description={description} table={table}><p className="text-[0.8rem] text-muted">There are no points to draw yet.</p></ChartFrame>;
  const summary = `${title}. ${shown.map((s) => `${s.name}: ${s.points.length} points, last ${s.points.length ? fmt(last(s).y) : "none"}`).join("; ")}`;
  return (
    <ChartFrame title={title} description={description} table={table}>
      <svg viewBox={`0 0 ${w} ${height}`} role="img" tabIndex={0} aria-label={summary} className="block h-auto w-full outline-none focus-visible:ring-1 focus-visible:ring-brand-strong">
        {[0, 0.5, 1].map((f) => <line key={f} x1={padL} x2={w - padR} y1={padT + f * (height - padT - padB)} y2={padT + f * (height - padT - padB)} className="stroke-line" strokeWidth={1} />)}
        <text x={padL - 6} y={padT + 4} textAnchor="end" className="fill-faint" fontSize={10}>{fmt(y1)}</text>
        <text x={padL - 6} y={height - padB} textAnchor="end" className="fill-faint" fontSize={10}>{fmt(y0)}</text>
        <text x={padL} y={height - 6} className="fill-faint" fontSize={10}>{fmt(x0)}</text>
        <text x={w - padR} y={height - 6} textAnchor="end" className="fill-faint" fontSize={10}>{fmt(x1)} {xLabel}</text>
        {cut.map((pts, i) => (
          <g key={shown[i].name} className={STROKE[i]}>
            <polyline fill="none" stroke="currentColor" strokeWidth={1.6} strokeDasharray={DASH[i]} strokeLinejoin="round"
              points={pts.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y)).map((p) => `${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ")} />
          </g>
        ))}
      </svg>
      <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[0.74rem] text-muted" aria-label="Series">
        {shown.map((s, i) => (
          <li key={s.name} className="flex items-center gap-2">
            <svg width="26" height="8" aria-hidden className={STROKE[i]}><line x1="0" y1="4" x2="26" y2="4" stroke="currentColor" strokeWidth="2" strokeDasharray={DASH[i]} /></svg>
            {s.name}{s.points.length ? <span className="tnum text-faint"> {fmt(last(s).y)}</span> : null}
          </li>
        ))}
        {series.length > MAX_SERIES && <li className="text-faint">{series.length - MAX_SERIES} more not drawn</li>}
      </ul>
    </ChartFrame>
  );
}
