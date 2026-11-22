import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { servingModels: vi.fn(), servingAdapters: vi.fn(), convertAdapterRuns: vi.fn(), servingStatus: vi.fn(), putSettings: vi.fn(), testConnection: vi.fn(), discoverModels: vi.fn() } };
});
import { api, type ServingFile, type Settings } from "../lib/api";
import { choose } from "../test/select";
import { ActivePage } from "./ActivePage";
import { clearRequestCache } from "../lib/request";

import { notifyChanged } from "../lib/changes";
import { ConfirmProvider } from "./hud/AlertDialog";
import { ToastProvider } from "./Toast";
import { LlmEnginePanel, llmEngineOf } from "./LlmEnginePanel";

const file = (name: string, over: Partial<ServingFile> = {}): ServingFile => ({ id: "org/model", name, path: `D:/models/${name}`, bytes: 400_000_000, source: "folder",
  info: { architecture: "qwen2", name: "x", quantisation: "Q4_K_M", context_length: 32768, layers: 24, parameters: 494_000_000 }, ...over });
const gateway = (over: Record<string, unknown> = {}) => ({ ollama: { running: true, version: "0.35.0", error: null }, running_models: [],
  choices: { llm: { engine: "local", model: "" }, embed: { engine: "local", model: "" } }, slots: {}, exclusive: null, ...over }) as any;
const base = { llm_engine: "local", llm_base_url: "", llm_api_key: "", llm_api_key_set: false, llm_api_key_hint: "", llm_model: "", llm_local_model: "", llm_local_adapter: "", llm_local_context: "" } as unknown as Settings;
const onUpdated = vi.fn();
const show = (s: Settings = base) => render(<ToastProvider><ConfirmProvider><LlmEnginePanel s={s} onUpdated={onUpdated} /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  onUpdated.mockReset();
  vi.mocked(api.servingModels).mockResolvedValue({ files: [file("qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"), file("jina-code-embeddings-0.5b-Q8_0.gguf"), file("broken.gguf", { info: null, error: "not a GGUF" })] });
  vi.mocked(api.servingAdapters).mockResolvedValue({ adapters: [{ name: "run-1.gguf", path: "D:/models/converted/run-1.gguf", bytes: 1000, sha256: "ab", base: "qwen2.5-coder-0.5b", revision: null, rank: 8, trained_from: null, created_at: null }] });
  vi.mocked(api.convertAdapterRuns).mockResolvedValue({ adapters: [] });
  vi.mocked(api.servingStatus).mockResolvedValue(gateway());
  vi.mocked(api.putSettings).mockResolvedValue(base);
});

describe("which engine is in effect", () => {
  it("is the one chosen, else external when an endpoint is saved and local when nothing is", () => {
    expect(llmEngineOf({ llm_engine: "external" } as unknown as Settings)).toBe("external");
    expect(llmEngineOf({ llm_engine: "local", llm_base_url: "http://x" } as unknown as Settings)).toBe("local");
    expect(llmEngineOf({ llm_engine: "", llm_base_url: "http://x" } as unknown as Settings)).toBe("external");
    expect(llmEngineOf({ llm_engine: "", llm_base_url: "" } as unknown as Settings)).toBe("local");
  });
});

