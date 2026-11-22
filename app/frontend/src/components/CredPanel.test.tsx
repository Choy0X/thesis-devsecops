import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { revealSecret: vi.fn(), putSettings: vi.fn(), testConnection: vi.fn() } };
});
import { api, type Settings } from "../lib/api";
import { ToastProvider } from "./Toast";
import { ConfirmProvider } from "./hud/AlertDialog";
import { CredPanel } from "./CredPanel";

const saved = { nvd_api_key: "", nvd_api_key_set: true, nvd_api_key_hint: "a1b2", github_token: "", github_token_set: false } as unknown as Settings;

const show = (s: Settings = saved) => render(
  <ToastProvider><ConfirmProvider>
    <CredPanel title="NVD" desc="API key for CVE enrichment." test="nvd" saved={s} onUpdated={() => undefined}
      fields={[{ k: "nvd_api_key", label: "API key", secret: true, hint: "Request a free key." }]} />
  </ConfirmProvider></ToastProvider>);

beforeEach(() => { for (const fn of Object.values(api)) (fn as any).mockReset?.(); });

describe("the key of a settings panel", () => {
  it("shows the saved key when the eye is pressed: the page asks the app for it by its setting name, only then", async () => {
    vi.mocked(api.revealSecret).mockResolvedValue("nvd-0123456789a1b2");
    const user = userEvent.setup();
    show();
    expect(api.revealSecret).not.toHaveBeenCalled();
    const input = screen.getByLabelText("API key");
    expect(input).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Show API key" }));
    await waitFor(() => expect(input).toHaveValue("nvd-0123456789a1b2"));
    expect(api.revealSecret).toHaveBeenCalledWith("nvd_api_key");
    expect(input).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide API key" }));
    expect(input).toHaveValue("");
  });

  it("tells the saved key apart by its last characters, in plain text", () => {
    show();
    expect(screen.getByTestId("secret-saved")).toHaveTextContent("saved...a1b2");
  });

  it("has nothing to ask for when no key is saved, and the eye shows what is typed", async () => {
    const user = userEvent.setup();
    show({ ...saved, nvd_api_key_set: false, nvd_api_key_hint: "" } as unknown as Settings);
    await user.type(screen.getByLabelText("API key"), "typed-key");
    await user.click(screen.getByRole("button", { name: "Show API key" }));
    expect(screen.getByLabelText("API key")).toHaveValue("typed-key");
    expect(api.revealSecret).not.toHaveBeenCalled();
  });

  it("does not send a key that was only shown: saving after Show sends nothing for it", async () => {
    vi.mocked(api.revealSecret).mockResolvedValue("nvd-0123456789a1b2");
    vi.mocked(api.putSettings).mockResolvedValue(saved);
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole("button", { name: "Show API key" }));
    await waitFor(() => expect(screen.getByLabelText("API key")).toHaveValue("nvd-0123456789a1b2"));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalled());
    expect(vi.mocked(api.putSettings).mock.calls[0][0]).toEqual({});
  });

  it("explains the failure under the field when the app does not answer", async () => {
    vi.mocked(api.revealSecret).mockRejectedValue(new Error("The app did not answer"));
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole("button", { name: "Show API key" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/could not be read/)).toBeInTheDocument();
  });
});
