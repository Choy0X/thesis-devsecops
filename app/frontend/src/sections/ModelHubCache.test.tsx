import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { localModels: vi.fn(), partialDownloads: vi.fn(), discardPartial: vi.fn(), removalPlan: vi.fn(), removeModel: vi.fn(), downloadPlan: vi.fn(), servingModels: vi.fn(),
    modelFit: vi.fn(), listJobs: vi.fn(), cancelJob: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), getSettings: vi.fn(), putSettings: vi.fn() } };
});
vi.mock("../components/DevicePanel", () => ({ DevicePanel: () => null }));
import { ApiError, api, type DownloadPlan, type Job, type LocalModels, type RemovalPlan } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { DevModeProvider } from "../lib/devMode";
import { JobsTray } from "../components/JobsTray";
import { resetJobsList } from "../lib/jobsList";
import { ToastProvider } from "../components/Toast";
import { FitDialog, HubDownload, InstalledModels } from "./ModelHub";

const GB = 1024 ** 3;
const wrap = (ui: React.ReactNode) => render(<MemoryRouter><DevModeProvider initial={false}><ToastProvider><ConfirmProvider>{ui}</ConfirmProvider></ToastProvider></DevModeProvider></MemoryRouter>);
const inventory: LocalModels = { hub_cache: "C:/cache", hub_cache_exists: true, models_dir: null, models_dir_exists: false, total_bytes: 3 * GB, models: [
  { id: "org/small", source: "hub_cache", path: "C:/cache/models--org--small", bytes: 3 * GB, revisions: 1, main_revision: "a".repeat(40), safetensors: true, gguf: [], weights: true, architecture: null, parameters: null } as never] };
const removal = (over: Partial<RemovalPlan> = {}): RemovalPlan => ({ repo_id: "org/small", bytes: 3 * GB, partial_bytes: 0, revisions: 1, path: "C:/cache/models--org--small", in_use: null, ...over });
const plan = (over: Partial<DownloadPlan> = {}): DownloadPlan => ({ repo_id: "org/m", revision: "b".repeat(40), gated: false, token_set: true, partial_bytes: 0, quants: [], needs_quant: false, files: 3, bytes: 2 * GB, free_bytes: 50 * GB, enough: true, ...over });

beforeEach(() => {
  resetJobsList();
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "" } as any);
  vi.mocked(api.servingModels).mockResolvedValue({ files: [], not_ready: [] });
  vi.mocked(api.localModels).mockResolvedValue(inventory);
  vi.mocked(api.partialDownloads).mockResolvedValue({ partial: [] });
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.modelFit).mockRejectedValue(new Error("no estimate in this test"));
});

