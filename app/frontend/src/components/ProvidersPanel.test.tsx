import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { agentProviders: vi.fn(), agentProviderPut: vi.fn(), agentProviderDelete: vi.fn(), agentProviderTest: vi.fn(), agentModelProbe: vi.fn() } };
});
import { api, type AgentModel, type AgentProvider, type ModelProfileSummary } from "../lib/api";
import { ConfirmProvider } from "./hud/AlertDialog";
import { ProvidersPanel, modelsToText, profileSaid, textToModels } from "./ProvidersPanel";
import { ToastProvider } from "./Toast";

const profile = (over: Partial<ModelProfileSummary> = {}): ModelProfileSummary => ({ protocol: "schema", source: "probed", family: "qwen2", reasoning: "none", tool_format: "hermes_xml", native_tools: true, max_output: 4096, context: 32768, notes: [], ...over });
const own = (over: Partial<AgentModel> = {}): AgentModel => ({ ref: "", label: "qwen2.5-coder-7b", provider: "default", provider_name: "This computer (llama.cpp)", trust: "local", configured: true, profile: profile(), ...over });
const lab = (over: Partial<AgentProvider> = {}): AgentProvider => ({ id: "lab", name: "Lab server", kind: "openai_compat", base_url: "http://192.168.1.20:8000/v1", api_key_set: true, api_key_hint: "1234", headers: [], trust: "lan",
  models: [{ id: "qwen3-8b", context: 32768, max_output: 4096 }], ...over });
const show = () => render(<ToastProvider><ConfirmProvider><ProvidersPanel /></ConfirmProvider></ToastProvider>);
const load = (providers: AgentProvider[], d: AgentModel = own()) => vi.mocked(api.agentProviders).mockResolvedValue({ providers, default: d });

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  load([lab()]);
  vi.mocked(api.agentProviderPut).mockImplementation(async (id, body) => ({ ...lab(), id, name: body.name ?? id, base_url: body.base_url, models: [] }));
});

describe("what a model is said to be", () => {
  it("says how it is asked and whether that was tested", () => {
    expect(profileSaid(profile())).toBe("Tested: asked with a schema for the reply.");
    expect(profileSaid(profile({ source: "derived", protocol: "native" }))).toBe("Not tested yet: asked with native tools.");
    expect(profileSaid(profile({ source: "manual", protocol: "native", reasoning: "toggle" }))).toBe("Set by you: asked with native tools; can think.");
    expect(profileSaid(profile({ reasoning: "always" }))).toBe("Tested: asked with a schema for the reply; thinks before every answer.");
    expect(profileSaid(null, "Nothing yet.")).toBe("Nothing yet.");
  });

  it("writes models as lines and reads the lines back, skipping and naming what it does not understand", () => {
    expect(modelsToText([{ id: "a", context: 8192, max_output: 2048, protocol: "native" }, { id: "b" }])).toBe("a context=8192 max_output=2048 protocol=native" + String.fromCharCode(10) + "b");
    const nl = String.fromCharCode(10);
    const r = textToModels(`# a note${nl}a context=8192 protocol=schema${nl}${nl}  b  ${nl}c context=12 flavour=mild${nl}d protocol=telepathy`);
    expect(r.models).toEqual({ a: { context: 8192, protocol: "schema" }, b: {}, c: {}, d: {} });
    expect(r.errors).toEqual([expect.stringContaining("Line 5: context=12 is not"), expect.stringContaining("Line 5: flavour=mild is not"), expect.stringContaining("Line 6: protocol=telepathy is not")]);
  });
});

