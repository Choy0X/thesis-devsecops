import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { servingStatus: vi.fn(), servingLoad: vi.fn(), servingUnload: vi.fn(), servingStatusLive: vi.fn() } };
});
import { api } from "../lib/api";
import { notifyChanged } from "../lib/changes";
import { ConfirmProvider } from "./hud/AlertDialog";
import { ToastProvider } from "./Toast";
import { GatewayNote, useGatewayStatus } from "./GatewayNote";

const status = (running: boolean) => ({ ollama: { running, error: null, version: running ? "0.35.0" : null },
  choices: {}, slots: {}, exclusive: null, free_vram_bytes: null, queue: [] }) as any;

const withModel = (role: "llm" | "embed", over: { engine?: "local" | "external"; model?: string; slot?: Record<string, unknown> } = {}) => ({
  ollama: { running: true, error: null, version: "0.35.0" },
  choices: { [role]: { engine: over.engine ?? "local", model: over.model ?? "qwen.gguf" } },
  slots: { [role]: { state: "absent", message: "", model: null, runs_on: null, gpu_bytes: 0, in_flight: 0, served: 0, idle_seconds: null, loaded_seconds: null, error: null, engine: null, ...over.slot } },
  exclusive: null, free_vram_bytes: null, queue: [],
}) as any;

beforeEach(() => {
  vi.mocked(api.servingStatus).mockReset();
  vi.mocked(api.servingLoad).mockReset();
  vi.mocked(api.servingUnload).mockReset();
  vi.mocked(api.servingStatusLive).mockReset();
});

describe("useGatewayStatus", () => {
  it("reads the status once when it opens", async () => {
    vi.mocked(api.servingStatus).mockResolvedValue(status(false));
    const { result } = renderHook(() => useGatewayStatus());
    await waitFor(() => expect(result.current.status?.ollama.running).toBe(false));
    expect(api.servingStatus).toHaveBeenCalledTimes(1);
  });

  it("refreshes the status when the model inventory changes", async () => {
    vi.mocked(api.servingStatus).mockResolvedValue(status(false));
    const { result } = renderHook(() => useGatewayStatus());
    await waitFor(() => expect(result.current.status?.ollama.running).toBe(false));
    vi.mocked(api.servingStatus).mockResolvedValue(status(true));
    act(() => notifyChanged("models"));
    await waitFor(() => expect(result.current.status?.ollama.running).toBe(true));
    expect(api.servingStatus).toHaveBeenCalledTimes(2);
  });
});

describe("GatewayNote, loading a model on purpose", () => {
  const show = (s: any) => render(<ToastProvider><ConfirmProvider><GatewayNote status={s} role="llm" /></ConfirmProvider></ToastProvider>);

  it("offers to load a local, chosen model that is not loaded, and opens the dialog", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: withModel("llm", { slot: { state: "loading" } }) });
    show(withModel("llm"));
    fireEvent.click(await screen.findByRole("button", { name: "Load" }));
    expect(await screen.findByRole("dialog", { name: "Loading the language model" })).toBeInTheDocument();
  });

  it("offers nothing for a remote endpoint: there is nothing here to load", async () => {
    show(withModel("llm", { engine: "external" }));
    await screen.findByText(/Not loaded/);
    expect(screen.queryByRole("button", { name: "Load" })).toBeNull();
  });

  it("offers nothing when no model is chosen yet", async () => {
    show(withModel("llm", { model: "" }));
    await screen.findByText(/Not loaded/);
    expect(screen.queryByRole("button", { name: "Load" })).toBeNull();
  });

  it("offers nothing once the model is already loading or ready: it is watched or unloaded, not loaded again", async () => {
    const { rerender } = show(withModel("llm", { slot: { state: "loading" } }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load" })).toBeNull());
    rerender(<ToastProvider><ConfirmProvider><GatewayNote status={withModel("llm", { slot: { state: "ready" } })} role="llm" /></ConfirmProvider></ToastProvider>);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load" })).toBeNull());
  });

  it("offers it again once a load has failed", async () => {
    show(withModel("llm", { slot: { state: "failed", error: { code: "does_not_fit", message: "x" } } }));
    expect(await screen.findByRole("button", { name: "Load" })).toBeInTheDocument();
  });
});

describe("GatewayNote, unloading a model on purpose", () => {
  const show = (s: any, onChanged?: () => void) => render(<ToastProvider><ConfirmProvider><GatewayNote status={s} role="llm" onChanged={onChanged} /></ConfirmProvider></ToastProvider>);

  it("offers to unload a model that is ready, asks to confirm, then unloads it", async () => {
    vi.mocked(api.servingUnload).mockResolvedValue({ unloaded: ["llm"], status: {} } as any);
    const onChanged = vi.fn();
    show(withModel("llm", { slot: { state: "ready" } }), onChanged);
    fireEvent.click(await screen.findByRole("button", { name: "Unload" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Unload the language model?");
    fireEvent.click(within(dialog).getByRole("button", { name: "Unload it" }));
    await waitFor(() => expect(api.servingUnload).toHaveBeenCalledWith({ role: "llm" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("does nothing when the confirmation is declined", async () => {
    show(withModel("llm", { slot: { state: "ready" } }));
    fireEvent.click(await screen.findByRole("button", { name: "Unload" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep it loaded" }));
    expect(api.servingUnload).not.toHaveBeenCalled();
  });

  it("offers nothing when the model is not loaded", async () => {
    show(withModel("llm", { slot: { state: "absent" } }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Unload" })).toBeNull());
  });

  it("offers nothing for a remote endpoint, even if the slot info were ready", async () => {
    show(withModel("llm", { engine: "external", slot: { state: "ready" } }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Unload" })).toBeNull());
  });

  it("offers Load, not Unload, once a model has failed to load", async () => {
    show(withModel("llm", { slot: { state: "failed", error: { code: "does_not_fit", message: "x" } } }));
    expect(await screen.findByRole("button", { name: "Load" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unload" })).toBeNull();
  });
});
