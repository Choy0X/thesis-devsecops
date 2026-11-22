import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { projectUpdates: vi.fn(), projectUpdate: vi.fn(), planProjectUpdate: vi.fn(), applyProjectUpdate: vi.fn(), revertProjectUpdate: vi.fn(),
    discardProjectUpdate: vi.fn(), projectUpdateTrial: vi.fn(), trialProjectUpdate: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { ApiError, api, type Project, type ProjectUpdate } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { ProjectUpdates, STATUS_WORDS } from "./ProjectUpdate";

const project = { id: "shop-ab12cd", name: "The shop", folders: [], languages: {}, ecosystems: [], created_at: "x", updated_at: "x", authorization: { acknowledged: true, at: "x", statement: "s" }, last_scan: null, instructions: "" } as Project;
const DIFF = "--- a/package.json\n+++ b/package.json\n@@ -3 +3 @@\n-    \"lodash\": \"^4.17.15\"\n+    \"lodash\": \"^4.17.21\"\n";
const update = (over: Partial<ProjectUpdate> = {}): ProjectUpdate => ({
  schema: "project-update/1", id: "20261009-200000-ab12", project_id: "shop-ab12cd", created: "2026-10-09T20:00:00Z", mode: "security", mode_words: "Security fixes only", from_scan: "s1", status: "candidate",
  words: "This is a candidate for you to review, made on a copy of the manifests. Nothing in the project has changed. No package was installed and no test was run, so it does not say that the project still works with the new versions.",
  plan: { mode: "security", changes: [
    { ecosystem: "npm", name: "lodash", from: "4.17.15", to: "4.17.21", kind: "fix", bump: "patch", folder: "C:/code/shop", advisories: ["GHSA-lodash"], files: ["package.json"], locks: ["package-lock.json"] },
    { ecosystem: "PyPI", name: "flask", from: "2.0.1", to: "3.0.0", kind: "fix", bump: "major", folder: "C:/code/shop", advisories: ["GHSA-flask"], files: ["requirements.txt"], locks: [] }],
    not_updated: [{ ecosystem: "npm", name: "left-pad", version: "1.3.0", reason: "an advisory affects it but no fixed release is known: replace the package or mitigate" }] },
  files: [{ file: "package.json", folder: "C:/code/shop", rel: "package.json", label: "", sha256_before: "a", sha256_after: "b", diff: DIFF, edits: [{ name: "lodash", line: 3, before: "x", after: "y" }] },
    { file: "requirements.txt", folder: "C:/code/shop", rel: "requirements.txt", label: "", sha256_before: "c", sha256_after: "d", diff: "--- a/requirements.txt\n", edits: [{ name: "flask", line: 2, before: "x", after: "y" }] }],
  skipped: [{ file: "pyproject.toml", reason: "click: only an upper bound or no version is written, so there is nothing to move" }], locks_to_regenerate: ["package-lock.json"],
  predicted: { packages: [], advisories_fixed: ["GHSA-lodash", "GHSA-flask"], advisories_remaining: [], advisories_introduced: [], words: "By advisory matching against the advisories on this computer, for the changed packages only." },
  ...over });
const item = (u: ProjectUpdate) => ({ id: u.id, created: u.created, mode: u.mode, mode_words: u.mode_words, status: u.status, changes: u.plan.changes.length, files: u.files.length });
const show = () => render(<ToastProvider><ConfirmProvider><ProjectUpdates project={project} /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.projectUpdateTrial).mockRejectedValue(new ApiError({ status: 404, title: "This update has not been tried in the sandbox." }));
  const u = update();
  vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [item(u)] });
  vi.mocked(api.projectUpdate).mockResolvedValue(u);
});

