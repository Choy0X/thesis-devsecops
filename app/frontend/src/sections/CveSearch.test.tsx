import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { cveSearch: vi.fn(), cveDetail: vi.fn(), ecosystemUpdate: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: () => "ws://test" };
});
import { ApiError, api, type CveDetail, type CveHit, type CveSearch as Found } from "../lib/api";
import { ToastProvider } from "../components/Toast";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { CveSearch } from "./CveSearch";

const show = () => render(<ToastProvider><ConfirmProvider><CveSearch /></ConfirmProvider></ToastProvider>);

const hit = (osv: string, over: Partial<CveHit> = {}): CveHit => ({ osv_id: osv, cve_id: "CVE-2021-23337", ecosystem: "npm", packages: ["lodash", "lodash-es"], more_packages: 0, summary: "Command injection in lodash",
  published: "2021-02-15T00:00:00Z", severity: "HIGH", aliases: [], state: "downloaded", ...over });
const found = (over: Partial<Found> = {}): Found => ({ query: "", ecosystem: "", state: "", total: 2, offset: 0, limit: 100, ecosystems_searched: ["PyPI", "npm"], downloaded_ecosystems: 2, unread: [],
  results: [hit("GHSA-aaaa", { state: "in_store" }), hit("GHSA-bbbb", { cve_id: null, packages: ["left-pad"], more_packages: 3, ecosystem: "PyPI", severity: null })], ...over });
const detail = (over: Partial<CveDetail> = {}): CveDetail => ({
  osv_id: "GHSA-aaaa", cve_id: "CVE-2021-23337", ecosystem: "npm", aliases: ["CVE-2021-23337"], summary: "Command injection in lodash", details: "Lodash versions before 4.17.21 are vulnerable.",
  published: "2021-02-15T00:00:00Z", modified: "2021-03-01T00:00:00Z", withdrawn: null, severity_label: "HIGH", cvss_v3: "CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:U/C:H/I:H/A:H", cvss_v4: null, cwe: ["CWE-77"],
  github_reviewed: true, nvd_published_at: null,
  affected: [{ package: "lodash", ecosystem: "npm", purl: null, ranges: [{ type: "SEMVER", repo: null, events: [{ introduced: "0" }, { fixed: "4.17.21" }] }], versions: ["4.17.20"], versions_more: 0 },
    { package: "lodash-es", ecosystem: "npm", purl: null, ranges: [], versions: [], versions_more: 0 }],
  fix_commits: [{ commit_key: "k", repo_url: "https://github.com/lodash/lodash", sha: "a".repeat(40), via: "osv_ref_fix" }], references: [{ type: "ADVISORY", url: "https://example.org/a" }, { type: "WEB", url: "javascript:alert(1)" }],
  source_record_hash: "f".repeat(64), source: "dump", state: "in_store", raw: { id: "GHSA-aaaa" }, ...over });

beforeEach(() => {
  for (const f of Object.values(api)) (f as ReturnType<typeof vi.fn>).mockReset();
  vi.mocked(api.cveSearch).mockResolvedValue(found());
  vi.mocked(api.cveDetail).mockResolvedValue(detail());
  vi.mocked(api.listJobs).mockResolvedValue([]);
});

