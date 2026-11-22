import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/apiError";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: { experiments: vi.fn(), listJobs: vi.fn(), getSettings: vi.fn(), storeSummary: vi.fn(), codeSummary: vi.fn(), builds: vi.fn(), mix: vi.fn(), device: vi.fn(), lifecycle: vi.fn() },
  };
});
import { api } from "../lib/api";
import { ToastProvider } from "../components/Toast";
import { Logs } from "./Logs";
import { Overview } from "./Overview";
import { MemoryRouter } from "react-router-dom";
import { Results } from "./Results";
import { Settings } from "./Settings";

const boom = () => new ApiError({ status: 500, title: "The app hit an error", detail: "The store is locked." });

describe("pages show a failed load instead of an empty state or a skeleton", () => {
  beforeEach(() => vi.clearAllMocks());

  it("Results: an error state with a retry that loads again", async () => {
    vi.mocked(api.experiments).mockRejectedValueOnce(boom()).mockResolvedValue([]);
    render(<Results />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not read the experiment results"));
    expect(screen.queryByText("No experiment runs yet")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByText("No experiment runs yet")).toBeInTheDocument());
    expect(api.experiments).toHaveBeenCalledTimes(2);
  });

  it("Activity log: shows why the list is missing", async () => {
    vi.mocked(api.listJobs).mockRejectedValue(boom());
    render(<Logs />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not read the activity log"));
  });

  it("Settings: leaves the skeleton with an error and a retry", async () => {
    vi.mocked(api.getSettings).mockRejectedValue(boom());
    render(<ToastProvider><Settings /></ToastProvider>);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not read the settings"));
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("Overview: says how many summaries failed", async () => {
    vi.mocked(api.storeSummary).mockRejectedValue(boom());
    vi.mocked(api.codeSummary).mockRejectedValue(boom());
    vi.mocked(api.builds).mockResolvedValue({ builds: [], next_version: "v1" } as any);
    vi.mocked(api.experiments).mockResolvedValue([]);
    vi.mocked(api.mix).mockResolvedValue({ scope: "store", total: 0, language: {}, ecosystem: {}, severity: {} });
    vi.mocked(api.listJobs).mockResolvedValue([]);
    vi.mocked(api.lifecycle).mockResolvedValue({ clean: true, note: "", at: null });
    vi.mocked(api.device).mockResolvedValue({ cpu: { physical_cores: 1, logical_cores: 1, workers: 1 }, ram: { total_gb: 1, free_gb: 1 }, disks: [], gpu: { available: false, reason: "none", cards: [] } });
    render(<MemoryRouter><Overview go={() => undefined} /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("2 of 4 summaries could not be loaded"));
  });
});
