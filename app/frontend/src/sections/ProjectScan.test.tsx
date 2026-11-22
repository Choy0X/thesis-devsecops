import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { tipOf } from "../test/hint";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { projectScans: vi.fn(), projectScan: vi.fn(), scanProjectDeps: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { api, type Capabilities, type DepReport, type DepRow, type Project } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { CapabilityTable, DepReportView, DependencyScan, STATE_WORDS } from "./ProjectScan";

const row = (over: Partial<DepRow>): DepRow => ({ id: "dep-1", ecosystem: "npm", name: "lodash", version: "4.17.15", declared: null, source: "lock", manifests: ["package-lock.json"], state: "no_finding",
  reason: "No finding by advisory matching: 3 advisories on this computer name this package and none affects 4.17.15.", checks: ["advisory match"], ...over });
const report = (over: Partial<DepReport> = {}): DepReport => ({ schema: "project-scan/1", kind: "dependencies", scan_id: "20261009-100000-ab12", project_id: "p", started: "2026-10-09T10:00:00Z", finished: "2026-10-09T10:00:09Z",
  outdated_checked: false, words: "A dependency without a finding means no finding by advisory matching, not that it is safe. A range without a lock file is inconclusive.",
  ecosystems: { npm: { checked: true, dependencies: 4 }, Go: { checked: false, reason: "Not supported yet for Go: its manifests cannot be read, so nothing was checked." } },
  problems: [{ ecosystem: "Maven", manifest: "pom.xml", reason: "pom.xml is a kind of file this app does not read yet." }], outdated: null,
  summary: { dependencies: 4, vulnerable: 1, no_finding: 1, inconclusive: 1, env_error: 1, severity: { critical: 0, high: 1, medium: 0, low: 0, unknown: 0 }, manifests_not_read: 1 },
  dependencies: [
    row({ id: "dep-1", state: "vulnerable", severity: "high", reason: "4.17.15 is affected by 1 advisory.", recommendation: "Upgrade to 4.17.21.",
      advisories: [{ id: "GHSA-35jh", cve: "CVE-2021-23337", summary: "Command injection in lodash", severity: "high", cwe: "CWE-77", affected_versions: ">=0 <4.17.21", first_fixed: "4.17.21", safe_version: "4.17.21", rule: "range >=0 <4.17.21" }] }),
    row({ id: "dep-2", name: "left-pad", version: "1.3.0" }),
    row({ id: "dep-3", name: "react", version: null, declared: "^18.0.0", state: "inconclusive", source: "declared", checks: [], manifests: ["package.json"], reason: "Only a range is declared (^18.0.0), so the version in use is not known. A lock file would say which version is installed." }),
    row({ id: "dep-4", ecosystem: "PyPI", name: "flask", version: "2.0.1", state: "env_error", checks: [], reason: "The advisories of PyPI are not on this computer, so nothing was checked. Download and ingest them under CVE Collections." }),
  ], ...over });
const project = { id: "p", name: "Shop", folders: [], languages: {}, ecosystems: ["npm"], created_at: "x", updated_at: "x", authorization: { acknowledged: true, at: "x", statement: "s" }, last_scan: null, instructions: "" } as Project;

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.projectScans).mockResolvedValue({ scans: [] });
});

