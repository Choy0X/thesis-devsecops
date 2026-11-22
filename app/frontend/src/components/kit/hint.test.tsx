import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { tipOf } from "../../test/hint";
import { Button, Checkbox, Hint, Radio, Switch } from "./index";

describe("Hint", () => {
  it("explains the text on keyboard focus, takes one tab stop and carries no native title", async () => {
    render(<p>It is <Hint label="Because the fix commit is newer than the release">suspect</Hint> now</p>);
    const word = screen.getByText("suspect");
    expect(word).toHaveAttribute("tabindex", "0");
    expect(word).not.toHaveAttribute("title");
    expect(await tipOf(word)).toBe("Because the fix commit is newer than the release");
  });

  it("passes the attributes of the element on (a test id, a data attribute)", () => {
    render(<Hint label="x" data-testid="chip" data-state="done">Done</Hint>);
    expect(screen.getByTestId("chip")).toHaveAttribute("data-state", "done");
  });
});

describe("a title on a control is a tooltip", () => {
  it("on a button", async () => {
    render(<Button title="Drafts the text with the language model">Enrich</Button>);
    const button = screen.getByRole("button", { name: "Enrich" });
    expect(button).not.toHaveAttribute("title");
    expect(await tipOf(button)).toBe("Drafts the text with the language model");
  });

  it("stays a native title on a disabled button, which takes no pointer events", () => {
    render(<Button disabled title="Choose an ecosystem first">Next</Button>);
    expect(screen.getByRole("button", { name: "Next" })).toHaveAttribute("title", "Choose an ecosystem first");
  });

  it("on a checkbox, a switch and a radio, shown when the input has the focus", async () => {
    render(<div>
      <Checkbox label="Train on everything" title="For the final model" />
      <Switch label="Live" title="Updates by itself" />
      <Radio name="r" label="Small" title="Fits the card" />
    </div>);
    for (const [name, role, text] of [["Train on everything", "checkbox", "For the final model"], ["Live", "switch", "Updates by itself"], ["Small", "radio", "Fits the card"]] as const) {
      const input = screen.getByRole(role, { name });
      expect(input).not.toHaveAttribute("title");
      expect(await tipOf(input)).toBe(text);
      input.blur();
    }
  });
});
