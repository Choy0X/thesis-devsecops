import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { compute: vi.fn(), agentSandboxStatus: vi.fn() }, sandbox: { ...actual.sandbox, stats: vi.fn() } };
});
import { api, sandbox as sandboxApi, type ComputeInfo } from "../lib/api";
import { DeviceStrip } from "./DeviceStrip";

const info = (over: Partial<ComputeInfo> = {}): ComputeInfo => ({
  physical_cores: 6, logical_cores: 12, ram_gb: 15.9, ram_free_gb: 4.4, cpu_workers: 5, auto_cpu_workers: 5, max_cpu_workers: 16,
  gpu: { available: true, name: "NVIDIA GeForce GTX 1660", total_mb: 6144, free_mb: 5138 }, ...over,
});

beforeEach(() => {
  vi.mocked(api.compute).mockClear();
  vi.mocked(api.agentSandboxStatus).mockReset();
  vi.mocked(sandboxApi.stats).mockReset();
});

describe("DeviceStrip", () => {
  it("shows cores, free memory and the card with its free video memory", async () => {
    vi.mocked(api.compute).mockResolvedValue(info());
    render(<DeviceStrip pollMs={60_000} />);
    expect(await screen.findByText("6 cores")).toBeInTheDocument();
    expect(screen.getByText("4.4 of 15.9 GB free")).toBeInTheDocument();
    expect(screen.getByText("GeForce GTX 1660 · 5.0 of 6.0 GB free")).toBeInTheDocument();
  });

  it("says there is no GPU instead of hiding the fact", async () => {
    vi.mocked(api.compute).mockResolvedValue(info({ gpu: { available: false, reason: "no CUDA device" } }));
    render(<DeviceStrip pollMs={60_000} />);
    expect(await screen.findByText("No GPU")).toBeInTheDocument();
  });

  it("asks again on its interval", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.compute).mockResolvedValue(info());
      render(<DeviceStrip pollMs={1000} />);
      await vi.advanceTimersByTimeAsync(2500);
      expect(vi.mocked(api.compute).mock.calls.length).toBeGreaterThanOrEqual(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows no agent-sandbox chip without a projectId, and never asks for one", async () => {
    vi.mocked(api.compute).mockResolvedValue(info());
    render(<DeviceStrip pollMs={60_000} />);
    await screen.findByText("6 cores");
    expect(api.agentSandboxStatus).not.toHaveBeenCalled();
    expect(screen.queryByText(/^Agent /)).toBeNull();
  });

  it("shows the project's agent sandbox CPU and memory only while it is running", async () => {
    vi.mocked(api.compute).mockResolvedValue(info());
    vi.mocked(api.agentSandboxStatus).mockResolvedValue({ running: true, container: "c1" });
    vi.mocked(sandboxApi.stats).mockResolvedValue({ cpu_pct: 12, mem_mb: 340, mem_limit_mb: 2048 });
    render(<DeviceStrip pollMs={60_000} projectId="shop-aaaaaa" />);
    expect(await screen.findByText("Agent 12% · 340/2048 MB")).toBeInTheDocument();
    expect(sandboxApi.stats).toHaveBeenCalledWith("c1");
  });

  it("shows nothing for a project with no agent sandbox running", async () => {
    vi.mocked(api.compute).mockResolvedValue(info());
    vi.mocked(api.agentSandboxStatus).mockResolvedValue({ running: false, container: null });
    render(<DeviceStrip pollMs={60_000} projectId="shop-aaaaaa" />);
    await screen.findByText("6 cores");
    expect(screen.queryByText(/^Agent /)).toBeNull();
    expect(sandboxApi.stats).not.toHaveBeenCalled();
  });
});
