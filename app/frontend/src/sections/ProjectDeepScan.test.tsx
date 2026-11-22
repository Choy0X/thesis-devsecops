import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { projectScans: vi.fn(), projectScan: vi.fn(), scanProjectCodebase: vi.fn() } };
});
vi.mock("../components/RunConsole", () => ({
  RunConsole: ({ label, launch, disabled, onDone }: any) => <button type="button" disabled={disabled} onClick={() => launch?.({}).then(() => onDone?.())}>{label}</button>,
}));
import { api, type DeepFinding, type DeepReport, type Project } from "../lib/api";
import { DeepReportView, DeepScan, portProblem } from "./ProjectDeepScan";

const project = { id: "shop-ab12cd", name: "The shop" } as Project;

const finding = (over: Partial<DeepFinding> = {}): DeepFinding => ({ id: "f1", tool: "semgrep", sources: ["rules", "semgrep"], rule: "py-exec-eval", rules: ["eval-detected", "py-exec-eval"], state: "inconclusive", basis: "static",
  severity: "high", severity_source: "rule", severity_by: "rules", file: "src/app.py", line: 3, cwe: ["CWE-95"], type: "Injection", message: "eval runs text as code", snippet: "eval(x)", confidence: "high",
  evidence: { file: "scans/s1/evidence/01-semgrep.txt", sha256: "a".repeat(64) }, extra: { why: "If part of that text comes from outside, whoever controls it runs their code.", advice: "Find where the argument comes from.",
  context: [{ line: 2, text: "import os", match: false }, { line: 3, text: "eval(x)", match: true }] }, ...over });

const report = (over: Partial<DeepReport> = {}): DeepReport => ({ schema: "project-scan/2", kind: "codebase", scan_id: "20261010-120000-abcd", project_id: "shop-ab12cd", started: "2026-10-10T12:00:00Z", finished: "2026-10-10T12:05:00Z",
  words: "2 findings from rules, semgrep. bandit did not run to its end, so the code was not fully checked. Not attempted: probe. A finding marked inconclusive is a place a tool flagged.",
  image: { tag: "aivdf/scan-tools:1" }, container: {}, copy: [{ folder: "C:/code/shop", as: "/work/shop", files: 12, bytes: 20480, skipped: { secret_like: 2, skipped_folders_and_caches: 3, links: 0 }, secret_like_names: [".env", "keys/id_rsa"] }],
  checks: [
    { tool: "rules", state: "ran", rule_set: "40 pattern rules", version: null, files: 12, findings: 1, note: "", evidence: null, sha256: null, problems: [] },
    { tool: "semgrep", state: "partial", rule_set: "vendored rules", version: "1.80.0", files: 11, findings: 2, note: "1 problems while reading files", evidence: "scans/s1/evidence/01-semgrep.txt", sha256: "a".repeat(64), problems: ["error: Syntax error at line 3 of broken.js"] },
    { tool: "bandit", state: "env_error", rule_set: "bandit default tests", version: null, files: null, findings: 0, note: "the tool is not in the image or cannot be started", evidence: "scans/s1/evidence/02-bandit.txt", sha256: "b".repeat(64), problems: [] },
    { tool: "pytest", state: "ran", rule_set: "the project's own tests", version: null, files: null, findings: 0, note: "4 tests: 2 passed, 1 failed, 1 skipped", evidence: null, sha256: null, problems: [], failing: ["tests.test_app.test_bad: assert 1 == 2"] },
    { tool: "probe", state: "skipped", rule_set: "loopback probes", version: null, files: null, findings: 0, note: "no start command and port are set for this project, so the program was not started", evidence: null, sha256: null, problems: [] }],
  findings: [finding(), finding({ id: "f2", tool: "gitleaks", sources: ["gitleaks"], rule: "aws-access-token", rules: undefined, state: "vulnerable", basis: "secret", severity: "unknown", severity_source: "unknown", severity_by: undefined,
    file: "deploy.py", line: 7, cwe: ["CWE-798"], type: "Secrets", message: "An AWS key is written in this file", extra: undefined, evidence: null })],
  summary: { findings: 2, by_severity: { critical: 0, high: 1, medium: 0, low: 0, unknown: 1 }, by_state: { vulnerable: 1, inconclusive: 1 },
    by_type: { Injection: 1, "Cross-site scripting": 0, "Access and paths": 0, Cryptography: 0, Secrets: 1, "Unsafe data handling": 0, "Exposure and configuration": 0, "Resource exhaustion": 0, Other: 0 }, checks_ran: ["rules", "semgrep", "pytest"], checks_partial: ["semgrep"], checks_failed: ["bandit"], checks_skipped: ["probe"],
    words: "x" }, ...over });

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.projectScans).mockResolvedValue({ scans: [] });
});

