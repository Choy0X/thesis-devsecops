import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { listJobs: vi.fn(), getSettings: vi.fn(), putSettings: vi.fn(), cancelJob: vi.fn(), forgetJob: vi.fn() } };
});
import { api, type Job } from "../lib/api";
import { DevModeProvider } from "../lib/devMode";
import { ToastProvider } from "./Toast";
import { resetJobsList } from "../lib/jobsList";
import { resetAlerts } from "../lib/jobAlerts";
import { JobsTray } from "./JobsTray";

const job = (id: string, stage: string, status: string, over: Partial<Job> = {}): Job => ({
  id, stage, status, params: {}, started: new Date(Date.now() - 65_000).toISOString(), finished: status === "running" ? "" : new Date().toISOString(), error: "", log_count: 0, ...over,
});
const show = (jobs: Job[], dev = false) => {
  vi.mocked(api.listJobs).mockResolvedValue(jobs);
  render(<MemoryRouter><DevModeProvider initial={dev}><ToastProvider><JobsTray /></ToastProvider></DevModeProvider></MemoryRouter>);
};

beforeEach(() => {
  resetJobsList(); resetAlerts();
  vi.mocked(api.listJobs).mockReset(); vi.mocked(api.getSettings).mockReset(); vi.mocked(api.putSettings).mockReset(); vi.mocked(api.cancelJob).mockReset().mockResolvedValue({ ok: true } as any);
  vi.mocked(api.forgetJob).mockReset().mockResolvedValue({ ok: true } as any);
  vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "" } as any);
  vi.mocked(api.putSettings).mockResolvedValue({ desktop_notify: "off" } as any);
});

