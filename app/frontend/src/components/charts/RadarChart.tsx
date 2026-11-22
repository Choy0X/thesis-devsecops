import { useState } from "react";
import { num } from "../../lib/format";
import { ChartFrame, ChartTable } from "./ChartFrame";

// A radar of one measure over a few categories. Every corner is a focusable mark with its value in words, and the same numbers are one click away as a
// table. A radar needs at least three categories; with fewer, the table is shown instead.

export type Axis = { label: string; value: number };

export function radarPoints(values: number[], max: number, radius: number, cx: number, cy: number): [number, number][] {
  const n = values.length;
  return values.map((v, i) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / n;
    const r = max > 0 ? (Math.max(0, v) / max) * radius : 0;
    return [Math.round((cx + r * Math.cos(angle)) * 100) / 100, Math.round((cy + r * Math.sin(angle)) * 100) / 100];
  });
}

export function RadarChart({ title, description, axes, unit = "" }: { title: string; description?: string; axes: Axis[]; unit?: string }) {
  const size = 240, c = size / 2, radius = 78;
  const max = Math.max(0, ...axes.map((a) => a.value));
  const total = axes.reduce((s, a) => s + a.value, 0);
  const pts = radarPoints(axes.map((a) => a.value), max, radius, c, c);
  const edge = radarPoints(axes.map(() => 1), 1, radius, c, c);
  const labelAt = radarPoints(axes.map(() => 1), 1, radius + 14, c, c);
  const [focus, setFocus] = useState<number | null>(null);
  const table = <ChartTable caption={title} head={["Category", `Count${unit ? ` (${unit})` : ""}`, "Share"]}
    rows={axes.map((a) => [a.label, num(a.value), total ? `${Math.round((a.value / total) * 100)}%` : "0%"])} />;
  if (axes.length < 3 || max === 0) {
    return <ChartFrame title={title} description={description} table={table}>
      <p className="text-[0.8rem] text-muted">{max === 0 ? "There is nothing to draw yet." : "A radar needs at least three categories, so the table below is shown instead."}</p>
      {max !== 0 && table}
    </ChartFrame>;
  }
  const ring = (f: number) => edge.map(([x, y]) => `${c + (x - c) * f},${c + (y - c) * f}`).join(" ");
  const summary = `${title}: ${axes.map((a) => `${a.label} ${num(a.value)}`).join(", ")}`;
  return (
    <ChartFrame title={title} description={description} table={table}>
      <svg viewBox={`0 0 ${size} ${size}`} role="group" aria-label={summary} className="mx-auto block h-auto w-full max-w-[17rem] text-brand-strong">
        {[0.25, 0.5, 0.75, 1].map((f) => <polygon key={f} points={ring(f)} fill="none" className="stroke-line" strokeWidth={1} />)}
        {edge.map(([x, y], i) => <line key={i} x1={c} y1={c} x2={x} y2={y} className="stroke-line" strokeWidth={1} />)}
        <polygon points={pts.map((p) => p.join(",")).join(" ")} fill="currentColor" fillOpacity={0.18} stroke="currentColor" strokeWidth={1.5} />
        {pts.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={focus === i ? 5 : 3.5} tabIndex={0} role="img" aria-label={`${axes[i].label}: ${num(axes[i].value)}${unit ? ` ${unit}` : ""}`}
            fill="currentColor" className="outline-none" onFocus={() => setFocus(i)} onBlur={() => setFocus(null)} onMouseEnter={() => setFocus(i)} onMouseLeave={() => setFocus(null)}>
            <title>{`${axes[i].label}: ${num(axes[i].value)}`}</title>
          </circle>
        ))}
        {axes.map((a, i) => {
          const [x, y] = labelAt[i];
          const anchor = Math.abs(x - c) < 6 ? "middle" : x > c ? "start" : "end";
          return <text key={a.label} x={x} y={y + 3} textAnchor={anchor} className="fill-muted" fontSize={9}>{a.label.length > 14 ? `${a.label.slice(0, 13)}...` : a.label}</text>;
        })}
      </svg>
      <p aria-live="polite" className="mt-1 h-4 text-center text-[0.74rem] text-muted">{focus !== null ? `${axes[focus].label}: ${num(axes[focus].value)}${unit ? ` ${unit}` : ""}` : ""}</p>
    </ChartFrame>
  );
}
