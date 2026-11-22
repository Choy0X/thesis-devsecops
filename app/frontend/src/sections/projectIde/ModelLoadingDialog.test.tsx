import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ModelLoadingDialog } from "./ModelLoadingDialog";
import { get, write } from "./client";

vi.mock("./client", async original => ({ ...await original<typeof import("./client")>(), get: vi.fn(), write: vi.fn() }));
const model = { id: "qwen", name: "Qwen2.5-Coder 7B Instruct", context: 8192 };
beforeEach(() => { vi.clearAllMocks(); });

it("shows actual progress and continues once, only after the model is ready", async () => {
  vi.mocked(write).mockResolvedValue({ id: "load", state: "loading", phase: "loading", message: "Loading the selected artifact", model } as never);
  vi.mocked(get).mockResolvedValue({ id: "load", state: "ready", model } as never);
  const onReady = vi.fn(async () => undefined);
  render(<ModelLoadingDialog model={model} onClose={vi.fn()} onReady={onReady} />);
  expect(await screen.findByText("Loading the selected artifact")).toBeInTheDocument();
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  expect(onReady).not.toHaveBeenCalled();
  await waitFor(() => expect(onReady).toHaveBeenCalledOnce());
  expect(write).toHaveBeenCalledWith("/api/ide/models/prepare", { model, context: 8192 });
});

it("requires an explicit shorter-context retry after a memory failure", async () => {
  vi.mocked(write).mockResolvedValue({ id: "load", state: "failed", model, error: { code: "does_not_fit", message: "Not enough memory. Free memory or choose a shorter context." } } as never);
  const onReady = vi.fn(async () => undefined);
  render(<ModelLoadingDialog model={model} onClose={vi.fn()} onReady={onReady} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Not enough memory");
  expect(write).toHaveBeenCalledOnce(); expect(onReady).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Retry with 4,096 tokens" }));
  await waitFor(() => expect(write).toHaveBeenLastCalledWith("/api/ide/models/prepare", { model, context: 4096 }));
});

it("closing a pending load prevents a late ready response from creating a chat", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(write).mockImplementation(async () => await new Promise<unknown>(resolve => { finish = resolve; }) as never);
  const onReady = vi.fn(async () => undefined);
  const { unmount } = render(<ModelLoadingDialog model={model} onClose={vi.fn()} onReady={onReady} />);
  unmount();
  await act(async () => { finish({ id: "load", state: "ready", model }); });
  expect(onReady).not.toHaveBeenCalled();
});

it("shows measured verification progress without treating it as model loading percentage", async () => {
  vi.mocked(write).mockResolvedValue({ id: "load", state: "loading", phase: "verifying", message: "Verifying the selected model artifact.", progress: { completed: 512, total: 1024, unit: "bytes", percent: 50 }, elapsed_seconds: 3, model } as never);
  vi.mocked(get).mockReturnValue(new Promise<never>(() => {}));
  render(<ModelLoadingDialog model={model} purpose="switch" onClose={vi.fn()} onReady={vi.fn()} />);
  expect(await screen.findByRole("progressbar", { name: "File verification" })).toHaveAttribute("aria-valuenow", "50");
  expect(screen.getByText(/50%/)).toBeInTheDocument();
  expect(screen.getByText(/current model stays selected/i)).toBeInTheDocument();
});
