import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Dialog } from "../hud/Dialog";
import { Button, SecretField, SelectField, SettingFacts, SettingNote, SettingRow, SettingsCard, TextField } from ".";

// What jsdom can say about the card and its rows: the structure, the names and the classes that put a name and its explanation on the left and the control on the right. Where the pieces land on
// the screen (one left edge for every control, one width for every card) is measured in a browser by e2e/layout.spec.ts.

describe("SettingsCard", () => {
  it("is a region named by its title, whose heading sits under the heading of the group (h3), or is h2 when it stands alone", () => {
    render(<><SettingsCard title="Models folder" desc="Where the models are."><SettingNote>text</SettingNote></SettingsCard><SettingsCard as="h2" title="Connection"><SettingNote>text</SettingNote></SettingsCard></>);
    expect(screen.getByRole("region", { name: "Models folder" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Models folder" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Connection" })).toBeInTheDocument();
    expect(screen.getByText("Where the models are.")).toBeInTheDocument();
  });

  it("puts its action on the title's line and its buttons in a footer", () => {
    render(<SettingsCard title="Compute" action={<Button variant="ghost">Refresh</Button>} footer={<Button variant="primary">Save</Button>}><SettingNote>text</SettingNote></SettingsCard>);
    const card = screen.getByRole("region", { name: "Compute" });
    expect(within(card.querySelector("header")!).getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    const footer = within(card).getByRole("button", { name: "Save" }).parentElement!;
    expect(footer.previousElementSibling).toBe(card.querySelector("header")!.nextElementSibling);       // header, rows, footer, in that order
    expect(footer.className).toMatch(/border-t/);
  });

  it("has no footer element when it has no buttons", () => {
    render(<SettingsCard title="Facts"><SettingNote>text</SettingNote></SettingsCard>);
    const card = screen.getByRole("region", { name: "Facts" });
    expect(card.children).toHaveLength(2);                                                          // the header and the rows
  });
});

describe("a field inside a card", () => {
  it("is a row: the name and the explanation on the left, the control on the right, and the name still focuses the control", async () => {
    const user = userEvent.setup();
    render(<SettingsCard title="Models folder card"><TextField label="Folder" hint="Empty means the default." defaultValue="" /></SettingsCard>);
    const input = screen.getByLabelText("Folder");
    const row = input.closest("div.grid") as HTMLElement;
    expect(row.className).toMatch(/lg:grid-cols-\[minmax\(0,5fr\)_minmax\(0,7fr\)\]/);
    const [left, right] = Array.from(row.children) as HTMLElement[];
    expect(within(left).getByText("Folder")).toBeInTheDocument();
    expect(within(left).getByText("Empty means the default.")).toBeInTheDocument();                  // the explanation is beside the name, not under the control
    expect(right.contains(input)).toBe(true);
    expect(left.contains(input)).toBe(false);
    expect(input).toHaveAccessibleDescription("Empty means the default.");
    await user.click(screen.getByText("Folder", { selector: "label" }));
    expect(input).toHaveFocus();
  });

  it("keeps an error under its control, in the right column, and links it", () => {
    render(<SettingsCard title="Mail server card"><TextField label="Port" error="Use a number." /></SettingsCard>);
    const input = screen.getByLabelText("Port");
    expect(input).toHaveAttribute("aria-invalid", "true");
    const alert = screen.getByRole("alert");
    expect(alert.parentElement).toBe(input.closest("div.grid")!.children[1]);
    expect(input).toHaveAccessibleDescription("Use a number.");
  });

  it("is the same for a select and a secret field: the saved tag sits by the name, the buttons by the control", () => {
    render(
      <SettingsCard title="Keys">
        <SelectField label="Device" value="" onValueChange={() => undefined} options={[{ value: "", label: "Automatic" }]} />
        <SecretField label="API key" value="" onChange={() => undefined} saved savedHint="a1b2" onRemove={() => undefined} />
      </SettingsCard>);
    const select = screen.getByRole("combobox", { name: "Device" });
    expect((select.closest("div.grid") as HTMLElement).children[1].contains(select)).toBe(true);
    const row = screen.getByLabelText("API key", { selector: "input" }).closest("div.grid") as HTMLElement;
    expect(within(row.children[0] as HTMLElement).getByTestId("secret-saved")).toHaveTextContent("saved...a1b2");
    expect(within(row.children[1] as HTMLElement).getByRole("button", { name: "Show API key" })).toBeInTheDocument();
  });

  it("is the old stacked field outside a card, and inside a dialog opened from a card", async () => {
    const user = userEvent.setup();
    render(<TextField label="Alone" />);
    expect(screen.getByLabelText("Alone").closest("div.grid")).toBeNull();
    function Opener() {
      const [open, setOpen] = useState(false);
      return (
        <SettingsCard title="Card" footer={<Button onClick={() => setOpen(true)}>Open it</Button>}>
          <SettingNote>text</SettingNote>
          <Dialog open={open} onOpenChange={setOpen} title="A question"><TextField label="Answer" /></Dialog>
        </SettingsCard>);
    }
    render(<Opener />);
    await user.click(screen.getByRole("button", { name: "Open it" }));
    const dialog = await screen.findByRole("dialog", { name: "A question" });
    expect(within(dialog).getByLabelText("Answer").closest("div.grid")).toBeNull();                 // a portal keeps React context; the dialog frame resets the rows
  });
});

describe("SettingRow, SettingFacts and SettingNote", () => {
  it("lays out a control that is not a field in the same two columns", () => {
    render(<SettingsCard title="App"><SettingRow label="Restart" hint="Both ask first."><Button>Restart the app</Button></SettingRow></SettingsCard>);
    const row = screen.getByRole("button", { name: "Restart the app" }).closest("div.grid") as HTMLElement;
    expect(row.className).toMatch(/lg:grid-cols-\[minmax\(0,5fr\)_minmax\(0,7fr\)\]/);
    expect(within(row.children[0] as HTMLElement).getByText("Restart")).toBeInTheDocument();
    expect(within(row.children[0] as HTMLElement).getByText("Both ask first.")).toBeInTheDocument();
  });

  it("lowers the name to the middle of a 36 px control, except for a switch (tight)", () => {
    render(<SettingsCard title="Rows"><SettingRow label="Choice"><Button>x</Button></SettingRow><SettingRow tight label="Switch"><Button>y</Button></SettingRow></SettingsCard>);
    const left = (name: string) => screen.getByText(name).closest("div.min-w-0") as HTMLElement;
    expect(left("Choice").className).toMatch(/lg:pt-2/);
    expect(left("Switch").className).not.toMatch(/lg:pt-2/);
  });

  it("shows facts as a list of terms and values in the same columns, with a name", () => {
    render(<SettingsCard title="Facts"><SettingFacts label="The folder"><dt>In use</dt><dd>data/models</dd><dt>Free</dt><dd>12 GB</dd></SettingFacts></SettingsCard>);
    const list = screen.getByText("In use").parentElement as HTMLElement;
    expect(list.tagName).toBe("DL");
    expect(list).toHaveAttribute("aria-label", "The folder");
    expect(list.className).toMatch(/lg:grid-cols-\[minmax\(0,5fr\)_minmax\(0,7fr\)\]/);
  });

  it("passes the role of a note on, so a status is announced", () => {
    render(<SettingsCard title="Notes"><SettingNote role="status">Not loaded yet.</SettingNote></SettingsCard>);
    expect(screen.getByRole("status")).toHaveTextContent("Not loaded yet.");
  });
});
