import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ConfirmProvider, useConfirm, type ConfirmOptions } from "../hud/AlertDialog";
import { Dialog } from "../hud/Dialog";

function Asker({ options, onAnswer }: { options: ConfirmOptions; onAnswer: (ok: boolean) => void }) {
  const confirm = useConfirm();
  return <button onClick={async () => onAnswer(await confirm(options))}>ask</button>;
}
const open = async (options: ConfirmOptions) => {
  const onAnswer = vi.fn();
  const user = userEvent.setup();
  render(<ConfirmProvider><Asker options={options} onAnswer={onAnswer} /></ConfirmProvider>);
  await user.click(screen.getByText("ask"));
  await screen.findByRole("alertdialog");
  return { user, onAnswer };
};

describe("the confirmation dialog, console style", () => {
  const base: ConfirmOptions = { title: "Quit the app?", description: "Jobs are not running.", confirmLabel: "Quit", rememberLabel: "Do not ask me again" };

  it("hands the 'do not ask again' choice to the caller only when the question is confirmed", async () => {
    const onRemember = vi.fn();
    const { user, onAnswer } = await open({ ...base, onRemember });
    await user.click(screen.getByRole("checkbox", { name: "Do not ask me again" }));
    await user.click(screen.getByRole("button", { name: "Quit" }));
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(true));
    expect(onRemember).toHaveBeenCalledWith(true);
  });

  it("reports an unticked box as false, and says nothing when the person cancels", async () => {
    const onRemember = vi.fn();
    const first = await open({ ...base, onRemember });
    await first.user.click(screen.getByRole("button", { name: "Quit" }));
    await waitFor(() => expect(onRemember).toHaveBeenCalledWith(false));
    onRemember.mockClear();
    await first.user.click(screen.getByText("ask"));
    await first.user.click(await screen.findByRole("checkbox", { name: "Do not ask me again" }));
    await first.user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(first.onAnswer).toHaveBeenLastCalledWith(false));
    expect(onRemember).not.toHaveBeenCalled();
  });

  it("offers no checkbox unless asked to", async () => {
    await open({ title: "Delete?", description: "Gone.", confirmLabel: "Delete" });
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("says Warning with an icon and a solid critical confirm button for a critical question, and Confirm otherwise", async () => {
    const critical = await open({ title: "Reset everything?", description: "All gone.", confirmLabel: "Reset", tone: "critical" });
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Warning");
    expect(dialog.querySelector(".hazard")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Reset" }).className).toContain("bg-crit");
    await critical.user.click(screen.getByRole("button", { name: "Cancel" }));
  });

  it("gives the focus back to the control that asked, however the question ends", async () => {
    const { user } = await open(base);
    const asker = screen.getByText("ask");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(asker).toHaveFocus());
    await user.click(asker);
    await screen.findByRole("alertdialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(asker).toHaveFocus());
    await user.click(asker);
    await user.click(await screen.findByRole("button", { name: "Quit" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(asker).toHaveFocus());
  });

  it("does not fail when the control that asked is gone by the time the question ends", async () => {
    function Vanishing() {
      const confirm = useConfirm();
      const [shown, setShown] = useState(true);
      return shown ? <button onClick={async () => { setShown(false); await confirm(base); }}>vanish</button> : <p>gone</p>;
    }
    const user = userEvent.setup();
    render(<ConfirmProvider><Vanishing /></ConfirmProvider>);
    await user.click(screen.getByText("vanish"));
    await screen.findByRole("alertdialog");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(screen.getByText("gone")).toBeInTheDocument();
  });

  it("keeps the confirm button off until the word is typed", async () => {
    const { user } = await open({ title: "Reset?", description: "x", confirmLabel: "Reset", tone: "critical", typeToConfirm: "RESET" });
    const button = screen.getByRole("button", { name: "Reset" });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText(/Type RESET to continue/), "RESET");
    expect(button).toBeEnabled();
  });
});

describe("Dialog", () => {
  it("has a titled, closable console window", async () => {
    const onOpenChange = vi.fn();
    const user = userEvent.setup();
    render(<Dialog open onOpenChange={onOpenChange} title="Test NVD" description="Sends one small request.">body</Dialog>);
    expect(screen.getByRole("dialog", { name: "Test NVD" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