describe("the report of the full scan", () => {
  it("lists every check with how it ended and why, so that no finding is read with its checks beside it", () => {
    render(<DeepReportView r={report()} />);
    const checks = screen.getByRole("table", { name: "Checks" });
    const row = (name: string) => within(checks).getByText(name, { selector: "td" }).closest("tr") as HTMLElement;
    expect(row("rules")).toHaveTextContent("Ran");
    expect(row("semgrep")).toHaveTextContent("Ran, could not read every file");
    expect(row("semgrep")).toHaveTextContent("Syntax error at line 3 of broken.js");
    expect(row("bandit")).toHaveTextContent("Did not run to its end");
    expect(row("bandit")).toHaveTextContent("the tool is not in the image or cannot be started");
    expect(row("pytest")).toHaveTextContent("4 tests: 2 passed, 1 failed, 1 skipped");
    expect(within(row("pytest")).getByRole("list", { name: "Failing tests" })).toHaveTextContent("tests.test_app.test_bad: assert 1 == 2");
    expect(row("probe")).toHaveTextContent("Not attempted");
    expect(row("probe")).toHaveTextContent("so the program was not started");
    expect(row("semgrep")).toHaveTextContent("Raw output: scans/s1/evidence/01-semgrep.txt");
    expect(screen.getByText(/bandit did not run to its end, so the code was not fully checked/)).toBeInTheDocument();
  });

  it("calls a flagged finding flagged and one with evidence vulnerable, and says who rated the severity", () => {
    render(<DeepReportView r={report()} />);
    const findings = screen.getByRole("table", { name: "Findings" });
    expect(within(findings).getByText("Flagged, not confirmed")).toBeInTheDocument();
    expect(within(findings).getByText("Vulnerable, with evidence")).toBeInTheDocument();
    expect(within(findings).getByText("by this app's policy")).toBeInTheDocument();
    expect(within(findings).getByText("not rated")).toBeInTheDocument();
    expect(within(findings).getByText("src/app.py:3")).toBeInTheDocument();
    expect(screen.queryByText(/is safe/)).toBeNull();
  });

  it("shows the evidence of a finding on request: the lines around it, who found it, and where the raw output is", () => {
    render(<DeepReportView r={report()} />);
    fireEvent.click(screen.getByRole("button", { name: "Show the evidence for src/app.py:3" }));
    expect(screen.getByLabelText("Lines around src/app.py line 3")).toHaveTextContent("eval(x)");
    expect(screen.getByText(/eval-detected, py-exec-eval · found by rules and semgrep · static · confidence high · the higher rating is rules's/)).toBeInTheDocument();
    expect(screen.getByText(/raw output: scans\/s1\/evidence\/01-semgrep.txt/)).toBeInTheDocument();
    expect(screen.getByText(/To check: Find where the argument comes from/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide the evidence for src/app.py:3" }));
    expect(screen.queryByLabelText("Lines around src/app.py line 3")).toBeNull();
  });

  it("draws the kinds of weakness as a radar with the same numbers in a table, and filters by kind", async () => {
    render(<DeepReportView r={report()} />);
    expect(screen.getByText("Finding types")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "Finding types" });
    expect(within(table).getByText("Injection").closest("tr")).toHaveTextContent("1");
    expect(within(table).getByText("Secrets").closest("tr")).toHaveTextContent("1");
    expect(within(table).getByText("Cryptography").closest("tr")).toHaveTextContent("0");
    await pickOption("Type", /^Secrets/);
    expect(screen.getByText("deploy.py:7")).toBeInTheDocument();
    expect(screen.queryByText("src/app.py:3")).toBeNull();
  });

  it("filters by state, severity and tool", async () => {
    render(<DeepReportView r={report()} />);
    await pickOption("State", /^Vulnerable, with evidence/);
    expect(screen.getByText("deploy.py:7")).toBeInTheDocument();
    expect(screen.queryByText("src/app.py:3")).toBeNull();
    await pickOption("State", "all");
    await pickOption("Found by", "semgrep");
    expect(screen.getByText("src/app.py:3")).toBeInTheDocument();
    expect(screen.queryByText("deploy.py:7")).toBeNull();
    await pickOption("Found by", "all");
    await pickOption("Severity", /^unknown/);
    expect(screen.getByText("deploy.py:7")).toBeInTheDocument();
    expect(screen.queryByText("src/app.py:3")).toBeNull();
  });

  it("says what was copied and what was not, naming credential-like files without reading them", () => {
    render(<DeepReportView r={report()} />);
    const copied = screen.getByRole("list", { name: "What was copied" });
    expect(copied).toHaveTextContent("12 files (20 KB) of C:/code/shop were copied to /work/shop");
    expect(copied).toHaveTextContent("left out: 2 files that look like keys or credentials, 3 folders of history, caches and packages");
    expect(copied).toHaveTextContent("not copied and not read: .env, keys/id_rsa");
  });

  it("does not read an empty list of findings as a clean bill of health", () => {
    render(<DeepReportView r={report({ findings: [], summary: { ...report().summary, findings: 0, by_state: { vulnerable: 0, inconclusive: 0 } } })} />);
    expect(screen.getByText(/No check found anything\. Read the list of checks above/)).toBeInTheDocument();
  });
});

describe("starting the full scan", () => {
  it("says there is no scan yet, and sends the start command and the port with the request", async () => {
    vi.mocked(api.scanProjectCodebase).mockResolvedValue({ id: "j1", stage: "scan_project_codebase" });
    render(<DeepScan project={project} />);
    expect(await screen.findByText("The code of this project has not been scanned in the sandbox yet.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Start command"), { target: { value: " uvicorn app:app --port $PORT " } });
    expect(screen.getByRole("status")).toHaveTextContent("need both a start command and a port");
    fireEvent.change(screen.getByLabelText("Port"), { target: { value: "8000" } });
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Scan in the sandbox" }));
    await waitFor(() => expect(api.scanProjectCodebase).toHaveBeenCalledWith("shop-ab12cd", { start_command: "uvicorn app:app --port $PORT", port: 8000 }));
  });

  it("starts without the questions when no start command is given", async () => {
    vi.mocked(api.scanProjectCodebase).mockResolvedValue({ id: "j1", stage: "scan_project_codebase" });
    render(<DeepScan project={project} />);
    fireEvent.click(await screen.findByRole("button", { name: "Scan in the sandbox" }));
    await waitFor(() => expect(api.scanProjectCodebase).toHaveBeenCalledWith("shop-ab12cd", { start_command: "", port: null }));
  });

  it("refuses a port that is not a port, and shows the newest report when there is one", async () => {
    vi.mocked(api.projectScans).mockResolvedValue({ scans: [{ scan_id: "20261010-120000-abcd", kind: "codebase", finished: "2026-10-10T12:05:00Z", summary: report().summary }] });
    vi.mocked(api.projectScan).mockResolvedValue(report());
    render(<DeepScan project={project} />);
    expect(await screen.findByRole("table", { name: "Checks" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Port"), { target: { value: "80" } });
    expect(screen.getByText("A port is a whole number from 1024 to 65535.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Scan in the sandbox" })).toBeDisabled();
    expect(api.projectScan).toHaveBeenCalledWith("shop-ab12cd", "20261010-120000-abcd");
  });

  it("checks ports", () => {
    expect([portProblem(""), portProblem("8000"), portProblem("1024"), portProblem("65535")]).toEqual(["", "", "", ""]);
    for (const bad of ["80", "70000", "abc", "80.5", "-1"]) expect(portProblem(bad)).not.toBe("");
  });
});
