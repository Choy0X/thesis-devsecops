import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { PermissionsPanel } from "./PermissionsPanel";
import { ToastProvider } from "./Toast";

vi.mock("../lib/api", async (orig) => ({ ...(await orig<typeof import("../lib/api")>()), api: { agentPermissions: vi.fn(), agentPermissionsSave: vi.fn() } }));

const panel = () => render(<ToastProvider><PermissionsPanel /></ToastProvider>);

describe("PermissionsPanel", () => {
  beforeEach(() => {
    vi.mocked(api.agentPermissions).mockResolvedValue({ rules: [{ tool: "bash", pattern: "git status *", action: "allow" }] });
    vi.mocked(api.agentPermissionsSave).mockImplementation(async (rules) => ({ rules }));
  });

  it("lists the rules the owner has", async () => {
    panel();
    const list = await screen.findByRole("list", { name: "Rules" });
    expect((within(list).getByLabelText("Rule 1: pattern") as HTMLInputElement).value).toBe("git status *");
  });

  it("says so when there are none", async () => {
    vi.mocked(api.agentPermissions).mockResolvedValue({ rules: [] });
    panel();
    expect(await screen.findByText(/No rules yet/)).toBeTruthy();
  });

  it("adds a rule, saves it, and keeps Save off until something changed", async () => {
    const user = userEvent.setup();
    panel();
    await screen.findByLabelText("Rule 1: pattern");
    const save = screen.getByRole("button", { name: "Save the rules" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "Add a rule" }));
    await user.type(screen.getByLabelText("Rule 2: pattern"), "git push *");
    expect(save.disabled).toBe(false);
    await user.click(save);
    await waitFor(() => expect(api.agentPermissionsSave).toHaveBeenCalledWith([{ tool: "bash", pattern: "git status *", action: "allow" }, { tool: "bash", pattern: "git push *", action: "ask" }]));
    await waitFor(() => expect(save.disabled).toBe(true));
  });

  it("shows the rule that the dependency scan dialog saves under its own name, and offers the kind for a new rule", async () => {
    vi.mocked(api.agentPermissions).mockResolvedValue({ rules: [{ tool: "depscan", pattern: "*", action: "allow" }] });
    const user = userEvent.setup();
    panel();
    const list = await screen.findByRole("list", { name: "Rules" });
    expect(within(list).getByRole("combobox", { name: "Rule 1: kind of call" })).toHaveTextContent("Dependency scan");
    await user.click(within(list).getByRole("combobox", { name: "Rule 1: kind of call" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toContain("Dependency scan");
  });

  it("removes a rule", async () => {
    const user = userEvent.setup();
    panel();
    await screen.findByLabelText("Rule 1: pattern");
    await user.click(screen.getByRole("button", { name: "Remove rule 1" }));
    expect(screen.queryByLabelText("Rule 1: pattern")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save the rules" }));
    await waitFor(() => expect(api.agentPermissionsSave).toHaveBeenCalledWith([]));
  });

  it("shows why the rules could not be read, with a way to try again", async () => {
    vi.mocked(api.agentPermissions).mockRejectedValueOnce(new Error("The server is not there."));
    panel();
    expect(await screen.findByRole("alert")).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Rule 1: pattern")).toBeTruthy();
  });
});
