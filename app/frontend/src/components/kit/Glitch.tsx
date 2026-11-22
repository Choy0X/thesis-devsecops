import { useCallback, useEffect, useRef, useState, type ElementType } from "react";
import { cn } from "../../lib/cn";
import { useMotionAllowed } from "./motion";

// A word -- or a small block of lines, a `\n` is left whole like a space -- that glitches once. Three triggers: when the pointer or keyboard focus arrives ("hover"), once when it
// first appears, as the characters settle out of noise ("mount"), and "auto" -- mount's own settle, then a fresh burst on a slow, fixed interval for as long as it stays on screen
// (an ambient, unattended terminal rather than a one-shot). Rules that keep it a nudge and not a strobe: one burst of three steps within a quarter of a second (index.css), at most
// one burst a second however fast the pointer moves (the same cooldown also paces "auto"'s own interval), slices of the text shifted sideways with no change in brightness, and
// nothing at all when motion is not allowed (the plain text is shown, and "auto" never starts its interval). A screen reader gets the plain text from a visually hidden copy; the
// animated copy is hidden from it, so the noise is never read out.

const NOISE = "!<>-_\\/[]{}=+*^?#0123456789ABCDEF";
const COOLDOWN_MS = 1000;
const SETTLE_MS = 420;
const STEP_MS = 42;
const AUTO_INTERVAL_MS = 7000;

function burstMs(): number {
  const raw = typeof document !== "undefined" ? getComputedStyle(document.documentElement).getPropertyValue("--fx-glitch-dur") : "";
  const ms = parseFloat(raw);
  return Number.isFinite(ms) && ms > 0 ? ms : 240;
}

const scramble = (text: string, settled: number) =>
  [...text].map((ch, i) => (ch === " " || ch === "\n" || i < settled ? ch : NOISE[Math.floor(Math.random() * NOISE.length)])).join("");

export function Glitch({ text, on = "hover", as, className, autoIntervalMs = AUTO_INTERVAL_MS }: { text: string; on?: "hover" | "mount" | "both" | "auto"; as?: ElementType;
  className?: string; /** "auto" only: how often it bursts on its own. Below the one-burst-a-second cooldown a tick is simply wasted, not a faster strobe. */ autoIntervalMs?: number }) {
  const Tag = (as ?? "span") as ElementType;
  const allowed = useMotionAllowed();
  const [bursting, setBursting] = useState(false);
  const [shown, setShown] = useState(text);
  const last = useRef(-Infinity);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const trigger = useCallback(() => {
    if (!allowed) return;
    const now = Date.now();
    if (now - last.current < COOLDOWN_MS) return;
    last.current = now;
    setBursting(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setBursting(false), burstMs());
  }, [allowed]);

  useEffect(() => () => clearTimeout(timer.current), []);

  useEffect(() => {
    setShown(text);
    if (!allowed || (on !== "mount" && on !== "both" && on !== "auto")) return;
    const letters = [...text].length;
    const steps = Math.ceil(SETTLE_MS / STEP_MS);
    let step = 0;
    setShown(scramble(text, 0));
    const id = setInterval(() => {
      step += 1;
      if (step >= steps) { clearInterval(id); setShown(text); return; }
      setShown(scramble(text, Math.floor((step / steps) * letters)));
    }, STEP_MS);
    return () => clearInterval(id);
  }, [text, on, allowed]);

  useEffect(() => {
    if (!allowed || on !== "auto") return;
    const id = setInterval(trigger, autoIntervalMs);
    return () => clearInterval(id);
  }, [allowed, on, trigger, autoIntervalMs]);

  const hover = on === "hover" || on === "both";
  return (
    <Tag className={cn("glitch", className)} data-text={text} data-glitch={allowed && bursting ? "on" : undefined}
      onMouseEnter={hover ? trigger : undefined} onFocus={hover ? trigger : undefined}>
      <span className="sr-only">{text}</span>
      <span aria-hidden>{shown}</span>
    </Tag>
  );
}