describe("downloads that stopped", () => {
  it("are listed with the bytes already fetched, continue by opening the model's download, and are discarded only after a question that says what goes", async () => {
    vi.mocked(api.partialDownloads).mockResolvedValue({ partial: [{ repo_id: "org/half", partial_bytes: 1.5 * GB, partial_files: 2, complete_bytes: 10 }] });
    vi.mocked(api.discardPartial).mockResolvedValue({ repo_id: "org/half", freed_bytes: 1.5 * GB, files: 2 });
    wrap(<InstalledModels />);
    const list = await screen.findByRole("list", { name: "Downloads that stopped" });
    expect(list).toHaveTextContent("org/half");
    expect(list).toHaveTextContent("1.5 GB fetched in 2 unfinished files");
    fireEvent.click(screen.getByRole("button", { name: "Discard the part of org/half" }));
    expect(await screen.findByText(/Deletes the 2 unfinished files \(1.5 GB\) of this model\. Files of it that finished are kept/)).toBeInTheDocument();
    expect(api.discardPartial).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Discard 1.5 GB" }));
    await waitFor(() => expect(api.discardPartial).toHaveBeenCalledWith("org/half"));
    vi.mocked(api.downloadPlan).mockResolvedValue(plan({ repo_id: "org/half", partial_bytes: 1.5 * GB }));
    fireEvent.click(screen.getByRole("button", { name: "Continue downloading org/half" }));
    expect(await screen.findByRole("button", { name: "Continue the download" })).toBeInTheDocument();
    expect(screen.getByText(/1.5 GB of it was fetched before the download stopped; it continues from there/)).toBeInTheDocument();
  });

  it("are not listed when there are none", async () => {
    wrap(<InstalledModels />);
    await screen.findByText("1-1 of 1");
    expect(screen.getByText("3.0 GB in all")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Downloads that stopped" })).toBeNull();
  });
});

describe("a gated model", () => {
  it("cannot be downloaded until a token is set, and says why", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(plan({ gated: true, token_set: false }));
    wrap(<HubDownload repoId="org/m" wontFit={false} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("gated and no Hugging Face token is set");
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("can be downloaded with a token", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue(plan({ gated: true, token_set: true }));
    wrap(<HubDownload repoId="org/m" wontFit={false} />);
    await screen.findByText(/3 files, 2.0 GB/);
    expect(screen.getByRole("button", { name: "Download" })).toBeEnabled();
  });
});

describe("removing a downloaded model", () => {
  const open = async () => {
    wrap(<InstalledModels />);
    fireEvent.click(await screen.findByText("org/small"));
    return screen.findByRole("button", { name: "Remove from this computer" });
  };

  it("asks first, saying how much is deleted and that it can be downloaded again, then removes and reloads the list", async () => {
    vi.mocked(api.removalPlan).mockResolvedValue(removal());
    vi.mocked(api.removeModel).mockResolvedValue({ repo_id: "org/small", freed_bytes: 3 * GB });
    fireEvent.click(await open());
    expect(await screen.findByText(/Deletes 3.0 GB \(1 version\) from the model cache on this computer\. It can be downloaded again/)).toBeInTheDocument();
    expect(api.removeModel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove 3.0 GB" }));
    await waitFor(() => expect(api.removeModel).toHaveBeenCalledWith("org/small"));
    await waitFor(() => expect(vi.mocked(api.localModels).mock.calls.length).toBeGreaterThan(1));
  });

  it("deletes nothing when the question is cancelled", async () => {
    vi.mocked(api.removalPlan).mockResolvedValue(removal());
    fireEvent.click(await open());
    await screen.findByText(/Deletes 3.0 GB/);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText(/Deletes 3.0 GB/)).toBeNull());
    expect(api.removeModel).not.toHaveBeenCalled();
  });

  it("does not even ask while something is using the model, and says what", async () => {
    vi.mocked(api.removalPlan).mockResolvedValue(removal({ in_use: "The local model server is using this model. Stop it on the Serving tab first." }));
    fireEvent.click(await open());
    expect(await screen.findByText(/The local model server is using this model/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove 3.0 GB/ })).toBeNull();
    expect(api.removeModel).not.toHaveBeenCalled();
  });

  it("shows the refusal of the server in words", async () => {
    vi.mocked(api.removalPlan).mockResolvedValue(removal());
    vi.mocked(api.removeModel).mockRejectedValue(new ApiError({ status: 409, title: "A download of this model is running; stop it first." }));
    fireEvent.click(await open());
    fireEvent.click(await screen.findByRole("button", { name: "Remove 3.0 GB" }));
    expect(await screen.findByText(/A download of this model is running/)).toBeInTheDocument();
  });

  it("is not offered for a model that is not in the Hugging Face cache", () => {
    wrap(<FitDialog model={{ id: "mine", source: "folder" }} onClose={() => undefined} onChanged={() => undefined} />);
    expect(screen.queryByRole("button", { name: "Remove from this computer" })).toBeNull();
  });
});

describe("a download in the Jobs tray", () => {
  const download = (over: Partial<Job> = {}): Job => ({ id: "d1", stage: "hf_download", status: "running", params: { repo_id: "org/m" }, started: new Date(Date.now() - 30_000).toISOString(), finished: "", error: "", log_count: 0,
    progress: { state: "downloading", label: "Downloading org/m", processed: 1.2 * GB, total: 3.4 * GB }, ...over });

  it("shows sizes instead of byte counts and a stop that keeps what was downloaded", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([download()]);
    vi.mocked(api.cancelJob).mockResolvedValue({ ok: true } as never);
    render(<MemoryRouter><DevModeProvider initial={false}><ToastProvider><JobsTray /></ToastProvider></DevModeProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Downloading org/m · 1.2 GB of 3.4 GB")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop the download" }));
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith("d1"));
  });

  it("gives a job that is not a download its own numbers and no stop", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([download({ id: "m1", stage: "mine_code", progress: { label: "Mining commits", processed: 40, total: 100 } })]);
    render(<MemoryRouter><DevModeProvider initial={false}><ToastProvider><JobsTray /></ToastProvider></DevModeProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Mining commits · 40 of 100")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop the download" })).toBeNull();
  });
});
