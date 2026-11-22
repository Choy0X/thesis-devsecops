import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatSummary } from "../../lib/api";
import { ConfirmProvider } from "../hud/AlertDialog";
import { ConversationList, dayGroup } from "./ConversationList";

const NOW = new Date(2026, 9, 13, 15, 30);                                         // 13 October 2026, a Tuesday, 15:30 on this computer
const at = (daysAgo: number, hour = 9) => new Date(2026, 9, 13 - daysAgo, hour, 0, 0).toISOString();
const chat = (id: string, title: string, daysAgo: number, over: Partial<ChatSummary> = {}): ChatSummary => ({ id, title, created_at: at(daysAgo), updated_at: at(daysAgo), message_count: 2, preview: `about ${title}`, ...over });

const show = (chats: ChatSummary[], props: Partial<React.ComponentProps<typeof ConversationList>> = {}) => {
  const handlers = { onSelect: vi.fn(), onNew: vi.fn(), onRename: vi.fn(), onDelete: vi.fn() };
  render(<ConfirmProvider><ConversationList chats={chats} activeId={null} now={NOW} {...handlers} {...props} /></ConfirmProvider>);
  return handlers;
};

describe("dayGroup", () => {
  it("groups by the calendar day of this computer", () => {
    expect(dayGroup(at(0, 0), NOW)).toBe("Today");
    expect(dayGroup(at(0, 23), NOW)).toBe("Today");
    expect(dayGroup(at(1, 23), NOW)).toBe("Yesterday");
    expect(dayGroup(at(1, 0), NOW)).toBe("Yesterday");
    expect(dayGroup(at(2), NOW)).toBe("Previous 7 days");
    expect(dayGroup(at(6), NOW)).toBe("Previous 7 days");
    expect(dayGroup(at(7), NOW)).toBe("Older");
    expect(dayGroup(at(400), NOW)).toBe("Older");
  });

  it("puts a time it cannot read under Older and a time in the future under Today", () => {
    expect(dayGroup("not a time", NOW)).toBe("Older");
    expect(dayGroup("", NOW)).toBe("Older");
    expect(dayGroup(new Date(2026, 9, 14, 8).toISOString(), NOW)).toBe("Today");
  });
});

