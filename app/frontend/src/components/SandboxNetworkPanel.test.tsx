import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { putSettings: vi.fn() } };
});
import { api, type Settings } from "../lib/api";
import { SandboxNetworkPanel } from "./SandboxNetworkPanel";
import { ToastProvider } from "./Toast";

const settings = (network?: string) => ({ agent_sandbox_network: network } as unknown as Settings);
const show = (s: Settings, onUpdated = vi.fn()) => ({ onUpdated, ...render(<ToastProvider><SandboxNetworkPanel s={s} onUpdated={onUpdated} /></ToastProvider>) });

beforeEach(() => { vi.mocked(api.putSettings).mockReset(); });

describe("SandboxNetworkPanel", () => {
  it("shows Off when the setting is empty or anything other than proxy, and no warning", () => {
    for (const value of [undefined, "", "off", "bridge"]) {
      const { unmount } = show(settings(value));
      expect((screen.getByRole("radio", { name: /^Off/ }) as HTMLInputElement).checked).toBe(true);
      expect((screen.getByRole("radio", { name: /guarded proxy/ }) as HTMLInputElement).checked).toBe(false);
      expect(screen.queryByRole("note")).toBeNull();
      unmount();
    }
  });

  it("says what the proxy refuses in the choice itself", () => {
    show(settings("off"));
    const text = screen.getByRole("radiogroup").textContent || "";
    expect(text).toContain("public web addresses on ports 80 and 443");
    expect(text).toContain("refuses this computer");
    expect(text).toContain("the database");
  });

  it("warns, when proxy is on, that the project's code can still send what is in the sandbox to a public address", () => {
    show(settings("proxy"));
    expect((screen.getByRole("radio", { name: /guarded proxy/ }) as HTMLInputElement).checked).toBe(true);
    const note = screen.getByRole("note");
    expect(note.textContent).toContain("does not keep the project's own code away from the internet");
    expect(note.textContent).toContain("port 80 or 443");
  });

  it("saves the choice and hands the answer to the page", async () => {
    const user = userEvent.setup();
    const next = settings("proxy");
    vi.mocked(api.putSettings).mockResolvedValue(next);
    const { onUpdated } = show(settings("off"));
    await user.click(screen.getByRole("radio", { name: /guarded proxy/ }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ agent_sandbox_network: "proxy" }));
    await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(next));
  });

  it("does not save when the chosen value is the one already set", async () => {
    const user = userEvent.setup();
    show(settings("proxy"));
    await user.click(screen.getByRole("radio", { name: /guarded proxy/ }));
    expect(api.putSettings).not.toHaveBeenCalled();
  });

  it("says so when the save is refused and does not tell the page it changed", async () => {
    const user = userEvent.setup();
    vi.mocked(api.putSettings).mockRejectedValue(new Error("The agent sandbox's network is off or proxy"));
    const { onUpdated } = show(settings("off"));
    await user.click(screen.getByRole("radio", { name: /guarded proxy/ }));
    expect(await screen.findByText(/Not saved: .*off or proxy/)).toBeTruthy();
    expect(onUpdated).not.toHaveBeenCalled();
  });
});
