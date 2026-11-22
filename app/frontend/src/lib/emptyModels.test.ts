import { describe, expect, it } from "vitest";
import type { NotReadyModel, RunAdapter } from "./api";
import { noAdapterReason, noModelReason } from "./emptyModels";

const model = (name: string, role: "chat" | "embedding" = "chat"): NotReadyModel => ({ id: `org/${name}`, name, source: "hub_cache", bytes: 1, role });
const run = (over: Partial<RunAdapter> = {}): RunAdapter => ({ run: "pilot/run", name: "pilot-run", folder: "x", base: "Qwen/Qwen2.5-Coder-7B", rank: 32, bytes: 1, base_installed: false, converted: false, ...over });

describe("noModelReason", () => {
  it("names installed language models that still need conversion", () => {
    const text = noModelReason([model("Qwen2.5-Coder-7B-Instruct"), model("jina-embed", "embedding")]);
    expect(text).toBe("No model that Ollama can run is ready. Qwen2.5-Coder-7B-Instruct is installed as safetensors, which Ollama cannot run as it is. "
      + "Press Finish install on it in Installed models of the Model Hub: it is converted here and listed in this field when that is done.");
  });

  it("says one of them when there are several, naming at most two", () => {
    expect(noModelReason([model("a"), model("b")])).toContain("a and b are installed as safetensors");
    expect(noModelReason([model("a"), model("b"), model("c"), model("d")])).toContain("a, b and 2 more are installed");
    expect(noModelReason([model("a"), model("b")])).toContain("Press Finish install on one in Installed models");
  });

  it("points to the Model Hub when no chat model is installed", () => {
    expect(noModelReason([])).toBe("No language model is installed. Install one from the Catalog tab of the Model Hub (a model that comes as safetensors is converted to GGUF as part of the install), or put a GGUF file in the models folder.");
    expect(noModelReason([model("jina-embed", "embedding")])).toBe(noModelReason([]));
  });

  it("does not point to the Serving tab, where nothing converts any more", () => {
    for (const text of [noModelReason([model("a")]), noModelReason([]), noAdapterReason([run()])]) expect(text).not.toMatch(/Serving/);
  });
});

describe("noAdapterReason", () => {
  it("says that adapters are converted by themselves, with nothing waiting, and points to where one is made", () => {
    expect(noAdapterReason()).toBe("No adapter has been converted yet. The adapter of an imported fine-tuning run is converted by itself once the model it was trained on is installed in the Model Hub. "
      + "To make one, fine-tune a model on the Train page and import its outputs there.");
    expect(noAdapterReason([run({ converted: true })])).toBe(noAdapterReason());
  });

  it("names a run that waits and the model it needs, or says its model is there and it follows at the next install or import", () => {
    expect(noAdapterReason([run()])).toContain("Waiting: pilot/run (install Qwen/Qwen2.5-Coder-7B first).");
    expect(noAdapterReason([run({ base: null })])).toContain("install the model it was trained on first");
    expect(noAdapterReason([run({ base_installed: true })])).toContain("pilot/run (its model, Qwen/Qwen2.5-Coder-7B, is installed; it is converted at the next install or import).");
  });

  it("names two runs and counts the rest", () => {
    const runs = ["a", "b", "c", "d"].map((n) => run({ run: n, base: `org/${n}` }));
    expect(noAdapterReason(runs)).toContain("Waiting: a (install org/a first); b (install org/b first) and 2 more.");
  });
});
