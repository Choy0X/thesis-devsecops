import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type ToolchainStatus, type ToolchainTool } from "../lib/api";
import { ToolchainPanel } from "./ToolchainPanel";
import { ToastProvider } from "./Toast";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { agentToolchain: vi.fn(), agentToolchainTest: vi.fn() } };
});

const tool = (over: Partial<ToolchainTool> = {}): ToolchainTool => ({ tool: "rtk", wanted: "1.0.1", licence: "Apache-2.0", note: "", supported: true, size: 5708680, installed: false, tested: false, notes: [], installed_at: null, ...over });
const status = (...tools: ToolchainTool[]): ToolchainStatus => ({ platform: "windows-x86_64", tools });
const show = () => render(<ToastProvider><ToolchainPanel /></ToastProvider>);

beforeEach(() => vi.resetAllMocks());

describe("ToolchainPanel", () => {
  it("shows ready and preparing app-managed tools without an install prompt", async () => {
    vi.mocked(api.agentToolchain).mockResolvedValue(status(tool({ installed: true, tested: true }), tool({ tool: "ripgrep", preparing: true })));
    show();
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Preparing")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /install|download/i })).toBeNull();
  });

  it("tests an installed tool again and reports a failed self-test", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentToolchain).mockResolvedValue(status(tool({ installed: true, tested: true })));
    vi.mocked(api.agentToolchainTest).mockResolvedValue(status(tool({ installed: true, tested: false, notes: ["Self-test failed"] })));
    show();
    await user.click(await screen.findByRole("button", { name: "Test" }));
    await waitFor(() => expect(api.agentToolchainTest).toHaveBeenCalledWith("rtk"));
    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Self-test failed")).toBeInTheDocument();
  });

  it("distinguishes unsupported platforms from a preparation error", async () => {
    vi.mocked(api.agentToolchain).mockResolvedValue(status(tool({ supported: false, size: null }), tool({ tool: "ripgrep", preparation_error: "Release download unavailable" })));
    show();
    expect(await screen.findByText("Unsupported platform")).toBeInTheDocument();
    expect(screen.getByText("Release download unavailable")).toBeInTheDocument();
  });

  it("can refresh command tool status after a failed request", async () => {
    vi.mocked(api.agentToolchain).mockRejectedValueOnce(new Error("The server is not there."));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("The server is not there.");
    vi.mocked(api.agentToolchain).mockResolvedValue(status(tool()));
    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh command tool status" }));
    expect(await screen.findByRole("list", { name: "Command tools" })).toBeInTheDocument();
  });
});
