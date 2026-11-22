import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { agentApplies: vi.fn(), agentApproveAll: vi.fn(), agentDiscardChanges: vi.fn(), agentRefresh: vi.fn(), agentApplyRevert: vi.fn(), agentCheckpoints: vi.fn(), agentRollback: vi.fn() } };
});
import { api, type AgentApply, type AgentDiffFile } from "../../lib/api";
import { ConfirmProvider } from "../hud/AlertDialog";
import { ChangesPanel, planOf } from "./ChangesPanel";

const file = (rel: string, kind: AgentDiffFile["kind"], over: Partial<AgentDiffFile> = {}): AgentDiffFile => ({ file: rel, folder: "/real/proj", rel, label: "proj", kind,
  sha256_before: kind === "added" ? null : "aa", sha256_after: kind === "deleted" ? null : "bb", diff: kind === "deleted" ? "" : `--- a/${rel}\n+++ b/${rel}\n@@ -1 +1 @@\n-old\n+new\n`, drift: false,
  ...(kind === "deleted" ? { can_delete: true, note: "Removed in the sandbox copy; it is removed from your project only if you tick it." } : {}), ...over });
const applied = (n: number, deleted: string[] = []): AgentApply => ({ schema: "agent_apply/1", id: "20261017-000000-aaaa", project_id: "p1", created: "2026-10-17T00:00:00Z", status: "applied",
  applied_at: "2026-10-17T00:00:01Z", files: Array.from({ length: n }, (_, i) => file(`f${i}.py`, "changed")), deleted, not_applied: [], words: "" });
const show = (files: AgentDiffFile[], over: Partial<React.ComponentProps<typeof ChangesPanel>> = {}) => {
  const onChanged = vi.fn();
  render(<ConfirmProvider><ChangesPanel projectId="p1" files={files} busy={false} onChanged={onChanged} {...over} /></ConfirmProvider>);
  return { onChanged };
};

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.agentApplies).mockResolvedValue({ applies: [] });
  vi.mocked(api.agentCheckpoints).mockResolvedValue({ checkpoints: [] });
});

describe("planOf", () => {
  it("counts what would be written, the removals that were ticked, and the files that block an apply", () => {
    const files = [file("a.py", "changed"), file("b.py", "added", { drift: true }), file("c.py", "deleted"), file("d.py", "deleted", { can_delete: false, drift: true }), file("k.env", "added", { sha256_after: null })];
    const plan = planOf(files, new Set(["c.py", "d.py"]));
    expect(plan.write.map((f) => f.rel)).toEqual(["a.py", "b.py"]);             // the kept-back file has no content to write
    expect(plan.remove.map((f) => f.rel)).toEqual(["c.py"]);                    // a removal that is not allowed is never counted, even when ticked
    expect(plan.blocked.map((f) => f.rel)).toEqual(["b.py"]);                   // a drifted removal does not block, a drifted write does
  });
});

describe("planOf with files left out", () => {
  it("leaves out what was unticked, counts it, and lets a drifted file that was left out stop nothing", () => {
    const files = [file("a.py", "changed"), file("b.py", "added", { drift: true }), file("c.py", "added")];
    const plan = planOf(files, new Set(), new Set(["b.py", "c.py"]));
    expect(plan.write.map((f) => f.rel)).toEqual(["a.py"]);
    expect(plan.left).toBe(2);
    expect(plan.blocked).toEqual([]);
    expect(planOf(files, new Set()).left).toBe(0);
  });
});

