import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Bell, Cpu } from "lucide-react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { SettingsMenu } from "./SettingsMenu";

const items = [{ id: "engines", label: "Engines", icon: Cpu }, { id: "notify", label: "Notifications", icon: Bell }];
const to = (id: string) => (id === "engines" ? "/settings" : `/settings?tab=${id}`);
function Where() { const l = useLocation(); return <p data-testid="where">{l.pathname}{l.search}</p>; }

describe("SettingsMenu", () => {
  it("is a navigation of links, one per group, with the current one marked and the others not", () => {
    render(<MemoryRouter><SettingsMenu items={items} value="notify" to={to} /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Settings groups" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(["Engines", "Notifications"]);
    expect(within(nav).getByRole("link", { name: "Notifications" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Engines" })).not.toHaveAttribute("aria-current");
  });

  it("draws the current entry like the current section of the rail, and the others without a frame", () => {
    render(<MemoryRouter><SettingsMenu items={items} value="engines" to={to} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Engines" }).className).toMatch(/chamfer-sm.*bg-brand-tint-strong/);
    expect(screen.getByRole("link", { name: "Notifications" }).className).toMatch(/border-transparent/);
  });

  it("gives every group an address of its own, so that a click goes there and the back button returns", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/settings"]}><SettingsMenu items={items} value="engines" to={to} /><Where /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Notifications" })).toHaveAttribute("href", "/settings?tab=notify");
    await user.click(screen.getByRole("link", { name: "Notifications" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/settings?tab=notify");
  });

  it("has an icon for each entry that a screen reader skips", () => {
    render(<MemoryRouter><SettingsMenu items={items} value="engines" to={to} /></MemoryRouter>);
    for (const link of screen.getAllByRole("link")) expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});
