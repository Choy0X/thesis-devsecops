import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { settingsMigrations: vi.fn(), acknowledgeSettingsMigrations: vi.fn() } };
});
import { ApiError, api } from "../lib/api";
import { SettingsChanges } from "./SettingsChanges";

const migration = { id: "ollama-cutover-2026-10", at: "2026-10-09T20:00:00Z", acknowledged: false,
  changes: ["The embeddings endpoint settings were removed.", "Your language-model endpoint is Ollama's address; the source is now the local server."] };

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
});

describe("SettingsChanges", () => {
  it("shows nothing when nothing changed", async () => {
    vi.mocked(api.settingsMigrations).mockResolvedValue({ migrations: [] });
    const { container } = render(<SettingsChanges />);
    await waitFor(() => expect(api.settingsMigrations).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing when the list cannot be read", async () => {
    vi.mocked(api.settingsMigrations).mockRejectedValue(new Error("down"));
    const { container } = render(<SettingsChanges />);
    await waitFor(() => expect(api.settingsMigrations).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("lists what was found and done, and hides it once it is acknowledged", async () => {
    vi.mocked(api.settingsMigrations).mockResolvedValue({ migrations: [migration] });
    vi.mocked(api.acknowledgeSettingsMigrations).mockResolvedValue({ acknowledged: 1 });
    render(<SettingsChanges />);
    const list = await screen.findByRole("list", { name: "Changes made to your settings" });
    expect(list).toHaveTextContent("The embeddings endpoint settings were removed.");
    expect(list).toHaveTextContent("the source is now the local server");
    fireEvent.click(screen.getByRole("button", { name: "Got it" }));
    await waitFor(() => expect(screen.queryByText("What changed in your settings")).not.toBeInTheDocument());
    expect(api.acknowledgeSettingsMigrations).toHaveBeenCalledWith(["ollama-cutover-2026-10"]);
  });

  it("keeps the list and says why when the acknowledgement fails", async () => {
    vi.mocked(api.settingsMigrations).mockResolvedValue({ migrations: [migration] });
    vi.mocked(api.acknowledgeSettingsMigrations).mockRejectedValue(new ApiError({ status: 500, title: "Could not save", detail: "The disk is full." }));
    render(<SettingsChanges />);
    fireEvent.click(await screen.findByRole("button", { name: "Got it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/disk is full|Could not save/);
    expect(screen.getByText("What changed in your settings")).toBeInTheDocument();
  });
});
