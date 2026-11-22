import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { listJobs: vi.fn(), shutdown: vi.fn(), shutdownStatus: vi.fn(), shellCloseAck: vi.fn(), shellQuit: vi.fn(), shellReboot: vi.fn(), getSettings: vi.fn(), putSettings: vi.fn() } };
});
import { api, type Job, type ShutdownStatus } from "../lib/api";
import { resetJobsList } from "../lib/jobsList";
import { fileDraftsFor, resetFileDrafts } from "../lib/fileDrafts";
import { ConfirmProvider } from "./hud/AlertDialog";
import { CloseGuard } from "./CloseGuard";

const job = (id: string, stage: string, status = "running"): Job => ({ id, stage, status, params: {}, started: "", finished: "", error: "", log_count: 0 });
const status = (over: Partial<ShutdownStatus> = {}): ShutdownStatus => ({ running: false, done: true, started: "x", deadline_seconds: 25, ok: true,
  steps: [{ name: "Stopping jobs", ok: true, detail: "no job was running", seconds: 0.01 }, { name: "Closing the databases", ok: true, detail: "database connections closed", seconds: 0.02 }], ...over });
const requestClose = () => act(() => { window.dispatchEvent(new CustomEvent("app-close-request")); });
const requestReboot = () => act(() => { window.dispatchEvent(new CustomEvent("app-reboot-request")); });
const reload = vi.fn();
const mount = () => render(<ConfirmProvider><CloseGuard pollMs={5} backPollMs={5} backLimitMs={400} reload={reload} /></ConfirmProvider>);
const settle = (ms = 30) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

beforeEach(() => {
  resetJobsList();
  reload.mockReset();
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.shellCloseAck).mockResolvedValue({ ok: true });
  vi.mocked(api.shellQuit).mockResolvedValue({ ok: true });
  vi.mocked(api.shellReboot).mockResolvedValue({ ok: true, pid: 7 });
  vi.mocked(api.shutdown).mockResolvedValue(status());
  vi.mocked(api.getSettings).mockResolvedValue({ confirm_quit: "" } as any);
  vi.mocked(api.putSettings).mockResolvedValue({} as any);
});
afterEach(() => vi.unstubAllGlobals());

describe("closing the window", () => {
  it("asks Quit the app?, acknowledges the request, runs the shutdown and lets the window close when nothing is running", async () => {
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("Quit the app?");
    expect(api.shutdown).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Quit the app" }));
    expect(await screen.findByRole("dialog", { name: "Closing the app" })).toBeInTheDocument();
    await waitFor(() => expect(api.shellQuit).toHaveBeenCalledTimes(1));
    expect(api.shellCloseAck).toHaveBeenCalled();
    expect(api.shutdown).toHaveBeenCalledTimes(1);
    expect(api.putSettings).not.toHaveBeenCalled();
    expect(screen.getByText("Stopping jobs")).toBeInTheDocument();
    expect(screen.getByText(/database connections closed/)).toBeInTheDocument();
  });

  it("keeps the app open when the person says so", async () => {
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Keep it open" }));
    await settle();
    expect(api.shutdown).not.toHaveBeenCalled();
    expect(api.shellQuit).not.toHaveBeenCalled();
    requestClose();                                                  // and it can be asked again
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Quit the app?");
  });

  it("remembers Do not ask me again in the settings, and then closes without asking", async () => {
    const first = mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    const ask = await screen.findByRole("alertdialog");
    fireEvent.click(within(ask).getByRole("checkbox", { name: "Do not ask me again" }));
    fireEvent.click(within(ask).getByRole("button", { name: "Quit the app" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ confirm_quit: "off" }));
    await waitFor(() => expect(api.shellQuit).toHaveBeenCalledTimes(1));
    vi.mocked(api.getSettings).mockResolvedValue({ confirm_quit: "off" } as any);
    vi.mocked(api.shellQuit).mockClear(); vi.mocked(api.shutdown).mockClear();
    first.unmount();                                                 // the next start of the app
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    await waitFor(() => expect(api.shellQuit).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("does not remember anything when the box is not ticked", async () => {
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Quit the app" }));
    await waitFor(() => expect(api.shellQuit).toHaveBeenCalled());
    expect(api.putSettings).not.toHaveBeenCalled();
  });

  it("does not ask when the setting says not to, and asks anyway when the settings cannot be read", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ confirm_quit: "off" } as any);
    const { unmount } = mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    await waitFor(() => expect(api.shellQuit).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    unmount();
    vi.mocked(api.getSettings).mockRejectedValue(new Error("down"));
    vi.mocked(api.shellQuit).mockClear();
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Quit the app?");
  });

  it("always asks about running jobs, never offers Do not ask me again for that, and keeps the app open if the person says so", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ confirm_quit: "off" } as any);
    vi.mocked(api.listJobs).mockResolvedValue([job("a1", "mine_code")]);
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    await settle(20);
    requestClose();
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("Mine code pairs");
    expect(ask).toHaveTextContent("A job is still running");
    expect(within(ask).queryByRole("checkbox")).toBeNull();
    fireEvent.click(within(ask).getByRole("button", { name: "Keep the app open" }));
    await settle();
    expect(api.shutdown).not.toHaveBeenCalled();
    expect(api.shellQuit).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Closing the app" })).toBeNull();
  });

  it("stops the jobs and quits when the person agrees", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("a1", "mine_code"), job("b2", "campaign")]);
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    await settle(20);
    requestClose();
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("2 jobs are still running");
    fireEvent.click(screen.getByRole("button", { name: "Stop the jobs and quit" }));
    await waitFor(() => expect(api.shellQuit).toHaveBeenCalledTimes(1));
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it("ignores a second close while the first is being handled", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ confirm_quit: "off" } as any);
    vi.mocked(api.shutdown).mockResolvedValue(status({ done: false, running: true, steps: [] }));
    vi.mocked(api.shutdownStatus).mockResolvedValue(status({ done: false, running: true, steps: [] }));
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    await screen.findByRole("dialog", { name: "Closing the app" });
    requestClose();
    requestClose();
    await settle(40);
    expect(api.shutdown).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.shellCloseAck).mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("offers Force quit while the shutdown is slow, and reports a step that failed", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ confirm_quit: "off" } as any);
    const slow = status({ done: false, running: true, steps: [{ name: "Stopping jobs", ok: false, detail: "did not finish within 10 s; moved on", seconds: 10 }] });
    vi.mocked(api.shutdown).mockResolvedValue(slow);
    vi.mocked(api.shutdownStatus).mockResolvedValue(slow);
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    expect(await screen.findByText(/did not finish within 10 s/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Force quit" }));
    await waitFor(() => expect(api.shellQuit).toHaveBeenCalled());
  });
});