describe("LlmEnginePanel, local", () => {
  it("offers the chat models of the models folder, not the embedding model and not a file that cannot be read, and saves the choice", async () => {
    const user = userEvent.setup();
    show();
    const trigger = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(api.servingModels).toHaveBeenCalled());
    await user.click(trigger);
    expect(await screen.findByRole("option", { name: /qwen2\.5-coder-0\.5b-instruct/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /jina-code-embeddings/ })).toBeNull();
    expect(screen.queryByRole("option", { name: /broken/ })).toBeNull();
    await user.click(screen.getByRole("option", { name: /qwen2\.5-coder-0\.5b-instruct/ }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ llm_local_model: "D:/models/qwen2.5-coder-0.5b-instruct-q4_k_m.gguf" }));
  });

  it("disables the model field and says where to get a model when none is installed, instead of opening an empty list", async () => {
    vi.mocked(api.servingModels).mockResolvedValue({ files: [] });
    show();
    const model = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(model).toBeDisabled());
    expect(screen.getByText(/No language model is installed\. Install one from the Catalog tab of the Model Hub \(a model that comes as safetensors is converted to GGUF as part of the install\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Catalog on the Serving page/)).toBeNull();
    expect(screen.getByRole("button", { name: "Test the model" })).toBeDisabled();
  });

  it("names the downloaded chat models that Ollama cannot run, and not an embedding model, as the reason for the empty field", async () => {
    vi.mocked(api.servingModels).mockResolvedValue({ files: [], not_ready: [
      { id: "Qwen/Qwen2.5-Coder-7B-Instruct", name: "Qwen2.5-Coder-7B-Instruct", source: "hub_cache", bytes: 15e9, role: "chat" },
      { id: "jinaai/jina-code-embeddings-0.5b", name: "jina-code-embeddings-0.5b", source: "hub_cache", bytes: 1e9, role: "embedding" }] });
    show();
    expect(await screen.findByText(/Qwen2\.5-Coder-7B-Instruct is installed as safetensors, which Ollama cannot run as it is/)).toBeInTheDocument();
    expect(screen.getByText(/Press Finish install on it in Installed models of the Model Hub: it is converted here and listed in this field when that is done/)).toBeInTheDocument();
    expect(screen.queryByText(/jina-code-embeddings-0\.5b is installed/)).toBeNull();
    expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  });

  it("does not claim that nothing is installed before the list has been read", async () => {
    vi.mocked(api.servingModels).mockReturnValue(new Promise(() => undefined));                // never answers
    show();
    const model = await screen.findByRole("combobox", { name: "Model" });
    expect(model).toBeEnabled();
    expect(model).toHaveTextContent("Reading the models folder");
    expect(screen.queryByText(/No language model is installed/)).toBeNull();
  });

  it("does not disable the model field while the list is still being read, or when there is a model", async () => {
    show();
    const model = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(api.servingModels).toHaveBeenCalled());
    await screen.findByText(/GGUF model from the models folder/);
    expect(model).toBeEnabled();
  });

  it("disables the adapter field and says why when no adapter has been converted", async () => {
    vi.mocked(api.servingAdapters).mockResolvedValue({ adapters: [] });
    show();
    fireEvent.click(await screen.findByText("Advanced"));
    const adapter = await screen.findByRole("combobox", { name: "Adapter" });
    await waitFor(() => expect(adapter).toBeDisabled());
    expect(adapter).toHaveTextContent("None");
    expect(screen.getByText(/No adapter has been converted yet/)).toBeInTheDocument();
  });

  it("lists a converted model by what it is, the model it was made from and its format, and saves the path of the file", async () => {
    const user = userEvent.setup();
    vi.mocked(api.servingModels).mockResolvedValue({ files: [file("Qwen-Qwen2.5-Coder-7B-Instruct-a1b2c3d4-Q4_K_M.gguf", { label: "Qwen/Qwen2.5-Coder-7B-Instruct, Q4_K_M", source: "converted" }), file("plain-q4.gguf", { label: "plain-q4.gguf" })] });
    show();
    await user.click(await screen.findByRole("combobox", { name: "Model" }));
    expect(await screen.findByRole("option", { name: /Qwen\/Qwen2\.5-Coder-7B-Instruct, Q4_K_M/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /a1b2c3d4/ })).toBeNull();
    expect(screen.getByRole("option", { name: /plain-q4\.gguf/ })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /Qwen\/Qwen2\.5-Coder-7B-Instruct, Q4_K_M/ }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ llm_local_model: "D:/models/Qwen-Qwen2.5-Coder-7B-Instruct-a1b2c3d4-Q4_K_M.gguf" }));
  });

  it("does not offer a converted model that was installed as an embedding model, and says the format of the others once", async () => {
    const user = userEvent.setup();
    vi.mocked(api.servingModels).mockResolvedValue({ files: [
      file("org-plain-name-1111-Q8_0.gguf", { label: "org/plain-name, Q8_0", role: "embedding", source: "converted" }),
      file("org-chat-2222-Q4_K_M.gguf", { label: "org/chat, Q4_K_M", role: "chat", source: "converted" }),
      file("plain-q4.gguf", { label: "plain-q4.gguf" })] });
    show();
    await user.click(await screen.findByRole("combobox", { name: "Model" }));
    const labelled = await screen.findByRole("option", { name: "org/chat, Q4_K_M" });
    expect(labelled.textContent).toBe("org/chat, Q4_K_M");                                                     // the format once: the hint repeating it is left out
    expect(screen.queryByRole("option", { name: /org\/plain-name/ })).toBeNull();
    expect(screen.getByRole("option", { name: /^plain-q4\.gguf/ })).toHaveTextContent("Q4_K_M");                 // a file with no label of its own still says its quantisation
  });

  it("falls back on the file name for a model that has no label", async () => {
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByRole("combobox", { name: "Model" }));
    expect(await screen.findByRole("option", { name: /qwen2\.5-coder-0\.5b-instruct-q4_k_m\.gguf/ })).toBeInTheDocument();
  });

  it("names the run whose adapter is waiting and the model it needs, as the reason for the empty adapter field", async () => {
    vi.mocked(api.servingAdapters).mockResolvedValue({ adapters: [] });
    vi.mocked(api.convertAdapterRuns).mockResolvedValue({ adapters: [{ run: "pilot/run-3", name: "pilot-run-3", folder: "x", base: "Qwen/Qwen2.5-Coder-7B", rank: 32, bytes: 1, base_installed: false, converted: false }] });
    show();
    fireEvent.click(await screen.findByText("Advanced"));
    expect(await screen.findByText(/Waiting: pilot\/run-3 \(install Qwen\/Qwen2\.5-Coder-7B first\)\./)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Adapter" })).toBeDisabled();
    expect(screen.queryByText(/Serving page/)).toBeNull();
  });

  it("gives the plain reason when the runs cannot be read", async () => {
    vi.mocked(api.servingAdapters).mockResolvedValue({ adapters: [] });
    vi.mocked(api.convertAdapterRuns).mockRejectedValue(new Error("down"));
    show();
    fireEvent.click(await screen.findByText("Advanced"));
    expect(await screen.findByText(/converted by itself once the model it was trained on is installed/)).toBeInTheDocument();
    expect(screen.queryByText(/Waiting:/)).toBeNull();
  });

  it("keeps the adapter field usable when an adapter exists", async () => {
    show();
    fireEvent.click(await screen.findByText("Advanced"));
    const adapter = await screen.findByRole("combobox", { name: "Adapter" });
    await waitFor(() => expect(api.servingAdapters).toHaveBeenCalled());
    await screen.findByText(/A LoRA adapter converted to GGUF/);
    expect(adapter).toBeEnabled();
  });

  it("keeps a saved choice that is no longer in the folder visible, and says so", async () => {
    show({ ...base, llm_local_model: "D:/gone/old-model.gguf" } as Settings);
    expect(await screen.findByRole("combobox", { name: "Model" })).toHaveTextContent("old-model.gguf (not in the models folder any more)");
  });

  it("keeps the adapter and the context size under Advanced and saves them", async () => {
    const user = userEvent.setup();
    show();
    fireEvent.click(await screen.findByText("Advanced"));
    await choose(user, await screen.findByRole("combobox", { name: "Adapter" }), /run-1\.gguf/);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ llm_local_adapter: "D:/models/converted/run-1.gguf" }));
    const context = screen.getByLabelText("Context size");
    const save = screen.getAllByRole("button", { name: "Save" }).slice(-1)[0];
    expect(save).toBeDisabled();
    fireEvent.change(context, { target: { value: "81x92" } });
    expect(context).toHaveValue("8192");
    fireEvent.click(save);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ llm_local_context: "8192" }));
  });

  it("tests the chosen model with a prompt and shows the reply, with no time limit mentioned as a number", async () => {
    vi.mocked(api.testConnection).mockResolvedValue({ ok: true, detail: "qwen.gguf replied.", response: "ok" });
    show({ ...base, llm_local_model: "D:/models/qwen.gguf" } as Settings);
    fireEvent.click(await screen.findByRole("button", { name: "Test the model" }));
    const dialog = await screen.findByRole("dialog", { name: "Test the language model" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Send prompt" }));
    expect(await within(dialog).findByText(/It works\. qwen\.gguf replied\./)).toBeInTheDocument();
    expect(api.testConnection).toHaveBeenCalledWith("llm", { llm_engine: "local", llm_local_model: "D:/models/qwen.gguf", input: "Reply with exactly: ok" });
  });

  it("switches to the external endpoint by saving the engine", async () => {
    show();
    fireEvent.click(await screen.findByRole("radio", { name: "External" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ llm_engine: "external" }));
  });

  it("reports a refused choice in words (an adapter trained on another model)", async () => {
    const user = userEvent.setup();
    vi.mocked(api.putSettings).mockRejectedValue(new Error("This adapter was trained on another model."));
    show();
    fireEvent.click(await screen.findByText("Advanced"));
    await choose(user, await screen.findByRole("combobox", { name: "Adapter" }), /run-1\.gguf/);
    expect(await screen.findByText(/Not saved: .*trained on another model/)).toBeInTheDocument();
    expect(onUpdated).not.toHaveBeenCalled();
  });
});

