import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { choose } from "../test/select";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { records: vi.fn(), recordFacets: vi.fn(), storeSummary: vi.fn(), byYear: vi.fn(), getRecord: vi.fn(), deleteRecord: vi.fn(), recordFixCommits: vi.fn(), dataStamp: vi.fn() } };
});
import { api, type DataRecord } from "../lib/api";
import { ToastProvider } from "../components/Toast";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { DevModeProvider } from "../lib/devMode";
import { RecordsPage, severityOf } from "./RecordsPage";

const rec = (id: number, over: Partial<DataRecord> & Record<string, unknown> = {}): DataRecord => ({
  id, osv_id: `GHSA-${id}`, cve_id: `CVE-2024-${1000 + id}`, package: `pkg-${id}`, summary: `Summary ${id}`, details: "Long text", published: "2024-03-01T00:00:00Z",
  cvss_vector: null, cvss_v4_vector: null, cvss_score: null, osv_cwe: "CWE-79", cwe: null, code_before: null, code_after: null, filename: null, source: "osv",
  ecosystem: "npm", language: "javascript", fix_commit_count: 2, code_status: "mined", pair_count: 5, ...over,
} as DataRecord);

const later = <T,>() => { let resolve!: (v: T) => void; const promise = new Promise<T>((a) => { resolve = a; }); return { promise, resolve }; };
const NONE = { language: "", code_status: "", dependency_status: "", severity: "", ecosystem: "", sort: "id", dir: "desc" };
const show = (dev = false) => render(<DevModeProvider initial={dev}><ToastProvider><ConfirmProvider><RecordsPage /></ConfirmProvider></ToastProvider></DevModeProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.recordFacets).mockResolvedValue({ total: 2, severity: { low: 0, medium: 0, high: 1, critical: 1, unrated: 0 }, ecosystem: { npm: 2 }, language: { javascript: 2 },
    code_status: { mined: 1, none: 1 }, dependency_status: { none: 2 } });
  vi.mocked(api.records).mockResolvedValue({ total: 2, limit: 100, offset: 0, records: [rec(1, { osv_severity: "HIGH" }), rec(2, { cvss_score: 9.8, code_status: null, pair_count: 0 })] } as any);
  vi.mocked(api.getRecord).mockImplementation(async (id: number) => rec(id, { details: "The whole advisory text", osv_severity: "HIGH" }));
  vi.mocked(api.recordFixCommits).mockResolvedValue([]);
});

describe("severityOf", () => {
  it("uses the advisory's own label first and the CVSS band otherwise", () => {
    expect(severityOf({ cvss_score: 1, osv_severity: "critical" })).toBe("critical");
    expect(severityOf({ cvss_score: null, osv_severity: "MODERATE" })).toBe("medium");
    expect(severityOf({ cvss_score: 9.1 })).toBe("critical");
    expect(severityOf({ cvss_score: 7.5 })).toBe("high");
    expect(severityOf({ cvss_score: 5 })).toBe("medium");
    expect(severityOf({ cvss_score: 2 })).toBe("low");
    expect(severityOf({ cvss_score: null })).toBeNull();
  });
});

