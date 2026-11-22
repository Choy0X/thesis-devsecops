import type { ModelSlot } from "./api";

// The sentences about what the model gateway holds, shared by the Serving page and the engine panels in Settings.

export const where = (runs_on: ModelSlot["runs_on"] | undefined, gpu_layers?: number | null, layers?: number | null): string =>
  runs_on === "gpu" ? "on the graphics card" : runs_on === "split"
    ? gpu_layers != null && layers != null ? `${gpu_layers} of ${layers} layers on the card, the rest on the processor` : "on the graphics card and processor"
    : runs_on === "cpu" ? "on the processor" : "";

export const minutes = (seconds: number | null): string => (seconds == null ? "" : seconds < 90 ? `${Math.round(seconds)} s` : `${Math.round(seconds / 60)} min`);

export function slotWords(slot: ModelSlot | undefined): string {
  if (!slot || slot.state === "absent") return "Not loaded. It loads when it is first used.";
  switch (slot.state) {
    case "loading": return slot.message || "Loading the model. This takes a few seconds; the first load on a graphics card can take a couple of minutes.";
    case "draining": return "Finishing the answers in progress; then it unloads. New requests wait for the swap.";
    case "unloading": return "Unloading.";
    case "failed": return slot.error?.message ?? slot.message ?? "The model could not be loaded.";
    default: {
      const e = slot.engine;
      const place = where(slot.runs_on, e?.layers_on_card ?? e?.gpu_layers, e?.layers);          // the layers on the card, not counting the output head
      const idle = slot.idle_seconds != null ? ` Unused for ${minutes(slot.idle_seconds)}.` : "";
      return `Ready${place ? `, ${place}` : ""}${slot.in_flight ? `, answering ${slot.in_flight} request${slot.in_flight === 1 ? "" : "s"}` : ""}.${slot.in_flight ? "" : idle}`;
    }
  }
}
