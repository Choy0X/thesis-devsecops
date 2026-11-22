import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { skills: vi.fn(), skillCreate: vi.fn(), skillGet: vi.fn(), skillApprove: vi.fn(), skillDelete: vi.fn() } };
});
import { api, type SkillSummary } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { Skills } from "./Skills";

const summary = (over: Partial<SkillSummary> = {}): SkillSummary =>
  ({ schema: "skill/1", name: "reviewer", description: "A code reviewer.", approved: false, created_at: "2026-10-15T00:00:00Z", prompt_sha256: "x", ...over });
const show = () => render(<ToastProvider><ConfirmProvider><Skills /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.skills).mockResolvedValue({ skills: [] });
});

describe("Skills", () => {
  it("says there is none yet when there is none", async () => {
    show();
    expect(await screen.findByText("No saved skills yet.")).toBeInTheDocument();
  });

  it("shows built-in project skills and filters the library by name and description", async () => {
    vi.mocked(api.skills).mockResolvedValue({ skills: [summary()], bundled: [{ name: "dependency-review", description: "Check public advisories." }] });
    show();
    expect(await screen.findByText("dependency-review")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search skills" }), { target: { value: "advisories" } });
    expect(screen.getByText("dependency-review")).toBeInTheDocument();
    expect(screen.queryByText("reviewer")).toBeNull();
  });

  it("lists every skill with its approval state, in the order the server gave them (approved first)", async () => {
    vi.mocked(api.skills).mockResolvedValue({ skills: [summary({ name: "beta", approved: true }), summary({ name: "alpha", approved: false })] });
    show();
    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText("beta")).toBeInTheDocument();
    expect(within(items[0]).getByText("Approved")).toBeInTheDocument();
    expect(within(items[1]).getByText("alpha")).toBeInTheDocument();
    expect(within(items[1]).getByText("Needs review")).toBeInTheDocument();
  });

  it("adding a skill clears the form, confirms it, and refreshes the list", async () => {
    vi.mocked(api.skillCreate).mockResolvedValue({ ...summary({ name: "new-one" }), prompt: "Do X." });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Add skill" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "new-one" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Does a thing." } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Do X." } });
    vi.mocked(api.skills).mockResolvedValue({ skills: [summary({ name: "new-one", description: "Does a thing." })] });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api.skillCreate).toHaveBeenCalledWith("new-one", "Does a thing.", "Do X."));
    expect(await screen.findByText("new-one")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("the Add button is disabled until all three fields have something in them", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Add skill" }));
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "d" } });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "p" } });
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
  });

  it("shows what create() refused, in words, without clearing the form", async () => {
    vi.mocked(api.skillCreate).mockRejectedValue(new Error('A skill named "x" already exists.'));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Add skill" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "d" } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "p" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText('A skill named "x" already exists.')).toBeInTheDocument();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("x");
  });

  it("Approve only shows for an unapproved skill, and disappears once it is approved", async () => {
    vi.mocked(api.skills).mockResolvedValue({ skills: [summary({ approved: false })] });
    vi.mocked(api.skillApprove).mockResolvedValue({ ...summary({ approved: true }), prompt: "p" });
    show();
    await screen.findByText("reviewer");
    const approveButton = screen.getByRole("button", { name: "Approve" });
    vi.mocked(api.skills).mockResolvedValue({ skills: [summary({ approved: true })] });
    fireEvent.click(approveButton);
    await waitFor(() => expect(api.skillApprove).toHaveBeenCalledWith("reviewer"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
    expect(await screen.findByText("Approved")).toBeInTheDocument();
  });

  it("Preview fetches and shows the full prompt, and Hide collapses it without a second fetch", async () => {
    vi.mocked(api.skills).mockResolvedValue({ skills: [summary()] });
    vi.mocked(api.skillGet).mockResolvedValue({ ...summary(), prompt: "Focus on security and injection." });
    show();
    await screen.findByText("reviewer");
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("Focus on security and injection.")).toBeInTheDocument();
    expect(api.skillGet).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByText("Focus on security and injection.")).toBeNull();
  });

  it("asks before removing, names the skill, and only removes it once confirmed", async () => {
    vi.mocked(api.skills).mockResolvedValue({ skills: [summary({ name: "doomed" })] });
    show();
    await screen.findByText("doomed");
    fireEvent.click(screen.getByRole("button", { name: "Remove doomed" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent('Remove "doomed"?');
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.skillDelete).not.toHaveBeenCalled();

    vi.mocked(api.skills).mockResolvedValue({ skills: [] });
    fireEvent.click(screen.getByRole("button", { name: "Remove doomed" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove it" }));
    await waitFor(() => expect(api.skillDelete).toHaveBeenCalledWith("doomed"));
    expect(await screen.findByText("No saved skills yet.")).toBeInTheDocument();
  });
});
