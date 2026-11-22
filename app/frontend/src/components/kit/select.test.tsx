import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Select, SelectField, type SelectProps } from ".";
import { choose } from "../../test/select";

const options = [
  { value: "auto", label: "Automatic" },
  { value: "gpu", label: "Graphics card", hint: "6 GB" },
  { value: "cpu", label: "Processor" },
];

function Harness(props: Partial<SelectProps> & { initial?: string; onPick?: (v: string) => void }) {
  const [v, setV] = useState(props.initial ?? "auto");
  return <SelectField label="Device" options={options} {...props} value={v} onValueChange={(x) => { setV(x); props.onPick?.(x); }} />;
}

describe("SelectField", () => {
  it("is a combobox named by its label, showing the chosen option", () => {
    render(<Harness />);
    const trigger = screen.getByRole("combobox", { name: "Device" });
    expect(trigger).toHaveTextContent("Automatic");
  });

  it("opens with a click, lists options with their hints, and reports the choice", async () => {
    const onPick = vi.fn();
    const user = userEvent.setup();
    render(<Harness onPick={onPick} />);
    await user.click(screen.getByRole("combobox", { name: "Device" }));
    expect(await screen.findByRole("listbox")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Graphics card/ })).toHaveTextContent("6 GB");
    await user.click(screen.getByRole("option", { name: "Processor" }));
    expect(onPick).toHaveBeenCalledWith("cpu");
    expect(screen.getByRole("combobox", { name: "Device" })).toHaveTextContent("Processor");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("works from the keyboard: open, move, choose, close with Escape", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    screen.getByRole("combobox", { name: "Device" }).focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByRole("combobox", { name: "Device" })).toHaveTextContent("Graphics card");
    await user.keyboard("{Enter}{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("maps an empty value to a real option and back", async () => {
    const onPick = vi.fn();
    const user = userEvent.setup();
    render(<Harness initial="gpu" onPick={onPick} options={[{ value: "", label: "Any device" }, ...options]} />);
    await choose(user, screen.getByRole("combobox", { name: "Device" }), "Any device");
    expect(onPick).toHaveBeenCalledWith("");
    expect(screen.getByRole("combobox", { name: "Device" })).toHaveTextContent("Any device");
  });

  it("shows the placeholder when the value is empty and no option is empty", () => {
    render(<Select aria-label="Pick" value="" onValueChange={() => undefined} options={options} placeholder="Choose a device" />);
    expect(screen.getByRole("combobox", { name: "Pick" })).toHaveTextContent("Choose a device");
  });

  it("groups options under a label and skips disabled ones", async () => {
    const user = userEvent.setup();
    render(<Select aria-label="Model" value="a" onValueChange={() => undefined} options={[
      { label: "Downloaded", options: [{ value: "a", label: "Alpha" }, { value: "b", label: "Beta", disabled: true }] },
      { label: "Other", options: [{ value: "c", label: "Gamma" }] },
    ]} />);
    await user.click(screen.getByRole("combobox", { name: "Model" }));
    expect(await screen.findByText("Downloaded")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Beta" })).toHaveAttribute("aria-disabled", "true");
  });

  it("links the hint and the error and marks itself invalid", () => {
    render(<SelectField label="Engine" value="a" onValueChange={() => undefined} options={[{ value: "a", label: "A" }]} hint="Local or external" error="Pick one" />);
    const trigger = screen.getByRole("combobox", { name: "Engine" });
    expect(trigger).toHaveAttribute("aria-invalid", "true");
    expect((trigger.getAttribute("aria-describedby") ?? "").split(" ")).toHaveLength(2);
    expect(screen.getByRole("alert")).toHaveTextContent("Pick one");
  });

  it("does not open when disabled", async () => {
    const user = userEvent.setup();
    render(<Select aria-label="Off" disabled value="a" onValueChange={() => undefined} options={[{ value: "a", label: "A" }]} />);
    await user.click(screen.getByRole("combobox", { name: "Off" }));
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("a select with nothing to choose", () => {
  it("is disabled and says why when it has no option and a reason, instead of opening an empty list", async () => {
    const user = userEvent.setup();
    render(<SelectField label="Model" options={[]} value="" onValueChange={() => undefined} emptyReason="No model is installed. Download one from the Catalog." />);
    const trigger = screen.getByRole("combobox", { name: "Model" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("Nothing to choose");
    expect(screen.getByText("No model is installed. Download one from the Catalog.")).toBeInTheDocument();
    expect(trigger).toHaveAccessibleDescription("No model is installed. Download one from the Catalog.");
    await user.click(trigger);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("counts only real options: a list with just a None or Automatic entry has nothing to choose either, and still shows that entry", () => {
    render(<SelectField label="Adapter" options={[{ value: "", label: "None" }]} value="" onValueChange={() => undefined} emptyReason="No adapter has been converted yet." />);
    const trigger = screen.getByRole("combobox", { name: "Adapter" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("None");
    expect(screen.getByText("No adapter has been converted yet.")).toBeInTheDocument();
  });

  it("is an ordinary select as soon as there is one real option, and the reason is not shown", async () => {
    const user = userEvent.setup();
    render(<SelectField label="Model" options={[{ value: "", label: "Automatic" }, { value: "a", label: "A" }]} value="" onValueChange={() => undefined} emptyReason="Nothing installed." hint="Its usual hint." />);
    const trigger = screen.getByRole("combobox", { name: "Model" });
    expect(trigger).toBeEnabled();
    expect(screen.queryByText("Nothing installed.")).toBeNull();
    expect(screen.getByText("Its usual hint.")).toBeInTheDocument();
    await user.click(trigger);
    expect(await screen.findByRole("option", { name: "A" })).toBeInTheDocument();
  });

  it("looks inside groups, and does nothing without a reason (an empty select without one stays as it was)", () => {
    const { unmount } = render(<SelectField label="Model" options={[{ label: "Group", options: [] }]} value="" onValueChange={() => undefined} emptyReason="Empty group." />);
    expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
    unmount();
    render(<SelectField label="Model" options={[]} value="" onValueChange={() => undefined} />);
    expect(screen.getByRole("combobox", { name: "Model" })).toBeEnabled();
  });

  it("keeps a select the caller disabled disabled, with its hint", () => {
    render(<SelectField label="Model" options={[{ value: "a", label: "A" }]} value="a" onValueChange={() => undefined} disabled hint="Locked." emptyReason="x" />);
    expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
    expect(screen.getByText("Locked.")).toBeInTheDocument();
  });
});
