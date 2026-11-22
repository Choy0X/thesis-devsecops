import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { putSettings: vi.fn(), getSettings: vi.fn(), testConnection: vi.fn() } };
});
import { api, type Settings } from "../lib/api";
import { choose } from "../test/select";
import { resetAlerts } from "../lib/jobAlerts";
import { ConfirmProvider } from "./hud/AlertDialog";
import { ToastProvider } from "./Toast";
import { DesktopNotificationsPanel, EmailPanel } from "./NotificationPanels";

const smtp = { smtp_host: "smtp.example.com", smtp_port: "587", smtp_user: "me", smtp_password: "", smtp_password_set: true, smtp_password_hint: "wxyz", smtp_from: "alerts@example.com", smtp_to: "me@example.com", notify_email: "" };
const none = { ...smtp, smtp_host: "", smtp_user: "", smtp_password_set: false, smtp_from: "", smtp_to: "" };
const settings = (over: Record<string, unknown> = {}) => ({ ...smtp, ...over }) as unknown as Settings;
const onUpdated = vi.fn();
const showEmail = (s: Settings) => render(<ToastProvider><ConfirmProvider><EmailPanel s={s} onUpdated={onUpdated} /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  onUpdated.mockReset();
  resetAlerts();
  vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "" } as any);
  vi.mocked(api.putSettings).mockImplementation(async (patch) => ({ ...smtp, ...patch }) as any);
});

describe("DesktopNotificationsPanel", () => {
  it("has the switch, on by default, and saves a change in the settings", async () => {
    render(<ToastProvider><DesktopNotificationsPanel /></ToastProvider>);
    const box = await screen.findByRole("switch", { name: "Tell me when a job finishes or stops" });
    expect(box).toBeChecked();
    fireEvent.click(box);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ desktop_notify: "off" }));
    expect(box).not.toBeChecked();
  });
});

describe("EmailPanel", () => {
  it("offers the choice of when to write only once a mail server is set up, and says so until then", () => {
    const { unmount } = showEmail(settings(none));
    expect(screen.getByText("Set up the email server above to be told by email when a job ends.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Email when a job ends" })).toBeNull();
    unmount();
    showEmail(settings());
    expect(screen.getByRole("combobox", { name: "Email when a job ends" })).toHaveTextContent("Off");
    expect(screen.queryByText(/Set up the email server above/)).toBeNull();
  });

  it("saves the choice at once: Off, Failed or All", async () => {
    const user = userEvent.setup();
    showEmail(settings());
    await choose(user, screen.getByRole("combobox", { name: "Email when a job ends" }), "Failed");
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ notify_email: "failed" }));
    expect(await screen.findByText("You will get a letter when a job ends.")).toBeInTheDocument();
  });

  it("shows the server as saved, never the password, and sends a password only when a new one is typed", async () => {
    showEmail(settings());
    expect(screen.getByLabelText("Host")).toHaveValue("smtp.example.com");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByTestId("secret-saved")).toHaveTextContent("wxyz");
    fireEvent.change(screen.getByLabelText("Port"), { target: { value: "465" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ smtp_host: "smtp.example.com", smtp_port: "465", smtp_user: "me", smtp_from: "alerts@example.com", smtp_to: "me@example.com" }));
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenLastCalledWith(expect.objectContaining({ smtp_password: "hunter2" })));
  });

  it("sends a test email to the recipient on the form and says how it went", async () => {
    vi.mocked(api.testConnection).mockResolvedValue({ ok: true, detail: "Test email sent to me@example.com." });
    showEmail(settings());
    fireEvent.click(screen.getByRole("button", { name: "Send a test email" }));
    const dialog = await screen.findByRole("dialog", { name: "Test the email server" });
    expect(within(dialog).getByLabelText("Send test email to")).toHaveValue("me@example.com");
    fireEvent.click(within(dialog).getByRole("button", { name: "Send test email" }));
    expect(await within(dialog).findByText(/It works\. Test email sent to me@example\.com\./)).toBeInTheDocument();
    expect(api.testConnection).toHaveBeenCalledWith("smtp", expect.objectContaining({ smtp_host: "smtp.example.com", input: "me@example.com" }));
  });
});
