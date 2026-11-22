import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { putSettings: vi.fn() } };
});
import { api, type Settings } from "../lib/api";
import { ToastProvider } from "./Toast";
import { AppPanel } from "./AppPanel";

const onUpdated = vi.fn();
const show = (s: Record<string, unknown> = {}) => render(<ToastProvider><AppPanel s={{ confirm_quit: "", ...s } as unknown as Settings} onUpdated={onUpdated} /></ToastProvider>);

beforeEach(() => {
  onUpdated.mockReset();
  vi.mocked(api.putSettings).mockReset();
  vi.mocked(api.putSettings).mockImplementation(async (patch) => patch as any);
});

describe("AppPanel", () => {
  it("asks the close guard to restart or to quit, the same request the window's close button makes", () => {
    const seen: string[] = [];
    const a = () => seen.push("reboot"), b = () => seen.push("close");
    window.addEventListener("app-reboot-request", a);
    window.addEventListener("app-close-request", b);
    try {
      show();
      fireEvent.click(screen.getByRole("button", { name: "Restart the app" }));
      fireEvent.click(screen.getByRole("button", { name: "Quit the app" }));
      expect(seen).toEqual(["reboot", "close"]);
    } finally {
      window.removeEventListener("app-reboot-request", a);
      window.removeEventListener("app-close-request", b);
    }
  });

  it("shows Ask before quitting on by default and off when the person ticked Do not ask me again", () => {
    const { unmount } = show();
    expect(screen.getByRole("switch", { name: "Ask before quitting" })).toBeChecked();
    unmount();
    show({ confirm_quit: "off" });
    expect(screen.getByRole("switch", { name: "Ask before quitting" })).not.toBeChecked();
  });

  it("turns the question off and on again in the settings", async () => {
    show();
    fireEvent.click(screen.getByRole("switch", { name: "Ask before quitting" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ confirm_quit: "off" }));
    expect(await screen.findByText("The app quits without asking.")).toBeInTheDocument();
    expect(onUpdated).toHaveBeenCalledWith({ confirm_quit: "off" });
  });

  it("says in words when the choice could not be saved", async () => {
    vi.mocked(api.putSettings).mockRejectedValue(new Error("The settings file is read-only."));
    show({ confirm_quit: "off" });
    fireEvent.click(screen.getByRole("switch", { name: "Ask before quitting" }));
    expect(await screen.findByText(/Not saved: .*read-only/)).toBeInTheDocument();
    expect(onUpdated).not.toHaveBeenCalled();
  });
});
