import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { servingStatus: vi.fn(), servingModels: vi.fn(), servingUnload: vi.fn(), trainConfig: vi.fn(), ggufFor: vi.fn(),
    localModels: vi.fn(), servingAdapters: vi.fn(), convertAdapterRuns: vi.fn(), servingDerived: vi.fn(), servingDerivedPin: vi.fn(), servingDerivedTrim: vi.fn(),
    putSettings: vi.fn(), ollamaModels: vi.fn() } };
});
import { api, type ServingFile, type ServingStatus } from "../lib/api";
import { notifyChanged } from "../lib/changes";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { Serving } from "./Serving";

const GB = 1024 ** 3;
const status = (over: Partial<ServingStatus> = {}): ServingStatus => ({
  ollama: { running: true, version: "0.35.0", error: null },
  choices: { llm: { engine: "local", model: "a-q4.gguf" } },
  slots: {}, exclusive: null, running_models: [], ...over,
});
const file = (name: string, over: Partial<ServingFile> = {}): ServingFile => ({ id: "org/model", name, path: `D:/m/${name}`, bytes: 2 * GB, source: "folder",
  info: { architecture: "qwen2", name: "Qwen", quantisation: "Q4_K_M", context_length: 32768, layers: 24, parameters: 494_000_000 }, ...over });

const show = () => render(<ToastProvider><ConfirmProvider><Serving /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.servingStatus).mockResolvedValue(status());
  vi.mocked(api.ollamaModels).mockResolvedValue({ models: [], status: "ready" });
  vi.mocked(api.servingModels).mockResolvedValue({ files: [file("a-q4.gguf")] });
  vi.mocked(api.servingUnload).mockResolvedValue({ unloaded: ["llm"], status: {} });
  vi.mocked(api.servingAdapters).mockResolvedValue({ adapters: [] });
  vi.mocked(api.convertAdapterRuns).mockResolvedValue({ adapters: [] });
  vi.mocked(api.servingDerived).mockResolvedValue({ path: "D:/m/converted", budget_gb: 0, used_bytes: 0, entries: [] });
  vi.mocked(api.localModels).mockResolvedValue({ hub_cache: "", hub_cache_exists: false, models_dir: null, models_dir_exists: false, models: [], total_bytes: 0 });
  vi.mocked(api.trainConfig).mockResolvedValue({ sections: [], counts: { settings: 0, fixed: 0, changeable: 0 }, file_sha256: null, unresolved: [] });
});
afterEach(() => vi.useRealTimers());

it("shows Ollama version and an Unload button per running model", async () => {
  vi.mocked(api.servingStatus).mockResolvedValue({
    ...status(),
    ollama: { running: true, version: "0.35.0", error: null },
    running_models: [
      { name: "aivf/chat:latest", context_length: 4096, size_vram: 2 * GB },
      { name: "aivf/embed:latest", context_length: 2048, size_vram: GB },
    ],
  } );
  show();
  expect(await screen.findByText(/Ollama 0\.35\.0/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Unload aivf/chat:latest" }));
  fireEvent.click(await screen.findByRole("button", { name: "Unload" }));
  await waitFor(() => expect(api.servingUnload).toHaveBeenCalledWith({ model: "aivf/chat:latest" }));
  expect(screen.getByRole("button", { name: "Unload aivf/embed:latest" })).toBeInTheDocument();
});

describe("Serving", () => {
  it("lists the converted models on the page", async () => {
    vi.mocked(api.servingDerived).mockResolvedValue({ path: "D:/m/converted", budget_gb: 0, used_bytes: 1, entries: [{ name: "a", path: "D:/m/converted/a", bytes: 1, files: 1, last_used: 0, used: false, pinned: false, label: "org/a, Q4_K_M", in_use: null, settings_roles: [], adapters: 0 }] });
    show();
    expect(await screen.findByRole("heading", { name: "Converted models" })).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Converted models" })).toBeInTheDocument();
  });

  it("names a converted model by what it is, with the name of its file under it, and any other by its file name", async () => {
    vi.mocked(api.servingModels).mockResolvedValue({ files: [file("Qwen-Qwen2.5-Coder-7B-Instruct-a1b2c3d4-Q4_K_M.gguf", { label: "Qwen/Qwen2.5-Coder-7B-Instruct, Q4_K_M", source: "converted" }), file("plain-q4.gguf", { label: "plain-q4.gguf", id: "plain" })] });
    show();
    const table = await screen.findByRole("table", { name: "Models on this computer" });
    expect(within(table).getByText("Qwen/Qwen2.5-Coder-7B-Instruct, Q4_K_M")).toBeInTheDocument();
    expect(within(table).getByText("Qwen-Qwen2.5-Coder-7B-Instruct-a1b2c3d4-Q4_K_M.gguf")).toBeInTheDocument();
    expect(within(table).getByText("plain-q4.gguf")).toBeInTheDocument();
    expect(within(table).getByText("plain")).toBeInTheDocument();
  });

  it("shows Ollama startup errors and the GPU reservation owner", async () => {
    vi.mocked(api.servingStatus).mockResolvedValue(status({
      ollama: { running: false, version: null, error: "Ollama is not installed." },
      exclusive: { owner: "training fixture", since: 1 },
    }));
    show();
    expect(await screen.findByText("Ollama is not installed.")).toBeInTheDocument();
    expect(screen.getByText("The graphics card is held by training fixture.")).toBeInTheDocument();
  });

  it("says there are no models when none is installed", async () => {
    vi.mocked(api.servingModels).mockResolvedValue({ files: [] });
    show();
    expect(await screen.findByText("No GGUF model is installed")).toBeInTheDocument();
  });
});

describe("what finishes elsewhere shows here without waiting for the next poll", () => {
  it("reads the status and the models again at once when a download or an install has ended", async () => {
    show();
    await screen.findByText("a-q4.gguf", { selector: "td, span, p, div" }).catch(() => undefined);
    await waitFor(() => expect(api.servingModels).toHaveBeenCalledTimes(1));
    vi.mocked(api.servingModels).mockResolvedValue({ files: [file("a-q4.gguf"), file("fresh-q5.gguf")] });
    act(() => notifyChanged("models"));
    await waitFor(() => expect(api.servingModels).toHaveBeenCalledTimes(2));
    expect(await screen.findAllByText(/fresh-q5\.gguf/)).not.toHaveLength(0);
  });
});
