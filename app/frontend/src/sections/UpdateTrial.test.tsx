import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://localhost:1/ws", api: { projectUpdateTrial: vi.fn(), trialProjectUpdate: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { ApiError, api, type ProjectUpdate, type UpdateTrial } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { CONFIRM, TrialView, UpdateTrialPanel } from "./UpdateTrial";

const u = { id: "20261009-200000-ab12", project_id: "shop-ab12cd", status: "candidate" } as ProjectUpdate;
const trial = (over: Partial<UpdateTrial> = {}): UpdateTrial => ({ schema: "update-trial/1", id: "t1", update_id: u.id, project_id: u.project_id, started: "2026-10-10T12:00:00Z", finished: "2026-10-10T12:09:00Z", state: "done",
  files: ["requirements.txt"], not_installed: ["package.json"], python: "3.11", platform: "Linux x86-64 (manylinux)",
  prefetch: { before: { ok: true, files: ["a.whl", "b.whl"], bytes: 2048000, note: "" }, after: { ok: true, files: ["a.whl", "b.whl", "c.whl"], bytes: 3072000, note: "" } },
  before: { label: "before", install: { state: "completed", exit_code: 0, evidence: "e1", note: "" }, checks: [], findings: [] }, after: { label: "after", install: { state: "completed", exit_code: 0, evidence: "e2", note: "" }, checks: [], findings: [] },
  comparison: { tests: { outcome: "regression", words: "2 tests fail after the update against 1 before", before: { note: "3 tests: 2 passed, 1 failed" }, after: { note: "3 tests: 1 passed, 2 failed" } },
    findings: { new: [{ id: "n1", file: "app.py", line: 3, rule: "r", message: "A new thing", state: "inconclusive", severity: "high" }], gone: [{ id: "g1", file: "old.py", line: null, rule: "r", message: "An old thing", state: "inconclusive", severity: "low" }], unchanged: 4 },
    checks_changed: [{ tool: "bandit", before: "ran", after: "env_error" }] },
  note: "", words: "With the updated packages in a closed container: 2 tests fail after the update against 1 before; 1 new findings, 1 gone, 4 unchanged. This is what the tests and the tools said; it does not say that the update is safe or correct, and nothing in the project has changed.", ...over });

const show = () => render(<ConfirmProvider><UpdateTrialPanel u={u} /></ConfirmProvider>);
beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.projectUpdateTrial).mockRejectedValue(new ApiError({ status: 404, title: "This update has not been tried in the sandbox." }));
  vi.mocked(api.trialProjectUpdate).mockResolvedValue({ id: "j1", stage: "trial_update" });
});

describe("trying an update in the sandbox", () => {
  it("says it has not been tried, and asks first because it uses the network on this computer", async () => {
    show();
    expect(await screen.findByText("This update has not been tried in the sandbox.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try in the sandbox" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("downloads the packages named in the update's requirements files from PyPI to this computer, as wheels only: nothing from them is run here");
    expect(dialog).toHaveTextContent("Nothing in your project changes");
    expect(api.trialProjectUpdate).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.trialProjectUpdate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Try in the sandbox" }));
    fireEvent.click(await within(await screen.findByRole("alertdialog")).findByRole("button", { name: "Try in the sandbox" }));
    await waitFor(() => expect(api.trialProjectUpdate).toHaveBeenCalledWith("shop-ab12cd", u.id));
    expect(CONFIRM).toContain("It needs Docker and the tool image");
  });

  it("shows the result: the packages fetched, the installs, the tests before and after, and the findings that came and went", async () => {
    vi.mocked(api.projectUpdateTrial).mockResolvedValue(trial());
    show();
    const view = await screen.findByLabelText("The result of the trial");
    expect(view).toHaveTextContent("Tried");
    expect(view).toHaveTextContent("Python 3.11, Linux x86-64 (manylinux)");
    expect(within(view).getByRole("list", { name: "Packages fetched" })).toHaveTextContent("before: 2 wheels, 2,000 KB");
    expect(view).toHaveTextContent("Not installed (only requirements files are, so far): package.json.");
    expect(view).toHaveTextContent("more tests fail with the updated packages");
    expect(view).toHaveTextContent("Before: 3 tests: 2 passed, 1 failed. After: 3 tests: 1 passed, 2 failed.");
    expect(view).toHaveTextContent("1 new, 1 gone, 4 unchanged");
    expect(within(view).getByRole("list", { name: "New with the updated packages" })).toHaveTextContent("app.py:3 A new thing");
    expect(within(view).getByRole("list", { name: "Gone with the updated packages" })).toHaveTextContent("old.py An old thing");
    expect(within(view).getByRole("list", { name: "Checks that ended differently" })).toHaveTextContent("bandit: ran before, env_error after");
    expect(view.textContent).toContain("it does not say that the update is safe or correct");
  });

  it("says why a trial stopped: a package without a wheel, an install that failed", () => {
    const { rerender } = render(<TrialView t={trial({ state: "prefetch_failed", comparison: null, before: null, after: null, words: "The after packages could not be fetched as wheels: No matching distribution found for flask==9.9.9",
      prefetch: { before: { ok: true, files: ["a.whl"], bytes: 10, note: "" }, after: { ok: false, files: [], bytes: 0, note: "No matching distribution found for flask==9.9.9" } } })} />);
    expect(screen.getByLabelText("The result of the trial")).toHaveTextContent("Stopped: a package could not be fetched as a wheel");
    expect(screen.getByRole("list", { name: "Packages fetched" })).toHaveTextContent("after: 0 wheels, 0 KB - No matching distribution found for flask==9.9.9");
    rerender(<TrialView t={trial({ state: "install_failed", comparison: null, after: { label: "after", install: { state: "failed", exit_code: 1, evidence: "e", note: "Could not find a version that satisfies werkzeug" }, checks: [], findings: [], skipped: "x" } })} />);
    expect(screen.getByLabelText("The result of the trial")).toHaveTextContent("Stopped: the packages could not be installed");
    expect(screen.getByLabelText("The result of the trial")).toHaveTextContent("Install of the updated packages: failed. Could not find a version that satisfies werkzeug");
  });

  it("reports a result that cannot be read", async () => {
    vi.mocked(api.projectUpdateTrial).mockRejectedValue(new ApiError({ status: 500, title: "The result could not be read." }));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("The result could not be read.");
  });
});
