import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DependencyScanDialog } from "./DependencyScanDialog";

const show = (over: Partial<React.ComponentProps<typeof DependencyScanDialog>> = {}) => {
  const onConfirm = vi.fn(); const onCancel = vi.fn();
  render(<DependencyScanDialog busy={false} onConfirm={onConfirm} onCancel={onCancel} {...over} />);
  return { onConfirm, onCancel };
};

describe("DependencyScanDialog", () => {
  it("says what the scan reads and what it does not do, before asking", () => {
    show();
    const dialog = screen.getByRole("dialog", { name: "Scan the dependencies?" });
    const text = dialog.textContent ?? "";
    expect(text).toContain("manifests and lock files");
    expect(text).toContain("never reads the code of a dependency, installs nothing and changes nothing");
    expect(text).toContain("does not look up newer releases");
    expect(text).toContain("its own knowledge");
    expect(text).toContain("Without a lock file");
    expect(screen.getByRole("status", { hidden: true })).toHaveTextContent("Waiting for your answer about the dependency scan.");          // behind the dialog, so the page hides it from a screen reader while it is open
  });

  it("confirms without remembering by default", async () => {
    const user = userEvent.setup();
    const { onConfirm, onCancel } = show();
    expect((screen.getByRole("checkbox", { name: "Do not ask me again" }) as HTMLInputElement).checked).toBe(false);
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalledWith(false);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("confirms and says not to ask again when the box is ticked", async () => {
    const user = userEvent.setup();
    const { onConfirm } = show();
    await user.click(screen.getByRole("checkbox", { name: "Do not ask me again" }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalledWith(true);
  });

  it("cancels with the button and does not remember a Cancel even when the box is ticked", async () => {
    const user = userEvent.setup();
    const { onConfirm, onCancel } = show();
    await user.click(screen.getByRole("checkbox", { name: "Do not ask me again" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("takes Escape and the Close button as a Cancel", async () => {
    const user = userEvent.setup();
    const { onCancel } = show();
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("does nothing while an answer is being sent", async () => {
    const user = userEvent.setup();
    const { onConfirm, onCancel } = show({ busy: true });
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Confirm" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(within(dialog).queryByRole("button", { name: "Close" })).toBeNull();
    await user.keyboard("{Escape}");
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });
});
