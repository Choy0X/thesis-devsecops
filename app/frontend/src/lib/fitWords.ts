import type { FitResult } from "./api";
import { bytes } from "./format";

// What a fit result means, in the words the Model Hub uses. The estimate is never presented as a measurement.

/** A size in gigabytes, one decimal (none from 10 GB on); below a tenth of a gigabyte in the unit that fits (6.2 MB, 740 kB), because "0.0 GB" says nothing about a small model. */
export const gigabytes = (n: number): string => (n > 0 && n < 0.1 * 1024 ** 3 ? bytes(n) : `${(n / 1024 ** 3).toFixed(n >= 10 * 1024 ** 3 ? 0 : 1)} GB`);

export type Headline = { tone: "ok" | "warn" | "crit" | "unknown"; title: string; detail: string };

export function headline(check: FitResult["check"], mode: string): Headline {
  const what = mode === "qlora" ? "fine-tuning" : "running";
  if (check.runs_on === "gpu") {
    return check.gpu === "fits"
      ? { tone: "ok", title: `Fits on the graphics card`, detail: `Estimated ${what} fits in the free video memory with room to spare.` }
      : { tone: "warn", title: "Fits on the graphics card, but only just", detail: `Estimated ${what} fits in the free video memory without the safety margin. Close other programs that use the card, or choose a smaller setting.` };
  }
  if (check.runs_on === "cpu") {
    return { tone: "warn", title: "Too big for the graphics card; the processor could do it, slowly",
      detail: `The estimate is larger than the free video memory, but it fits in main memory. ${what === "fine-tuning" ? "Fine-tuning on the processor takes very long." : "Answers will be much slower than on the card."}` };
  }
  if (check.gpu === "unknown" && check.ram === "unknown") return { tone: "unknown", title: "Cannot tell", detail: "The free memory of this computer could not be read, so the estimate cannot be compared with it." };
  return { tone: "crit", title: "Does not fit on this computer", detail: `The estimate is larger than both the free video memory and the free main memory. Choose a smaller model, a smaller context or a smaller batch.` };
}

export const PART_LABEL: Record<string, string> = {
  weights: "Model weights", kv_cache: "Key and value cache", adapters_and_optimizer: "Adapters and optimizer", activations: "Activations", logits: "Output scores", overhead: "Fixed overhead",
};
