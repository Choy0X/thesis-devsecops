import type { NotReadyModel, RunAdapter } from "./api";

// What to say when a model picker has nothing to offer. A model that is installed but cannot be run as it is (safetensors, which Ollama does not read) is named, and the way to make it
// runnable is said where it is done: Finish install, in Installed models of the Model Hub. Nobody is left with an empty list and no idea why.

const names = (models: NotReadyModel[]) => (models.length <= 2 ? models.map((m) => m.name).join(" and ") : `${models[0].name}, ${models[1].name} and ${models.length - 2} more`);

/** Explain why the language model picker is empty. */
export function noModelReason(notReady: NotReadyModel[]): string {
  const mine = notReady.filter((m) => m.role === "chat");
  if (mine.length) {
    return `No model that Ollama can run is ready. ${names(mine)} ${mine.length === 1 ? "is" : "are"} installed as safetensors, which Ollama cannot run as it is. `
      + `Press Finish install on ${mine.length === 1 ? "it" : "one"} in Installed models of the Model Hub: it is converted here and listed in this field when that is done.`;
  }
  return "No language model is installed. Install one from the Catalog tab of the Model Hub (a model that comes as safetensors is converted to GGUF as part of the install), or put a GGUF file in the models folder.";
}

/** The reason the adapter picker is empty: adapters are converted by themselves once the model they were trained on is installed, so a run that is waiting is named with the model it needs. */
export function noAdapterReason(runs: RunAdapter[] = []): string {
  const base = "No adapter has been converted yet. The adapter of an imported fine-tuning run is converted by itself once the model it was trained on is installed in the Model Hub.";
  const waiting = runs.filter((r) => !r.converted);
  if (!waiting.length) return `${base} To make one, fine-tune a model on the Train page and import its outputs there.`;
  const words = waiting.slice(0, 2).map((r) => (r.base_installed
    ? `${r.run} (its model, ${r.base}, is installed; it is converted at the next install or import)`
    : `${r.run} (install ${r.base ?? "the model it was trained on"} first)`));
  return `${base} Waiting: ${words.join("; ")}${waiting.length > 2 ? ` and ${waiting.length - 2} more` : ""}.`;
}
