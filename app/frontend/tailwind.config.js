/** @type {import('tailwindcss').Config} */

// A colour of the theme is a CSS variable (src/index.css), and Tailwind cannot put a transparency on a bare variable: `bg-bg/80` or `border-line/60` on `var(--bg)` produced no CSS at all, so the
// dimming behind every dialog and the soft borders were silently missing. Each colour is therefore a function: used plainly it is the variable itself, and with a modifier (`/60`) it is the
// variable mixed with transparent in that proportion. Plain uses are unchanged.
const colour = (name) => ({ opacityValue }) =>
  opacityValue === undefined || String(opacityValue).startsWith("var(")
    ? `var(--${name})`
    : `color-mix(in srgb, var(--${name}) ${Math.round(Number(opacityValue) * 10000) / 100}%, transparent)`;

const names = (list) => Object.fromEntries(list.map((n) => [n, colour(n)]));
const group = (prefix, list) => Object.fromEntries(list.map((n) => [n, colour(`${prefix}-${n}`)]));

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      screens: { rail: "1200px" },                     // the left rail shows labels from this width, icons only below it
      colors: {
        bg: colour("bg"),
        surface: colour("surface"),
        "surface-2": colour("surface-2"),
        "surface-3": colour("surface-3"),
        terminal: colour("terminal"),
        line: colour("border"),
        "line-strong": colour("border-strong"),
        fg: colour("fg"),
        muted: colour("muted"),
        faint: colour("faint"),
        ...names(["brand", "brand-strong", "brand-hover", "on-brand", "brand-line", "brand-tint", "brand-tint-strong",
          "info", "crit", "crit-soft", "crit-line", "crit-tint", "warn", "warn-line", "warn-tint", "ok", "ok-soft", "ok-line", "ok-tint", "ok-tint-strong"]),
        diff: { add: colour("diff-add"), "add-bg": colour("diff-add-bg"), del: colour("diff-del"), "del-bg": colour("diff-del-bg") },
        sev: group("sev", ["low", "medium", "high", "critical"]),
        chart: Object.fromEntries([1, 2, 3, 4, 5, 6].map((n) => [n, colour(`chart-${n}`)])),
      },
      spacing: { tight: "var(--space-tight)", panel: "var(--space-panel)", gutter: "var(--space-gutter)" },
      zIndex: { header: "var(--z-header)", overlay: "var(--z-overlay)", dialog: "var(--z-dialog)", toast: "var(--z-toast)" },
      transitionDuration: { fast: "var(--dur-fast)", DEFAULT: "var(--dur)", slow: "var(--dur-slow)" },
      transitionTimingFunction: { DEFAULT: "var(--ease)" },
      fontFamily: {
        sans: ["Inter Variable", "Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      borderRadius: { sm: "var(--radius-sm)", DEFAULT: "var(--radius)", card: "var(--radius-card)" },
      boxShadow: {
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)",
      },
    },
  },
  plugins: [],
};