describe("CveSearch", () => {
  it("lists the newest advisories as soon as the page opens, with nothing to click", async () => {
    show();
    expect(screen.getByRole("status", { name: "Loading advisories" })).toBeInTheDocument();
    await screen.findByRole("list", { name: "Advisories found" });
    expect(api.cveSearch).toHaveBeenCalledTimes(1);
    expect(api.cveSearch).toHaveBeenCalledWith({ q: "", ecosystem: "", state: "", limit: 100, offset: 0 });
    expect(screen.queryByRole("button", { name: "Show the newest advisories" })).toBeNull();
  });

  it("shows each advisory with its packages, its facts and whether the store holds it", async () => {
    show();
    const list = await screen.findByRole("list", { name: "Advisories found" });
    const [first, second] = within(list).getAllByRole("listitem");
    expect(first).toHaveTextContent("CVE-2021-23337");
    expect(first).toHaveTextContent("GHSA-aaaa");
    expect(first).toHaveTextContent("npm lodash, lodash-es");
    expect(first).toHaveTextContent("HIGH · 2021-02-15");
    expect(first).toHaveTextContent("In your store");
    expect(second).toHaveTextContent("GHSA-bbbb");
    expect(second).toHaveTextContent("PyPI left-pad and 3 more");
    expect(second).toHaveTextContent("Downloaded, not in your store");
    expect(screen.getByText(/1 to 2 of 2 advisories in 2 downloaded ecosystems/)).toBeInTheDocument();
  });

  it("waits a moment after typing, then asks with the words, and offers the ecosystems that were downloaded", async () => {
    show();
    await screen.findByRole("list", { name: "Advisories found" });
    vi.mocked(api.cveSearch).mockClear();
    fireEvent.change(screen.getByLabelText("Search advisories"), { target: { value: "  lodash " } });
    expect(api.cveSearch).not.toHaveBeenCalled();                                          // not on every key
    await waitFor(() => expect(api.cveSearch).toHaveBeenCalledWith({ q: "lodash", ecosystem: "", state: "", limit: 100, offset: 0 }));
    await screen.findByRole("list", { name: "Advisories found" });
    await userEvent.setup().click(screen.getByRole("combobox", { name: "Ecosystem" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["All downloaded ecosystems", "PyPI", "npm"]);
    await userEvent.setup().keyboard("{Escape}");
  });

  it("asks again for one ecosystem or one state, starting from the first page", async () => {
    show();
    await screen.findByRole("list", { name: "Advisories found" });
    await pickOption("Ecosystem", "npm");
    await waitFor(() => expect(api.cveSearch).toHaveBeenLastCalledWith({ q: "", ecosystem: "npm", state: "", limit: 100, offset: 0 }));
    await pickOption("State", "In your store");
    await waitFor(() => expect(api.cveSearch).toHaveBeenLastCalledWith({ q: "", ecosystem: "npm", state: "in_store", limit: 100, offset: 0 }));
  });

  it("pages forward and back", async () => {
    vi.mocked(api.cveSearch).mockResolvedValue(found({ total: 120, results: Array.from({ length: 100 }, (_, i) => hit(`GHSA-${i}`)) }));
    show();
    await screen.findByText(/1 to 100 of 120 advisories/);
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    vi.mocked(api.cveSearch).mockResolvedValue(found({ total: 120, offset: 100, results: Array.from({ length: 20 }, (_, i) => hit(`GHSA-n${i}`)) }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/101 to 120 of 120 advisories/);
    expect(api.cveSearch).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 100 }));
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();
  });

  it("says when nothing matches, and what it was looking for", async () => {
    vi.mocked(api.cveSearch).mockResolvedValue(found({ query: "zzz", total: 0, results: [] }));
    show();
    expect(await screen.findByText('No advisory in the downloaded dumps matches "zzz".')).toBeInTheDocument();
  });

  it("says when no ecosystem is downloaded, and points to the table above", async () => {
    vi.mocked(api.cveSearch).mockResolvedValue(found({ downloaded_ecosystems: 0, ecosystems_searched: [], total: 0, results: [] }));
    show();
    expect(await screen.findByText("No ecosystem is downloaded yet")).toBeInTheDocument();
    expect(screen.getByText(/Download an ecosystem in the table above/)).toBeInTheDocument();
  });

  it("says why a search was refused, such as too many matches for a state", async () => {
    vi.mocked(api.cveSearch).mockRejectedValue(new ApiError({ status: 400, title: "More than 20,000 advisories match, too many to sort by state. Type a CVE id, an advisory id or a package to narrow the search." }));
    show();
    expect(await screen.findByText(/too many to sort by state/)).toBeInTheDocument();
  });
});

