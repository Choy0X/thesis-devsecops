import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { localModels: vi.fn(), modelShelf: vi.fn(), searchModels: vi.fn() } };
});
import { api, type LocalModel } from "../lib/api";
import { ModelPicker, trainable } from "./ModelPicker";

const GB = 1024 ** 3;
const local = (id: string, over: Partial<LocalModel> = {}): LocalModel => ({ training_eligible: true, id, source: "hub_cache", path: `C:/hub/${id}`, bytes: GB, revisions: ["abc"], main_revision: "abc", safetensors: true, gguf: [], weights: true, config: true, ...over });
const inv = (models: LocalModel[]) => ({ hub_cache: "C:/hub", hub_cache_exists: true, models_dir: null, models_dir_exists: false, models, total_bytes: 0 });

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.localModels).mockResolvedValue(inv([local("Qwen/Qwen2.5-Coder-0.5B"), local("org/gguf-only", { safetensors: false, gguf: ["a.gguf"] }), local("my-folder-model", { source: "folder" }),
    local("Qwen/no-config", { config: false })]));
});

describe("which models can be a base model", () => {
  it("keeps the downloads that are safetensors with a configuration and a Hub name, and lists the folder models apart", () => {
    const models = [local("a/b"), local("c/d", { safetensors: false }), local("e/f", { config: false }), local("not a repo", { source: "hub_cache" }), local("x/y", { source: "folder" }), local("z/w", { source: "converted" })];
    expect(trainable(models).map((m) => m.id)).toEqual(["a/b"]);
    expect(trainable([local("org/embed", { training_eligible: false }), local("org/unknown", { training_eligible: undefined })])).toEqual([]);
  });
});

describe("ModelPicker", () => {
  it("shows the chosen model, says whether it is downloaded, and offers the downloads and the folder models with the reason they cannot be used", async () => {
    const onChange = vi.fn();
    render(<ModelPicker value="Qwen/Qwen2.5-Coder-7B" contractDefault="Qwen/Qwen2.5-Coder-7B" onChange={onChange} />);
    expect(screen.getByTestId("chosen-model")).toHaveTextContent("Qwen/Qwen2.5-Coder-7B");
    expect(screen.getByText(/Not downloaded on this computer\./)).toBeInTheDocument();
    const list = await screen.findByRole("region", { name: "Downloaded models" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Qwen/Qwen2.5-Coder-0.5B1.0 GBUse"]);
    expect(screen.queryByRole("region", { name: "Models in your folders" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use Qwen/Qwen2.5-Coder-0.5B" }));
    expect(onChange).toHaveBeenCalledWith("Qwen/Qwen2.5-Coder-0.5B");
  });

  it("marks the chosen model, says it is downloaded, and goes back to the contract's", async () => {
    const onChange = vi.fn();
    render(<ModelPicker value="Qwen/Qwen2.5-Coder-0.5B" contractDefault="Qwen/Qwen2.5-Coder-7B" onChange={onChange} />);
    expect(await screen.findByRole("button", { name: "Use Qwen/Qwen2.5-Coder-0.5B" })).toHaveTextContent("Chosen");
    expect(screen.getByText(/Downloaded on this computer\./)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Use default model" }));
    expect(onChange).toHaveBeenCalledWith("Qwen/Qwen2.5-Coder-7B");
  });

  it("sends nothing to the Hub until it is asked, then lists the recommended models and the search results by repository id", async () => {
    vi.mocked(api.modelShelf).mockResolvedValue({ models: [{ training_eligible: true, id: "Qwen/Qwen2.5-Coder-7B", role: "fine-tuning base" }, { id: "broken", role: "x" }, { id: "org/bad", role: "y", error: "not found" }] });
    vi.mocked(api.searchModels).mockResolvedValue({ query: "coder", task: "generation", models: [{ training_eligible: true, id: "org/coder-1b", downloads: 1200, likes: 3, task: "generation", library: null, gated: true, parameters: null, updated: null },
      { id: "no-slash", downloads: 1, likes: 0, task: null, library: null, gated: false, parameters: null, updated: null }] });
    const onChange = vi.fn();
    render(<ModelPicker value="Qwen/Qwen2.5-Coder-7B" contractDefault="Qwen/Qwen2.5-Coder-7B" onChange={onChange} />);
    await screen.findByRole("region", { name: "Downloaded models" });
    fireEvent.click(screen.getByRole("tab", { name: "Hugging Face" }));
    expect(api.modelShelf).not.toHaveBeenCalled();
    expect(api.searchModels).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Show supported models" }));
    const shelf = await screen.findByRole("list", { name: "Project models" });
    expect(within(shelf).getAllByRole("listitem")).toHaveLength(1);                                      // a name that is not a repository id, and one that could not be checked, are left out
    fireEvent.change(screen.getByLabelText("Search models"), { target: { value: "coder" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    const results = await screen.findByRole("list", { name: "Search results" });
    expect(results).toHaveTextContent("org/coder-1b");
    expect(results).toHaveTextContent("gated, 1200 downloads");
    expect(results).not.toHaveTextContent("no-slash");
    expect(api.searchModels).toHaveBeenCalledWith("coder", "generation");
    fireEvent.click(within(results).getByRole("button", { name: "Use org/coder-1b" }));
    expect(onChange).toHaveBeenCalledWith("org/coder-1b");
  });

  it("says why a search or a lookup failed, and when the list of downloads cannot be read", async () => {
    vi.mocked(api.localModels).mockRejectedValue(new Error("the disk cannot be read"));
    vi.mocked(api.searchModels).mockRejectedValue(new Error("Hugging Face could not be reached"));
    vi.mocked(api.modelShelf).mockRejectedValue(new Error("offline"));
    render(<ModelPicker value="a/b" contractDefault="a/b" onChange={() => undefined} />);
    expect(await screen.findByText("the disk cannot be read")).toBeInTheDocument();
    expect(screen.getByText("No supported generative model downloaded")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Hugging Face" }));
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Hugging Face could not be reached")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show supported models" }));
    await waitFor(() => expect(screen.getAllByRole("alert").map((a) => a.textContent)).toContain("offline"));
  });
});
