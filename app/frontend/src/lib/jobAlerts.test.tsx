import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, api: { listJobs: vi.fn(), shellNotify: vi.fn(), getSettings: vi.fn(), putSettings: vi.fn() } };
});
import { api, type Job } from "./api";
import { ToastProvider } from "../components/Toast";
import { alertsEnabled, alertsFor, loadAlertsSetting, resetAlerts, setAlertsEnabled, useJobAlerts, windowInBackground } from "./jobAlerts";
import { refreshJobs, resetJobsList } from "./jobsList";

const job = (id: string, stage: string, status: string): Job => ({ id, stage, status, params: {}, started: "2026-10-09T08:00:00Z", finished: status === "running" ? "" : "2026-10-09T08:05:00Z", error: "", log_count: 0 });

describe("alertsFor", () => {
  it("does not announce a waiting qualification handoff as dataset completion", () => {
    const waiting = {...job("qualification", "qualify_npm_dataset", "done"), progress: {state: "waiting"}};
    const previous = new Map([[waiting.id, "running"]]);
    expect(alertsFor(previous, [waiting]).alerts).toEqual([]);
    expect(alertsFor(previous, [{...waiting, status: "error"}]).alerts).toHaveLength(1);
    expect(alertsFor(previous, [{...waiting, progress: {state: "completed"}}]).alerts).toHaveLength(1);
  });
  it("says nothing on the first look, whatever the jobs were", () => {
    expect(alertsFor(null, [job("a", "mine_code", "done"), job("b", "ingest", "error")]).alerts).toEqual([]);
  });
  it("tells once for a job that was running and has ended, in words without ids", () => {
    const first = alertsFor(null, [job("a", "mine_code", "running"), job("b", "ingest", "running"), job("c", "campaign", "running"), job("d", "extract_fixes", "running")]);
    const { alerts, next } = alertsFor(first.next, [job("a", "mine_code", "done"), job("b", "ingest", "error"), job("c", "campaign", "interrupted"), job("d", "extract_fixes", "running")]);
    expect(alerts.map((a) => [a.tone, a.text])).toEqual([
      ["success", "Mine code pairs finished."],
      ["error", "OSV ingest stopped with an error. Open the Jobs list to see why."],
      ["error", "Collection campaign was cut off when the app closed."],
    ]);
    expect(alertsFor(next, [job("a", "mine_code", "done"), job("b", "ingest", "error"), job("c", "campaign", "interrupted"), job("d", "extract_fixes", "running")]).alerts).toEqual([]);
  });
  it("does not tell for a job the person stopped", () => {
    expect(alertsFor(new Map([["a", "running"]]), [job("a", "mine_code", "cancelled")]).alerts).toEqual([]);
  });
});

describe("the switch", () => {
  beforeEach(() => { resetAlerts(); vi.mocked(api.getSettings).mockReset(); vi.mocked(api.putSettings).mockReset(); vi.mocked(api.putSettings).mockResolvedValue({} as any); });

  it("is on until the settings say off, and the choice is saved in the settings and not in the page", async () => {
    localStorage.clear();
    vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "" } as any);
    expect(alertsEnabled()).toBe(true);
    await setAlertsEnabled(false);
    expect(alertsEnabled()).toBe(false);
    expect(api.putSettings).toHaveBeenLastCalledWith({ desktop_notify: "off" });
    await setAlertsEnabled(true);
    expect(alertsEnabled()).toBe(true);
    expect(api.putSettings).toHaveBeenLastCalledWith({ desktop_notify: "" });
    expect(localStorage.length).toBe(0);
  });

  it("reads the choice from the settings once, and stays on when they cannot be read", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "off" } as any);
    await loadAlertsSetting();
    await loadAlertsSetting();
    expect(alertsEnabled()).toBe(false);
    expect(api.getSettings).toHaveBeenCalledTimes(1);
    resetAlerts();
    vi.mocked(api.getSettings).mockRejectedValueOnce(new Error("down"));
    await loadAlertsSetting();
    expect(alertsEnabled()).toBe(true);
  });

  it("goes back to the earlier value and says why when the choice cannot be saved", async () => {
    vi.mocked(api.putSettings).mockRejectedValue(new Error("read-only"));
    await expect(setAlertsEnabled(false)).rejects.toThrow("read-only");
    expect(alertsEnabled()).toBe(true);
  });
});

function Probe() { useJobAlerts(); return null; }

describe("useJobAlerts", () => {
  beforeEach(() => { resetJobsList(); resetAlerts(); localStorage.clear(); vi.mocked(api.getSettings).mockReset(); vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "" } as any); vi.mocked(api.putSettings).mockReset(); vi.mocked(api.putSettings).mockResolvedValue({} as any); vi.mocked(api.listJobs).mockReset(); vi.mocked(api.shellNotify).mockReset(); vi.mocked(api.shellNotify).mockResolvedValue({ ok: true, delivered: true, detail: null }); });
  afterEach(() => vi.restoreAllMocks());

  const end = async (inBackground: boolean) => {
    vi.spyOn(document, "hasFocus").mockReturnValue(!inBackground);
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "running")]);
    render(<ToastProvider><Probe /></ToastProvider>);
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "done")]);
    await act(async () => { refreshJobs(); });
    await screen.findByText("Mine code pairs finished.");
  };

  it("also tells the system when the window is in the background, in the same words and without ids", async () => {
    await end(true);
    expect(api.shellNotify).toHaveBeenCalledTimes(1);
    expect(api.shellNotify).toHaveBeenCalledWith("AI Vulnerability Framework", "Mine code pairs finished.");
  });

  it("does not notify the system while the window is in front, where the toast is seen", async () => {
    await end(false);
    expect(api.shellNotify).not.toHaveBeenCalled();
  });

  it("does not notify the system when the person turned the alerts off", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "off" } as any);
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "running")]);
    render(<ToastProvider><Probe /></ToastProvider>);
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "done")]);
    await act(async () => { refreshJobs(); });
    await new Promise((r) => setTimeout(r, 50));
    expect(api.shellNotify).not.toHaveBeenCalled();
  });

  it("carries on when the system notification cannot be delivered", async () => {
    vi.mocked(api.shellNotify).mockImplementation(() => Promise.reject(new Error("no window")));
    await end(true);
    expect(api.shellNotify).toHaveBeenCalled();
  });

  it("knows a hidden or unfocused window as in the background", () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    expect(windowInBackground()).toBe(false);
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    expect(windowInBackground()).toBe(true);
  });

  it("shows a toast when a running job ends, and none when alerts are off", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "running")]);
    render(<ToastProvider><Probe /></ToastProvider>);
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "done")]);
    await act(async () => { refreshJobs(); });
    expect(await screen.findByText("Mine code pairs finished.")).toBeInTheDocument();
  });

  it("stays quiet when the person turned the alerts off", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "off" } as any);
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "running")]);
    render(<ToastProvider><Probe /></ToastProvider>);
    await waitFor(() => expect(api.listJobs).toHaveBeenCalled());
    vi.mocked(api.listJobs).mockResolvedValue([job("a", "mine_code", "done")]);
    await act(async () => { refreshJobs(); });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("Mine code pairs finished.")).toBeNull();
  });
});
