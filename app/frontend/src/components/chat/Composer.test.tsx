import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";

const setup = (props: Partial<React.ComponentProps<typeof Composer>> = {}) => {
  const onSend = vi.fn().mockResolvedValue(true);
  const onStop = vi.fn();
  render(<Composer onSend={onSend} onStop={onStop} busy={false} {...props} />);
  return { onSend, onStop, box: screen.getByRole("textbox", { name: "Your question" }) as HTMLTextAreaElement };
};
const type = (box: HTMLElement, value: string) => fireEvent.change(box, { target: { value } });

describe("Composer", () => {
  it("sends the trimmed question with Enter, empties the box and keeps the focus in it", async () => {
    const { onSend, box } = setup();
    box.focus();
    type(box, "  What is wrong here?  ");
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("What is wrong here?"));
    await waitFor(() => expect(box).toHaveValue(""));
    expect(box).toHaveFocus();
  });

  it("starts a new line with Shift+Enter instead of sending", () => {
    const { onSend, box } = setup();
    type(box, "line one");
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not send while a key is only choosing a character in an input method", () => {
    const { onSend, box } = setup();
    type(box, "nihao");
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("sends with the Send button, which is off while the box is empty or blank", async () => {
    const { onSend, box } = setup();
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    type(box, "   ");
    expect(send).toBeDisabled();
    type(box, "a question");
    expect(send).toBeEnabled();
    fireEvent.click(send);
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("a question"));
  });

  it("sends nothing for an empty box, and nothing while it is busy or disabled", () => {
    const first = setup();
    fireEvent.keyDown(first.box, { key: "Enter" });
    expect(first.onSend).not.toHaveBeenCalled();
  });

  it("is Stop while an answer is written, and Stop calls onStop", () => {
    const { onStop, onSend, box } = setup({ busy: true });
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
    type(box, "typed while it writes");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(box).toHaveValue("typed while it writes");
  });

  it("is off when it is disabled", () => {
    const { box, onSend } = setup({ disabled: true });
    expect(box).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("keeps the words when sending was refused, and clears them when it returns nothing at all", async () => {
    const onSend = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(undefined);
    render(<Composer onSend={onSend} onStop={() => undefined} busy={false} />);
    const box = screen.getByRole("textbox", { name: "Your question" });
    type(box, "first try");
    fireEvent.keyDown(box, { key: "Enter" });
    await act(async () => { await Promise.resolve(); });
    expect(box).toHaveValue("first try");
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(box).toHaveValue(""));
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it("has as many rows as lines, between one and eight", () => {
    const { box } = setup();
    expect(box).toHaveAttribute("rows", "1");
    type(box, "a\nb\nc");
    expect(box).toHaveAttribute("rows", "3");
    type(box, Array.from({ length: 20 }, (_, i) => `line ${i}`).join("\n"));
    expect(box).toHaveAttribute("rows", "8");
  });

  it("says how to send, and the hint can be replaced", () => {
    const { unmount } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} busy={false} />);
    expect(screen.getByText(/Press Enter to send and Shift\+Enter for a new line/)).toBeInTheDocument();
    unmount();
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} busy={false} hint="Pick a model first." placeholder="Ask" />);
    expect(screen.getByText("Pick a model first.")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Ask")).toBeInTheDocument();
  });

  it("shows beforeSend between the box and the Send button", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} busy={false} beforeSend={<button type="button">Mode: Plan</button>} />);
    expect(screen.getByRole("button", { name: "Mode: Plan" })).toBeInTheDocument();
  });

  it("sizes Send and Stop as tall as the box itself, not the kit's own shorter default", () => {
    const { rerender } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} busy={false} />);
    expect(screen.getByRole("button", { name: "Send" }).className).toContain("h-[2.75rem]");
    rerender(<Composer onSend={vi.fn()} onStop={vi.fn()} busy />);
    expect(screen.getByRole("button", { name: "Stop" }).className).toContain("h-[2.75rem]");
  });

  it("shows no slash menu at all when commands is false, and still sends a leading slash as a plain question", async () => {
    const onSend = vi.fn().mockResolvedValue(true);
    render(<Composer onSend={onSend} onStop={vi.fn()} busy={false} commands={false} />);
    const box = screen.getByRole("textbox", { name: "Your question" });
    type(box, "/plan");
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("/plan"));
  });
});

describe("the slash menu", () => {
  it("opens on a leading slash, lists every command, and closes once a space ends the word", () => {
    const { box } = setup();
    type(box, "/");
    const menu = screen.getByRole("listbox", { name: "Commands" });
    expect(within(menu).getAllByRole("option")).toHaveLength(14);
    type(box, "/plan ");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("narrows the list to the commands that start with what was typed", () => {
    const { box } = setup();
    type(box, "/co");
    const names = within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.textContent);
    expect(names.some((t) => t?.includes("/compact"))).toBe(true);
    expect(names.some((t) => t?.includes("/context"))).toBe(true);
    expect(names.some((t) => t?.includes("/resume"))).toBe(false);
  });

  it("shows no menu for an unrecognized leading slash, so the words can still be sent as a plain question", async () => {
    const { box, onSend } = setup();
    type(box, "/nowhere near a command");
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("/nowhere near a command"));
  });

  it("moves the highlight with the arrow keys and picks the highlighted command with Enter, without sending", () => {
    const { box, onSend } = setup();
    type(box, "/");
    const optionNamed = (n: number) => within(screen.getByRole("listbox")).getAllByRole("option")[n];
    expect(optionNamed(0)).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(optionNamed(0)).toHaveAttribute("aria-selected", "false");
    expect(optionNamed(1)).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(optionNamed(0)).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(box).toHaveValue("/resume ");
    expect(onSend).not.toHaveBeenCalled();
    expect(box).toHaveFocus();
  });

  it("picks with Tab the same way as Enter", () => {
    const { box } = setup();
    type(box, "/clear");
    fireEvent.keyDown(box, { key: "Tab" });
    expect(box).toHaveValue("/clear ");
  });

  it("picks with a click of the pointer", () => {
    const { box } = setup();
    type(box, "/");
    fireEvent.mouseDown(screen.getByRole("option", { name: /\/status/ }));
    expect(box).toHaveValue("/status ");
  });

  it("closes on Escape without changing the draft, and reopens once more is typed", () => {
    const { box } = setup();
    type(box, "/pl");
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(box).toHaveValue("/pl");
    type(box, "/pla");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });
});
