// The jobs that have a choice of device (the processor, the graphics card, both) and the saved "remember for this job type" choices. The list is the backend's (pipeline/compute_choice.py
// TABLE); a test reads both and fails if they differ.

export const DEVICE_STAGES: ReadonlySet<string> = new Set(["train_local", "gpu_smoke", "gpu_eval"]);

export type Device = "cpu" | "gpu" | "hybrid";

/** The remembered choices from the saved setting (a JSON object, job -> device); anything that is not that is no choice. */
export function rememberedOf(raw: unknown): Record<string, Device> {
  try {
    const data = typeof raw === "string" && raw.trim() ? JSON.parse(raw) : {};
    if (!data || typeof data !== "object" || Array.isArray(data)) return {};
    return Object.fromEntries(Object.entries(data).filter(([, v]) => v === "cpu" || v === "gpu" || v === "hybrid")) as Record<string, Device>;
  } catch {
    return {};                  // ignored: a saved value that does not parse is no remembered choice
  }
}

/** The text to save for a set of remembered choices ("" for none). */
export const rememberedText = (choices: Record<string, Device>): string => (Object.keys(choices).length ? JSON.stringify(choices) : "");

const SECONDS = (s: number) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${(s / 60).toFixed(1)} min` : `${(s / 3600).toFixed(1)} h`);
export const durationText = SECONDS;