describe("ChangesPanel leaving files out", () => {
  it("applies only the files that stay ticked, sends the ones left out, and says what stays in the sandbox", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentApproveAll).mockResolvedValue(applied(1));
    show([file("calc.py", "changed"), file("README.md", "added"), file("ci.yml", "added")]);
    await user.click(screen.getByRole("checkbox", { name: "Apply README.md" }));
    await user.click(screen.getByRole("checkbox", { name: "Apply ci.yml" }));
    expect(screen.getByText("2 files left out: they stay in the sandbox and are not applied.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve and apply all" }));
    expect(await screen.findByText(/2 files you left out stay in the sandbox/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply 1 change" }));
    await waitFor(() => expect(api.agentApproveAll).toHaveBeenCalledWith("p1", [], [{ file: "calc.py", sha256_after: "bb" }], ["README.md", "ci.yml"]));
  });

  it("sends no list of left-out files when nothing was left out, and every file starts ticked", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentApproveAll).mockResolvedValue(applied(2));
    show([file("a.py", "changed"), file("b.py", "added")]);
    expect((screen.getByRole("checkbox", { name: "Apply a.py" }) as HTMLInputElement).checked).toBe(true);
    await user.click(screen.getByRole("button", { name: "Approve and apply all" }));
    await user.click(await screen.findByRole("button", { name: "Apply 2 changes" }));
    await waitFor(() => expect(api.agentApproveAll).toHaveBeenCalledWith("p1", [], [{ file: "a.py", sha256_after: "bb" }, { file: "b.py", sha256_after: "bb" }], []));
  });

  it("does not offer to leave out a removal or a file with no content, and a drifted file that is left out no longer blocks", async () => {
    const user = userEvent.setup();
    show([file("a.py", "changed"), file("gone.py", "deleted"), file("k.env", "added", { sha256_after: null }), file("n.py", "added", { drift: true })]);
    expect(screen.queryByRole("checkbox", { name: "Apply gone.py" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Apply k.env" })).toBeNull();
    expect(screen.getByRole("button", { name: "Approve and apply all" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Apply n.py" }));
    expect(screen.getByRole("button", { name: "Approve and apply all" })).toBeEnabled();
  });

  it("disables the button when every file is left out", async () => {
    const user = userEvent.setup();
    show([file("a.py", "changed")]);
    await user.click(screen.getByRole("checkbox", { name: "Apply a.py" }));
    expect(screen.getByRole("button", { name: "Approve and apply all" })).toBeDisabled();
  });
});

describe("ChangesPanel", () => {
  it("lists each file with its kind and says what a drifted file needs", () => {
    show([file("a.py", "changed"), file("n.py", "added", { drift: true })]);
    expect(screen.getByText("a.py")).toBeInTheDocument();
    expect(screen.getByText("changed")).toBeInTheDocument();
    expect(screen.getByText("Changed in your project since the copy. Refresh from project first.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("1 file changed in your project after the copy, so nothing can be applied until you refresh from project.");
    expect(screen.getByRole("button", { name: "Approve and apply all" })).toBeDisabled();
  });

  it("opens the diff of a file in a window", async () => {
    const user = userEvent.setup();
    show([file("a.py", "changed")]);
    await user.click(screen.getByRole("button", { name: "a.py", description: "Show the change to a.py" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("region", { name: "Changes to a.py" })).toBeInTheDocument();
  });

  it("asks before applying, applies everything in one call, and offers Undo afterwards", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentApproveAll).mockResolvedValue(applied(2));
    const { onChanged } = show([file("a.py", "changed"), file("b.py", "added")]);
    await user.click(screen.getByRole("button", { name: "Approve and apply all" }));
    expect(await screen.findByText(/This writes 2 files into your real project/)).toBeInTheDocument();
    expect(api.agentApproveAll).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Apply 2 changes" }));
    await waitFor(() => expect(api.agentApproveAll).toHaveBeenCalledWith("p1", [], [{ file: "a.py", sha256_after: "bb" }, { file: "b.py", sha256_after: "bb" }], []));
    expect(await screen.findByText(/Applied 2 files to your project/)).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Undo last apply" })).toBeInTheDocument();
  });

  it("sends only the removals that were ticked", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentApproveAll).mockResolvedValue(applied(1, ["old.py"]));
    show([file("a.py", "changed"), file("old.py", "deleted"), file("other.py", "deleted")]);
    await user.click(screen.getAllByRole("checkbox", { name: "Also remove it from my project" })[0]);
    await user.click(screen.getByRole("button", { name: "Approve and apply all" }));
    expect(await screen.findByText(/and removes 1 file you ticked/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply 2 changes" }));
    await waitFor(() => expect(api.agentApproveAll).toHaveBeenCalledWith("p1", ["old.py"], [{ file: "a.py", sha256_after: "bb" }], []));
  });

  it("does not apply when the question is declined", async () => {
    const user = userEvent.setup();
    show([file("a.py", "changed")]);
    await user.click(screen.getByRole("button", { name: "Approve and apply all" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(api.agentApproveAll).not.toHaveBeenCalled();
  });

  it("says why nothing was written when the apply is refused", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentApproveAll).mockRejectedValue(new Error("Nothing was written. Your project changed a.py after the copy."));
    show([file("a.py", "changed")]);
    await user.click(screen.getByRole("button", { name: "Approve and apply all" }));
    await user.click(await screen.findByRole("button", { name: "Apply 1 change" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Nothing was written. Your project changed a.py after the copy.");
  });

  it("refreshes from the project and names the files both sides changed", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentRefresh).mockResolvedValue({ updated: ["notes.txt"], conflicts: ["src/util.py"], summary: "Brought 1 file from your project into the sandbox copy; 1 changed on both sides and were left as the session has them." });
    const { onChanged } = show([file("a.py", "changed")]);
    await user.click(screen.getByRole("button", { name: "Refresh from project" }));
    expect(await screen.findByText(/Brought 1 file from your project/)).toBeInTheDocument();
    expect(screen.getByText(/left as the session has them: src\/util.py/)).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it("discards only after a yes", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentDiscardChanges).mockResolvedValue({ roots: { "/work/proj": "abc1234" }, summary: "The sandbox copy is back to the project as it was when it was copied." });
    show([file("a.py", "changed")]);
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(await screen.findByText(/Your real project is not touched/)).toBeInTheDocument();
    expect(api.agentDiscardChanges).not.toHaveBeenCalled();
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(api.agentDiscardChanges).toHaveBeenCalledWith("p1"));
    expect(await screen.findByText("The sandbox copy is back to the project as it was when it was copied.")).toBeInTheDocument();
  });

  it("undoes the last apply that is still applied", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentApplies).mockResolvedValue({ applies: [{ id: "20261017-000000-aaaa", created: "2026-10-17T00:00:00Z", status: "applied", files: 1 }] });
    vi.mocked(api.agentApplyRevert).mockResolvedValue({ ...applied(1), status: "reverted" });
    show([file("a.py", "changed")]);
    await user.click(await screen.findByRole("button", { name: "Undo last apply" }));
    await user.click(await screen.findByRole("button", { name: "Undo the apply" }));
    await waitFor(() => expect(api.agentApplyRevert).toHaveBeenCalledWith("p1", "20261017-000000-aaaa"));
    expect(await screen.findByText("Put your project back as it was before the apply.")).toBeInTheDocument();
  });

  it("locks the actions while a run is in progress", () => {
    show([file("a.py", "changed")], { busy: true });
    for (const name of ["Approve and apply all", "Refresh from project", "Discard changes"]) expect(screen.getByRole("button", { name })).toBeDisabled();
  });

  it("lists the checkpoints when asked and rolls one back after a warning", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentCheckpoints).mockResolvedValue({ checkpoints: [{ root: "/work/proj", hash: "abc1234", at: 1, message: "Before refreshing from the project" }] });
    vi.mocked(api.agentRollback).mockResolvedValue({ root: "/work/proj", hash: "abc1234" });
    show([file("a.py", "changed")]);
    await user.click(screen.getByText("Checkpoints"));
    expect(await screen.findByText("Before refreshing from the project")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Roll back" }));
    expect(await screen.findByText(/Everything written after it is lost/)).toBeInTheDocument();
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(api.agentRollback).toHaveBeenCalledWith("p1", "/work/proj", "abc1234"));
  });
});
