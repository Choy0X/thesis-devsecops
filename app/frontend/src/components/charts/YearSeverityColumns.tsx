import { useMemo, useState } from "react";
import { num } from "../../lib/format";
import type { ByYear } from "../../lib/api";
import { ChartFrame, ChartTable } from "./ChartFrame";

// How many vulnerabilities were published in each year, as one column per year. A column is as high as the number published that year and is split into bands by the severity the
// advisory gives (low at the bottom, critical on top; advisories without a severity word are the faint band at the very bottom). Each band has its own texture as well as its
// colour, and the legend and the table say the same in words, so nothing depends on telling colours apart. A column can be hovered or reached with the keyboard to read its counts, and
// the same numbers are one click away as a table. Nothing is animated.

export const BANDS = ["unrated", "low", "medium", "high", "critical"] as const;
type Band = (typeof BANDS)[number];
const LABEL: Record<Band, string> = { unrated: "No severity word", low: "Low", medium: "Medium", high: "High", critical: "Critical" };
const FILL: Record<Band, string> = { unrated: "fill-faint", low: "fill-sev-low", medium: "fill-sev-medium", high: "fill-sev-high", critical: "fill-sev-critical" };
// the stripes laid over a band's colour: none for low and critical (plain), diagonal for medium, crossed for high, dots for no severity word
const PATTERN: Record<Band, string | null> = { unrated: "ysc-dots", low: null, medium: "ysc-diagonal", high: "ysc-cross", critical: null };

/** The step of a y axis that gives about four to six round ticks up to ``max`` (1, 2, 2.5 or 5 times a power of ten). */
export function niceStep(max: number, ticks = 4): number {
  if (max <= 0) return 1;
  const raw = max / ticks, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
}

/** Which year labels to write under the columns so that they do not run into each other: all when there are few, otherwise every k-th and always the first and last. */
export function labelledYears(years: number[], room = 12): Set<number> {
  if (years.length <= room) return new Set(years);
  const k = Math.ceil(years.length / room);
  const out = new Set<number>();
  years.forEach((y, i) => { if (i % k === 0) out.add(y); });
  const lastLabelled = years.length - 1 - ((years.length - 1) % k);              // index of the last column that carries a label
  if (years.length - 1 - lastLabelled >= Math.ceil(k / 2)) out.add(years[years.length - 1]);
  return out;
}

const W = 640, H = 260, LEFT = 46, RIGHT = 8, TOP = 12, BOTTOM = 26;

function Swatch({ band }: { band: Band }) {
  return (
    <svg width={14} height={14} viewBox="0 0 14 14" aria-hidden className="shrink-0">
      <rect width={14} height={14} className={FILL[band]} />
      {PATTERN[band] && <rect width={14} height={14} fill={`url(#${PATTERN[band]})`} />}
    </svg>
  );
}

