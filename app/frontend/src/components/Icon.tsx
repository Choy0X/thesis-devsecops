// One drawn icon system: 1.6 stroke, round caps, 24-grid. No emoji/unicode.
import type { SVGProps } from "react";
const P = (d: string) => (p: SVGProps<SVGSVGElement>) => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
    strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden {...p}>
    {d.split("|").map((seg, i) => <path key={i} d={seg} />)}
  </svg>
);
export const Icon = {
  overview: P("M4 13h7V4H4z|M13 20h7v-9h-7z|M4 20h7v-4H4z|M13 8h7V4h-7z"),
  settings: P("M12 15a3 3 0 100-6 3 3 0 000 6z|M19.4 15a1.6 1.6 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.6 1.6 0 00-2.7 1.1V21a2 2 0 11-4 0v-.1A1.6 1.6 0 007.5 19a1.6 1.6 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.6 1.6 0 00-1.1-2.7H1a2 2 0 110-4h.1A1.6 1.6 0 003 7.5a1.6 1.6 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.6 1.6 0 001.8.3H9a1.6 1.6 0 001-1.5V1a2 2 0 114 0v.1a1.6 1.6 0 001 1.5 1.6 1.6 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.6 1.6 0 00-.3 1.8V9a1.6 1.6 0 001.5 1H23a2 2 0 110 4h-.1a1.6 1.6 0 00-1.5 1z"),
  data: P("M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3z|M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6|M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"),
  engine: P("M12 2v4|M12 18v4|M2 12h4|M18 12h4|M12 8a4 4 0 100 8 4 4 0 000-8z|M5 5l2.5 2.5|M16.5 16.5L19 19"),
  tune: P("M4 21v-7|M4 10V3|M12 21v-9|M12 8V3|M20 21v-5|M20 12V3|M1 14h6|M9 8h6|M17 16h6"),
  results: P("M4 20V10|M10 20V4|M16 20v-7|M22 20H2"),
  patch: P("M14.5 3.5l6 6-5 5-6-6z|M9.5 8.5l-6 6 6 6 6-6|M8 16l-2 2"),
  shield: P("M12 3l8 3v5c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6z|M9 12l2 2 4-4"),
  logs: P("M8 6h12|M8 12h12|M8 18h12|M3.5 6h.01|M3.5 12h.01|M3.5 18h.01"),
  play: P("M6 4l14 8-14 8z"),
  check: P("M20 6L9 17l-5-5"),
  alert: P("M12 9v4|M12 17h.01|M10.3 3.9L2 18a2 2 0 001.7 3h16.6A2 2 0 0022 18L13.7 3.9a2 2 0 00-3.4 0z"),
  spark: P("M12 3v3|M12 18v3|M3 12h3|M18 12h3|M5.6 5.6l2.1 2.1|M16.3 16.3l2.1 2.1|M18.4 5.6l-2.1 2.1|M7.7 16.3l-2.1 2.1"),
  box: P("M21 8l-9-5-9 5|M21 8v8l-9 5-9-5V8|M3 8l9 5 9-5|M12 13v9"),
  terminal: P("M4 5h16v14H4z|M8 10l3 2-3 2|M13 14h4"),
  external: P("M14 4h6v6|M20 4l-8 8|M18 14v4a2 2 0 01-2 2H6a2 2 0 01-2-2V8a2 2 0 012-2h4"),
};
export type IconName = keyof typeof Icon;
