import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ConfirmProvider, useConfirm, type ConfirmOptions } from "./AlertDialog";
import { Badge, SeverityBadge } from "./Badge";
import { Dialog, Sheet } from "./Dialog";
import { Progress } from "./Progress";
import { Tabs } from "./Tabs";
import { Tooltip } from "./Tooltip";

function Asker({ options, onAnswer }: { options: ConfirmOptions; onAnswer: (ok: boolean) => void }) {
  const confirm = useConfirm();
  return <button onClick={async () => onAnswer(await confirm(options))}>ask</button>;
}
const renderAsk = (options: ConfirmOptions) => {
  const onAnswer = vi.fn();
  render(<ConfirmProvider><Asker options={options} onAnswer={onAnswer} /></ConfirmProvider>);
  return onAnswer;
};

describe("the layer behind a dialog", () => {
  it("marks the document while a dialog is open, keeps the mark while another one is stacked over it, and clears it when the last closes", async () => {
    const user = userEvent.setup();
    function Stack() {
      const [one, setOne] = useState(false);
      const [two, setTwo] = useState(false);
      return (
        <>
          <button onClick={() => setOne(true)}>first</button>
          <Dialog open={one} onOpenChange={setOne} title="First">
            <button onClick={() => setTwo(true)}>second</button>
          </Dialog>
          <Dialog open={two} onOpenChange={setTwo} title="Second"><p>over the first</p></Dialog>
        </>);
    }
    render(<Stack />);
    expect(document.documentElement.dataset.scrim).toBeUndefined();
    await user.click(screen.getByText("first"));
    expect(document.documentElement.dataset.scrim).toBe("on");
    await user.click(await screen.findByText("second"));
    expect(await screen.findByRole("dialog", { name: "Second" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Second" })).toBeNull());
    expect(document.documentElement.dataset.scrim).toBe("on");               // the first is still open
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "First" })).toBeNull());
    expect(document.documentElement.dataset.scrim).toBeUndefined();
  });

  it("is a clear layer that still catches the pointer, never a translucent box over the window", () => {
    render(<Dialog open onOpenChange={() => undefined} title="Layer"><p>text</p></Dialog>);
    const layer = document.querySelector(".scrim") as HTMLElement;
    expect(layer).not.toBeNull();
    expect(layer.className).not.toMatch(/\bbg-/);
  });
});

describe("the confirmation dialog", () => {
  const base: ConfirmOptions = { title: "Delete 120 records?", description: "They cannot be recovered.", confirmLabel: "Delete 120 records" };

  it("names the action on the button and resolves true when it is chosen", async () => {
    const answer = renderAsk(base);
    fireEvent.click(screen.getByText("ask"));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("They cannot be recovered.");
    expect(screen.getByRole("heading", { name: "Delete 120 records?" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete 120 records" }));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(true));
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("resolves false on Cancel and on Escape", async () => {
    const answer = renderAsk(base);
    fireEvent.click(screen.getByText("ask"));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(answer).toHaveBeenLastCalledWith(false));
    fireEvent.click(screen.getByText("ask"));
    await screen.findByRole("alertdialog");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
    expect(answer).toHaveBeenLastCalledWith(false);
  });

  it("keeps the confirm button disabled until the word is typed", async () => {
    const answer = renderAsk({ ...base, typeToConfirm: "DELETE", tone: "critical" });
    fireEvent.click(screen.getByText("ask"));
    const confirmButton = await screen.findByRole("button", { name: "Delete 120 records" });
    expect(confirmButton).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type DELETE/), "DELETE");
    expect(confirmButton).toBeEnabled();
    fireEvent.click(confirmButton);
    await waitFor(() => expect(answer).toHaveBeenCalledWith(true));
  });

  it("wears the hazard stripe only when the request is critical", async () => {
    renderAsk({ ...base, tone: "critical" });
    fireEvent.click(screen.getByText("ask"));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.querySelector(".hazard")).not.toBeNull();
  });

  it("fails closed without a provider: the action is not taken", async () => {
    const answers: unknown[] = [];
    function Bare() {
      const confirm = useConfirm();
      return <button onClick={() => confirm(base).then((ok) => answers.push(ok), (e) => answers.push(String(e)))}>ask</button>;
    }
    render(<Bare />);
    fireEvent.click(screen.getByText("ask"));
    await waitFor(() => expect(answers).toHaveLength(1));
    expect(String(answers[0])).toMatch(/ConfirmProvider/);
  });
});

describe("dialogs and sheets", () => {
  it("open with a title, close with the button and trap nothing when closed", async () => {
    function Host() {
      const [open, setOpen] = useState(true);
      return <><Dialog open={open} onOpenChange={setOpen} title="Edit record"><p>body</p></Dialog><Sheet open={false} onOpenChange={() => undefined} title="Hidden">x</Sheet></>;
    }
    render(<Host />);
    expect(await screen.findByRole("dialog", { name: "Edit record" })).toHaveTextContent("body");
    expect(screen.queryByText("Hidden")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("tabs", () => {
  it("switch with a click and report the chosen id", () => {
    const onChange = vi.fn();
    render(<Tabs value="local" onValueChange={onChange} items={[{ id: "local", label: "Local", count: 3, content: <p>local models</p> }, { id: "hf", label: "Hugging Face", content: <p>hub models</p> }]} />);
    expect(screen.getByRole("tab", { name: /Local/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("local models")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Hugging Face" }));
    expect(onChange).toHaveBeenCalledWith("hf");
  });
});

describe("progress", () => {
  it("fills the segments in proportion and says the percentage in words", () => {
    const { container } = render(<Progress value={50} segments={10} label="Mining" />);
    const bar = screen.getByRole("progressbar", { name: "Mining" });
    expect(bar).toHaveAttribute("aria-valuetext", "50 percent");
    expect(container.querySelectorAll("[data-filled=true]")).toHaveLength(5);
    expect(container.querySelectorAll("[data-filled=false]")).toHaveLength(5);
  });

  it("clamps values outside the range", () => {
    const { container, rerender } = render(<Progress value={250} segments={4} />);
    expect(container.querySelectorAll("[data-filled=true]")).toHaveLength(4);
    rerender(<Progress value={-5} segments={4} />);
    expect(container.querySelectorAll("[data-filled=true]")).toHaveLength(0);
  });
});

describe("badges", () => {
  it("carry severity as a word and an icon, and only critical has a solid fill", () => {
    const { container } = render(<>{(["low", "medium", "high", "critical"] as const).map((l) => <SeverityBadge key={l} level={l} />)}<Badge>3</Badge></>);
    for (const word of ["Low", "Medium", "High", "Critical"]) expect(screen.getByText(word)).toBeInTheDocument();
    expect(container.querySelectorAll("svg")).toHaveLength(4);
    const solid = [...container.querySelectorAll("[data-severity]")].filter((e) => e.className.includes("bg-crit"));
    expect(solid.map((e) => e.getAttribute("data-severity"))).toEqual(["critical"]);
    expect(container.innerHTML).not.toMatch(/(?<!on-)brand/);
  });
});

describe("tooltip", () => {
  it("shows its label when the trigger is focused", async () => {
    render(<Tooltip label="Stops the job"><button>Stop</button></Tooltip>);
    await userEvent.tab();
    expect((await screen.findAllByText("Stops the job")).length).toBeGreaterThan(0);
  });
});
