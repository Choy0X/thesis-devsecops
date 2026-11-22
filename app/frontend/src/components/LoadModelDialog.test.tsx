import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { servingLoad: vi.fn(), servingStatusLive: vi.fn() } };
});
import { api, type ServingStatus } from "../lib/api";
import { LoadModelDialog } from "./LoadModelDialog";

const status = (slot: Partial<NonNullable<ServingStatus["slots"]["llm"]>> | undefined): ServingStatus => ({
  ollama: { running: true, version: "0.35.0", error: null }, running_models: [],
  choices: { llm: { engine: "local", model: "qwen.gguf" } },
  slots: { llm: slot ? ({ state: "loading", message: "", model: "qwen.gguf", runs_on: null, gpu_bytes: 0, in_flight: 0, served: 0, idle_seconds: null, loaded_seconds: null, error: null, engine: null, ...slot } as any) : undefined },
  exclusive: null,
});

beforeEach(() => {
  vi.mocked(api.servingLoad).mockReset();
  vi.mocked(api.servingStatusLive).mockReset();
});

const show = () => render(<LoadModelDialog role="llm" label="Language model" onClose={vi.fn()} />);

describe("LoadModelDialog", () => {
  it("asks the gateway to load, then shows what the slot says while it is loading", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading", message: "placing 24 of 24 layers on the card" }) });
    show();
    expect(await screen.findByText(/placing 24 of 24 layers on the card/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Loading the language model" })).toBeInTheDocument();
  });

  it("polls the live status (never the cached one) until the model is ready", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading" }) });
    vi.mocked(api.servingStatusLive).mockResolvedValue(status({ state: "ready", message: "" }));
    show();
    await screen.findByText(/Loading/);
    expect(await screen.findByText(/Ready/, {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(api.servingStatusLive).toHaveBeenCalled();
  });

  it("calls onReady once, as soon as the slot turns ready, without waiting for Done to be clicked", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading" }) });
    vi.mocked(api.servingStatusLive).mockResolvedValue(status({ state: "ready", message: "" }));
    const onReady = vi.fn();
    render(<LoadModelDialog role="llm" label="Language model" onClose={vi.fn()} onReady={onReady} />);
    await screen.findByText(/Loading/);
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1), { timeout: 4000 });
  });

  it("shows the reason once the load fails, with a way to try again", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading" }) });
    vi.mocked(api.servingStatusLive).mockResolvedValue(status({ state: "failed", error: { code: "does_not_fit", message: "The model needs more memory than is free." } }));
    show();
    await screen.findByText(/Loading/);
    expect(await screen.findByText("The model needs more memory than is free.", {}, { timeout: 4000 })).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    vi.mocked(api.servingLoad).mockClear().mockResolvedValue({ started: true, status: status({ state: "loading" }) });
    fireEvent.click(retry);
    await waitFor(() => expect(api.servingLoad).toHaveBeenCalledTimes(1));
  });

  it("says why at once when the gateway refuses the request itself, with no loading shown", async () => {
    vi.mocked(api.servingLoad).mockRejectedValue(new Error("The graphics card is held by training."));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("The graphics card is held by training.");
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(/Asking the gateway to start/)).toBeNull();
  });

  it("cannot be dismissed while the model is loading: no button to do it, and Esc does nothing", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading" }) });
    const onClose = vi.fn();
    render(<LoadModelDialog role="llm" label="Language model" onClose={onClose} />);
    await screen.findByText(/Loading/);
    expect(screen.queryByRole("button", { name: "Hide" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("can be closed once the model is ready", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading" }) });
    vi.mocked(api.servingStatusLive).mockResolvedValue(status({ state: "ready", message: "" }));
    const onClose = vi.fn();
    render(<LoadModelDialog role="llm" label="Language model" onClose={onClose} />);
    await screen.findByText(/Loading/);
    fireEvent.click(await screen.findByRole("button", { name: "Done" }, { timeout: 4000 }));
    expect(onClose).toHaveBeenCalled();
  });

  it("can be closed once the load has failed", async () => {
    vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading" }) });
    vi.mocked(api.servingStatusLive).mockResolvedValue(status({ state: "failed", error: { code: "does_not_fit", message: "The model needs more memory than is free." } }));
    const onClose = vi.fn();
    render(<LoadModelDialog role="llm" label="Language model" onClose={onClose} />);
    await screen.findByText(/Loading/);
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }, { timeout: 4000 }));
    expect(onClose).toHaveBeenCalled();
  });

  it("does not ask again once the slot is actually loading, even past the grace period", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.servingLoad).mockResolvedValue({ started: true, status: status({ state: "loading" }) });
      vi.mocked(api.servingStatusLive).mockResolvedValue(status({ state: "loading" }));
      render(<LoadModelDialog role="llm" label="Language model" onClose={vi.fn()} />);
      await vi.waitFor(() => expect(api.servingLoad).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(20_000);
      expect(api.servingLoad).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