export function YearSeverityColumns({ data, title = "Advisories by year and severity", description = "How many vulnerabilities were published each year. Each column is split by the severity the advisory gives." }:
  { data: ByYear; title?: string; description?: string }) {
  const [active, setActive] = useState<number | null>(null);
  const years = data.years;
  const max = useMemo(() => Math.max(0, ...years.map((y) => y.total)), [years]);
  const step = niceStep(max);
  const top = Math.max(step, Math.ceil(max / step) * step);
  const table = <ChartTable caption={title} head={["Year", ...[...BANDS].reverse().map((b) => LABEL[b]), "All"]}
    rows={[...years].reverse().map((y) => [String(y.year), ...[...BANDS].reverse().map((b) => num(y[b])), num(y.total)])} />;
  if (!years.length) return <ChartFrame title={title} description={description} table={table}><p className="text-[0.8rem] text-muted">No vulnerability in the store has a publication date yet.</p></ChartFrame>;

  const plotW = W - LEFT - RIGHT, plotH = H - TOP - BOTTOM;
  const slot = plotW / years.length, barW = Math.max(2, Math.min(40, slot * 0.72));
  const y = (v: number) => TOP + plotH - (v / top) * plotH;
  const labels = labelledYears(years.map((r) => r.year));
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  const row = active != null ? years.find((r) => r.year === active) : undefined;
  const words = (r: (typeof years)[number]) => `${r.year}: ${num(r.total)} ${r.total === 1 ? "advisory" : "advisories"}. ${[...BANDS].reverse().filter((b) => r[b]).map((b) => `${LABEL[b]} ${num(r[b])}`).join(", ") || "none"}.`;
  return (
    <ChartFrame title={title} description={description} table={table}>
      <p role="status" className="mb-2 min-h-[1.2rem] text-[0.78rem] text-muted tnum">{row ? words(row) : "Hover a column, or move to one with the Tab key, to read its counts."}</p>
      <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label={`${title}. ${num(data.dated)} dated advisories from ${years[0].year} to ${years[years.length - 1].year}.`} className="block h-auto w-full">
        <defs>
          <pattern id="ysc-diagonal" width={6} height={6} patternUnits="userSpaceOnUse"><path d="M-1 1 L1 -1 M0 6 L6 0 M5 7 L7 5" style={{ stroke: "var(--bg)" }} strokeWidth={1.4} /></pattern>
          <pattern id="ysc-cross" width={6} height={6} patternUnits="userSpaceOnUse"><path d="M-1 1 L1 -1 M0 6 L6 0 M5 7 L7 5 M0 0 L6 6" style={{ stroke: "var(--bg)" }} strokeWidth={1.1} /></pattern>
          <pattern id="ysc-dots" width={5} height={5} patternUnits="userSpaceOnUse"><circle cx={2.5} cy={2.5} r={0.9} style={{ fill: "var(--bg)" }} /></pattern>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={LEFT} x2={W - RIGHT} y1={y(t)} y2={y(t)} className={t === 0 ? "stroke-line-strong" : "stroke-line"} strokeWidth={1} />
            <text x={LEFT - 6} y={y(t) + 3} textAnchor="end" className="fill-muted" fontSize={10}>{num(t)}</text>
          </g>))}
        {years.map((r, i) => {
          const cx = LEFT + slot * i + slot / 2, x = cx - barW / 2;
          let below = 0;
          return (
            <g key={r.year} tabIndex={0} role="img" aria-label={words(r)} data-year={r.year}
              onMouseEnter={() => setActive(r.year)} onMouseLeave={() => setActive((a) => (a === r.year ? null : a))} onFocus={() => setActive(r.year)} onBlur={() => setActive((a) => (a === r.year ? null : a))}
              className="outline-none [&:focus-visible>rect.ysc-ring]:stroke-brand-strong">
              <rect className="ysc-ring" x={LEFT + slot * i} y={TOP} width={slot} height={plotH} fill="transparent" strokeWidth={1.5} style={{ stroke: active === r.year ? "var(--muted)" : "transparent" }} />
              {BANDS.map((b) => {
                const v = r[b];
                if (!v) return null;
                const y0 = y(below + v), h = y(below) - y0;
                below += v;
                return (
                  <g key={b}>
                    <rect x={x} y={y0} width={barW} height={Math.max(h, 0.5)} className={FILL[b]} />
                    {PATTERN[b] && <rect x={x} y={y0} width={barW} height={Math.max(h, 0.5)} fill={`url(#${PATTERN[b]})`} />}
                  </g>);
              })}
              {labels.has(r.year) && <text x={cx} y={H - 8} textAnchor="middle" className="fill-muted" fontSize={10}>{r.year}</text>}
            </g>);
        })}
      </svg>
      <ul aria-label="Legend" className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.74rem] text-muted">
        {[...BANDS].reverse().map((b) => <li key={b} className="flex items-center gap-1.5"><Swatch band={b} />{LABEL[b]}</li>)}
      </ul>
      {data.undated > 0 && <p className="mt-1 text-[0.72rem] text-faint">{num(data.undated)} {data.undated === 1 ? "advisory has" : "advisories have"} no publication date and {data.undated === 1 ? "is" : "are"} not in a column.</p>}
    </ChartFrame>
  );
}