describe("ConversationList", () => {
  it("lists the conversations in groups, in the order given, with a preview each", () => {
    show([chat("a", "Today one", 0), chat("b", "Yesterday one", 1), chat("c", "Last week", 4), chat("d", "Ancient", 30), chat("e", "Today two", 0)]);
    const groups = screen.getAllByRole("region").map((g) => g.getAttribute("aria-label"));
    expect(groups).toEqual(["Today", "Yesterday", "Previous 7 days", "Older"]);
    const today = screen.getByRole("region", { name: "Today" });
    expect(within(today).getAllByRole("button", { name: /^Today/ }).map((b) => b.textContent)).toEqual(["Today oneabout Today one", "Today twoabout Today two"]);
    expect(screen.getByRole("heading", { name: "Yesterday" })).toBeInTheDocument();
  });

  it("leaves out a group with nothing in it", () => {
    show([chat("a", "Only old", 40)]);
    expect(screen.getAllByRole("region").map((g) => g.getAttribute("aria-label"))).toEqual(["Older"]);
  });

  it("says there is none yet when there is none, and shows the filter only for a longer list", () => {
    const { unmount } = render(<ConfirmProvider><ConversationList chats={[]} activeId={null} onSelect={vi.fn()} onNew={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} /></ConfirmProvider>);
    expect(screen.getByText("No conversation yet. Ask a question to start one.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Filter the conversations" })).toBeNull();
    unmount();
    show([chat("a", "A", 0), chat("b", "B", 0), chat("c", "C", 0), chat("d", "D", 0)]);
    expect(screen.queryByRole("textbox", { name: "Filter the conversations" })).toBeNull();
  });

  it("filters by the title and by what the last message said, and says when nothing matches", () => {
    show([chat("a", "Login flow", 0, { preview: "sessions are stored in a cookie" }), chat("b", "Dependencies", 0, { preview: "lodash is old" }), chat("c", "c", 0), chat("d", "d", 0), chat("e", "e", 0)]);
    const filter = screen.getByRole("textbox", { name: "Filter the conversations" });
    fireEvent.change(filter, { target: { value: "LOGIN" } });
    expect(screen.getAllByRole("button", { name: /^Login flow/ })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /^Dependencies/ })).toBeNull();
    fireEvent.change(filter, { target: { value: "lodash" } });
    expect(screen.getByRole("button", { name: /^Dependencies/ })).toBeInTheDocument();
    fireEvent.change(filter, { target: { value: "zzz" } });
    expect(screen.getByRole("status")).toHaveTextContent('No conversation matches "zzz".');
    expect(screen.queryByRole("region")).toBeNull();
    fireEvent.change(filter, { target: { value: "  " } });
    expect(screen.getAllByRole("region")).toHaveLength(1);
  });

  it("opens a conversation, and says which one is open", () => {
    const h = show([chat("a", "First", 0), chat("b", "Second", 0)], { activeId: "b" });
    expect(screen.getByRole("button", { name: /^Second/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: /^First/ })).not.toHaveAttribute("aria-current");
    fireEvent.click(screen.getByRole("button", { name: /^First/ }));
    expect(h.onSelect).toHaveBeenCalledWith("a");
  });

  it("starts a new chat", () => {
    const h = show([]);
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(h.onNew).toHaveBeenCalledTimes(1);
  });

  it("renames with Enter, and not when the name is empty or unchanged", async () => {
    const h = show([chat("a", "Old name", 0)]);
    fireEvent.click(screen.getByRole("button", { name: "Rename Old name" }));
    const box = screen.getByRole("textbox", { name: "New name of Old name" });
    expect(box).toHaveValue("Old name");
    fireEvent.change(box, { target: { value: "  A better name  " } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(h.onRename).toHaveBeenCalledWith("a", "A better name"));
    expect(screen.queryByRole("textbox", { name: /New name/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Rename Old name" }));
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Rename Old name" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "   " } });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
    expect(h.onRename).toHaveBeenCalledTimes(1);
  });

  it("keeps the old name on Escape and renames when the box loses the focus", async () => {
    const h = show([chat("a", "Old name", 0)]);
    fireEvent.click(screen.getByRole("button", { name: "Rename Old name" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "never kept" } });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(h.onRename).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Rename Old name" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "kept on blur" } });
    fireEvent.blur(screen.getByRole("textbox"));
    await waitFor(() => expect(h.onRename).toHaveBeenCalledWith("a", "kept on blur"));
  });

  it("asks before deleting, names the conversation, and deletes only when the person says yes", async () => {
    const h = show([chat("a", "Doomed", 0)]);
    fireEvent.click(screen.getByRole("button", { name: "Delete Doomed" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent('Delete "Doomed"?');
    expect(ask).toHaveTextContent("removed from this computer");
    fireEvent.click(within(ask).getByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(h.onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete Doomed" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete it" }));
    await waitFor(() => expect(h.onDelete).toHaveBeenCalledWith("a"));
  });

  it("says how many files of conversations could not be read", () => {
    show([chat("a", "A", 0)], { damaged: 1 });
    expect(screen.getByText("1 file of a conversation could not be read and is not listed.")).toBeInTheDocument();
  });

  it("says it in the plural, and says nothing when every file was read", () => {
    const { unmount } = render(<ConfirmProvider><ConversationList chats={[chat("a", "A", 0)]} activeId={null} damaged={3} now={NOW} onSelect={vi.fn()} onNew={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} /></ConfirmProvider>);
    expect(screen.getByText("3 files of conversations could not be read and are not listed.")).toBeInTheDocument();
    unmount();
    show([chat("a", "A", 0)], { damaged: 0 });
    expect(screen.queryByText(/could not be read/)).toBeNull();
  });

  it("keeps the person in the conversation that is being answered: no new chat, no other conversation, no rename or delete of it", () => {
    show([chat("a", "Answering", 0), chat("b", "Other", 0)], { activeId: "a", busy: true });
    expect(screen.getByRole("button", { name: "New chat" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Other/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Answering/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Rename Answering" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete Answering" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rename Other" })).toBeEnabled();
  });

  it("has no Rename button at all for a list whose items cannot be renamed", () => {
    show([chat("a", "Session one", 0)], { onRename: undefined });
    expect(screen.queryByRole("button", { name: /^Rename/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Delete Session one" })).toBeInTheDocument();
  });

  it("uses a given nav label and New-chat text in place of the defaults", () => {
    show([], { navLabel: "Agent sessions", newLabel: "New session" });
    expect(screen.getByRole("navigation", { name: "Agent sessions" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New session" })).toBeInTheDocument();
  });
});
