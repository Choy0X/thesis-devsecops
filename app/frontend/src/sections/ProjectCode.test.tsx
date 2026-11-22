import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { projectScans: vi.fn(), projectScan: vi.fn(), scanProjectCode: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { api, type CodeFinding, type CodeReport, type Project } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { CodeReportView, CodeScan, PAGE } from "./ProjectCode";

const finding = (over: Partial<CodeFinding> = {}): CodeFinding => ({ id: "code-1", rule: "py-os-system", severity: "high", cwe: "CWE-78", summary: "Shell command execution (command injection)",
  why: "A shell command is run. If a piece of the command comes from a request, a crafted value can add its own commands.", advice: "Pass the command as a list without shell=True.",
  state: "candidate", file: "src/app.py", folder: "C:\\code\\shop", line: 2, language: "Python", in_tests: false, snippet: "os.system('rm ' + name)",
  context: [{ line: 1, text: "import os", match: false }, { line: 2, text: "os.system('rm ' + name)", match: true }], ...over });
const report = (over: Partial<CodeReport> = {}): CodeReport => ({ schema: "project-scan/1", kind: "code", scan_id: "20261009-120000-ab12", project_id: "p", started: "2026-10-09T12:00:00Z",
  finished: "2026-10-09T12:00:03Z", words: "Every entry is a candidate: a text pattern matched a line, which is a place to look, not a proof that the program is vulnerable. No entry does not mean no problem.",
  rules: 28, languages: { Python: { files: 3, scanned: 3, coverage: "rules", reason: "" }, JavaScript: { files: 1, scanned: 1, coverage: "rules", reason: "" }, Java: { files: 2, scanned: 2, coverage: "secrets_only", reason: "There are no vulnerability rules for Java yet, so these files were checked for hard-coded secrets only." } },
  skipped: { too_large: 1, binary: 0, unreadable: 0, links: 0 },
  summary: { files: 6, findings: 3, severity: { critical: 0, high: 2, medium: 1, low: 0 }, by_rule: { "py-os-system": 1, "js-innerhtml": 1, "py-exec-eval": 1 }, in_tests: 1, secrets_only_languages: ["Java"], skipped: 1, cut_short: false },
  findings: [
    finding(),
    finding({ id: "code-2", rule: "py-exec-eval", cwe: "CWE-95", summary: "Use of eval/exec on dynamic input (code injection)", file: "tests/test_app.py", line: 7, in_tests: true, context: [], snippet: "eval(x)" }),
    finding({ id: "code-3", rule: "js-innerhtml", severity: "medium", cwe: "CWE-79", summary: "HTML written from a string (cross-site scripting)", file: "src/ui.js", line: 1, language: "JavaScript", snippet: "el.innerHTML = html;", note: "A note about this one." }),
  ], ...over });
const project = { id: "p", name: "Shop", folders: [], languages: {}, ecosystems: [], created_at: "x", updated_at: "x", authorization: { acknowledged: true, at: "x", statement: "s" }, last_scan: null, instructions: "" } as Project;

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.projectScans).mockResolvedValue({ scans: [] });
});

