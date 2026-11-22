import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { gpuMode: vi.fn() } };
});
import { api } from "../lib/api";
import { TitleBar } from "./TitleBar";

type Slots = Record<string, (...args: any[]) => unknown>;
// A stand-in for the Qt bridge: a slot hands its answer to the callback it is given as the last argument.
function installBridge(state: { framed: boolean; maximised: boolean }) {
  const calls: string[] = [];
  const answer = (name: string, value: unknown) => (...args: any[]) => { calls.push(name); const cb = args[args.length - 1]; if (typeof cb === "function") cb(value); };
  const slots: Slots = {
    getFrame: answer("getFrame", state.framed),
    isMaximized: (...args: any[]) => { calls.push("isMaximized"); args[args.length - 1](state.maximised); },
    minimize: answer("minimize", undefined),
    toggleMaximize: (...args: any[]) => { calls.push("toggleMaximize"); state.maximised = !state.maximised; args[args.length - 1]?.(undefined); },
    close: answer("close", undefined),
  };
  (window as any).__PYLOID__ = slots;
  return calls;
}
beforeEach(() => { vi.mocked(api.gpuMode).mockReset(); vi.mocked(api.gpuMode).mockResolvedValue({ mode: "idle", holders: [] }); });
afterEach(() => { delete (window as any).__PYLOID__; document.documentElement.style.removeProperty("--titlebar-h"); });

describe("the title bar", () => {
  it("draws nothing in a browser, where there is no bridge", async () => {
    const { container } = render(<TitleBar section="Records" />);
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(container).toBeEmptyDOMElement();
  });

  it("draws nothing when the window kept the operating system's frame", async () => {
    installBridge({ framed: true, maximised: false });
    const { container } = render(<TitleBar section="Records" />);
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(container).toBeEmptyDOMElement();
  });

  it("draws the product, the section and three named buttons in a frameless window", async () => {
    installBridge({ framed: false, maximised: false });
    render(<TitleBar section="Source records" />);
    expect(await screen.findByText("Vuln Framework")).toBeInTheDocument();
    expect(screen.getByText("Source records")).toBeInTheDocument();
    for (const name of ["Minimise", "Maximise", "Close"]) expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(await screen.findByText("GPU idle")).toBeInTheDocument();
  });

  it("says what has the graphics card, in the bar and in its title", async () => {
    vi.mocked(api.gpuMode).mockResolvedValue({ mode: "serve", holders: [{ mode: "serve", what: "the local model server (qwen.gguf)", stoppable: true }] });
    installBridge({ framed: false, maximised: false });
    render(<TitleBar section="x" />);
    const label = await screen.findByText("GPU serving");
    expect(label.closest("span")).toHaveAttribute("title", "The graphics card is in use by the local model server (qwen.gguf)");
  });

  it("draws nothing about the card until the backend has answered, and keeps the last answer when a read fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      installBridge({ framed: false, maximised: false });
      vi.mocked(api.gpuMode).mockResolvedValue({ mode: "train", holders: [{ mode: "train", what: "a training or GPU check", stoppable: false }] });
      render(<TitleBar section="x" />);
      expect(await screen.findByText("GPU training")).toBeInTheDocument();
      vi.mocked(api.gpuMode).mockImplementation(() => Promise.reject(new Error("busy")));
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      expect(screen.getByText("GPU training")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("marks its empty area as the drag region", async () => {
    installBridge({ framed: false, maximised: false });
    const { container } = render(<TitleBar section="x" />);
    await screen.findByText("Vuln Framework");
    expect(container.querySelectorAll("[data-pyloid-drag-region]").length).toBeGreaterThanOrEqual(1);
  });

  it("minimises, maximises and closes through the bridge", async () => {
    const calls = installBridge({ framed: false, maximised: false });
    render(<TitleBar section="x" />);
    await screen.findByText("Vuln Framework");
    fireEvent.click(screen.getByRole("button", { name: "Minimise" }));
    fireEvent.click(screen.getByRole("button", { name: "Maximise" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(calls).toEqual(expect.arrayContaining(["minimize", "toggleMaximize", "close"]));
  });

  it("toggles maximise on a double-click of the bar", async () => {
    const calls = installBridge({ framed: false, maximised: false });
    const { container } = render(<TitleBar section="x" />);
    await screen.findByText("Vuln Framework");
    fireEvent.doubleClick(container.querySelector("[data-pyloid-drag-region]")!);
    await waitFor(() => expect(calls).toContain("toggleMaximize"));
  });

  it("waits for the shell when the bridge arrives after the page", async () => {
    render(<TitleBar section="x" />);
    expect(screen.queryByText("Vuln Framework")).toBeNull();
    installBridge({ framed: false, maximised: false });
    act(() => { document.dispatchEvent(new CustomEvent("pyloidReady")); });
    expect(await screen.findByText("Vuln Framework")).toBeInTheDocument();
  });
});
