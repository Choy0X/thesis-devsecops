import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Menu } from "./Menu";

const items = (over: { onSelect?: () => void } = {}) => [
  { key: "md", label: "Markdown (.md)", href: "/export?format=md", download: true },
  { key: "json", label: "JSON (.json)", description: "Everything, for another program", href: "/export?format=json", download: true },
  { key: "act", label: "An action", onSelect: over.onSelect ?? (() => undefined) },
  { key: "off", label: "Not now", disabled: true, onSelect: () => undefined },
];
const show = (extra: Partial<React.ComponentProps<typeof Menu>> = {}) => render(<div><Menu label="Export" items={items()} {...extra} /><button type="button">elsewhere</button></div>);

describe("Menu", () => {
  it("is a closed button that says it opens a menu, and opens a list of entries when pressed", async () => {
    const user = userEvent.setup();
    show();
    const button = screen.getByRole("button", { name: "Export" });
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).toBeNull();
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    const menu = screen.getByRole("menu", { name: "Export" });
    expect(menu).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem").map((e) => e.textContent)).toEqual(["Markdown (.md)", "JSON (.json)Everything, for another program", "An action", "Not now"]);
  });

  it("makes an entry with a link a link that is saved, and closes after it is used", async () => {
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole("button", { name: "Export" }));
    const md = screen.getByRole("menuitem", { name: "Markdown (.md)" });
    expect(md).toHaveAttribute("href", "/export?format=md");
    expect(md).toHaveAttribute("download");
    md.addEventListener("click", (e) => e.preventDefault());                           // jsdom does not navigate; the menu must still close
    await user.click(md);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("runs an action and closes, giving the focus back to the button", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<Menu label="More" items={items({ onSelect })} />);
    await user.click(screen.getByRole("button", { name: "More" }));
    await user.click(screen.getByRole("menuitem", { name: "An action" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("button", { name: "More" })).toHaveFocus();
  });

  it("puts the focus on the first entry, moves with the arrows past an entry that is disabled, and wraps", async () => {
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole("button", { name: "Export" }));
    expect(screen.getByRole("menuitem", { name: "Markdown (.md)" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: /JSON/ })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "An action" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Markdown (.md)" })).toHaveFocus();                 // the disabled one is skipped, and the end wraps to the start
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("menuitem", { name: "An action" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("menuitem", { name: "Markdown (.md)" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("menuitem", { name: "An action" })).toHaveFocus();
  });

  it("closes on Escape and gives the focus back to the button", async () => {
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole("button", { name: "Export" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("button", { name: "Export" })).toHaveFocus();
  });

  it("closes when something else is clicked, and on Tab", async () => {
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole("button", { name: "Export" }));
    await user.click(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByRole("menu")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Export" }));
    await user.keyboard("{Tab}");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("opens with the Down arrow from the button, and toggles shut when the button is pressed again", async () => {
    const user = userEvent.setup();
    show();
    screen.getByRole("button", { name: "Export" }).focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Export" }));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("does not open when disabled and does not run a disabled entry", async () => {
    const user = userEvent.setup();
    const picked = vi.fn();
    const { unmount } = render(<Menu label="Locked" items={items()} disabled />);
    await user.click(screen.getByRole("button", { name: "Locked" }));
    expect(screen.queryByRole("menu")).toBeNull();
    unmount();
    render(<Menu label="Open" items={[{ key: "off", label: "Not now", disabled: true, onSelect: picked }]} />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("menuitem", { name: "Not now" })).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("menuitem", { name: "Not now" }));
    expect(picked).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeInTheDocument();                                          // and the menu stays open
  });
});
