import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { Chat } from "./client";
import { ResumePicker } from "./ResumePicker";

const now = Date.now();
const model = { id: "local", name: "Local Coder" };
const chats: Chat[] = [
  { id: "older", title: "Review invoices", model, last_active_at: new Date(now - 2 * 86400000).toISOString() },
  { id: "archived", title: "Old login issue", model, archived: true, last_active_at: new Date(now).toISOString() },
  { id: "current", title: "Fix the parser", model, last_active_at: new Date(now - 3 * 60000).toISOString() },
  { id: "latest", title: "Review login", model, last_active_at: new Date(now - 2 * 60000).toISOString() },
];

function show(query = "", rows = chats) {
  const onPick = vi.fn(), onClose = vi.fn();
  render(<ResumePicker chats={rows} currentId="current" query={query} onPick={onPick} onClose={onClose} />);
  return { onPick, onClose, input: screen.getByRole("combobox", { name: "Filter conversations" }) };
}

it("lists recent conversations by last activity and groups archived conversations last", () => {
  show();
  const rows = screen.getAllByRole("row");
  expect(rows.map(row => within(row).getByText(/invoice|login|parser/).textContent)).toEqual([
    "Review login", "Fix the parser", "Review invoices", "Old login issue",
  ]);
  expect(rows[0]).toHaveTextContent("2 min ago");
  expect(rows[0]).toHaveTextContent("Local Coder");
  expect(within(rows[1]).getByRole("button", { name: /Fix the parser/ })).toHaveAttribute("aria-disabled", "true");
  expect(within(screen.getByRole("rowgroup", { name: "Archived" })).getByRole("row")).toBe(rows[3]);
});

it("filters titles case-insensitively and updates the highlighted result", () => {
  const { input, onPick } = show("LOGIN");
  expect(screen.getAllByRole("row")).toHaveLength(2);
  fireEvent.change(input, { target: { value: "invoice" } });
  expect(screen.getAllByRole("row")).toHaveLength(1);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onPick).toHaveBeenCalledWith("older");
});

it("picks the keyboard-highlighted row and skips the current conversation", () => {
  const { input, onPick } = show();
  expect(input).toHaveFocus();
  fireEvent.click(screen.getByText("Fix the parser"));
  expect(onPick).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "ArrowUp" });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onPick).toHaveBeenCalledWith("older");
});

it("closes on Escape without switching", () => {
  const { input, onPick, onClose } = show();
  fireEvent.keyDown(input, { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
  expect(onPick).not.toHaveBeenCalled();
});

it("shows an empty state when the project has no earlier conversations", () => {
  const { input, onPick } = show("", []);
  expect(screen.getByText("No earlier conversations in this project")).toBeInTheDocument();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onPick).not.toHaveBeenCalled();
});

it("shows the empty state when the only conversation is the current one", () => {
  const { input, onPick } = show("", chats.filter(chat => chat.id === "current"));
  expect(screen.getByRole("status")).toHaveTextContent("No earlier conversations in this project");
  expect(screen.getByRole("row")).toHaveTextContent("Fix the parser");
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onPick).not.toHaveBeenCalled();
});

it("says the filter matched nothing when other conversations exist", () => {
  show("parser");
  expect(screen.getByRole("status")).toHaveTextContent("No conversations match this filter");
});
