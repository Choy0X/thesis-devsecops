import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { ecosystems: vi.fn(), ecosystemUpdate: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: () => "ws://test" };
});
import { api, type EcosystemCatalog, type EcosystemRow } from "../lib/api";
import { DevModeProvider } from "../lib/devMode";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { EcosystemTable } from "./EcosystemTable";

const row = (name: string, over: Partial<EcosystemRow> = {}): EcosystemRow => ({ name, enabled: false, remote: true, dump_bytes: null, snapshot: null, downloaded: false, ingested: false, etag: null, ...over });
const catalog = (over: Partial<EcosystemCatalog> = {}): EcosystemCatalog => ({
  source: "online", note: null, ecosystems: [
    row("npm", { enabled: true, downloaded: true, ingested: true, dump_bytes: 207_000_000, snapshot: "2026-10-01T00:00:00Z" }),
    row("Go", { downloaded: true, dump_bytes: 11_500_000 }),
    row("crates.io"),
    row("oldeco", { remote: false, downloaded: true, dump_bytes: 1000 }),
  ], ...over,
});
const show = (dev = false) => render(<DevModeProvider initial={dev}><ToastProvider><ConfirmProvider><EcosystemTable /></ConfirmProvider></ToastProvider></DevModeProvider>);

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  vi.mocked(api.ecosystems).mockReset(); vi.mocked(api.ecosystems).mockResolvedValue(catalog());
  vi.mocked(api.ecosystemUpdate).mockReset(); vi.mocked(api.startJob).mockReset(); vi.mocked(api.getJob).mockReset();
  vi.mocked(api.listJobs).mockReset(); vi.mocked(api.listJobs).mockResolvedValue([]);
});