describe("ProjectUpdates", () => {
  it("says no update has been planned and offers to plan one", async () => {
    vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [] });
    show();
    expect(await screen.findByText("No update has been planned for this project yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan an update" })).toBeEnabled();
  });

  it("shows the candidate: what it is, each change with the size of the step, what was left alone and why, and the lock files not edited", async () => {
    show();
    expect(await screen.findByText(STATUS_WORDS.candidate)).toBeInTheDocument();
    expect(screen.getByText(/does not say that the project still works with the new versions/)).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Planned changes" });
    const rows = within(table).getAllByRole("row");
    expect(rows.find((r) => r.textContent?.startsWith("lodash"))).toHaveTextContent("4.17.154.17.21patchfixes GHSA-lodash");
    expect(rows.find((r) => r.textContent?.startsWith("flask"))).toHaveTextContent("major: may break the code that uses it");
    fireEvent.click(screen.getByText(/1 not updated, and why/));
    expect(screen.getByText(/no fixed release is known: replace the package or mitigate/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Left as they are" })).toHaveTextContent("pyproject.toml");
    expect(screen.getByRole("status")).toHaveTextContent("Not edited, because only your package manager can write them: package-lock.json. Regenerate it after applying.");
  });

  it("says what the scan would say afterwards and that this is advisory matching only", async () => {
    show();
    const box = await screen.findByLabelText("What the scan would say afterwards");
    expect(box).toHaveTextContent("2 advisories no longer affect the changed packages.");
    expect(box).toHaveTextContent("for the changed packages only");
  });

  it("shows the diff of each file on request", async () => {
    show();
    const files = await screen.findByLabelText("The changes to the files");
    fireEvent.click(within(files).getByText("package.json"));
    expect(files).toHaveTextContent('"lodash": "^4.17.21"');
  });

  it("writes the files only after a confirmation that lists them, and shows the result", async () => {
    const done = update({ status: "applied", applied_at: "2026-10-09T20:05:00Z" });
    vi.mocked(api.applyProjectUpdate).mockImplementation(async () => { vi.mocked(api.projectUpdate).mockResolvedValue(done); vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [item(done)] }); return done; });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Apply to the project's files" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Write 2 files into The shop?");
    expect(within(dialog).getByLabelText("Files that will be written")).toHaveTextContent("package.jsonrequirements.txt");
    expect(dialog).toHaveTextContent("it does not say that the project works with the new versions");
    fireEvent.click(within(dialog).getByRole("button", { name: "Not now" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.applyProjectUpdate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Apply to the project's files" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Write these files" }));
    await waitFor(() => expect(api.applyProjectUpdate).toHaveBeenCalledWith("shop-ab12cd", "20261009-200000-ab12", ["package.json", "requirements.txt"]));
    expect(await screen.findByText(STATUS_WORDS.applied)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Apply to the project's files" })).toBeNull();
  });

  it("says why a write was refused and leaves the candidate as it is", async () => {
    vi.mocked(api.applyProjectUpdate).mockRejectedValue(new ApiError({ status: 400, title: "Not allowed", detail: "Nothing was written. package.json has changed since the update was planned." }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Apply to the project's files" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Write these files" }));
    await waitFor(() => expect(api.applyProjectUpdate).toHaveBeenCalled());
    expect(screen.getByText(STATUS_WORDS.candidate)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply to the project's files" })).toBeEnabled();
  });

  it("undoes an applied update after a confirmation", async () => {
    const applied = update({ status: "applied", applied_at: "2026-10-09T20:05:00Z" });
    vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [item(applied)] });
    vi.mocked(api.projectUpdate).mockResolvedValue(applied);
    const back = update({ status: "reverted" });
    vi.mocked(api.revertProjectUpdate).mockImplementation(async () => { vi.mocked(api.projectUpdate).mockResolvedValue(back); vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [item(back)] }); return back; });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("if they are still exactly as this update wrote them");
    fireEvent.click(within(dialog).getByRole("button", { name: "Put the originals back" }));
    await waitFor(() => expect(api.revertProjectUpdate).toHaveBeenCalledWith("shop-ab12cd", "20261009-200000-ab12"));
    expect(await screen.findByText(STATUS_WORDS.reverted)).toBeInTheDocument();
  });

  it("discards a candidate after a confirmation that leaves the project alone", async () => {
    vi.mocked(api.discardProjectUpdate).mockResolvedValue({ ok: true });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Nothing in your project is touched");
    vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [] });
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard it" }));
    await waitFor(() => expect(api.discardProjectUpdate).toHaveBeenCalledWith("shop-ab12cd", "20261009-200000-ab12"));
    expect(await screen.findByText("No update has been planned for this project yet.")).toBeInTheDocument();
  });

  it("lets the person pick an earlier update and reports a failure to read them", async () => {
    const a = update({ id: "20261009-210000-ffff", mode: "all", mode_words: "Everything that is outdated" });
    vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [item(a), item(update())] });
    vi.mocked(api.projectUpdate).mockImplementation(async (_id, uid) => (uid === a.id ? a : update()));
    show();
    const pick = await screen.findByRole("combobox", { name: "Update" });
    expect(pick).toHaveTextContent("everything that is outdated");
    await userEvent.setup().click(pick);
    const options = await screen.findAllByRole("option");
    expect(options).toHaveLength(2);
    await userEvent.setup().click(options[1]);
    await waitFor(() => expect(api.projectUpdate).toHaveBeenLastCalledWith("shop-ab12cd", "20261009-200000-ab12"));
  });

  it("reports an update list that could not be read", async () => {
    vi.mocked(api.projectUpdates).mockRejectedValue(new Error("offline"));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  });
});
