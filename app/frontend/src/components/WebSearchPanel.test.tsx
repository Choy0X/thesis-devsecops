import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { putSettings: vi.fn(), revealSecret: vi.fn() } };
});
import { api, type Settings } from "../lib/api";
import { ConfirmProvider } from "./hud/AlertDialog";
import { ToastProvider } from "./Toast";
import { WebSearchPanel } from "./WebSearchPanel";

const settings = (over: Partial<Settings> = {}): Settings => ({ web_search_provider: "", brave_api_key_set: false, brave_api_key_hint: "",
  google_api_key_set: false, google_api_key_hint: "", google_cse_id: "", ...over } as unknown as Settings);
const onUpdated = vi.fn();
const show = (s: Settings = settings()) => render(<ToastProvider><ConfirmProvider><WebSearchPanel s={s} onUpdated={onUpdated} /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  onUpdated.mockReset();
  vi.mocked(api.putSettings).mockImplementation(async (patch) => settings(patch as Partial<Settings>));
});

describe("WebSearchPanel", () => {
  it("defaults to Off and shows no provider fields", () => {
    show();
    expect(screen.getByRole("radio", { name: "Off" })).toBeChecked();
    expect(screen.queryByLabelText("Brave API key")).toBeNull();
    expect(screen.queryByLabelText("Google API key")).toBeNull();
  });

  it("saves the provider as soon as it is picked", async () => {
    show();
    fireEvent.click(screen.getByRole("radio", { name: "Brave Search" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ web_search_provider: "brave" }));
    expect(onUpdated).toHaveBeenCalled();
  });

  it("shows the Brave key field once Brave is chosen, and saves it only once typed", async () => {
    show(settings({ web_search_provider: "brave" }));
    expect(screen.queryByRole("button", { name: "Save the key" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Brave API key"), { target: { value: "a-real-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the key" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ brave_api_key: "a-real-key" }));
  });

  it("shows the Google key and engine id fields once Google is chosen, and saves them together", async () => {
    show(settings({ web_search_provider: "google" }));
    fireEvent.change(screen.getByLabelText("Google API key"), { target: { value: "g-key" } });
    fireEvent.change(screen.getByLabelText("Custom Search Engine id"), { target: { value: "cse-123" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ google_api_key: "g-key", google_cse_id: "cse-123" }));
  });

  it("offers Save for Google once only the engine id changed, with no new key typed", () => {
    show(settings({ web_search_provider: "google", google_cse_id: "old-cse" }));
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Custom Search Engine id"), { target: { value: "new-cse" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("says DuckDuckGo needs no key and warns that it is unofficial", () => {
    show(settings({ web_search_provider: "duckduckgo" }));
    expect(screen.getByText(/It is an unofficial endpoint, not a published API/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Brave API key")).toBeNull();
    expect(screen.queryByLabelText("Google API key")).toBeNull();
  });
});
