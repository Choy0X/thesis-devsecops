import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Button, FormRow, Input, NumberField, TextField, TextareaField } from ".";

describe("FormRow and TextField", () => {
  it("ties the label, the hint and the error to the control by id", () => {
    render(<TextField label="Endpoint" hint="Where the model answers" error="Not a URL" defaultValue="x" />);
    const input = screen.getByLabelText("Endpoint");
    expect(input.tagName).toBe("INPUT");
    expect(input).toHaveAttribute("aria-invalid", "true");
    const described = (input.getAttribute("aria-describedby") ?? "").split(" ");
    expect(described).toHaveLength(2);
    expect(document.getElementById(described[0])).toHaveTextContent("Where the model answers");
    expect(screen.getByRole("alert")).toHaveTextContent("Not a URL");
  });

  it("focuses the control when its label is clicked and never presses a button next to it", async () => {
    // The old Field wrapped everything in a <label>: a click on the text went to the first button inside, which was Remove.
    const remove = vi.fn();
    render(<FormRow label="API key" action={<Button onClick={remove}>Remove</Button>}><Input defaultValue="" /></FormRow>);
    await userEvent.click(screen.getByText("API key"));
    expect(screen.getByLabelText("API key")).toHaveFocus();
    expect(remove).not.toHaveBeenCalled();
  });

  it("shows a prefix and a suffix and passes className to the frame, not the control", () => {
    const { container } = render(<Input prefix="https://" suffix=".local" className="w-40" aria-label="host" />);
    expect(screen.getByText("https://")).toBeInTheDocument();
    expect(screen.getByText(".local")).toBeInTheDocument();
    expect(screen.getByLabelText("host").className).not.toContain("w-40");
    expect((container.firstChild as HTMLElement).className).toContain("w-40");
  });

  it("is disabled as a whole and keeps the typed value controlled", async () => {
    function Controlled() {
      const [v, setV] = useState("");
      return <TextField label="Name" value={v} onChange={(e) => setV(e.target.value)} />;
    }
    render(<Controlled />);
    await userEvent.type(screen.getByLabelText("Name"), "abc");
    expect(screen.getByLabelText("Name")).toHaveValue("abc");
  });

  it("has a multi-line field with the same wiring", () => {
    render(<TextareaField label="Notes" hint="Optional" />);
    expect(screen.getByLabelText("Notes").tagName).toBe("TEXTAREA");
  });
});

describe("NumberField", () => {
  function Counter({ min, max, step }: { min?: number; max?: number; step?: number }) {
    const [v, setV] = useState("5");
    return <NumberField label="Jobs" value={v} min={min} max={max} step={step} onChange={(e) => setV(e.target.value)} />;
  }

  it("steps with real buttons and still reports through the native onChange", async () => {
    render(<Counter min={1} max={6} />);
    await userEvent.click(screen.getByRole("button", { name: "Increase" }));
    expect(screen.getByLabelText("Jobs")).toHaveValue(6);
    await userEvent.click(screen.getByRole("button", { name: "Increase" }));
    expect(screen.getByLabelText("Jobs")).toHaveValue(6);       // stops at max
    await userEvent.click(screen.getByRole("button", { name: "Decrease" }));
    expect(screen.getByLabelText("Jobs")).toHaveValue(5);
  });

  it("keeps the precision of a decimal step", async () => {
    render(<Counter step={0.1} />);
    await userEvent.click(screen.getByRole("button", { name: "Increase" }));
    expect(screen.getByLabelText("Jobs")).toHaveValue(5.1);
  });

  it("accepts typed values and disables its buttons when disabled", () => {
    const { rerender } = render(<NumberField label="N" defaultValue={2} />);
    fireEvent.change(screen.getByLabelText("N"), { target: { value: "9" } });
    expect(screen.getByLabelText("N")).toHaveValue(9);
    rerender(<NumberField label="N" defaultValue={2} disabled />);
    expect(screen.getByRole("button", { name: "Increase" })).toBeDisabled();
  });
});