describe("RecordsPage", () => {
  it("lists the records with the columns of the plan and the severity as a word", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Source records" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["CVE", "Package", "Ecosystem", "Language", "Severity", "Published", "Fix commits", "Code", "Pairs"]);
    expect(within(table).getByText("CVE-2024-1001")).toBeInTheDocument();
    expect(within(table).getByText("High")).toBeInTheDocument();
    expect(within(table).getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("1 to 2 of 2 records")).toBeInTheDocument();
  });

  it("asks the server for the filters it was given and goes back to the first page", async () => {
    show();
    await screen.findByRole("table");
    const user = userEvent.setup();
    await choose(user, screen.getByRole("combobox", { name: "Language" }), /^javascript/);
    await waitFor(() => expect(vi.mocked(api.records)).toHaveBeenLastCalledWith("", 100, 0, { ...NONE, language: "javascript" }));
    await choose(user, screen.getByRole("combobox", { name: "Code status" }), /^mined/i);
    await waitFor(() => expect(vi.mocked(api.records)).toHaveBeenLastCalledWith("", 100, 0, { ...NONE, language: "javascript", code_status: "mined" }));
  });

  it("searches after a short pause", async () => {
    show();
    await screen.findByRole("table");
    fireEvent.change(screen.getByLabelText("Search records"), { target: { value: "left-pad" } });
    await waitFor(() => expect(vi.mocked(api.records)).toHaveBeenLastCalledWith("left-pad", 100, 0, expect.anything()), { timeout: 2000 });
  });

  it("opens one record in a drawer with the whole record read on demand, and no internal id", async () => {
    show();
    fireEvent.click(await screen.findByText("CVE-2024-1001"));
    const drawer = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    expect(api.getRecord).toHaveBeenCalledWith(1, expect.anything());
    expect(within(drawer).getByText("Summary 1")).toBeInTheDocument();
    expect(within(drawer).queryByText(/GHSA-1 ·/)).toBeNull();
    expect(within(drawer).getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("shows the internal ids in Developer mode", async () => {
    show(true);
    fireEvent.click(await screen.findByText("CVE-2024-1001"));
    expect(await screen.findByText(/id 1 · GHSA-1/)).toBeInTheDocument();
  });

  it("asks before deleting, says manual pairs go too, and deletes only when confirmed", async () => {
    vi.mocked(api.deleteRecord).mockResolvedValue({ deleted: true });
    show();
    fireEvent.click(await screen.findByText("CVE-2024-1001"));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("code pairs made by hand");
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.deleteRecord).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete the record" }));
    await waitFor(() => expect(api.deleteRecord).toHaveBeenCalledWith(1));
  });

  it("says what to do on an empty database", async () => {
    vi.mocked(api.records).mockResolvedValue({ total: 0, limit: 100, offset: 0, records: [] } as any);
    show();
    expect(await screen.findByText("No records yet")).toBeInTheDocument();
  });

  it("shows why the list could not be read, with a retry", async () => {
    vi.mocked(api.records).mockRejectedValueOnce(new Error("The store is locked.")).mockResolvedValue({ total: 0, limit: 100, offset: 0, records: [] } as any);
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the records");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("shows the advisories over time only on request", async () => {
    vi.mocked(api.byYear).mockResolvedValue({ total: 3, dated: 3, undated: 0, unrated: 0, years: [
      { year: 2020, unrated: 0, low: 0, medium: 0, high: 2, critical: 0, total: 2 }, { year: 2021, unrated: 0, low: 1, medium: 0, high: 0, critical: 0, total: 1 }] });
    show();
    await screen.findByRole("table", { name: "Source records" });
    expect(api.byYear).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await screen.findByRole("group", { name: /3 dated advisories from 2020 to 2021/ })).toBeInTheDocument();
    expect(api.byYear).toHaveBeenCalledTimes(1);
  });

  it("opens the drawer at once from the row and shows a skeleton where the rest of the record will be", async () => {
    const whole = later<DataRecord>();
    vi.mocked(api.getRecord).mockReturnValue(whole.promise);
    show();
    fireEvent.click(await screen.findByText("CVE-2024-1001"));
    const drawer = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    expect(within(drawer).getByText("Summary 1")).toBeInTheDocument();
    expect(within(drawer).getByRole("status", { name: "Loading the whole record" })).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "Edit" })).toBeDisabled();
    expect(within(drawer).getByRole("button", { name: "Delete" })).toBeEnabled();
    whole.resolve(rec(1, { details: "The whole advisory text", osv_severity: "HIGH" }));
    expect(await within(drawer).findByText("Advisory text")).toBeInTheDocument();
    expect(within(drawer).queryByRole("status", { name: "Loading the whole record" })).toBeNull();
    expect(within(drawer).getByRole("button", { name: "Edit" })).toBeEnabled();
  });

  it("drops a record that arrives after the drawer was closed and another record was opened", async () => {
    const first = later<DataRecord>(), second = later<DataRecord>();
    vi.mocked(api.getRecord).mockImplementation((id: number) => (id === 1 ? first.promise : second.promise));
    show();
    fireEvent.click(await screen.findByText("CVE-2024-1001"));
    await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByText("CVE-2024-1002"));
    const drawer = await screen.findByRole("dialog", { name: "CVE-2024-1002" });
    second.resolve(rec(2, { details: "Details of two" }));
    expect(await within(drawer).findByText("Advisory text")).toBeInTheDocument();
    first.resolve(rec(1, { details: "Details of one", summary: "Late summary" }));
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText("Late summary")).toBeNull();
    expect(within(drawer).getByText("Summary 2")).toBeInTheDocument();
  });

  it("says why the whole record could not be read and reads it again on request", async () => {
    vi.mocked(api.getRecord).mockRejectedValueOnce(new Error("The store is locked.")).mockImplementation(async (id: number) => rec(id, { details: "The whole advisory text" }));
    show();
    fireEvent.click(await screen.findByText("CVE-2024-1001"));
    const drawer = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    expect(await within(drawer).findByRole("alert")).toHaveTextContent("Could not read the whole record");
    expect(within(drawer).getByText("Summary 1")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "Try again" }));
    expect(await within(drawer).findByText("Advisory text")).toBeInTheDocument();
    expect(within(drawer).queryByRole("alert")).toBeNull();
  });

  it("starts reading a record when the pointer rests on its row, and not when it only passes", async () => {
    show();
    const row = (await screen.findByText("CVE-2024-1001")).closest("[role=row]") as HTMLElement;
    fireEvent.mouseEnter(row);
    expect(api.getRecord).not.toHaveBeenCalled();
    await waitFor(() => expect(api.getRecord).toHaveBeenCalledWith(1), { timeout: 1500 });
    vi.mocked(api.getRecord).mockClear();
    const other = screen.getByText("CVE-2024-1002").closest("[role=row]") as HTMLElement;
    fireEvent.mouseEnter(other);
    fireEvent.mouseLeave(other);
    await new Promise((r) => setTimeout(r, 350));
    expect(api.getRecord).not.toHaveBeenCalled();
  });

  it("sorts on the server, announces the sort and goes back to the first page", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Source records" });
    const header = () => within(table).getByRole("columnheader", { name: "CVE" });
    fireEvent.click(within(header()).getByRole("button", { name: "CVE" }));
    await waitFor(() => expect(vi.mocked(api.records)).toHaveBeenLastCalledWith("", 100, 0, { ...NONE, sort: "cve_id", dir: "asc" }));
    expect(header()).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(within(header()).getByRole("button", { name: "CVE" }));
    await waitFor(() => expect(vi.mocked(api.records)).toHaveBeenLastCalledWith("", 100, 0, { ...NONE, sort: "cve_id", dir: "desc" }));
    fireEvent.click(within(header()).getByRole("button", { name: "CVE" }));
    await waitFor(() => expect(vi.mocked(api.records)).toHaveBeenLastCalledWith("", 100, 0, NONE));
    expect(within(table).getByRole("columnheader", { name: "Published" })).toHaveAttribute("aria-sort", "none");
  });

  it("offers severity and ecosystem filters with how many records each gives, and counts under the filters chosen", async () => {
    show();
    await screen.findByRole("table");
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: "Ecosystem" }));
    expect(await screen.findByRole("option", { name: "npm (2)" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("combobox", { name: "Severity" }));
    expect(await screen.findByRole("option", { name: "Critical (1)" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "High (1)" }));
    await waitFor(() => expect(vi.mocked(api.records)).toHaveBeenLastCalledWith("", 100, 0, { ...NONE, severity: "high" }));
    await waitFor(() => expect(vi.mocked(api.recordFacets)).toHaveBeenLastCalledWith("", expect.objectContaining({ severity: "high" })));
  });

  it("says 10,000+ when the server stopped counting at its cap", async () => {
    vi.mocked(api.records).mockResolvedValue({ total: 10000, total_exact: false, limit: 100, offset: 0, records: [rec(1), rec(2)] } as any);
    show();
    expect(await screen.findByText("1 to 2 of 10,000+ records")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Source records" })).toHaveAttribute("aria-rowcount", "-1");
  });

  it("announces a row's place in the whole list, not in the page", async () => {
    const rows = (from: number) => Array.from({ length: Math.min(100, 121 - from) }, (_, i) => rec(from + i));
    vi.mocked(api.records).mockImplementation((async (_q: string, _l: number, offset: number) => ({ total: 120, total_exact: true, limit: 100, offset, records: rows(offset + 1) })) as any);
    show();
    const table = await screen.findByRole("table", { name: "Source records" });
    expect(table).toHaveAttribute("aria-rowcount", "121");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("CVE-2024-1101");
    expect(screen.getByText("CVE-2024-1101").closest("[role=row]")).toHaveAttribute("aria-rowindex", "102");
    expect(screen.getByText("101 to 120 of 120 records")).toBeInTheDocument();
  });

  it("shows the rows of the filter on screen when an answer to an older filter comes last", async () => {
    show();
    await screen.findByRole("table");
    const older = later<any>(), newer = later<any>();
    vi.mocked(api.records).mockReset();
    vi.mocked(api.records).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    const user = userEvent.setup();
    await choose(user, screen.getByRole("combobox", { name: "Language" }), /^javascript/);
    await choose(user, screen.getByRole("combobox", { name: "Code status" }), /^mined/i);
    await waitFor(() => expect(api.records).toHaveBeenCalledTimes(2));
    newer.resolve({ total: 1, total_exact: true, limit: 100, offset: 0, records: [rec(2)] });
    await screen.findByText("CVE-2024-1002");
    older.resolve({ total: 1, total_exact: true, limit: 100, offset: 0, records: [rec(1)] });
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText("CVE-2024-1001")).toBeNull();
    expect(screen.getByText("CVE-2024-1002")).toBeInTheDocument();
  });
});