describe("CveSearch: one advisory in full", () => {
  it("opens a window at once with what the row knew, then fills in the advisory", async () => {
    let finish: (d: CveDetail) => void = () => undefined;
    vi.mocked(api.cveDetail).mockReturnValue(new Promise((r) => { finish = r; }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Open CVE-2021-23337 in npm" }));
    const dialog = await screen.findByRole("dialog", { name: "CVE-2021-23337" });
    expect(api.cveDetail).toHaveBeenCalledWith("npm", "GHSA-aaaa");
    expect(within(dialog).getByRole("status", { name: "Loading the advisory" })).toHaveTextContent("Command injection in lodash");       // a skeleton under the row's own summary
    finish(detail());
    expect(await within(dialog).findByText("Affected packages (2)")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("introduced 0; fixed 4.17.21");
    expect(dialog).toHaveTextContent("CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:U/C:H/I:H/A:H");
    expect(dialog).toHaveTextContent("CWE-77");
    expect(dialog).toHaveTextContent("In your store");
    expect(dialog).toHaveTextContent("Read from the downloaded dump.");
    expect(within(dialog).getByRole("link", { name: /github.com\/lodash\/lodash @ aaaaaaaaaa/ })).toHaveAttribute("href", "https://github.com/lodash/lodash/commit/" + "a".repeat(40));
    expect(within(dialog).getByRole("link", { name: "https://example.org/a" })).toHaveAttribute("href", "https://example.org/a");
    expect(within(dialog).queryByRole("link", { name: /javascript/ })).toBeNull();                         // an address that is not http or https is text, never a link
    expect(within(dialog).getByText("Raw record (JSON)")).toBeInTheDocument();
  });

  it("does not let a late answer for an earlier advisory replace the one that is open", async () => {
    const waiting: Record<string, (d: CveDetail) => void> = {};
    vi.mocked(api.cveDetail).mockImplementation((_eco, id) => new Promise((r) => { waiting[id] = r; }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Open CVE-2021-23337 in npm" }));
    await screen.findByRole("dialog");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Open GHSA-bbbb in PyPI" }));
    const second = await screen.findByRole("dialog", { name: "GHSA-bbbb" });
    waiting["GHSA-bbbb"](detail({ osv_id: "GHSA-bbbb", cve_id: null, ecosystem: "PyPI", summary: "The second one", affected: [] }));
    expect(await within(second).findByText("The second one")).toBeInTheDocument();
    waiting["GHSA-aaaa"](detail({ summary: "The first one, late" }));                                      // arrives after: must not show
    await new Promise((r) => setTimeout(r, 30));
    expect(within(second).queryByText("The first one, late")).toBeNull();
    expect(within(second).getByText("The second one")).toBeInTheDocument();
  });

  it("says why an advisory could not be opened and tries again when asked", async () => {
    vi.mocked(api.cveDetail).mockRejectedValueOnce(new ApiError({ status: 404, title: "That advisory is not in the downloaded dump of this ecosystem." }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Open CVE-2021-23337 in npm" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Could not open this advisory")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /retry|try again/i }));
    expect(await within(dialog).findByText("Affected packages (2)")).toBeInTheDocument();
    expect(api.cveDetail).toHaveBeenCalledTimes(2);
  });
});

describe("CveSearch: a dump that was not read yet", () => {
  it("names it and offers to read it, which starts the read without downloading anything", async () => {
    vi.mocked(api.cveSearch).mockResolvedValue(found({ unread: [{ ecosystem: "Ubuntu", dump_bytes: 777_941_722 }] }));
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "osv_install", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "osv_install", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    const note = await screen.findByText(/A downloaded dump has not been read yet, so it is not searched/);
    expect(note.closest("[role=status]")).toHaveTextContent("Ubuntu");
    expect(note.closest("[role=status]")).toHaveTextContent("741.9 MB");
    fireEvent.click(screen.getByRole("button", { name: "Read the Ubuntu dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Read the Ubuntu dump" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Read" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("osv_install", { ecosystem: "Ubuntu", skip_download: true }));
    vi.unstubAllGlobals();
  });

  it("says that no dump has been read yet when the only downloads are unread ones", async () => {
    vi.mocked(api.cveSearch).mockResolvedValue(found({ downloaded_ecosystems: 0, ecosystems_searched: [], total: 0, results: [], unread: [{ ecosystem: "npm", dump_bytes: 1000 }] }));
    show();
    expect(await screen.findByText("No dump has been read yet")).toBeInTheDocument();
  });
});

describe("CveSearch: a read that was cut", () => {
  it("says how far it got and continues it, without downloading anything", async () => {
    vi.mocked(api.cveSearch).mockResolvedValue(found({ unread: [{ ecosystem: "Ubuntu", dump_bytes: 777_941_722, partial: { percent: 45, done: 45, total: 100, rows: 90, phase: "parsing", updated_at: null, active: false } }] }));
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "osv_install", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "osv_install", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    const note = await screen.findByText(/A downloaded dump has not been read yet/);
    expect(note.closest("[role=status]")).toHaveTextContent("stopped at 45%");
    expect(screen.queryByRole("button", { name: "Read the Ubuntu dump" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Continue reading the Ubuntu dump" }));
    const dialog = await screen.findByRole("dialog", { name: "Continue reading the Ubuntu dump" });
    expect(dialog).toHaveTextContent("It stopped at 45%. Continue picks up from there.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("osv_install", { ecosystem: "Ubuntu", skip_download: true }));
    vi.unstubAllGlobals();
  });

  it("cannot be continued while another process is reading it", async () => {
    vi.mocked(api.cveSearch).mockResolvedValue(found({ unread: [{ ecosystem: "Ubuntu", dump_bytes: 1000, partial: { percent: 10, done: 1, total: 10, rows: 2, phase: "parsing", updated_at: null, active: true } }] }));
    show();
    await screen.findByText(/A downloaded dump has not been read yet/);
    expect(screen.getByRole("button", { name: "Continue reading the Ubuntu dump" })).toBeDisabled();
  });
});