describe("the Jobs tray", () => {
  it("counts running jobs on the button and flags failures", async () => {
    show([job("a1", "mine_code", "running"), job("b2", "ingest", "error", { error: "Traceback\nValueError: no data" })]);
    const button = await screen.findByRole("button", { name: "Jobs: 1 running, 1 failed" });
    expect(button).toHaveTextContent("1");
  });

  it("lists a running job as a named step with a bar and the time it has taken", async () => {
    show([job("a1", "mine_code", "running", { progress: { label: "Mining commits", processed: 40, total: 100 } })]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Mine code pairs")).toBeInTheDocument();
    expect(screen.getByText("Mining commits · 40 of 100")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Mine code pairs progress" })).toHaveAttribute("aria-valuetext", "40 percent");
    expect(screen.getByText(/1 min/)).toBeInTheDocument();
  });

  it("names a campaign's own job by the campaign, not by the generic word every campaign's job shares", async () => {
    // Without this, two different campaigns' jobs (an old, deleted one and a brand new one) both say only "Campaign" and cannot be told apart at a glance.
    show([
      job("c1", "campaign_run", "running", { params: { campaign_id: "cmp-20261013-aaaa", campaign_name: "npm, full" }, progress: { label: "npm: mining", processed: 1, total: 2 } }),
      job("c2", "campaign_run", "interrupted", { params: { campaign_id: "cmp-20261012-bbbb", campaign_name: "PyPI, collect" }, error: "The app was closed while this job was running." }),
    ]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Campaign: npm, full")).toBeInTheDocument();
    expect(screen.getByText("Campaign: PyPI, collect")).toBeInTheDocument();
    expect(screen.queryByText("Campaign", { exact: true })).toBeNull();
  });

  it("falls back to the plain word for a campaign's job with no name on it (an older job, or an unnamed campaign)", async () => {
    show([job("c3", "campaign_run", "running", { params: { campaign_id: "cmp-20261011-cccc" }, progress: { label: "x", processed: 1, total: 2 } })]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Campaign", { exact: true })).toBeInTheDocument();
  });

  it("shows the percentage of a conversion, not a count of 37 of 100, and the step it is on", async () => {
    show([job("i1", "install_model", "running", { params: { repo_id: "org/m" }, progress: { label: "Converting org/m to Q4_K_M", unit: "percent", processed: 36.6, total: 100, step: 3, steps: 3 } })]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Converting org/m to Q4_K_M · step 3 of 3 · 37%")).toBeInTheDocument();
    expect(screen.queryByText(/of 100/)).toBeNull();
  });

  it("stops an install the way it stops a download, saying what is kept", async () => {
    show([job("i1", "install_model", "running", { progress: { label: "Downloading org/m", unit: "bytes", processed: 1024 ** 2, total: 8 * 1024 ** 2, step: 2, steps: 3 } })]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    const stop = await screen.findByRole("button", { name: "Stop the install" });
    expect(stop).toHaveTextContent("Stop (keeps what is downloaded and converted)");
    expect(screen.queryByRole("button", { name: "Stop the download" })).toBeNull();
    fireEvent.click(stop);
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith("i1"));
  });

  it("shows sizes with their unit for a download that has not reported its unit yet, and not for other jobs", async () => {
    show([job("d1", "hf_download", "running", { progress: { label: "Downloading org/m", processed: 1024 ** 2, total: 8 * 1024 ** 2 } }),
      job("m1", "mine_code", "running", { progress: { label: "Mining commits", processed: 40, total: 100 } })]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Downloading org/m · 1.0 MB of 8.0 MB")).toBeInTheDocument();
    expect(screen.getByText("Mining commits · 40 of 100")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop the download" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Stop the/ })).toHaveLength(1);
  });

  it("has a switch for being told when a job ends, on by default and kept in the settings", async () => {
    show([job("a1", "mine_code", "done")]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    const box = await screen.findByRole("switch", { name: "Tell me when a job finishes or stops" });
    expect(box).toBeChecked();
    fireEvent.click(box);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ desktop_notify: "off" }));
    expect(box).not.toBeChecked();
  });

  it("shows the switch off when the settings say so, and puts it back when the choice cannot be saved", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ desktop_notify: "off" } as any);
    show([job("a1", "mine_code", "done")]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    const box = await screen.findByRole("switch", { name: "Tell me when a job finishes or stops" });
    await waitFor(() => expect(box).not.toBeChecked());
    vi.mocked(api.putSettings).mockRejectedValue(new Error("The settings file is read-only."));
    fireEvent.click(box);
    expect(await screen.findByText(/Not saved: .*read-only/)).toBeInTheDocument();
    await waitFor(() => expect(box).not.toBeChecked());
  });

  it("always shows why a job failed or was cut off, in plain words", async () => {
    show([job("b2", "ingest", "error", { error: "Traceback (most recent call last):\n  File x\nValueError: the OSV dump is empty" }),
      job("c3", "campaign", "interrupted", { error: "The app was closed while this job was running." })]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("Failed: ValueError: the OSV dump is empty")).toBeInTheDocument();
    expect(screen.getByText("Cut off: The app was closed while this job was running.")).toBeInTheDocument();
  });

  it("can dismiss a finished or failed job for good, but not one that is running", async () => {
    show([job("a1", "mine_code", "running"), job("b2", "ingest", "error", { error: "ValueError: no data" })]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(screen.queryByRole("button", { name: "Dismiss Mine code pairs" })).toBeNull();    // the running job has none
    const dismiss = await screen.findByRole("button", { name: "Dismiss OSV ingest" });
    vi.mocked(api.listJobs).mockResolvedValue([job("a1", "mine_code", "running")]);          // what the tray re-reads once b2 is gone
    fireEvent.click(dismiss);
    await waitFor(() => expect(api.forgetJob).toHaveBeenCalledWith("b2"));
    await waitFor(() => expect(screen.queryByText("Failed: ValueError: no data")).toBeNull());
  });

  it("hides ids and the log link unless Developer mode is on", async () => {
    show([job("abc123def456", "ingest", "done")]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText("OSV ingest")).toBeInTheDocument();
    expect(screen.queryByText(/abc123def456/)).toBeNull();
    expect(screen.queryByText("Open the full activity log")).toBeNull();
  });

  it("shows ids and the log link in Developer mode", async () => {
    show([job("abc123def456", "ingest", "done")], true);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    expect(await screen.findByText(/job abc123def456/)).toBeInTheDocument();
    expect(screen.getByText("Open the full activity log")).toBeInTheDocument();
  });

  it("says so when nothing has run", async () => {
    show([]);
    fireEvent.click(await screen.findByRole("button", { name: /Jobs:/ }));
    await waitFor(() => expect(screen.getByText(/Nothing has run yet/)).toBeInTheDocument());
  });
});
