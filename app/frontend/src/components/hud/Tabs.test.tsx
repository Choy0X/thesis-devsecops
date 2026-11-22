import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Tabs } from "./Tabs";

// Radix's own trigger activates on mousedown (and keyboard), not a synthetic click event -- fireEvent.mouseDown is what a real pointer click delivers first, and is enough on its own.
const choose = (name: string) => fireEvent.mouseDown(screen.getByRole("tab", { name }));

/** Mirrors how a real caller (ProjectConversationFocus) gates content: a tab's own content is null until it has been visited at least once, exactly like Tabs' own
 *  content != null guard expects -- so this is what actually exercises "mounted once visited, then never unmounted again" rather than assuming Tabs does the gating itself. */
function Probe({ keepMounted }: { keepMounted?: boolean }) {
  const [value, setValue] = useState("a");
  const [visited, setVisited] = useState(new Set(["a"]));
  const onValueChange = (v: string) => { setValue(v); setVisited((s) => (s.has(v) ? s : new Set(s).add(v))); };
  return (
    <Tabs value={value} onValueChange={onValueChange} keepMounted={keepMounted} items={[
      { id: "a", label: "A", content: visited.has("a") ? <p>content of a</p> : null },
      { id: "b", label: "B", content: visited.has("b") ? <p>content of b</p> : null },
    ]} />
  );
}

describe("Tabs", () => {
  it("wires the trigger and the panel with the right roles, and switches on a mousedown", () => {
    render(<Probe />);
    expect(screen.getByRole("tab", { name: "A" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("content of a");
    choose("B");
    expect(screen.getByRole("tab", { name: "B" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("content of b");
  });

  it("unmounts the tab left behind by default", () => {
    render(<Probe />);
    expect(screen.getByText("content of a")).toBeInTheDocument();
    choose("B");
    expect(screen.queryByText("content of a")).toBeNull();
    choose("A");
    expect(screen.getByText("content of a")).toBeInTheDocument();
    expect(screen.queryByText("content of b")).toBeNull();
  });

  it("with keepMounted, a tab with no content yet is still not mounted -- gating is the caller's, not Tabs'", () => {
    render(<Probe keepMounted />);
    expect(screen.queryByText("content of b")).toBeNull();
  });

  it("unmounts inactive content even when the legacy layout flag is set", () => {
    render(<Probe keepMounted />);
    choose("B");
    expect(screen.getByText("content of b").closest('[role="tabpanel"]')).toHaveAttribute("data-state", "active");
    expect(screen.queryByText("content of a")).not.toBeInTheDocument();
    choose("A");
    expect(screen.getByText("content of a").closest('[role="tabpanel"]')).toHaveAttribute("data-state", "active");
    expect(screen.queryByText("content of b")).not.toBeInTheDocument();
  });
});
