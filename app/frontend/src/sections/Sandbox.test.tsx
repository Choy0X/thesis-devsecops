import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, sandbox: { status: vi.fn(), list: vi.fn(), containersPage: vi.fn(), images: vi.fn(), create: vi.fn(), cleanup: vi.fn(), retryCleanup: vi.fn() } };
});
import { sandbox } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { Sandbox, HardeningChips } from "./Sandbox";
import { ApiError } from "../lib/apiError";

const HARDENING = [{ id: "caps", label: "No extra privileges", detail: "Every Linux capability is dropped." }, { id: "user", label: "Not root", detail: "Runs as an ordinary user." }];

beforeEach(() => {
  for (const fn of Object.values(sandbox)) (fn as any).mockReset?.();
  vi.mocked(sandbox.status).mockResolvedValue({ docker_available: true, version: "27.0", hardening: HARDENING } as any);
  vi.mocked(sandbox.list).mockResolvedValue([]);
  vi.mocked(sandbox.containersPage).mockResolvedValue({ containers: [], total: 0, limit: 100, offset: 0 });
  vi.mocked(sandbox.cleanup).mockResolvedValue({cleanup_errors: [], pending_images: {}});
  vi.mocked(sandbox.retryCleanup).mockResolvedValue({cleanup_errors: [], pending_images: {}});
  vi.mocked(sandbox.images).mockResolvedValue([{ id: "a", tags: ["python:3.11-slim"], size_mb: 120.5 }]);
});

describe("the sandbox page", () => {
  it("keeps failed cleanup retries visible and clears the error when cleanup succeeds", async () => {
    vi.mocked(sandbox.cleanup).mockRejectedValueOnce(new Error("Cleanup service unavailable"));
    vi.mocked(sandbox.retryCleanup).mockRejectedValueOnce(new ApiError({ status: 503, title: "Docker unavailable", detail: "Docker is still unavailable" }));
    render(<ConfirmProvider><Sandbox /></ConfirmProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Retry pending cleanup" }));
    expect(await screen.findByText("Docker is still unavailable")).toBeInTheDocument();
    expect(screen.queryByText("The container list could not be refreshed")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry pending cleanup" }));
    await waitFor(() => expect(screen.queryByText("Could not check Docker cleanup")).not.toBeInTheDocument());
    expect(sandbox.retryCleanup).toHaveBeenCalledTimes(2);
  });
  it("shows a cleanup service failure and allows retry without hiding the container list", async () => {
    vi.mocked(sandbox.cleanup).mockRejectedValueOnce(new Error("Cleanup service unavailable"));
    render(<ConfirmProvider><Sandbox /></ConfirmProvider>);
    expect(await screen.findByText("Could not check Docker cleanup")).toBeInTheDocument();
    expect(await screen.findByText("No sandboxes")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    await waitFor(() => expect(screen.queryByText("Could not check Docker cleanup")).not.toBeInTheDocument());
    expect(sandbox.cleanup).toHaveBeenCalledTimes(2);
  });
  it("explains that Docker is needed when it is not there", async () => {
    vi.mocked(sandbox.status).mockResolvedValue({ docker_available: false } as any);
    render(<ConfirmProvider><Sandbox /></ConfirmProvider>);
    expect(await screen.findByText("Docker is not available")).toBeInTheDocument();
  });

  it("shows protections without creating a container", () => {
    render(<HardeningChips fixed={HARDENING} network={false} readonly={false} />);
    expect(screen.getByLabelText('Protections')).toHaveTextContent('Not root');
    expect(sandbox.create).not.toHaveBeenCalled();
  });

  it("warns in words when a container has network access", () => {
    render(<HardeningChips fixed={HARDENING} network readonly={false} />);
    expect(screen.getByRole('alert')).toHaveTextContent('can reach the internet');
  });

  it("does not offer standalone creation or start work while browsing", async () => {
    render(<ConfirmProvider><Sandbox /></ConfirmProvider>);
    expect(await screen.findByText('No sandboxes')).toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'New'})).not.toBeInTheDocument();
    expect(screen.getByText(/start automatically when a chat task needs/)).toBeInTheDocument();
    expect(sandbox.create).not.toHaveBeenCalled();
  });
});
