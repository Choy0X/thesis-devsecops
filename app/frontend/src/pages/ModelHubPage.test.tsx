import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { choose } from "../test/select";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { localModels: vi.fn(), partialDownloads: vi.fn(), installedRemovalPlan: vi.fn(), removeInstalled: vi.fn(), modelFit: vi.fn(), device: vi.fn(), searchModels: vi.fn(), modelShelf: vi.fn(), downloadPlan: vi.fn(), installPlan: vi.fn(), finishPlan: vi.fn(), servingModels: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn(),
    ollamaModels: vi.fn(), installOllamaModel: vi.fn(), convertAdapterRuns: vi.fn() } };
});
import { api, type FitResult, type InstalledRemovalPlan, type LocalModel, type LocalModels } from "../lib/api";
import { notifyChanged } from "../lib/changes";
import { DevModeProvider } from "../lib/devMode";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { ApiError } from "../lib/apiError";
import { headline } from "../lib/fitWords";
import { ModelHubPage } from "./ModelHubPage";

const GB = 1024 ** 3;
const inv: LocalModels = {
  hub_cache: "C:/Users/x/.cache/huggingface/hub", hub_cache_exists: true, models_dir: null, models_dir_exists: false, total_bytes: 4 * GB,
  models: [
    { id: "org/small", source: "hub_cache", path: "p1", bytes: 3 * GB, revisions: ["abc"], main_revision: "abc", safetensors: true, gguf: [], weights: true, config: true },
    { id: "quant-q4", source: "folder", path: "p2", bytes: 1 * GB, revisions: [], main_revision: null, safetensors: false, gguf: ["a.gguf"], weights: false, config: false },
  ],
};
const fitted = (over: Partial<FitResult["check"]> = {}): FitResult => ({
  mode: "inference", parts: { weights: 3 * GB, kv_cache: 0.2 * GB, overhead: 0.75 * GB, total: 3.95 * GB }, total: 3.95 * GB, notes: ["Dimensions were read from config.json"],
  check: { gpu: "fits", ram: "fits", disk: "unknown", runs_on: "gpu", gpu_free_bytes: 6 * GB, ram_free_bytes: 12 * GB, ...over },
});

const show = (dev = false, path = "/") => render(<MemoryRouter initialEntries={[path]}><DevModeProvider initial={dev}><ToastProvider><ConfirmProvider><ModelHubPage /></ConfirmProvider></ToastProvider></DevModeProvider></MemoryRouter>);

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.downloadPlan).mockResolvedValue({ repo_id: "x", revision: "d".repeat(40), gated: false, quants: [], needs_quant: false, files: 4, bytes: 3 * GB, free_bytes: 100 * GB, enough: true });
  vi.mocked(api.localModels).mockResolvedValue(inv);
  vi.mocked(api.partialDownloads).mockResolvedValue({ partial: [] });
  vi.mocked(api.servingModels).mockResolvedValue({ files: [], not_ready: [] });
  vi.mocked(api.modelFit).mockResolvedValue(fitted());
  vi.mocked(api.device).mockResolvedValue({ cpu: { physical_cores: 6, logical_cores: 12, workers: 5 }, ram: { total_gb: 16, free_gb: 8 }, disks: [], gpu: { available: false, reason: "none", cards: [] } });
  vi.mocked(api.ollamaModels).mockResolvedValue({ models: [], status: "ready" });
});

