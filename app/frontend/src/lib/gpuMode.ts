import { useEffect, useState } from "react";
import { api, type GpuMode } from "./api";

// Who has the graphics card, for the title bar: idle, training, serving a model or building an index. Read from the backend every few seconds, and only while the
// window is shown; a failed read leaves the last answer (the bar must never say something it did not hear).

export const POLL_MS = 5000;

export const MODE_WORDS: Record<GpuMode["mode"], string> = { idle: "GPU idle", train: "GPU training", serve: "GPU serving", embed: "GPU indexing" };

export function modeTitle(m: GpuMode | null): string {
  if (!m) return "The graphics card's state is not known yet";
  if (m.mode === "idle" || m.holders.length === 0) return "No model is using the graphics card";
  return `The graphics card is in use by ${m.holders.map((h) => h.what).join(" and ")}`;
}

export function useGpuMode(enabled = true, pollMs = POLL_MS): GpuMode | null {
  const [mode, setMode] = useState<GpuMode | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const read = () => { if (!document.hidden) api.gpuMode().then((m) => { if (alive) setMode(m); }).catch(() => undefined); };    // ignored: the last answer stays
    read();
    const t = window.setInterval(read, pollMs);
    return () => { alive = false; window.clearInterval(t); };
  }, [enabled, pollMs]);
  return mode;
}