describe("EcosystemTable", () => {
  it("says for each ecosystem what is on this computer, in words, with size, date and whether the app collects it", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Ecosystems" });
    const npm = within(table).getByText("npm").closest("[role=row]") as HTMLElement;
    expect(npm).toHaveTextContent("Downloaded and read");
    expect(npm).toHaveTextContent("207 MB");
    expect(npm).toHaveTextContent("2026-10-01");
    expect(npm).toHaveTextContent("Yes");
    expect(within(table).getAllByText("Downloaded, not read yet")).toHaveLength(2);
    expect(within(table).getByText("Not downloaded")).toBeInTheDocument();
    expect(within(table).getByText("(no longer listed by OSV)")).toBeInTheDocument();
    expect(screen.getByText("4 of 4 ecosystems.")).toBeInTheDocument();
  });

  it("filters by name and by what is on the computer or collected", async () => {
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    fireEvent.change(screen.getByLabelText("Filter ecosystems"), { target: { value: "GO" } });
    expect(screen.getByText("1 of 4 ecosystems.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter ecosystems"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("radio", { name: "On this computer or collected" }));
    expect(screen.getByText("3 of 4 ecosystems.")).toBeInTheDocument();
    expect(screen.queryByText("crates.io")).toBeNull();
  });

  it("warns that the list may be out of date when OSV could not be reached", async () => {
    vi.mocked(api.ecosystems).mockResolvedValue(catalog({ source: "saved", note: "OSV could not be reached, so this is the list saved earlier; it may be out of date." }));
    show();
    expect(await screen.findByRole("status")).toHaveTextContent("may be out of date");
  });

  it("says why there is nothing to show when OSV cannot be reached and nothing was saved", async () => {
    vi.mocked(api.ecosystems).mockResolvedValue(catalog({ source: "none", note: "OSV could not be reached and no list was saved earlier.", ecosystems: [] }));
    show();
    expect(await screen.findByText("No ecosystem to show")).toBeInTheDocument();
    expect(screen.getByText("OSV could not be reached and nothing is saved yet.")).toBeInTheDocument();
  });

  it("asks again when told to", async () => {
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    fireEvent.click(screen.getByRole("button", { name: "Ask OSV again" }));
    expect(api.ecosystems).toHaveBeenCalledTimes(2);
  });

  it("names where the list came from only in Developer mode", async () => {
    const { unmount } = show(false);
    await screen.findByRole("table", { name: "Ecosystems" });
    expect(screen.queryByText("list source: online")).toBeNull();
    unmount();
    show(true);
    expect(await screen.findByText("list source: online")).toBeInTheDocument();
  });

  const quietSocket = () => { class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} } vi.stubGlobal("WebSocket", Quiet); };
  const running = (id = "j1") => ({ id, stage: "osv_install", status: "running", logs: [], progress: null }) as any;

  it("offers Download for an ecosystem that is not here, Read for one that is not read yet, and Check for one that is read, and starts the install for the one chosen", async () => {
    vi.mocked(api.startJob).mockResolvedValue(running());
    vi.mocked(api.getJob).mockResolvedValue(running());
    vi.mocked(api.ecosystemUpdate).mockResolvedValue({ name: "npm", state: "current" });
    quietSocket();
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    expect(screen.getByRole("button", { name: "Check the npm dump" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Read the Go dump" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Read the oldeco dump" })).toBeInTheDocument();         // no longer listed by OSV: it can be read from disk, never fetched or checked
    expect(screen.queryByRole("button", { name: /(Download|Update|Check) the oldeco dump/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Download the crates.io dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Download the crates.io dump" });
    expect(dialog).toHaveTextContent("hundreds of megabytes");
    fireEvent.click(within(dialog).getByRole("button", { name: "Download" }));
    await vi.waitFor(() => expect(api.startJob).toHaveBeenCalledWith("osv_install", { ecosystem: "crates.io", refresh: false }));
  });

  it("reads a dump that is here without downloading it again", async () => {
    vi.mocked(api.startJob).mockResolvedValue(running());
    vi.mocked(api.getJob).mockResolvedValue(running());
    quietSocket();
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    fireEvent.click(screen.getByRole("button", { name: "Read the Go dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Read the Go dump" });
    expect(dialog).toHaveTextContent("Nothing is downloaded");
    fireEvent.click(within(dialog).getByRole("button", { name: "Read" }));
    await vi.waitFor(() => expect(api.startJob).toHaveBeenCalledWith("osv_install", { ecosystem: "Go", skip_download: true }));
  });

  it("asks OSV about a dump that is here as the dialog opens, in words, and again when asked", async () => {
    vi.mocked(api.ecosystemUpdate).mockResolvedValue({ name: "npm", state: "update_available" });
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    fireEvent.click(screen.getByRole("button", { name: "Check the npm dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Check the npm dump" });
    expect(await within(dialog).findByText("OSV has a newer copy. Update to fetch it.")).toBeInTheDocument();
    expect(api.ecosystemUpdate).toHaveBeenCalledWith("npm");
    vi.mocked(api.ecosystemUpdate).mockResolvedValue({ name: "npm", state: "current" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Check for a newer copy" }));
    expect(await within(dialog).findByText("The copy on this computer is the newest one.")).toBeInTheDocument();
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Download the crates.io dump" }));
    const other = await screen.findByRole("dialog", { name: "Download the crates.io dump" });
    expect(within(other).queryByRole("button", { name: "Check for a newer copy" })).toBeNull();
  });

  it("says in the row when a dump is up to date or has an update, and offers Update only for the one that has", async () => {
    vi.mocked(api.ecosystems).mockResolvedValue(catalog({ ecosystems: [
      row("npm", { downloaded: true, ingested: true, update_state: "current", update_checked_at: new Date().toISOString() }),
      row("PyPI", { downloaded: true, ingested: true, update_state: "update_available", update_checked_at: new Date().toISOString() }),
    ] }));
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    expect(screen.getByText("Up to date")).toBeInTheDocument();
    expect(screen.getByText("Update available")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Update the PyPI dump" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check the npm dump" })).toBeInTheDocument();
    expect(api.ecosystemUpdate).not.toHaveBeenCalled();                     // both were looked at a moment ago
  });

  it("looks at OSV by itself for a dump nobody has looked at yet, and shows what it found", async () => {
    vi.mocked(api.ecosystems).mockResolvedValue(catalog({ ecosystems: [row("npm", { downloaded: true, ingested: true, update_state: "unknown" })] }));
    vi.mocked(api.ecosystemUpdate).mockResolvedValue({ name: "npm", state: "update_available" });
    show();
    expect(await screen.findByText("Update available")).toBeInTheDocument();
    expect(api.ecosystemUpdate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Update the npm dump" })).toBeInTheDocument();
  });

  it("shows the progress of a running install in that ecosystem's row", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([{ id: "j9", stage: "osv_install", status: "running", params: { ecosystem: "crates.io" }, started: "", finished: "", error: "", log_count: 0,
      progress: { state: "downloading", phase: "download", step: 1, steps: 2, unit: "bytes", processed: 5_000_000, total: 20_000_000 } }] as any);
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    expect(await screen.findByText("Downloading 25%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download the crates.io dump" })).toBeDisabled();
  });
});

describe("EcosystemTable: an install that was cut", () => {
  const read = (over = {}) => ({ percent: 45, done: 45, total: 100, rows: 90, phase: "parsing" as const, updated_at: null, active: false, ...over });
  const download = (over = {}) => ({ bytes: 40_000_000, total: 100_000_000, percent: 40, update: false, active: false, updated_at: null, ...over });
  const quiet = () => { class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} } vi.stubGlobal("WebSocket", Quiet); };
  const withRows = (...rows: EcosystemRow[]) => vi.mocked(api.ecosystems).mockResolvedValue(catalog({ ecosystems: rows }));
  const started = () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "osv_install", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "osv_install", status: "running", logs: [], progress: null } as any);
    quiet();
  };

  it("says a read stopped at 45% and offers Continue, which continues the read without downloading", async () => {
    withRows(row("Go", { downloaded: true, dump_bytes: 11_500_000, read_partial: read() }));
    started();
    show();
    const table = await screen.findByRole("table", { name: "Ecosystems" });
    expect(within(table).getByText("Reading stopped at 45%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue the Go dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Continue reading the Go dump" });
    expect(dialog).toHaveTextContent("It stopped at 45%. Continue picks up from there.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await vi.waitFor(() => expect(api.startJob).toHaveBeenCalledWith("osv_install", { ecosystem: "Go", skip_download: true }));
  });

  it("starts over only after a confirmation, and then asks the stage to throw the saved work away", async () => {
    withRows(row("Go", { downloaded: true, read_partial: read() }));
    started();
    show();
    await screen.findByRole("table", { name: "Ecosystems" });
    fireEvent.click(screen.getByRole("button", { name: "Continue the Go dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Continue reading the Go dump" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start over" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("throws away the part of the dump already read");
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await new Promise((r) => setTimeout(r, 20));
    expect(api.startJob).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Start over" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Start over" }));
    await vi.waitFor(() => expect(api.startJob).toHaveBeenCalledWith("osv_install", { ecosystem: "Go", skip_download: true, fresh: true }));
  });

  it("names a download that stopped, for a dump not here yet and for the fetch of a newer copy", async () => {
    withRows(row("crates.io", { download_partial: download() }), row("npm", { downloaded: true, ingested: true, download_partial: download({ update: true }) }));
    started();
    show();
    const table = await screen.findByRole("table", { name: "Ecosystems" });
    expect(within(table).getByText("Download stopped at 40%")).toBeInTheDocument();
    expect(within(table).getByText("Update stopped at 40%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue the crates.io dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Continue the crates.io download" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await vi.waitFor(() => expect(api.startJob).toHaveBeenCalledWith("osv_install", { ecosystem: "crates.io", refresh: false }));
  });

  it("says when the read is only finishing, and when another job is working on it (no Continue then)", async () => {
    withRows(row("Go", { downloaded: true, read_partial: read({ phase: "assembling", percent: 99 }) }), row("Hex", { downloaded: true, read_partial: read({ active: true }) }));
    show();
    const table = await screen.findByRole("table", { name: "Ecosystems" });
    expect(within(table).getByText("Finishing the read")).toBeInTheDocument();
    expect(within(table).getByText("Being worked on by another job")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue the Hex dump" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Continue the Go dump" })).toBeEnabled();
  });

  it("shows the running job instead of the stop while it continues", async () => {
    withRows(row("Go", { downloaded: true, read_partial: read() }));
    vi.mocked(api.listJobs).mockResolvedValue([{ id: "j9", stage: "osv_install", status: "running", params: { ecosystem: "Go" }, started: "", finished: "", error: "", log_count: 0,
      progress: { state: "reading", phase: "read", unit: "files", processed: 50, total: 100, resumed_from: 45, resumed: true } }] as any);
    show();
    const table = await screen.findByRole("table", { name: "Ecosystems" });
    expect(await within(table).findByText("Reading 50 of 100 files")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue the Go dump" })).toBeDisabled();
  });
});
