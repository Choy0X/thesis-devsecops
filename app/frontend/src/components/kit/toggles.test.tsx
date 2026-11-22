import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Checkbox, FileInput, Radio, RadioGroup, Switch } from ".";

describe("Switch", () => {
  it("is a switch named by its label, toggled by a click on the text or the space bar", async () => {
    const onCheckedChange = vi.fn();
    const user = userEvent.setup();
    function Harness() {
      const [on, setOn] = useState(false);
      return <Switch label="Ask before quitting" checked={on} onCheckedChange={(v) => { setOn(v); onCheckedChange(v); }} />;
    }
    render(<Harness />);
    const sw = screen.getByRole("switch", { name: "Ask before quitting" });
    expect(sw).not.toBeChecked();
    await user.click(screen.getByText("Ask before quitting"));
    expect(sw).toBeChecked();
    sw.focus();
    await user.keyboard(" ");
    expect(sw).not.toBeChecked();
    expect(onCheckedChange.mock.calls.map((c) => c[0])).toEqual([true, false]);
  });

  it("is described by its description and cannot be toggled when disabled", async () => {
    const user = userEvent.setup();
    render(<Switch label="Desktop notifications" description="A message when a job ends" disabled />);
    const sw = screen.getByRole("switch", { name: "Desktop notifications" });
    expect(sw).toHaveAccessibleDescription("A message when a job ends");
    await user.click(sw);
    expect(sw).not.toBeChecked();
  });
});

describe("Checkbox", () => {
  it("is a checkbox that reports through both the native and the convenient callback", async () => {
    const onChange = vi.fn();
    const onCheckedChange = vi.fn();
    const user = userEvent.setup();
    render(<Checkbox label="Do not ask me again" onChange={onChange} onCheckedChange={onCheckedChange} />);
    await user.click(screen.getByRole("checkbox", { name: "Do not ask me again" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });
});

describe("RadioGroup", () => {
  const options = [{ value: "local", label: "Local" }, { value: "external", label: "External", description: "Needs an endpoint" }, { value: "off", label: "Off", disabled: true }];

  function Harness({ variant }: { variant?: "list" | "segmented" }) {
    const [v, setV] = useState("local");
    return <RadioGroup legend="LLM engine" value={v} onValueChange={setV} options={options} variant={variant} />;
  }

  it("is a radio group named by its legend, with one choice selected", () => {
    render(<Harness />);
    const group = screen.getByRole("radiogroup", { name: "LLM engine" });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Local" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "External" })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: "Off" })).toBeDisabled();
  });

  it("changes the choice with a click and with the arrow keys", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByText("External"));
    expect(screen.getByRole("radio", { name: "External" })).toBeChecked();
    screen.getByRole("radio", { name: "External" }).focus();
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("radio", { name: "Local" })).toBeChecked();
  });

  it("has a segmented look with the same behaviour", async () => {
    const user = userEvent.setup();
    render(<Harness variant="segmented" />);
    await user.click(screen.getByText("External"));
    expect(screen.getByRole("radio", { name: "External" })).toBeChecked();
  });
});

describe("Radio", () => {
  function Group() {
    const [v, setV] = useState("a");
    return (
      <div role="radiogroup" aria-label="Which file">
        <Radio name="f" value="a" checked={v === "a"} onChange={() => setV("a")} aria-label="first.parquet" />
        <Radio name="f" value="b" checked={v === "b"} onChange={() => setV("b")} aria-label="second.parquet" />
        <Radio name="f" value="c" checked={v === "c"} onChange={() => setV("c")} disabled aria-label="third.parquet" />
        <Radio name="f" value="d" checked={v === "d"} onChange={() => setV("d")} label="Labelled" description="and described" />
      </div>
    );
  }

  it("is a radio named by its aria-label or its label, one of a group by the native name, and a disabled one cannot be chosen", async () => {
    const user = userEvent.setup();
    render(<Group />);
    expect(screen.getByRole("radio", { name: "first.parquet" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "second.parquet" }));
    expect(screen.getByRole("radio", { name: "second.parquet" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "first.parquet" })).not.toBeChecked();
    await user.click(screen.getByRole("radio", { name: "third.parquet" }));
    expect(screen.getByRole("radio", { name: "third.parquet" })).not.toBeChecked();
    await user.click(screen.getByText("Labelled"));
    expect(screen.getByRole("radio", { name: "Labelled" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Labelled" })).toHaveAccessibleDescription("and described");
  });

  it("reports through both callbacks", async () => {
    const onChange = vi.fn(); const onCheckedChange = vi.fn();
    render(<Radio aria-label="one" onChange={onChange} onCheckedChange={onCheckedChange} />);
    await userEvent.setup().click(screen.getByRole("radio", { name: "one" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });
});

describe("FileInput", () => {
  it("is a file input with a button drawn over it, named by its aria-label, that gives the chosen file to onChange", async () => {
    const seen: string[] = [];
    const file = new File(["a,b\n1,2"], "rows.csv", { type: "text/csv" });
    render(<FileInput aria-label="File to import" accept=".csv" buttonLabel="Choose file" onChange={(e) => seen.push(e.target.files?.[0]?.name ?? "")} />);
    const input = screen.getByLabelText("File to import") as HTMLInputElement;
    expect(input).toHaveAttribute("type", "file");
    expect(input).toHaveAttribute("accept", ".csv");
    expect(screen.getByText("Choose file")).toBeInTheDocument();
    await userEvent.setup().upload(input, file);
    expect(seen).toEqual(["rows.csv"]);
  });

  it("cannot be used when disabled", async () => {
    const seen: string[] = [];
    render(<FileInput aria-label="File" disabled onChange={() => seen.push("x")} />);
    expect(screen.getByLabelText("File")).toBeDisabled();
    await userEvent.setup().upload(screen.getByLabelText("File"), new File(["x"], "x.txt"));
    expect(seen).toEqual([]);
  });
});