describe("restarting the app", () => {
  const healthSequence = (answers: boolean[]) => {
    let i = 0;
    vi.stubGlobal("fetch", vi.fn(async () => { const ok = answers[Math.min(i++, answers.length - 1)]; if (!ok) throw new Error("connection refused"); return { ok: true } as Response; }));
  };

  it("asks, runs the shutdown, asks the app to start again, waits until the old one is gone and the new one answers, then reloads the page", async () => {
    healthSequence([true, false, false, true]);
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestReboot();
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("Restart the app?");
    expect(within(ask).queryByRole("checkbox")).toBeNull();
    fireEvent.click(within(ask).getByRole("button", { name: "Restart the app" }));
    expect(await screen.findByRole("dialog", { name: "Restarting the app" })).toBeInTheDocument();
    await waitFor(() => expect(api.shellReboot).toHaveBeenCalledTimes(1));
    expect(api.shutdown).toHaveBeenCalledTimes(1);
    expect(api.shellQuit).not.toHaveBeenCalled();
    expect(await screen.findByText(/The app is starting again/)).toBeInTheDocument();
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("goes on to the restart with the defaults too: the guard re-renders while it works and must not lose its place", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("connection refused"); }));
    vi.mocked(api.shutdown).mockResolvedValue(status({ done: false, running: true, steps: [] }));
    vi.mocked(api.shutdownStatus).mockResolvedValueOnce(status({ done: false, running: true, steps: [{ name: "Stopping jobs", ok: true, detail: "no job was running", seconds: 0 }] })).mockResolvedValue(status());
    const view = render(<ConfirmProvider><CloseGuard pollMs={5} backPollMs={5} backLimitMs={60} /></ConfirmProvider>);       // no `reload`: the real default
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestReboot();
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Restart the app" }));
    await waitFor(() => expect(api.shellReboot).toHaveBeenCalledTimes(1), { timeout: 2000 });
    view.unmount();
  });

  it("does not reload on the answer of the copy that is still going down", async () => {
    healthSequence([true, true, true]);
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestReboot();
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Restart the app" }));
    await waitFor(() => expect(api.shellReboot).toHaveBeenCalled());
    await settle(150);
    expect(reload).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("did not answer again"), { timeout: 2000 });
  });

  it("keeps the app open and says why when the new copy could not be started", async () => {
    vi.mocked(api.shellReboot).mockResolvedValue({ ok: false, detail: "The new copy of the app ended at once (exit code 1)." });
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestReboot();
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Restart the app" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ended at once (exit code 1)");
    expect(reload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Keep using the app" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Restarting the app" })).toBeNull());
    requestReboot();                                                 // and another try is possible
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Restart the app?");
  });

  it("asks about running jobs first, in the same words as when quitting", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("a1", "mine_code")]);
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    await settle(20);
    requestReboot();
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("A job is still running");
    expect(ask).toHaveTextContent("Restarting the app stops it");
    fireEvent.click(within(ask).getByRole("button", { name: "Keep the app open" }));
    await settle();
    expect(api.shutdown).not.toHaveBeenCalled();
    expect(api.shellReboot).not.toHaveBeenCalled();
  });

  it("does not offer Force quit while restarting", async () => {
    vi.mocked(api.shutdown).mockResolvedValue(status({ done: false, running: true, steps: [] }));
    vi.mocked(api.shutdownStatus).mockResolvedValue(status({ done: false, running: true, steps: [] }));
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestReboot();
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Restart the app" }));
    await screen.findByRole("dialog", { name: "Restarting the app" });
    expect(screen.queryByRole("button", { name: "Force quit" })).toBeNull();
  });
});


describe("unsaved Projects files", () => {
  it("names unsaved editor drafts and keeps the app open unless the person discards them", async () => {
    // Review F3: workspace file drafts live outside the page, so closing the window is the moment they can be lost.
    fileDraftsFor("chat").set("src/app.py", { file: { path: "src/app.py", text: "a", version: "v1" }, text: "a edited" });
    vi.mocked(api.getSettings).mockResolvedValue({ confirm_quit: "off" } as any);
    mount();
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    requestClose();
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("A file has unsaved edits");
    fireEvent.click(within(ask).getByRole("button", { name: "Keep the app open" }));
    await settle();
    expect(api.shutdown).not.toHaveBeenCalled();
    requestClose();
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Discard the edits and quit" }));
    await waitFor(() => expect(api.shutdown).toHaveBeenCalledTimes(1));
    resetFileDrafts();
  });
});