describe("ProvidersPanel", () => {
  it("shows the model of the settings and each provider with where it is and whether a key is set, and never a key", async () => {
    show();
    expect(await screen.findByText("This computer (llama.cpp): qwen2.5-coder-7b")).toBeInTheDocument();
    expect(screen.getByText("Tested: asked with a schema for the reply.")).toBeInTheDocument();
    const item = (await screen.findByText("Lab server")).closest("li")!;
    expect(within(item).getByText("http://192.168.1.20:8000/v1")).toBeInTheDocument();
    expect(within(item).getByText(/Another computer on the network\. A key is set \(ends 1234\)\./)).toBeInTheDocument();
    expect(within(item).getByText("qwen3-8b")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/sk-/);
  });

  it("says plainly when nothing is added and when no model is chosen yet", async () => {
    load([], own({ configured: false, label: "", profile: null }));
    show();
    expect(await screen.findByText("No provider added yet.")).toBeInTheDocument();
    expect(screen.getByText("No model is chosen above yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test tools" })).toBeDisabled();
  });

  it("tests the connection, shows what the server has, and lists models that are not listed yet", async () => {
    vi.mocked(api.agentProviderTest).mockResolvedValue({ ok: true, models: ["qwen3-8b", "llama-3.1-8b"], count: 2 });
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/Reached it: 2 models, qwen3-8b, llama-3.1-8b\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "List these models" }));
    await waitFor(() => expect(api.agentProviderPut).toHaveBeenCalledWith("lab", { name: "Lab server", base_url: "http://192.168.1.20:8000/v1",
      models: { "qwen3-8b": { name: undefined, context: 32768, max_output: 4096, protocol: undefined }, "llama-3.1-8b": {} } }));
  });

  it("says why a connection did not work", async () => {
    vi.mocked(api.agentProviderTest).mockResolvedValue({ ok: false, error: "The model endpoint refused the key (401). Check the key in Settings." });
    show();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/refused the key \(401\)/)).toBeInTheDocument();
  });

  it("asks a model how it takes tools when told to, for the settings' own model and for a provider's, and shows the answer afterwards", async () => {
    vi.mocked(api.agentModelProbe).mockResolvedValue({ profile: profile({ source: "probed" }) });
    show();
    const user = userEvent.setup();
    await screen.findByText("Lab server");
    const [own_] = screen.getAllByRole("button", { name: "Test tools" });
    await user.click(own_);
    await waitFor(() => expect(api.agentModelProbe).toHaveBeenCalledWith(""));
    const second = () => screen.getAllByRole("button", { name: "Test tools" })[1];       // looked up again each time: a button that was disabled and is enabled again is a new element
    await waitFor(() => expect(second()).toBeEnabled());                                  // one test at a time: the others wait until it has answered
    await user.click(second());
    await waitFor(() => expect(api.agentModelProbe).toHaveBeenCalledWith("lab/qwen3-8b"));
    expect(api.agentProviders).toHaveBeenCalledTimes(3);                                  // the page is read again after each test
  });

  it("adds a provider: the id, the address and the models, and sends a key only when one was typed", async () => {
    load([]);
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add a provider" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a provider" });
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Id"), "Lab-Box");
    await user.type(within(dialog).getByLabelText("Name"), "Lab box");
    await user.type(within(dialog).getByLabelText("Address"), "http://192.168.1.20:8000/v1/chat/completions");
    await user.type(within(dialog).getByLabelText("Models"), "qwen3-8b context=32768 protocol=native");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.agentProviderPut).toHaveBeenCalledWith("lab-box", { name: "Lab box", base_url: "http://192.168.1.20:8000/v1/chat/completions", models: { "qwen3-8b": { context: 32768, protocol: "native" } } }));
  });

  it("will not save a models line it does not understand and says which", async () => {
    load([]);
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add a provider" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a provider" });
    await user.type(within(dialog).getByLabelText("Id"), "x");
    await user.type(within(dialog).getByLabelText("Address"), "http://localhost:1234/v1");
    await user.type(within(dialog).getByLabelText("Models"), "m protocol=telepathy");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Line 1: protocol=telepathy is not");
    expect(api.agentProviderPut).not.toHaveBeenCalled();
  });

  it("edits a provider keeping its key, the id fixed, and can remove the key", async () => {
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit Lab server" }));
    let dialog = await screen.findByRole("dialog", { name: "Edit Lab server" });
    expect(within(dialog).getByLabelText("Id")).toBeDisabled();
    expect(within(dialog).getByLabelText("Models")).toHaveValue("qwen3-8b context=32768 max_output=4096");
    await user.clear(within(dialog).getByLabelText("Name"));
    await user.type(within(dialog).getByLabelText("Name"), "Renamed");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.agentProviderPut).toHaveBeenLastCalledWith("lab", { name: "Renamed", base_url: "http://192.168.1.20:8000/v1", models: { "qwen3-8b": { context: 32768, max_output: 4096 } } }));
    await user.click(await screen.findByRole("button", { name: "Edit Lab server" }));
    dialog = await screen.findByRole("dialog", { name: "Edit Lab server" });
    await user.click(within(dialog).getByRole("button", { name: /remove/i }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.agentProviderPut).toHaveBeenLastCalledWith("lab", expect.objectContaining({ api_key: "" })));
  });

  it("deletes a provider only after being asked", async () => {
    vi.mocked(api.agentProviderDelete).mockResolvedValue({ deleted: true });
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete Lab server" }));
    expect(api.agentProviderDelete).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.agentProviderDelete).toHaveBeenCalledWith("lab"));
  });
});