describe("LlmEnginePanel, external", () => {
  const external = { ...base, llm_engine: "external", llm_base_url: "http://localhost:11434/v1", llm_model: "qwen", llm_api_key_set: true, llm_api_key_hint: "abcd" } as unknown as Settings;

  it("shows the endpoint, the model and the key as saved (the key only by its last characters)", async () => {
    show(external);
    expect(await screen.findByLabelText("Base URL")).toHaveValue("http://localhost:11434/v1");
    expect(screen.getByLabelText("Model")).toHaveValue("qwen");
    expect(screen.getByTestId("secret-saved")).toHaveTextContent("abcd");
    expect(screen.queryByRole("combobox", { name: "Adapter" })).toBeNull();
  });

  it("saves the endpoint with the engine and sends the key only when a new one was typed", async () => {
    vi.mocked(api.putSettings).mockImplementation(async (patch) => ({ ...external, ...patch, llm_api_key: "" }) as Settings);      // the server answers with what it saved; a key is never sent back
    show(external);
    fireEvent.change(await screen.findByLabelText("Base URL"), { target: { value: "http://localhost:8080/v1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ llm_engine: "external", llm_base_url: "http://localhost:8080/v1", llm_model: "qwen" }));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-new" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenLastCalledWith({ llm_engine: "external", llm_base_url: "http://localhost:8080/v1", llm_api_key: "sk-new", llm_model: "qwen" }));
  });

  it("discovers the models of the endpoint and picks one from a list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.discoverModels).mockResolvedValue({ ok: true, models: ["qwen", "llama3"], detail: "2 model(s) found." });
    show(external);
    fireEvent.click(await screen.findByRole("button", { name: "Discover" }));
    expect(await screen.findByText("2 model(s) found.")).toBeInTheDocument();
    await choose(user, screen.getByRole("combobox", { name: "Model" }), "llama3");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith(expect.objectContaining({ llm_model: "llama3" })));
  });

  it("tests the connection with the values on the form", async () => {
    vi.mocked(api.testConnection).mockResolvedValue({ ok: false, detail: "The server refused the key." });
    show(external);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    const dialog = await screen.findByRole("dialog", { name: "Test the language model" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Send prompt" }));
    expect(await within(dialog).findByText(/It did not work\. The server refused the key\./)).toBeInTheDocument();
    expect(api.testConnection).toHaveBeenCalledWith("llm", expect.objectContaining({ llm_engine: "external", llm_base_url: "http://localhost:11434/v1", llm_model: "qwen" }));
  });
});

describe("what finishes elsewhere shows here without a restart", () => {
  it("reads the program and the models again when an install or a download has ended", async () => {
    const user = userEvent.setup();
    vi.mocked(api.servingStatus).mockResolvedValue(gateway({ ollama: { running: false, version: null, error: null } }));
    vi.mocked(api.servingModels).mockResolvedValue({ files: [] });
    show();
    expect(await screen.findByText(/Ollama is not running/)).toBeInTheDocument();
    vi.mocked(api.servingStatus).mockResolvedValue(gateway());
    vi.mocked(api.servingModels).mockResolvedValue({ files: [file("new-model-q4_k_m.gguf")] });
    act(() => notifyChanged("program", "models"));
    await waitFor(() => expect(screen.queryByText(/Ollama is not running/)).toBeNull());
    await user.click(await screen.findByRole("combobox", { name: "Model" }));
    expect(await screen.findByRole("option", { name: /new-model-q4_k_m/ })).toBeInTheDocument();
  });

  it("reads again when the page that stayed mounted is brought back after something was written", async () => {
    const page = (active: boolean) => <ToastProvider><ConfirmProvider><ActivePage active={active}><LlmEnginePanel s={base} onUpdated={onUpdated} /></ActivePage></ConfirmProvider></ToastProvider>;
    const { rerender } = render(page(true));
    await waitFor(() => expect(api.servingModels).toHaveBeenCalledTimes(1));
    rerender(page(false));
    clearRequestCache();                                                             // a write or a finished job elsewhere moves the request layer's epoch
    rerender(page(true));
    await waitFor(() => expect(api.servingModels).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.servingStatus).mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