describe("DepReportView", () => {
  it("shows the counts of the four states and never calls a state without a finding safe", () => {
    render(<DepReportView r={report()} />);
    expect(screen.getByText("no finding by advisory matching")).toBeInTheDocument();
    expect(screen.getByText(/not that it is safe/)).toBeInTheDocument();
    expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(4);
    expect(screen.getByRole("status")).toHaveTextContent("Go: Not supported yet for Go");
    expect(screen.queryByText(/\bsafe\b/i, { selector: "td" })).toBeNull();
  });

  it("lists every dependency with its state in words, the vulnerable with their severity", () => {
    render(<DepReportView r={report()} />);
    const lodash = screen.getByText("lodash").closest("tr") as HTMLElement;
    expect(lodash).toHaveTextContent("high · vulnerable");
    expect(screen.getByText("left-pad").closest("tr")).toHaveTextContent(STATE_WORDS.no_finding);
    expect(screen.getByText("react").closest("tr")).toHaveTextContent("^18.0.0");
    expect(screen.getByText("react").closest("tr")).toHaveTextContent("Inconclusive: the version in use is not known");
    expect(screen.getByText("flask").closest("tr")).toHaveTextContent("Not checked");
  });

  it("shows the evidence of a finding on request: the advisory, its range, how it matched, the fix and where the dependency came from", () => {
    render(<DepReportView r={report()} />);
    fireEvent.click(within(screen.getByText("lodash").closest("tr") as HTMLElement).getByRole("button", { name: "Why" }));
    expect(screen.getByText("Upgrade to 4.17.21.")).toBeInTheDocument();
    expect(screen.getByText(/Affects >=0 <4.17.21; matched by range >=0 <4.17.21; first fixed in 4.17.21; CWE-77\. Advisory GHSA-35jh\./)).toBeInTheDocument();
    expect(screen.getByText("CVE-2021-23337")).toBeInTheDocument();
    expect(screen.getByText(/From package-lock\.json; checks run: advisory match/)).toBeInTheDocument();
  });

  it("says what could not be checked: a range with no lock file and an ecosystem without advisories", () => {
    render(<DepReportView r={report()} />);
    fireEvent.click(within(screen.getByText("react").closest("tr") as HTMLElement).getByRole("button", { name: "Why" }));
    expect(screen.getByText(/A lock file would say which version is installed\./)).toBeInTheDocument();
    expect(screen.getByText(/no check was run/)).toBeInTheDocument();
    fireEvent.click(within(screen.getByText("flask").closest("tr") as HTMLElement).getByRole("button", { name: "Why" }));
    expect(screen.getByText(/Download and ingest them under CVE Collections/)).toBeInTheDocument();
  });

  it("filters by state and lists the manifests that were not read", async () => {
    render(<DepReportView r={report()} />);
    await pickOption("Show", /^Vulnerable/);
    expect(screen.getByText("lodash")).toBeInTheDocument();
    expect(screen.queryByText("left-pad")).toBeNull();
    await pickOption("Show", /^all/);
    fireEvent.click(screen.getByText(/1 manifest was not read/));
    expect(screen.getByText(/pom.xml is a kind of file this app does not read yet/)).toBeInTheDocument();
  });

  it("shows the latest release and how far behind a dependency is when the registries were asked", () => {
    const r = report({ outdated_checked: true, outdated: { looked_up: 2, outdated: 1, up_to_date: 1, unavailable: 0, not_looked_up: 0 },
      dependencies: [row({ outdated: { state: "outdated", latest: "4.17.21", behind: "patch" } }), row({ id: "dep-2", name: "left-pad", outdated: { state: "up_to_date", latest: "1.3.0", behind: null } }),
        row({ id: "dep-3", name: "x", outdated: { state: "unavailable", reason: "The registry did not answer." } })] });
    render(<DepReportView r={r} />);
    expect(screen.getByText("lodash").closest("tr")).toHaveTextContent("newer: 4.17.21 (patch)");
    expect(screen.getByText("left-pad").closest("tr")).toHaveTextContent("latest");
    expect(screen.getByText("x", { selector: "td" }).closest("tr")).toHaveTextContent("The registry did not answer.");
    expect(screen.getByText(/Registries: 1 outdated, 1 on the latest release, 0 without an answer\./)).toBeInTheDocument();
  });
});

describe("DependencyScan", () => {
  const show = () => render(<ConfirmProvider><DependencyScan project={project} /></ConfirmProvider>);

  it("says a project has not been scanned yet", async () => {
    show();
    expect(await screen.findByText("This project has not been scanned yet.")).toBeInTheDocument();
  });

  it("shows the newest report of an earlier scan", async () => {
    vi.mocked(api.projectScans).mockResolvedValue({ scans: [{ scan_id: "20261009-100000-ab12", kind: "dependencies", finished: "2026-10-09T10:00:09Z", summary: report().summary }] });
    vi.mocked(api.projectScan).mockResolvedValue(report());
    show();
    expect(await screen.findByText("lodash")).toBeInTheDocument();
    expect(api.projectScan).toHaveBeenCalledWith("p", "20261009-100000-ab12");
  });

  it("starts the scan through the project's own route and says the registry lookup sends package names, not code", async () => {
    vi.mocked(api.scanProjectDeps).mockResolvedValue({ id: "j1", stage: "scan_project_deps" });
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "scan_project_deps", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    await screen.findByText("This project has not been scanned yet.");
    expect(screen.getByText(/sends the package names \(not the project's code\)/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Also look up newer releases"));
    fireEvent.click(screen.getByRole("button", { name: "Scan the dependencies" }));
    await waitFor(() => expect(api.scanProjectDeps).toHaveBeenCalledWith("p", true));
    vi.unstubAllGlobals();
  });

  it("reports a failure to read the scans", async () => {
    vi.mocked(api.projectScans).mockImplementation(() => Promise.reject(new Error("offline")));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  });
});

describe("CapabilityTable", () => {
  const caps: Capabilities = { capabilities: [{ key: "manifest_parsing", label: "Reads the manifests and lock files" }, { key: "advisory_match", label: "Matches the versions against known advisories" }],
    ecosystems: { Go: { manifest_parsing: { status: "not_implemented", note: "" }, advisory_match: { status: "not_implemented", note: "no manifest can be read for this ecosystem yet" } },
      npm: { manifest_parsing: { status: "partial", note: "package.json, package-lock.json" }, advisory_match: { status: "needs_data", note: "Download and ingest them under CVE Collections" } } } };

  it("puts the project's own ecosystems first and says in words what each cell means", async () => {
    render(<CapabilityTable data={caps} mine={["npm"]} />);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("npm");
    expect(rows[0]).toHaveTextContent("in this project");
    expect(rows[0]).toHaveTextContent("In part");
    expect(rows[0]).toHaveTextContent("Needs advisories");
    expect(rows[1]).toHaveTextContent("Go");
    expect(rows[1]).toHaveTextContent("Not yet");
    expect(await tipOf(within(rows[0]).getByText("Needs advisories"))).toBe("Download and ingest them under CVE Collections");
  });
});
