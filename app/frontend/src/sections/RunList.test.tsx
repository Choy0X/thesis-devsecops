import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { resetWorkspaceState } from "../lib/workspaceState";
vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { finetuneRuns: vi.fn(), finetuneRun: vi.fn(), previewRunDeletion: vi.fn(), deleteRuns: vi.fn(), getJob: vi.fn(), listJobs: vi.fn() } };
});
vi.mock("../lib/jobsList", () => ({ refreshJobs: vi.fn(), useRunningJobId: () => null }));
vi.mock("./TrainWizard", () => ({ TrainWizard: () => <div>Run setup</div> }));
vi.mock("./LocalTrain", () => ({ LocalTrain: () => null }));
vi.mock("../components/RunCurvesView", () => ({ RunCurvesView: () => null }));
vi.mock("../components/RunConsole", () => ({ RunConsole: () => null }));
vi.mock("../components/GuidedImport", () => ({ GuidedImport: () => null }));
vi.mock("./FullModelExport", () => ({ FullModelExport: () => null }));
import { api, type FinetuneRun } from "../lib/api";
import { FinetuneRuns, profileWords } from "./FinetuneRuns";
const run = (name: string): FinetuneRun => ({ name, state: "configured", dataset: { tag: "v1", manifest_sha256: "a", status: "frozen" }, model_revision: "b", stage: 1, blocked: [], bundle: { path: "p", sha256: "c", bytes: 100, files: 1, warnings: [] }, model: "org/model", purpose: "train", profile: null, overrides: 0 });
beforeEach(() => { vi.clearAllMocks(); resetWorkspaceState(); vi.mocked(api.finetuneRuns).mockImplementation(async p => ({ runs: [run(p?.offset ? "second-page" : "first-page")], total: 201, root: "", limit: 100, offset: p?.offset ?? 0 })); vi.mocked(api.deleteRuns).mockResolvedValue({ job_id: "delete-1" }); });
const show = () => render(<ConfirmProvider><FinetuneRuns /></ConfirmProvider>);
it("requests 100 rows per page and preserves individual selection across pages", async () => {
  show(); await screen.findByRole("button", { name: "first-page" });
  fireEvent.click(screen.getByLabelText("Select first-page"));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  await screen.findByRole("button", { name: "second-page" });
  expect(api.finetuneRuns).toHaveBeenLastCalledWith({ limit: 100, offset: 100, search: "", state: "" });
  expect(screen.getByRole("button", { name: "Delete selected (1)" })).toBeEnabled();
  expect(screen.getByText("101-200 of 201")).toBeInTheDocument();
});
it("previews all matching runs on the server and deletes only after confirmation", async () => {
  vi.mocked(api.previewRunDeletion).mockResolvedValue({ preview_token: "preview-1", names: ["first-page"], total: 201, bytes: 10, artifacts: [], dependencies: [], blocked: false });
  show(); await screen.findByRole("button", { name: "first-page" });
  fireEvent.click(screen.getByRole("button", { name: "Select all 201 matching runs" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete selected (201)" }));
  expect(await screen.findByRole("alertdialog")).toHaveTextContent('Selected runs: first-page');
  expect(api.previewRunDeletion).toHaveBeenCalledWith({ select_all: true, search: "", state: "" });
  expect(api.deleteRuns).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Delete runs" }));
  await waitFor(() => expect(api.deleteRuns).toHaveBeenCalledWith("preview-1"));
  expect(screen.getByRole("status")).toHaveTextContent("Deleting runs");
});
it("keeps profile names readable", () => { expect(profileWords({ profile: "higher_quality", overrides: 2 })).toBe("higher quality (2 settings changed)"); });

it("shows server deletion dependencies without asking to delete a blocked selection", async () => {
  vi.mocked(api.previewRunDeletion).mockResolvedValue({ preview_token: "blocked-preview", names: ["first-page"], total: 1, bytes: 10, artifacts: [], dependencies: [{ name: "first-page", reason: "Training job is running" }], blocked: true } as any);
  show(); await screen.findByRole("button", { name: "first-page" });
  fireEvent.click(screen.getByLabelText("Select first-page"));
  fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Training job is running");
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  expect(api.deleteRuns).not.toHaveBeenCalled();
});
it("renders details from a lightweight run catalog without bundle warnings", async () => {
  const lightweight = run("catalog-run"); delete (lightweight.bundle as any).warnings;
  vi.mocked(api.finetuneRuns).mockResolvedValue({ runs: [lightweight], total: 1, root: "", limit: 100, offset: 0 });
  vi.mocked(api.finetuneRun).mockResolvedValue({ name: "catalog-run", prepared: {}, cpu_validation: null, state: { state: "configured", blocked: [], evidence: {} }, imports: [], eval_imports: [], local_gpu: null });
  show(); fireEvent.click(await screen.findByRole("button", { name: "catalog-run" }));
  fireEvent.click(await screen.findByText("Downloads and exports"));
  expect(await screen.findByRole("button", { name: "Download bundle" })).toBeInTheDocument();
  expect(await screen.findByText("No evaluation metrics imported for this run.")).toBeInTheDocument();
});
