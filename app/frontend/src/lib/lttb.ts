export type Pt = { x: number; y: number };

/** Largest-Triangle-Three-Buckets: keeps `threshold` points of a long series so that its shape (peaks, dips, trends) survives. The first and the last
 *  point are always kept. A series that is already short enough is returned as it is. */
export function lttb(data: Pt[], threshold: number): Pt[] {
  const n = data.length;
  if (threshold >= n || threshold < 3) return data;
  const out: Pt[] = [data[0]];
  const every = (n - 2) / (threshold - 2);
  let a = 0;
  for (let i = 0; i < threshold - 2; i++) {
    const nextStart = Math.floor((i + 1) * every) + 1;
    const nextEnd = Math.min(Math.floor((i + 2) * every) + 1, n);
    let avgX = 0, avgY = 0;
    for (let j = nextStart; j < nextEnd; j++) { avgX += data[j].x; avgY += data[j].y; }
    const count = Math.max(1, nextEnd - nextStart);
    avgX /= count; avgY /= count;
    const start = Math.floor(i * every) + 1;
    const end = Math.floor((i + 1) * every) + 1;
    let best = -1, pick = start;
    for (let j = start; j < end; j++) {
      const area = Math.abs((data[a].x - avgX) * (data[j].y - data[a].y) - (data[a].x - data[j].x) * (avgY - data[a].y));
      if (area > best) { best = area; pick = j; }
    }
    out.push(data[pick]);
    a = pick;
  }
  out.push(data[n - 1]);
  return out;
}