describe("Ollama models in Model Hub", () => {
  let mockSocket: { onmessage?: (event: { data: string }) => void };
  beforeEach(() => {
    vi.stubGlobal("WebSocket", class {
      constructor() { mockSocket = this; }
      onmessage?: (event: { data: string }) => void;
      close() {}
    });
  });

  it("shows exact installed tags, roles and an independent cloud group", async () => {
    vi.mocked(api.ollamaModels).mockResolvedValue({ status: "ready", models: [
      { tag: "aivf/reviewer-v40:latest", name: "AIVF Reviewer v40", digest: "abc", capabilities: ["completion"], location: "local", available: true, size: GB },
      { tag: "nomic-embed-text:latest", name: "Nomic Embed Text", digest: "def", capabilities: ["embedding"], location: "local", available: true, size: GB },
      { tag: "gpt-oss:120b-cloud", name: "GPT OSS", digest: "ghi", capabilities: ["completion"], location: "cloud", available: true, size: 0 },
    ] });
    show();
    const local = await screen.findByRole("table", { name: "Ollama local models" });
    expect(within(local).getByText("AIVF Reviewer v40")).toBeInTheDocument();
    expect(within(local).getByText("aivf/reviewer-v40:latest")).toBeInTheDocument();
    expect(within(local).getByText("Embeddings")).toBeInTheDocument();
    expect(within(local).queryByText("gpt-oss:120b-cloud")).toBeNull();
    const cloud = screen.getByRole("table", { name: "Ollama Cloud models" });
    expect(within(cloud).getByText("gpt-oss:120b-cloud")).toBeInTheDocument();
    expect(screen.getByText(/requires opt-in before sending project content/)).toBeInTheDocument();
  });

  it("starts an Ollama pull from the requested tag and reports refusals", async () => {
    vi.mocked(api.installOllamaModel).mockRejectedValue(new Error("The tag is unavailable."));
    show();
    fireEvent.click(screen.getByRole("button", { name: "Add model to Ollama" }));
    const dialog = await screen.findByRole("dialog", { name: "Add model to Ollama" });
    fireEvent.change(within(dialog).getByLabelText("Ollama tag"), { target: { value: "qwen2.5-coder:7b" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Install model" }));
    await waitFor(() => expect(api.installOllamaModel).toHaveBeenCalledWith({ kind: "pull", tag: "qwen2.5-coder:7b" }));
    expect(await within(dialog).findByText(/The tag is unavailable/)).toBeInTheDocument();
  });

  it("supports the optional embedding installation without preselecting a model", async () => {
    show(false, "/models?purpose=embedding");
    fireEvent.click(screen.getByRole("button", { name: "Add embedding model" }));
    const dialog = await screen.findByRole("dialog", { name: "Add embedding model" });
    expect(within(dialog).getByLabelText("Ollama tag")).toHaveValue("");
    expect(within(dialog).getByRole("button", { name: "Install model" })).toBeDisabled();
    expect(api.installOllamaModel).not.toHaveBeenCalled();
  });

  it("installs the selected GGUF from the catalog even when only its file size can be estimated", async () => {
    const repo = "jinaai/jina-code-embeddings-0.5b-GGUF";
    const filename = "variants/jina-code-embeddings-0.5b-Q4_K_M.gguf";
    const revision = "b".repeat(40);
    vi.mocked(api.searchModels).mockResolvedValue({ query: "", task: "embedding", models: [
      { id: repo, downloads: 5, likes: 1, task: "feature-extraction", library: null, gated: false, parameters: null, updated: null },
    ] });
    vi.mocked(api.downloadPlan).mockResolvedValue({
      repo_id: repo, revision, gated: false, quants: [], needs_quant: false, files: 1, bytes: GB, free_bytes: 100 * GB, enough: true,
      gguf_files: [{ name: filename, bytes: GB }],
    });
    vi.mocked(api.modelFit).mockResolvedValue({
      ...fitted({ gpu: "unknown", ram: "unknown", runs_on: "nowhere" }), estimate_complete: false,
      parts: { weights: GB }, total: GB, notes: ["This embedding model's memory use depends on how Ollama runs it."],
    });
    vi.mocked(api.installOllamaModel).mockResolvedValue({ id: "catalog-gguf", stage: "install_ollama_model" });
    show(false, "/models?tab=catalog");
    fireEvent.click(await screen.findByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByText(repo));
    const dialog = await screen.findByRole("dialog", { name: "Does jina-code-embeddings-0.5b-GGUF fit?" });
    expect(await within(dialog).findByText("Runtime memory estimate unavailable")).toBeInTheDocument();
    expect(within(dialog).getByText("Model file size")).toBeInTheDocument();
    expect(within(dialog).queryByText("Estimated total")).toBeNull();
    expect(api.modelFit).toHaveBeenCalledWith(expect.objectContaining({ gguf_file: filename, mode: "inference" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Install in Ollama" }));
    const confirmation = await screen.findByRole("alertdialog");
    fireEvent.click(within(confirmation).getByRole("button", { name: "Install in Ollama" }));
    await waitFor(() => expect(api.installOllamaModel).toHaveBeenCalledWith({ kind: "huggingface", repo_id: repo, revision, filename }));
    expect(api.startJob).not.toHaveBeenCalled();
    await waitFor(() => expect(mockSocket?.onmessage).toBeDefined());
    act(() => mockSocket.onmessage?.({ data: JSON.stringify({ type: "progress", progress: { state: "installing", label: "Registering GGUF with Ollama" } }) }));
    expect(await within(dialog).findByText("Registering GGUF with Ollama")).toBeInTheDocument();
    act(() => {
      mockSocket.onmessage?.({ data: JSON.stringify({ type: "progress", progress: { state: "done", model_tag: "aivf/jina-code:latest" } }) });
      mockSocket.onmessage?.({ data: JSON.stringify({ type: "status", status: "done" }) });
    });
    expect(await within(dialog).findByText("aivf/jina-code:latest is available in Ollama.")).toBeInTheDocument();
  });

  it.each([
    {
      source: "Hugging Face GGUF", inputs: [["Repository", "org/model-gguf"], ["Revision", "b".repeat(40)], ["GGUF filename", "model-Q4_K_M.gguf"]],
      request: { kind: "huggingface", repo_id: "org/model-gguf", revision: "b".repeat(40), filename: "model-Q4_K_M.gguf" },
    },
    {
      source: "Local GGUF", inputs: [["GGUF file path", "C:/models/local.gguf"]],
      request: { kind: "gguf", path: "C:/models/local.gguf" },
    },
  ])("passes the selected $source to Ollama", async ({ source, inputs, request }) => {
    vi.mocked(api.installOllamaModel).mockResolvedValue({ id: "fixture", stage: "install_ollama_model" });
    show();
    fireEvent.click(screen.getByRole("button", { name: "Add model to Ollama" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("radio", { name: source }));
    for (const [label, value] of inputs) fireEvent.change(within(dialog).getByLabelText(label, { exact: true }), { target: { value } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Install model" }));
    await waitFor(() => expect(api.installOllamaModel).toHaveBeenCalledWith(request));
  });

  it("deploys a selected training run on the selected local base", async () => {
    vi.mocked(api.convertAdapterRuns).mockResolvedValue({ adapters: [
      { run: "stage-a", name: "Stage A", folder: "C:/runs/stage-a", base: "Qwen/Qwen2.5-Coder-7B-Instruct", rank: 16, bytes: 1000, base_installed: true, converted: false },
    ] });
    vi.mocked(api.servingModels).mockResolvedValue({ files: [
      { id: "qwen", name: "base.gguf", path: "C:/models/base.gguf", bytes: GB, source: "folder", info: { architecture: "qwen2", name: "Qwen", quantisation: "Q4_K_M", context_length: 8192, layers: 28, parameters: 7e9 } },
    ] });
    vi.mocked(api.installOllamaModel).mockResolvedValue({ id: "fixture", stage: "install_ollama_model" });
    const user = userEvent.setup();
    show();
    fireEvent.click(screen.getByRole("button", { name: "Add model to Ollama" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("radio", { name: "Training run" }));
    await choose(user, await within(dialog).findByRole("combobox", { name: "Training run" }), /Stage A/);
    await choose(user, within(dialog).getByRole("combobox", { name: "Base GGUF" }), /base\.gguf/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Deploy adapter" }));
    await waitFor(() => expect(api.installOllamaModel).toHaveBeenCalledWith({ kind: "adapter", run_name: "stage-a", base_path: "C:/models/base.gguf" }));
  });

  it("tracks installation progress, offers Stop, and reloads registered tags after completion", async () => {
    vi.mocked(api.installOllamaModel).mockResolvedValue({ id: "install-proof", stage: "install_ollama_model" });
    vi.mocked(api.cancelJob).mockResolvedValue({ ok: true });
    show();
    await screen.findByText("No local Ollama models");
    fireEvent.click(screen.getByRole("button", { name: "Add model to Ollama" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Ollama tag"), { target: { value: "qwen2.5-coder:7b" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Install model" }));
    const stop = await within(dialog).findByRole("button", { name: "Stop installation" });
    await waitFor(() => expect(stop).toBeEnabled());
    act(() => mockSocket.onmessage?.({ data: JSON.stringify({ type: "progress", progress: { label: "Registering the model", total: 100, processed: 90, unit: "percent" } }) }));
    expect(await within(dialog).findByText("Registering the model")).toBeInTheDocument();
    fireEvent.click(stop);
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith("install-proof"));
    vi.mocked(api.listJobs).mockResolvedValue([{ id: "install-proof", stage: "install_ollama_model", status: "done" }] as never);
    vi.mocked(api.ollamaModels).mockResolvedValue({ status: "ready", models: [
      { tag: "qwen2.5-coder:7b", name: "Qwen2.5 Coder 7B", digest: "fresh", capabilities: ["completion"], location: "local", available: true, size: GB },
    ] });
    act(() => mockSocket.onmessage?.({ data: JSON.stringify({ type: "status", status: "done" }) }));
    expect(await within(dialog).findByText("The model is registered in Ollama.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(await screen.findByText("Qwen2.5 Coder 7B")).toBeInTheDocument();
  });
});

describe("headline", () => {
  it("says fits, tight, slow on the processor, does not fit and cannot tell", () => {
    expect(headline(fitted().check, "inference").title).toBe("Fits on the graphics card");
    expect(headline(fitted({ gpu: "tight" }).check, "inference").title).toMatch(/only just/);
    expect(headline(fitted({ gpu: "too_big", runs_on: "cpu" }).check, "qlora").detail).toMatch(/Fine-tuning on the processor takes very long/);
    expect(headline(fitted({ gpu: "too_big", ram: "too_big", runs_on: "nowhere" }).check, "inference").tone).toBe("crit");
    expect(headline(fitted({ gpu: "unknown", ram: "unknown", runs_on: "nowhere" }).check, "inference").tone).toBe("unknown");
  });
});

describe("ModelHubPage", () => {
  it("lists the installed models with size, formats and where they are", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Installed models" });
    expect(within(table).getByText("org/small")).toBeInTheDocument();
    expect(within(table).getByText("3.0 GB")).toBeInTheDocument();
    expect(within(table).getByText("safetensors")).toBeInTheDocument();
    expect(within(table).getByText("GGUF (1)")).toBeInTheDocument();
    expect(screen.getByText("1-2 of 2")).toBeInTheDocument();
    expect(screen.getByText("4.0 GB in all")).toBeInTheDocument();
    expect(screen.getByText("This computer")).toBeInTheDocument();
  });

  it("says what to do when there is no models folder yet", async () => {
    vi.mocked(api.localModels).mockResolvedValue({ ...inv, models: [], hub_cache_exists: false, total_bytes: 0 });
    show();
    expect(await screen.findByText("No model found")).toBeInTheDocument();
    expect(screen.getByText(/The models folder does not exist yet/)).toBeInTheDocument();
  });

  it("estimates the memory for a model when it is opened, as an estimate, with the free memory next to it", async () => {
    show();
    fireEvent.click(await screen.findByText("org/small"));
    const dialog = await screen.findByRole("dialog", { name: "Does small fit?" });
    expect(await within(dialog).findByRole("status")).toHaveTextContent("Fits on the graphics card");
    expect(api.modelFit).toHaveBeenCalledWith({ model_id: "org/small", source: "hub_cache", mode: "inference", context: 2048, micro_batch: 1, rank: 16 });
    expect(within(dialog).getByText("Key and value cache")).toBeInTheDocument();
    expect(within(dialog).getByText("Estimated total")).toBeInTheDocument();
    expect(within(dialog).getByText("6.0 GB")).toBeInTheDocument();
    expect(within(dialog).getByText(/not a measurement/)).toBeInTheDocument();
    expect(within(dialog).getByText("Dimensions were read from config.json")).toBeInTheDocument();
  });

  it("estimates runtime without training controls", async () => {
    show();
    fireEvent.click(await screen.findByText("org/small"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Adapter rank")).toBeNull();
    expect(within(dialog).queryByRole("radio", { name: "Fine-tune it (QLoRA)" })).toBeNull();
    expect(within(dialog).queryByRole("radio", { name: "Run it" })).toBeNull();
    await waitFor(() => expect(api.modelFit).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "inference" })));
  });

  it("explains in words why an estimate could not be made", async () => {
    vi.mocked(api.modelFit).mockRejectedValue(new Error("the model's configuration does not state: n_layers"));
    show();
    fireEvent.click(await screen.findByText("quant-q4"));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not state: n_layers");
  });

  it("sends nothing to the Hub until a search is asked for, and says so", async () => {
    show(false, "/?tab=catalog");
    expect(await screen.findByText("Nothing searched yet")).toBeInTheDocument();
    expect(screen.getByText(/sends your words to huggingface\.co; nothing is downloaded/)).toBeInTheDocument();
    expect(api.searchModels).not.toHaveBeenCalled();
  });

  it("searches with the words and the task, lists the answers and opens a fit check on one", async () => {
    vi.mocked(api.searchModels).mockResolvedValue({ query: "qwen", task: "generation", models: [
      { id: "Qwen/Qwen2.5-Coder-1.5B", downloads: 2_500_000, likes: 300, task: "text-generation", library: "transformers", gated: false, parameters: 1_540_000_000, updated: null },
      { id: "org/closed", downloads: 10, likes: 1, task: "text-generation", library: null, gated: true, parameters: null, updated: null }] });
    show(false, "/?tab=catalog");
    fireEvent.change(await screen.findByLabelText("Search models"), { target: { value: " qwen " } });
    await choose(userEvent.setup(), screen.getByRole("combobox", { name: "What the model is for" }), "text generation");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    const table = await screen.findByRole("table", { name: "Models on the Hub" });
    expect(api.searchModels).toHaveBeenCalledWith("qwen", "generation");
    expect(within(table).getByText("1.5 B")).toBeInTheDocument();
    expect(within(table).getByText("3 M")).toBeInTheDocument();
    expect(within(table).getByText("Gated")).toBeInTheDocument();
    expect(screen.getByText(/2 models, the most downloaded first\./)).toBeInTheDocument();
    expect(screen.queryByText(/not built yet/)).toBeNull();                         // downloading is built: the footer must not say otherwise
    fireEvent.click(within(table).getByText("Qwen/Qwen2.5-Coder-1.5B"));
    await screen.findByRole("dialog", { name: "Does Qwen2.5-Coder-1.5B fit?" });
    expect(api.modelFit).toHaveBeenCalledWith(expect.objectContaining({ model_id: "Qwen/Qwen2.5-Coder-1.5B", source: "hub", mode: "inference" }));
  });

  it("says plainly when the Hub could not be reached and keeps the search form", async () => {
    vi.mocked(api.searchModels).mockImplementation(() => Promise.reject(new ApiError({ status: 502, title: "Hugging Face could not be reached, so the search has no answer.", hint: "Check the internet connection." })));
    show(false, "/?tab=catalog");
    fireEvent.click(await screen.findByRole("button", { name: "Search" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Hugging Face could not be reached");
    expect(screen.getByText("Check the internet connection.")).toBeInTheDocument();
    expect(screen.getByLabelText("Search models")).toBeInTheDocument();
  });

  it("looks the project's own models up only when asked, and says what each is and whether it needs terms accepted", async () => {
    vi.mocked(api.modelShelf).mockResolvedValue({ models: [
      { id: "Qwen/Qwen2.5-Coder-7B", role: "Base model for fine-tuning", parameters: 7_615_616_512, gated: false },
      { id: "org/embed", role: "Default embedding model", gated: true, parameters: 600_000_000 },
      { id: "org/gone", role: "Other", error: "OSError: 404" }] });
    show(false, "/?tab=catalog");
    const panel = (await screen.findByText("Used by this project")).closest("section") as HTMLElement;
    expect(within(panel).getByText(/Not looked up yet/)).toBeInTheDocument();
    expect(api.modelShelf).not.toHaveBeenCalled();
    fireEvent.click(within(panel).getByRole("button", { name: "Check them now" }));
    const list = await within(panel).findByRole("list", { name: "Project models" });
    expect(within(list).getByText("Base model for fine-tuning")).toBeInTheDocument();
    expect(within(list).getByText("7.6 B parameters")).toBeInTheDocument();
    expect(within(list).getByText(/Gated: accept its terms on the Hub first/)).toBeInTheDocument();
    expect(within(list).getByText("Could not be checked: OSError: 404")).toBeInTheDocument();
    fireEvent.click(within(list).getAllByRole("button", { name: "Does it fit?" })[0]);
    await screen.findByRole("dialog", { name: "Does Qwen2.5-Coder-7B fit?" });
    await waitFor(() => expect(api.modelFit).toHaveBeenCalledWith(expect.objectContaining({ model_id: "Qwen/Qwen2.5-Coder-7B", source: "hub" })));
  });

  it("shows the folders that were looked at only in Developer mode", async () => {
    const { unmount } = show(false);
    await screen.findByRole("table", { name: "Installed models" });
    expect(screen.queryByText(/cache C:/)).toBeNull();
    unmount();
    show(true);
    expect(await screen.findByText(/cache C:\/Users\/x\/\.cache\/huggingface\/hub/)).toBeInTheDocument();
  });

  const openHub = async () => {
    vi.mocked(api.searchModels).mockResolvedValue({ query: "", task: "", models: [{ id: "org/big", downloads: 5, likes: 1, task: "text-generation", library: null, gated: true, parameters: 7_000_000_000, updated: null }] });
    show(false, "/?tab=catalog");
    fireEvent.click(await screen.findByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByText("org/big"));
    return screen.findByRole("dialog", { name: "Does big fit?" });
  };

  it("shows what a download would fetch and whether the disk can hold it, and asks first with the gated and will-not-fit warnings", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue({ repo_id: "org/big", revision: "d".repeat(40), gated: true, token_set: true, quants: [], needs_quant: false, files: 4, bytes: 14 * GB, free_bytes: 120 * GB, enough: true });
    vi.mocked(api.modelFit).mockResolvedValue(fitted({ gpu: "too_big", ram: "too_big", runs_on: "nowhere" }));
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "hf_download", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "hf_download", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    const dialog = await openHub();
    expect(await within(dialog).findByText(/4 files, 14 GB\. 120 GB free on the disk that holds the cache\. That is enough\./)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Download" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("downloads 4 files, 14 GB, from huggingface.co");
    expect(ask).toHaveTextContent("The model is gated");
    expect(ask).toHaveTextContent("will NOT fit");
    expect(api.startJob).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Download" }));
    await vi.waitFor(() => expect(api.startJob).toHaveBeenCalledWith("hf_download", { repo_id: "org/big", quant: "" }));
  });

  const startDownload = async (planOver: Record<string, unknown> = {}) => {
    vi.mocked(api.downloadPlan).mockResolvedValue({ repo_id: "org/big", revision: "d".repeat(40), gated: false, quants: [], needs_quant: false, files: 4, bytes: 14 * GB, free_bytes: 120 * GB, enough: true, ...planOver } as any);
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    const running = { id: "j1", stage: "hf_download", status: "running", params: { repo_id: "org/big", quant: "" }, logs: ["Model     org/big", "Plan      4 files, 14.0 GB"], log_start: 0,
      progress: { state: "downloading", unit: "bytes", label: "Downloading org/big", processed: 2 * GB, total: 14 * GB, rate: GB / 10, eta: 120, files: 1, files_total: 4 } };
    vi.mocked(api.startJob).mockResolvedValue(running as any);
    vi.mocked(api.getJob).mockResolvedValue(running as any);
    vi.stubGlobal("WebSocket", class { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } });
    const dialog = await openHub();
    await within(dialog).findByText("Estimated total");
    fireEvent.click(within(dialog).getByRole("button", { name: "Download" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Download" }));
    await within(dialog).findByLabelText("Download of org/big");
    return dialog;
  };

  it("shows the whole estimate before a download, and folds it into one line once one starts, with a way to bring it back", async () => {
    const dialog = await startDownload();
    expect(within(dialog).queryByText("Estimated total")).toBeNull();
    expect(within(dialog).queryByRole("radiogroup", { name: "What to do with the model" })).toBeNull();
    expect(within(dialog).getByText(/Fits on the graphics card: 4\.0 GB estimated, 6\.0 GB of video memory free\./)).toBeInTheDocument();
    const toggle = within(dialog).getByRole("button", { name: "Show estimate" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(await within(dialog).findByText("Estimated total")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Hide estimate" })).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(within(dialog).getByRole("button", { name: "Hide estimate" }));
    expect(within(dialog).queryByText("Estimated total")).toBeNull();
  });

  it("puts the panel of the download and its terminal in the dialog, and keeps the file count and speed in view", async () => {
    const dialog = await startDownload();
    expect(await within(dialog).findByText("1 of 4 files", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(within(dialog).getByText("Downloading")).toBeInTheDocument();
    expect(within(dialog).getByRole("log", { name: "Output of Download a model" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("does not let the quantisation change under a download that has begun", async () => {
    vi.mocked(api.downloadPlan).mockImplementation(async (_id: string, quant = "") => gguf(quant, { recommended_quant: gguf("").recommended_quant, quant_bytes: gguf("").quant_bytes }));
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    const running = { id: "j2", stage: "hf_download", status: "running", params: { repo_id: "org/big", quant: "Q4_K_M" }, logs: [], log_start: 0, progress: null };
    vi.mocked(api.startJob).mockResolvedValue(running as any);
    vi.mocked(api.getJob).mockResolvedValue(running as any);
    vi.stubGlobal("WebSocket", class { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } });
    const dialog = await openHub();
    const select = await within(dialog).findByRole("combobox", { name: "Quantisation" });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled());
    expect(select).toBeEnabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Download" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Download" }));
    await within(dialog).findByLabelText("Download of org/big");
    expect(within(dialog).getByRole("combobox", { name: "Quantisation" })).toBeDisabled();
  });

  // -- installing a model so that it is ready to run -------------------------------------------------------------------------------------------------------

  const installPlan = (over: Record<string, unknown> = {}) => ({
    kind: "convert", repo_id: "org/big", revision: "d".repeat(40), role: "chat", role_detected: true, outtype: "f16", quant: "Q4_K_M", format: "Q4_K_M", format_reason: "Q4_K_M: the usual balance.", format_fit: "split",
    parameters: 7_000_000_000, parameters_estimated: false, architecture: "Qwen2ForCausalLM", supported: null,
    download: { files: 4, bytes: 14 * GB, partial_bytes: 0, free_bytes: 120 * GB, enough: true, gated: false, token_set: false },
    convert: { intermediate_bytes: 13 * GB, final_bytes: 4.2 * GB, final_bytes_max: 4.2 * GB, peak_bytes: 17 * GB, fallback: false, seconds: 780, seconds_note: "An estimate: built on one timing." },
    scripts: { tag: "b11528", installed: false, installed_tag: null, will_fetch: true, blockers: [], note: "about 115 small files, under 2 MB" }, disk_needed_bytes: 31 * GB, problems: [], ready: true, notes: [], ...over }) as any;
  const safetensorsPlan = (over: Record<string, unknown> = {}) => ({ repo_id: "org/big", revision: "d".repeat(40), gated: false, token_set: true, quants: [], needs_quant: false, files: 4, bytes: 14 * GB, free_bytes: 120 * GB, enough: true,
    partial_bytes: 0, patterns: ["*.safetensors", "*.json"], ...over }) as any;
  const running = { id: "j9", stage: "install_model", status: "running", params: { repo_id: "org/big" }, logs: [], log_start: 0, progress: null } as any;
  const quietSocket = () => vi.stubGlobal("WebSocket", class { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } });

  it("offers to install a model that comes as safetensors ready to run, with what it will do and cost, and that is what the button does", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(safetensorsPlan());
    vi.mocked(api.installPlan).mockResolvedValue(installPlan());
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    vi.mocked(api.startJob).mockResolvedValue(running);
    vi.mocked(api.getJob).mockResolvedValue(running);
    quietSocket();
    const dialog = await openHub();
    expect(await within(dialog).findByRole("radio", { name: /Install it ready to run/ })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: /Only download the files/ })).not.toBeChecked();
    expect(api.installPlan).toHaveBeenCalledWith("org/big", "");
    expect(within(dialog).getByText("Will be set up as a language model.")).toBeInTheDocument();          // told, not asked
    expect(within(dialog).queryByRole("radio", { name: "Language model" })).toBeNull();
    expect(within(dialog).getByText("Q4_K_M: the usual balance.")).toBeInTheDocument();
    expect(within(dialog).getByText(/about 13 minutes on the processor/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Download" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Install" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("This downloads 4 files, 14 GB, from huggingface.co; fetches llama.cpp b11528's converter scripts from github.com");
    expect(ask).toHaveTextContent("converts the model on this computer's processor to Q4_K_M GGUF");
    expect(ask).toHaveTextContent("The downloaded files are kept.");
    expect(api.startJob).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Install" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("install_model", { repo_id: "org/big", quant: "Q4_K_M", role: "chat" }));
    expect(await within(dialog).findByLabelText("Install of org/big")).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: /Only download the files/ })).toBeDisabled());       // what to do with it is settled once it runs
    expect(within(dialog).queryByRole("button", { name: "Change what it is used as" })).toBeNull();       // what it is set up as is settled too
  });

  it("downloads only the files when that is chosen, with the plain button and the plain job", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(safetensorsPlan());
    vi.mocked(api.installPlan).mockResolvedValue(installPlan());
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    vi.mocked(api.startJob).mockResolvedValue({ ...running, stage: "hf_download" });
    vi.mocked(api.getJob).mockResolvedValue({ ...running, stage: "hf_download" });
    quietSocket();
    const dialog = await openHub();
    fireEvent.click(await within(dialog).findByRole("radio", { name: /Only download the files/ }));
    expect(within(dialog).queryByRole("button", { name: "Install" })).toBeNull();
    expect(within(dialog).queryByText("Q4_K_M: the usual balance.")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Download" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Download" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("hf_download", { repo_id: "org/big", quant: "" }));
  });

  it("plans again for the role the person chooses and installs it as that", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(safetensorsPlan());
    vi.mocked(api.installPlan).mockImplementation(async (_id: string, role = "") => (role === "embedding"
      ? installPlan({ role: "embedding", role_detected: false, outtype: "q8_0", quant: null, format: "Q8_0", format_reason: "An embedding model keeps 8 bits." }) : installPlan()));
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    vi.mocked(api.startJob).mockResolvedValue(running);
    vi.mocked(api.getJob).mockResolvedValue(running);
    quietSocket();
    const dialog = await openHub();
    fireEvent.click(await within(dialog).findByRole("button", { name: "Change what it is used as" }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "Embedding model" }));
    await waitFor(() => expect(api.installPlan).toHaveBeenLastCalledWith("org/big", "embedding"));
    expect(await within(dialog).findByText("An embedding model keeps 8 bits.")).toBeInTheDocument();
    expect(within(dialog).queryByText(/Found from the name of the model/)).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Install" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Install" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("install_model", { repo_id: "org/big", quant: "Q8_0", role: "embedding" }));
  });

  it("cannot install while the plan finds something wrong, says what, and still offers to download the files", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(safetensorsPlan());
    vi.mocked(api.installPlan).mockResolvedValue(installPlan({ ready: false, problems: ["The llama.cpp program is not installed, and the converter has to match its release."] }));
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    const dialog = await openHub();
    expect(await within(dialog).findByText(/The llama\.cpp program is not installed, and the converter has to match its release\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Install" })).toBeDisabled();
    expect(within(dialog).getByText("This cannot be installed yet: see above.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("radio", { name: /Only download the files/ }));
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled();
  });

  it("says that the install could not be planned and offers the plain download", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(safetensorsPlan());
    vi.mocked(api.installPlan).mockRejectedValue(new ApiError({ status: 502, title: "Hugging Face could not be reached, so the install cannot be planned." }));
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    const dialog = await openHub();
    expect(await within(dialog).findByText(/Installing it ready to run could not be planned \(Hugging Face could not be reached, so the install cannot be planned\.\); the files can still be downloaded\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled();
    expect(within(dialog).queryByRole("radio", { name: /Install it ready to run/ })).toBeNull();
  });

  it("does not offer an install for a repository of GGUF files, which are ready as they are", async () => {
    vi.mocked(api.downloadPlan).mockImplementation(async (_id: string, quant = "") => gguf(quant, { patterns: ["m-Q4_K_M.gguf"], recommended_quant: gguf("").recommended_quant, quant_bytes: gguf("").quant_bytes }));
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    const dialog = await openHub();
    await within(dialog).findByRole("combobox", { name: "Quantisation" });
    expect(api.installPlan).not.toHaveBeenCalled();
    expect(within(dialog).queryByRole("radio", { name: /Install it ready to run/ })).toBeNull();
  });

  it("says the same in the question when the model is gated or will not fit", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(safetensorsPlan({ gated: true }));
    vi.mocked(api.installPlan).mockResolvedValue(installPlan());
    vi.mocked(api.modelFit).mockResolvedValue(fitted({ gpu: "too_big", ram: "too_big", runs_on: "nowhere" }));
    const dialog = await openHub();
    fireEvent.click(await within(dialog).findByRole("button", { name: "Install" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("The model is gated: a Hugging Face token must be set in Settings");
    expect(ask).toHaveTextContent("it can still be installed, but not run here");
  });

  describe("the Installed list says which models are ready to run", () => {
    const withNotReady = (not_ready: unknown[] | null) => (not_ready === null ? vi.mocked(api.servingModels).mockRejectedValue(new Error("down")) : vi.mocked(api.servingModels).mockResolvedValue({ files: [], not_ready } as any));
    const notReadyRow = { id: "org/small", name: "small", source: "hub_cache", bytes: 3 * GB, role: "chat" };

    it("marks a model that llama.cpp cannot run as it is, with a button to finish its install, and the others as ready", async () => {
      withNotReady([notReadyRow]);
      show();
      const table = await screen.findByRole("table", { name: "Installed models" });
      const small = within(table).getByText("org/small").closest('[role="row"]') as HTMLElement;
      expect(await within(small).findByText("Not ready")).toBeInTheDocument();
      expect(within(small).getByRole("button", { name: "Finish the install of org/small" })).toBeInTheDocument();
      const gguf = within(table).getByText("quant-q4").closest('[role="row"]') as HTMLElement;
      expect(within(gguf).getByText("Ready")).toBeInTheDocument();
      expect(within(gguf).queryByRole("button", { name: /Finish the install/ })).toBeNull();
    });

    it("says nothing about being ready before it knows, or when it cannot find out", async () => {
      withNotReady(null);
      show();
      const table = await screen.findByRole("table", { name: "Installed models" });
      await waitFor(() => expect(api.servingModels).toHaveBeenCalled());
      expect(within(table).queryByText("Ready")).toBeNull();
      expect(within(table).queryByText("Not ready")).toBeNull();
    });

    it("opens the plan of the install from the button, and not the fit check of the row", async () => {
      withNotReady([notReadyRow]);
      vi.mocked(api.finishPlan).mockResolvedValue(installPlan({ kind: "finish", repo_id: "org/small", download: null }));
      show();
      fireEvent.click(await screen.findByRole("button", { name: "Finish the install of org/small" }));
      const dialog = await screen.findByRole("dialog", { name: "Finish installing small" });
      expect(api.finishPlan).toHaveBeenCalledWith("org/small", "hub_cache", "");
      expect(await within(dialog).findByText("Q4_K_M: the usual balance.")).toBeInTheDocument();
      expect(api.modelFit).not.toHaveBeenCalled();
      expect(screen.queryByRole("dialog", { name: "Does small fit?" })).toBeNull();
    });

    it("reads the list again when an install ended elsewhere, so a model that was finished is ready at once", async () => {
      withNotReady([notReadyRow]);
      show();
      const table = await screen.findByRole("table", { name: "Installed models" });
      await within(table).findByText("Not ready");
      withNotReady([]);
      act(() => notifyChanged("models"));
      await waitFor(() => expect(within(table).queryByText("Not ready")).toBeNull());
      expect(within(table).getAllByText("Ready")).toHaveLength(2);
    });
  });

  it("does not show a download of another model in this model's dialog", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([{ id: "o1", stage: "hf_download", status: "running", params: { repo_id: "org/other", quant: "" }, started: new Date().toISOString(), finished: "", error: "", log_count: 0, progress: null } as any]);
    vi.mocked(api.downloadPlan).mockResolvedValue({ repo_id: "org/big", revision: "d".repeat(40), gated: false, quants: [], needs_quant: false, files: 4, bytes: 14 * GB, free_bytes: 120 * GB, enough: true });
    vi.mocked(api.modelFit).mockResolvedValue(fitted());
    const dialog = await openHub();
    await within(dialog).findByText("Estimated total");
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    expect(within(dialog).queryByLabelText(/Download of/)).toBeNull();
    expect(within(dialog).getByText("Estimated total")).toBeInTheDocument();
    expect(api.getJob).not.toHaveBeenCalled();
  });

  it("writes the size of a small model in megabytes, in the plan, in the question and in the estimate, not as 0.0 GB", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue({ repo_id: "org/big", revision: "d".repeat(40), gated: false, token_set: false, quants: [], needs_quant: false, files: 7, bytes: 6_476_069, free_bytes: 292 * GB, enough: true, partial_bytes: 2 * 1024 ** 2 } as any);
    vi.mocked(api.modelFit).mockResolvedValue({ ...fitted(), parts: { weights: 6_476_069, kv_cache: 400_000, overhead: 0.75 * GB, total: 0.76 * GB }, total: 0.76 * GB });
    const dialog = await openHub();
    expect(await within(dialog).findByText(/7 files, 6\.2 MB\. 292 GB free on the disk that holds the cache\./)).toBeInTheDocument();
    expect(within(dialog).getByText(/2\.0 MB of it was fetched before the download stopped/)).toBeInTheDocument();
    const weights = within(dialog).getByRole("row", { name: /Model weights/ });
    expect(weights).toHaveTextContent("6.2 MB");
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue the download" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("downloads 7 files, 6.2 MB, from huggingface.co");
    expect(ask).toHaveTextContent("2.0 MB of it is already there");
    expect(ask).not.toHaveTextContent("0.0 GB");
  });

  it("does not offer a download the disk cannot hold, and says why", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue({ repo_id: "org/big", revision: "d".repeat(40), gated: false, quants: [], needs_quant: false, files: 4, bytes: 14 * GB, free_bytes: 10 * GB, enough: false });
    const dialog = await openHub();
    expect(await within(dialog).findByText(/That is not enough: the disk needs a tenth more/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("asks for a quantisation first when the model comes only as GGUF files, then plans that one", async () => {
    vi.mocked(api.downloadPlan).mockImplementation(async (_id: string, quant = "") => quant
      ? { repo_id: "org/big", revision: "d".repeat(40), gated: false, quants: ["Q4_K_M", "Q8_0"], needs_quant: false, files: 1, bytes: 4 * GB, free_bytes: 100 * GB, enough: true }
      : { repo_id: "org/big", revision: "d".repeat(40), gated: false, quants: ["Q4_K_M", "Q8_0"], needs_quant: true, files: 0, bytes: 0, free_bytes: 100 * GB, enough: false });
    const dialog = await openHub();
    const select = await within(dialog).findByRole("combobox", { name: "Quantisation" });
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeDisabled();
    expect(within(dialog).getByText("Choose a quantisation first.")).toBeInTheDocument();
    await choose(userEvent.setup(), select, "Q4_K_M");
    expect(await within(dialog).findByText(/1 file, 4\.0 GB/)).toBeInTheDocument();
    expect(api.downloadPlan).toHaveBeenLastCalledWith("org/big", "Q4_K_M");
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled();
  });

  const gguf = (quant: string, over: Record<string, unknown> = {}) => (quant
    ? { repo_id: "org/big", revision: "d".repeat(40), gated: false, quants: ["Q4_K_M", "Q8_0"], needs_quant: false, files: 1, bytes: 4 * GB, free_bytes: 100 * GB, enough: true, ...over }
    : { repo_id: "org/big", revision: "d".repeat(40), gated: false, quants: ["Q4_K_M", "Q8_0"], quant_bytes: { Q4_K_M: 4.4 * GB, Q8_0: 7.7 * GB }, needs_quant: true, files: 0, bytes: 0, free_bytes: 100 * GB, enough: false,
      recommended_quant: { quant: "Q4_K_M", reason: "Q4_K_M: 4.4 GB, the usual balance of size and quality. It does not fit the card's 4.8 GB free as a whole, so it runs split between the card and the processor, more slowly.", fit: "split" }, ...over }) as any;

  it("starts on the recommended quantisation, says why, and plans that one", async () => {
    vi.mocked(api.downloadPlan).mockImplementation(async (_id: string, quant = "") => gguf(quant, quant ? { recommended_quant: gguf("").recommended_quant, quant_bytes: gguf("").quant_bytes } : {}));
    const dialog = await openHub();
    const select = await within(dialog).findByRole("combobox", { name: "Quantisation" });
    await waitFor(() => expect(select).toHaveTextContent("Q4_K_M (4.4 GB, recommended)"));
    expect(api.downloadPlan).toHaveBeenLastCalledWith("org/big", "Q4_K_M");
    expect(within(dialog).getByText(/Q4_K_M: 4\.4 GB, the usual balance/)).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Download" })).toBeEnabled());
    await userEvent.setup().click(select);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["choose one", "Q4_K_M (4.4 GB, recommended)", "Q8_0 (7.7 GB)"]);
  });

  it("lets another quantisation replace it, points back to the recommendation, and does not choose again after 'choose one'", async () => {
    vi.mocked(api.downloadPlan).mockImplementation(async (_id: string, quant = "") => gguf(quant, { recommended_quant: gguf("").recommended_quant, quant_bytes: gguf("").quant_bytes }));
    const dialog = await openHub();
    const user = userEvent.setup();
    const select = await within(dialog).findByRole("combobox", { name: "Quantisation" });
    await waitFor(() => expect(select).toHaveTextContent("Q4_K_M (4.4 GB, recommended)"));
    await choose(user, select, "Q8_0 (7.7 GB)");
    await waitFor(() => expect(api.downloadPlan).toHaveBeenLastCalledWith("org/big", "Q8_0"));
    expect(await within(dialog).findByText(/Recommended quantisation: Q4_K_M\./)).toBeInTheDocument();
    await choose(user, within(dialog).getByRole("combobox", { name: "Quantisation" }), "choose one");
    await waitFor(() => expect(api.downloadPlan).toHaveBeenLastCalledWith("org/big", ""));
    await waitFor(() => expect(within(dialog).getByRole("combobox", { name: "Quantisation" })).toHaveTextContent("choose one"));
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("has no recommendation to start from when the backend gives none", async () => {
    vi.mocked(api.downloadPlan).mockImplementation(async (_id: string, quant = "") => gguf(quant, { recommended_quant: null }));
    const dialog = await openHub();
    const select = await within(dialog).findByRole("combobox", { name: "Quantisation" });
    expect(select).toHaveTextContent("choose one");
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeDisabled();
  });
});

describe("Deleting an installed model", () => {
  const plan = (over: Partial<InstalledRemovalPlan> = {}): InstalledRemovalPlan => ({
    source: "hub_cache", id: "org/small", path: "p1", kind: "cache", removable: true, refusal: null, bytes: 3 * GB, files: 12, revisions: 1, kept_shared_files: 0,
    in_use: null, settings_use: [], ollama_running: false, ollama_processes: [], ...over });
  const model = (id: string, source: LocalModel["source"], path: string, bytes = GB): LocalModel => ({ id, source, path, bytes, revisions: [], main_revision: null, safetensors: false, gguf: ["a.gguf"], weights: false, config: false });
  const listOf = (...models: LocalModel[]): LocalModels => ({ ...inv, models, total_bytes: models.reduce((n, m) => n + m.bytes, 0) });

  beforeEach(() => {
    vi.mocked(api.installedRemovalPlan).mockResolvedValue(plan());
    vi.mocked(api.removeInstalled).mockResolvedValue({ source: "hub_cache", id: "org/small", freed_bytes: 3 * GB, files: 12, stopped: [], left_behind: [] });
  });
  const press = async (name: string) => { fireEvent.click(await screen.findByRole("button", { name })); return screen.findByRole("alertdialog"); };

  it("has a delete button on every row, and pressing it asks first instead of opening the fit check", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Installed models" });
    expect(within(table).getByRole("columnheader", { name: "Delete" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Delete quant-q4" })).toBeInTheDocument();
    const ask = await press("Delete org/small");
    expect(ask).toHaveTextContent("Delete org/small?");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.modelFit).not.toHaveBeenCalled();
  });

  it("is reached and pressed from the keyboard without opening the fit check", async () => {
    show();
    const button = await screen.findByRole("button", { name: "Delete org/small" });
    button.focus();
    await userEvent.setup().keyboard("{Enter}");
    await screen.findByRole("alertdialog");
    expect(api.modelFit).not.toHaveBeenCalled();
  });

  it("says how much goes and what it means, deletes only after the confirmation, says what was freed and lists again", async () => {
    show();
    const ask = await press("Delete org/small");
    expect(api.installedRemovalPlan).toHaveBeenCalledWith(expect.objectContaining({ source: "hub_cache", id: "org/small", path: "p1" }));
    expect(ask).toHaveTextContent("Deletes 3.0 GB (12 files, 1 version) from the Hugging Face cache on this computer. It can be downloaded again.");
    expect(api.removeInstalled).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Delete 3.0 GB" }));
    await waitFor(() => expect(api.removeInstalled).toHaveBeenCalledWith(expect.objectContaining({ id: "org/small", path: "p1" }), false));
    expect(await screen.findByText("org/small deleted; 3.0 GB freed.")).toBeInTheDocument();
    await waitFor(() => expect(api.localModels).toHaveBeenCalledTimes(2));
  });

  it("deletes nothing when the question is cancelled", async () => {
    show();
    const ask = await press("Delete org/small");
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.removeInstalled).not.toHaveBeenCalled();
    expect(api.localModels).toHaveBeenCalledTimes(1);
  });

  it("names the folder and says that it goes with everything in it, and says a converted model can be made again", async () => {
    vi.mocked(api.localModels).mockResolvedValue(listOf(model("quant-q4", "folder", "D:/models/quant-q4"), model("conv", "converted", "D:/models/converted/conv")));
    vi.mocked(api.installedRemovalPlan).mockImplementation(async (m) => plan({ source: m.source, id: m.id, path: m.path, kind: "folder", bytes: GB, files: 3 }));
    show();
    let ask = await press("Delete quant-q4");
    expect(ask).toHaveTextContent("Deletes the folder below and everything in it (3 files, 1.0 GB). The app cannot get it back");
    expect(ask).toHaveTextContent("D:/models/quant-q4");
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    ask = await press("Delete conv");
    expect(ask).toHaveTextContent("A converted model can be made again by converting its original model again.");
    expect(ask).not.toHaveTextContent("cannot get it back");
  });

  it("says which roles Settings gives the model", async () => {
    vi.mocked(api.installedRemovalPlan).mockResolvedValue(plan({ settings_use: ["the language model", "the embedding model"] }));
    show();
    const ask = await press("Delete org/small");
    expect(ask).toHaveTextContent("Settings uses it as the language model and the embedding model. After the deletion that choice points to a file that is not there");
  });

  it("shows the reason, where the list is, for a model that is in use, and asks nothing", async () => {
    vi.mocked(api.installedRemovalPlan).mockResolvedValue(plan({ in_use: "The model gateway has this model loaded. Unload it on the Serving tab first." }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Delete org/small" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("org/small is in use. The model gateway has this model loaded.");
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.removeInstalled).not.toHaveBeenCalled();
    fireEvent.click(within(alert).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the reason for a model the app does not delete (loose files in the models folder)", async () => {
    vi.mocked(api.installedRemovalPlan).mockResolvedValue(plan({ removable: false, refusal: "This row stands for loose model files that lie directly in the folder, not for a folder of its own." }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Delete quant-q4" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("quant-q4 cannot be deleted here. This row stands for loose model files");
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("says in a sentence when the delete itself is refused, and leaves the list as it was", async () => {
    vi.mocked(api.removeInstalled).mockRejectedValue(new ApiError({ status: 409, title: "A file of this model (model.safetensors) is open in another program, so it was not deleted." }));
    show();
    const ask = await press("Delete org/small");
    fireEvent.click(within(ask).getByRole("button", { name: "Delete 3.0 GB" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("org/small was not deleted. A file of this model (model.safetensors) is open in another program");
    expect(api.localModels).toHaveBeenCalledTimes(1);
  });

  it("removes an Ollama model the way Ollama does, and says which of its files stay", async () => {
    vi.mocked(api.localModels).mockResolvedValue(listOf(model("qwen:7b", "ollama", "C:/o/blobs/sha256-a")));
    vi.mocked(api.installedRemovalPlan).mockResolvedValue(plan({ source: "ollama", id: "qwen:7b", path: "C:/o/blobs/sha256-a", kind: "ollama", bytes: GB, files: 3, kept_shared_files: 1 }));
    vi.mocked(api.removeInstalled).mockResolvedValue({ source: "ollama", id: "qwen:7b", freed_bytes: GB, files: 3, stopped: [], left_behind: [] });
    show();
    const ask = await press("Delete qwen:7b");
    expect(ask).toHaveTextContent("Removes this model from Ollama the way Ollama itself does: its manifest and the files no other Ollama model uses (3 files, 1.0 GB). 1 file that another model uses stay.");
    expect(ask).not.toHaveTextContent("Ollama is running");
    fireEvent.click(within(ask).getByRole("button", { name: "Delete 1.0 GB" }));
    await waitFor(() => expect(api.removeInstalled).toHaveBeenCalledWith(expect.objectContaining({ id: "qwen:7b" }), false));
  });

  describe("while Ollama is running", () => {
    const running = () => {
      vi.mocked(api.localModels).mockResolvedValue(listOf(model("qwen:7b", "ollama", "C:/o/blobs/sha256-a")));
      vi.mocked(api.installedRemovalPlan).mockResolvedValue(plan({ source: "ollama", id: "qwen:7b", path: "C:/o/blobs/sha256-a", kind: "ollama", bytes: GB, files: 3,
        ollama_running: true, ollama_processes: ["ollama app.exe", "ollama.exe"] }));
      vi.mocked(api.removeInstalled).mockResolvedValue({ source: "ollama", id: "qwen:7b", freed_bytes: GB, files: 3, stopped: ["ollama app.exe", "ollama.exe"], left_behind: [] });
      show();
    };

    it("asks to cancel or to stop Ollama and delete, and stops nothing on Cancel", async () => {
      running();
      const ask = await press("Delete qwen:7b");
      expect(ask).toHaveTextContent("Ollama is running");
      expect(ask).toHaveTextContent("Ollama is running (ollama app.exe and ollama.exe). It keeps the files of its models open");
      expect(ask).toHaveTextContent("The app does not start Ollama again afterwards.");
      expect(within(ask).getByRole("button", { name: "Stop Ollama and delete" })).toBeInTheDocument();
      fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      expect(api.removeInstalled).not.toHaveBeenCalled();
    });

    it("stops Ollama and deletes when told to, and says that Ollama was stopped", async () => {
      running();
      const ask = await press("Delete qwen:7b");
      fireEvent.click(within(ask).getByRole("button", { name: "Stop Ollama and delete" }));
      await waitFor(() => expect(api.removeInstalled).toHaveBeenCalledWith(expect.objectContaining({ id: "qwen:7b", source: "ollama" }), true));
      expect(await screen.findByText(/qwen:7b deleted; 1\.0 GB freed\. Ollama was stopped; start it again when you need it\./)).toBeInTheDocument();
    });
  });

  it("reports small files that stayed because another program holds them", async () => {
    vi.mocked(api.removeInstalled).mockResolvedValue({ source: "hub_cache", id: "org/small", freed_bytes: 3 * GB, files: 12, stopped: [], left_behind: ["sha256-e", "sha256-f"] });
    show();
    const ask = await press("Delete org/small");
    fireEvent.click(within(ask).getByRole("button", { name: "Delete 3.0 GB" }));
    expect(await screen.findByText(/2 files of org\/small could not be deleted because another program holds them/)).toBeInTheDocument();
  });
});

describe("what finishes elsewhere shows in the Installed list", () => {
  it("reads the list again at once when a download has ended anywhere in the app", async () => {
    show();
    await screen.findByText("org/small");
    expect(api.localModels).toHaveBeenCalledTimes(1);
    vi.mocked(api.localModels).mockResolvedValue({ ...inv, models: [...inv.models, { id: "org/new", source: "hub_cache", path: "p9", bytes: GB, revisions: [], main_revision: null, safetensors: true, gguf: [], weights: true, config: true }] });
    act(() => notifyChanged("models"));
    expect(await screen.findByText("org/new")).toBeInTheDocument();
    expect(api.localModels).toHaveBeenCalledTimes(2);
  });
});
