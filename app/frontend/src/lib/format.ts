export const pct = (x: number | null | undefined, d = 1) =>
  x == null ? "—" : `${(x * 100).toFixed(d)}%`;
export const num = (x: number | null | undefined) =>
  x == null ? "—" : x.toLocaleString("en-US");
export const fixed = (x: number | null | undefined, d = 3) =>
  x == null ? "—" : x.toFixed(d);
/** A size in bytes in words: 740 kB, 12.3 MB, 1.4 GB. */
export const bytes = (n: number | null | undefined) =>
  n == null ? "—" : n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} GB` : n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} kB` : `${n} B`;
/** A time span as m:ss or h:mm:ss. */
export const clockSpan = (sec: number) => {
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
};
/** An amount in the unit a job reports: bytes as sizes, files as a count with the word, anything else as a plain count. */
export const amountOf = (n: number, unit?: string) => (unit === "bytes" ? bytes(n) : unit === "files" ? `${num(n)} ${n === 1 ? "file" : "files"}` : num(n));

export function whenWords(seconds: number, now: number = Date.now() / 1000): string {
  const s = Math.max(0, now - seconds);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
