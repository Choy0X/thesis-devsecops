import { useEffect, useRef } from "react";
import { cn } from "../../lib/cn";
import { useMotionAllowed } from "./motion";

// Faint rain of characters, the Matrix nod of the interface. It is for the places that are mostly empty (the empty states, the start screen), never behind a table or dense text. It is a
// transparent canvas that fills its parent: columns of glyphs fall at about 20 frames a second and fade out behind themselves, in the brand red from --fx-rain. It draws nothing when motion
// is not allowed, pauses while the tab is hidden or the canvas is off screen, and cleans up after itself. A parent should be `relative`; the rain is aria-hidden and takes no pointer events.

const GLYPHS = [...Array(48).keys()].map((i) => String.fromCharCode(0xff66 + i)).join("") + "0123456789";
const FRAME_MS = 50;
const CELL = 16;

export function MatrixRain({ className }: { className?: string }) {
  const allowed = useMotionAllowed();
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!allowed || !canvas) return;
    let ctx: CanvasRenderingContext2D | null = null;
    try { ctx = canvas.getContext("2d"); } catch { ctx = null; }
    if (!ctx) return;
    const g = ctx;
    const colour = getComputedStyle(document.documentElement).getPropertyValue("--fx-rain").trim() || "currentColor";
    let width = 0, height = 0, drops: number[] = [];
    const resize = () => {
      const box = canvas.parentElement?.getBoundingClientRect();
      width = canvas.width = Math.max(1, Math.floor(box?.width ?? 0));
      height = canvas.height = Math.max(1, Math.floor(box?.height ?? 0));
      drops = Array.from({ length: Math.ceil(width / CELL) }, () => Math.floor(Math.random() * -40));
      g.font = `${CELL - 2}px ui-monospace, monospace`;
    };
    resize();

    let visible = true, raf = 0, last = 0;
    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      if (!visible || document.hidden || now - last < FRAME_MS) return;
      last = now;
      g.globalCompositeOperation = "destination-out";       // fade what is there, so each glyph leaves a short trail
      g.globalAlpha = 0.12;
      g.fillStyle = "black";
      g.fillRect(0, 0, width, height);
      g.globalCompositeOperation = "source-over";
      g.globalAlpha = 1;
      g.fillStyle = colour;
      drops.forEach((y, i) => {
        if (y >= 0) g.fillText(GLYPHS[Math.floor(Math.random() * GLYPHS.length)], i * CELL, y * CELL);
        drops[i] = y * CELL > height && Math.random() > 0.96 ? Math.floor(Math.random() * -20) : y + 1;
      });
    };
    raf = requestAnimationFrame(frame);

    const watch = typeof IntersectionObserver !== "undefined" ? new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; }) : null;
    watch?.observe(canvas);
    const size = typeof ResizeObserver !== "undefined" ? new ResizeObserver(resize) : null;
    if (canvas.parentElement) size?.observe(canvas.parentElement);
    return () => { cancelAnimationFrame(raf); watch?.disconnect(); size?.disconnect(); };
  }, [allowed]);

  if (!allowed) return null;
  return <canvas ref={ref} aria-hidden className={cn("pointer-events-none absolute inset-0 h-full w-full opacity-25", className)} />;
}