describe("CodeReportView", () => {
  it("says every entry is a candidate and that no entry is not the same as no problem", () => {
    render(<CodeReportView r={report()} />);
    expect(screen.getByText(/Every entry is a candidate/)).toBeInTheDocument();
    expect(screen.getByText(/No entry does not mean no problem/)).toBeInTheDocument();
    expect(screen.getByText(/Scanned 2026-10-09 12:00 with 28 rules/)).toBeInTheDocument();
  });

  it("says per language how it was checked: with rules, or for hard-coded secrets only", () => {
    render(<CodeReportView r={report()} />);
    const list = screen.getByRole("list", { name: "What was checked" });
    expect(within(list).getByText("Python", { selector: "span" }).closest("li")).toHaveTextContent("3 of 3 files read): checked with rules");
    expect(within(list).getByText("Java", { selector: "span" }).closest("li")).toHaveTextContent("There are no vulnerability rules for Java yet");
    expect(screen.getByText(/Files not read: 1 too large\./)).toBeInTheDocument();
  });

  it("lists the candidates, the serious first, with where they are and the rule's CWE", () => {
    render(<CodeReportView r={report()} />);
    const table = screen.getByRole("table", { name: "Candidates found in the code" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("high");
    expect(rows[0]).toHaveTextContent("src/app.py:2");
    expect(rows[0]).toHaveTextContent("CWE-78");
    expect(rows[1]).toHaveTextContent("test code");
  });

  it("shows the evidence of a candidate on request: the lines around it, why it matters and what to check", () => {
    render(<CodeReportView r={report()} />);
    fireEvent.click(screen.getByRole("button", { name: "Show the evidence for src/app.py line 2" }));
    expect(screen.getByText(/A shell command is run/)).toBeInTheDocument();
    expect(screen.getByText(/To check: Pass the command as a list/)).toBeInTheDocument();
    expect(screen.getByLabelText("Lines around src/app.py line 2")).toHaveTextContent("import os");
    expect(screen.getByText(/a candidate: a pattern matched this line, nothing was run/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide the evidence for src/app.py line 2" }));
    expect(screen.queryByText(/A shell command is run/)).toBeNull();
  });

  it("shows a rule's note and, without surrounding lines, the line itself", () => {
    render(<CodeReportView r={report()} />);
    fireEvent.click(screen.getByRole("button", { name: "Show the evidence for src/ui.js line 1" }));
    expect(screen.getByText("A note about this one.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show the evidence for tests/test_app.py line 7" }));
    expect(screen.getByText("eval(x)", { selector: "p" })).toBeInTheDocument();
  });

  it("filters by severity, rule and language, and can hide test code", async () => {
    render(<CodeReportView r={report()} />);
    await pickOption("Severity", /^medium/);
    expect(screen.getByText("src/ui.js:1")).toBeInTheDocument();
    expect(screen.queryByText("src/app.py:2")).toBeNull();
    await pickOption("Severity", "all");
    await pickOption("Rule", /^py-exec-eval/);
    expect(screen.getByText(/tests\/test_app\.py:7/)).toBeInTheDocument();
    await pickOption("Rule", "all");
    await pickOption("Language", "JavaScript");
    expect(screen.queryByText(/src\/app\.py:2/)).toBeNull();
    await pickOption("Language", "all");
    fireEvent.click(screen.getByLabelText("Hide candidates in test code"));
    expect(screen.queryByText(/tests\/test_app\.py:7/)).toBeNull();
    expect(screen.getByText("src/app.py:2")).toBeInTheDocument();
  });

  it("says that no match is not a clean bill, and that a scan which hit its limits may have missed some", () => {
    const empty = report({ findings: [], summary: { ...report().summary, findings: 0, severity: { critical: 0, high: 0, medium: 0, low: 0 }, by_rule: {}, in_tests: 0, cut_short: true } });
    render(<CodeReportView r={empty} />);
    expect(screen.getByText(/No pattern matched\. That is not the same as no problem/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("The scan stopped at its limits");
  });

  it("says when no source file of a known language was found", () => {
    render(<CodeReportView r={report({ languages: {}, findings: [], skipped: {} })} />);
    expect(screen.getByText(/No source files of a known language were found/)).toBeInTheDocument();
  });

  it("shows a long list a page at a time", () => {
    const many = Array.from({ length: PAGE + 30 }, (_, i) => finding({ id: `code-${i + 1}`, file: `src/f${i}.py`, line: 1 }));
    render(<CodeReportView r={report({ findings: many })} />);
    expect(screen.getAllByRole("button", { name: /Show the evidence for/ })).toHaveLength(PAGE);
    fireEvent.click(screen.getByRole("button", { name: "Show 30 more of 30" }));
    expect(screen.getAllByRole("button", { name: /Show the evidence for/ })).toHaveLength(PAGE + 30);
  });
});

describe("CodeScan", () => {
  const show = () => render(<ConfirmProvider><CodeScan project={project} /></ConfirmProvider>);

  it("says the code has not been scanned yet", async () => {
    show();
    expect(await screen.findByText("The code of this project has not been scanned yet.")).toBeInTheDocument();
  });

  it("shows the newest report of the code, not of the dependencies", async () => {
    vi.mocked(api.projectScans).mockResolvedValue({ scans: [
      { scan_id: "20261009-130000-aaaa", kind: "dependencies", finished: "2026-10-09T13:00:00Z", summary: { dependencies: 1, vulnerable: 0, no_finding: 1, inconclusive: 0, env_error: 0, severity: {}, manifests_not_read: 0 } as any },
      { scan_id: "20261009-120000-ab12", kind: "code", finished: "2026-10-09T12:00:03Z", summary: report().summary }] });
    vi.mocked(api.projectScan).mockResolvedValue(report());
    show();
    expect(await screen.findByText("src/app.py:2")).toBeInTheDocument();
    expect(api.projectScan).toHaveBeenCalledWith("p", "20261009-120000-ab12");
  });

  it("starts the scan through the project's own route", async () => {
    vi.mocked(api.scanProjectCode).mockResolvedValue({ id: "j1", stage: "scan_project_code" });
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "scan_project_code", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    await screen.findByText("The code of this project has not been scanned yet.");
    fireEvent.click(screen.getByRole("button", { name: "Scan the code" }));
    await waitFor(() => expect(api.scanProjectCode).toHaveBeenCalledWith("p"));
    vi.unstubAllGlobals();
  });

  it("reports a failure to read the scans", async () => {
    vi.mocked(api.projectScans).mockImplementation(() => Promise.reject(new Error("offline")));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  });
});
