import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { projectCandidate: vi.fn() } };
});
import { api, type Candidate, type ResponseFinding, type ResponseReport } from "../lib/api";
import { ResponseReportView } from "./ResponseReport";

const finding = (over: Partial<ResponseFinding> = {}): ResponseFinding => ({ id: "f1", kind: "codebase", rule: "py-exec-eval", message: "eval runs text as code", state: "inconclusive", file: "src/app.py", line: 4,
  cwe: ["CWE-95"], severity: "high", severity_class: "Warning", class_source: "rule", class_note: "rated high by this app", action: { action: "candidate_made", candidate_id: "20261010-120000-abcd", outcome: "improved", receipt_state: "not_verified" }, ...over });

const report = (over: Partial<ResponseReport> = {}): ResponseReport => ({ schema: "project-scan/3", kind: "response", scan_id: "20261010-120500-abcd", project_id: "shop-ab12cd", started: "2026-10-10T12:00:00Z", finished: "2026-10-10T12:05:00Z",
  scope: "both", mode: "auto-patch", depth: "quick", options: { assist: true, allow_remote: false, test: true }, email: null, notes: ["1 dependencies could not be matched exactly and 0 could not be checked; they are not findings and not clean"],
  findings: [finding(), finding({ id: "f2", kind: "dependency", package: "flask", version: "2.0.1", message: "Flask session cookie leak", cve: "CVE-2023-30861", file: "requirements.txt", line: null, severity_class: "Critical", class_source: "cvss_vector",
    action: { action: "upgrade_recommended", upgrade_to: "2.2.5", how: "Use Update dependencies" } }),
    finding({ id: "f3", message: "A header is missing", severity_class: "Unknown", class_source: "unknown", class_note: "the finding carries no score", action: { action: "no_candidate", why: "this finding is not about a file of the project" } }),
    finding({ id: "f4", message: "Hard-coded value", severity_class: "Recommendation", class_source: "model", action: { action: "candidate_stopped", why: "No language model is configured, so no patch can be proposed." } })],
  summary: { findings: 4, by_class: { Critical: 1, Warning: 1, Recommendation: 1, Unknown: 1 }, by_action: { candidate_made: 1, upgrade_recommended: 1, no_candidate: 1, candidate_stopped: 1 } },
  words: "Every response here is staged for a person: a candidate patch is not applied, not verified and not called correct, and an upgrade is a recommendation.", ...over });

const candidate: Candidate = { schema: "patch-candidate/1", candidate_id: "20261010-120000-abcd", project_id: "shop-ab12cd", created: "2026-10-10T12:03:00Z", label: "candidate", receipt_state: "not_verified", file: "src/app.py",
  reasons: ["no test reproduces the weakness before the patch, so it cannot be verified"], words: "A candidate for a person to review. It has not been applied to the project, and it is not called correct, safe or ready to merge: no test reproduces the weakness.",
  steps: { target: { status: "passed", detail: "src/app.py, 4 lines" }, model: { status: "passed", detail: "tiny-1 on 127.0.0.1" }, diff: { status: "passed", detail: "9 lines" }, apply: { status: "passed", detail: "applied to a temporary copy of the file" },
    tests_before: { status: "passed", detail: "3 tests: 2 passed, 1 failed, 0 errors, 0 skipped" }, tests_after: { status: "not_run", detail: "the tool image has not been built yet" } },
  model: { model: "tiny-1", host: "127.0.0.1", local: true }, diff: "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,4 +1,4 @@\n-    return eval(x)\n+    return int(x)\n",
  tests: { before: null, after: null, outcome: "same", words: "1 tests pass after the patch, as many failures as before (1)" }, finding: { id: "f1", file: "src/app.py", line: 4, rule: "py-exec-eval", message: "eval runs text as code" } };

beforeEach(() => {
  vi.mocked(api.projectCandidate).mockReset();
  vi.mocked(api.projectCandidate).mockResolvedValue(candidate);
});

describe("the report of a response run", () => {
  it("counts the findings by class, says where each class came from, and never calls an unscored finding a warning", () => {
    render(<ResponseReportView r={report()} />);
    const table = screen.getByRole("table", { name: "Findings and responses" });
    const row = (text: string) => within(table).getByText(text).closest("tr") as HTMLElement;
    expect(row("Flask session cookie leak")).toHaveTextContent("Critical");
    expect(row("Flask session cookie leak")).toHaveTextContent("from the CVSS vector");
    expect(row("eval runs text as code")).toHaveTextContent("rated by this app's rule");
    expect(row("A header is missing")).toHaveTextContent("Unknown");
    expect(row("A header is missing")).toHaveTextContent("no score");
    expect(row("Hard-coded value")).toHaveTextContent("a model's estimate, not a measured score");
    expect(screen.getByRole("list", { name: "Notes" })).toHaveTextContent("could not be matched exactly");
  });

  it("says what was done about each finding, with the reason when nothing could be", () => {
    render(<ResponseReportView r={report()} />);
    const table = screen.getByRole("table", { name: "Findings and responses" });
    expect(within(within(table).getByText("Flask session cookie leak").closest("tr") as HTMLElement).getByText("Upgrade recommended")).toBeInTheDocument();
    expect(within(table).getByText("to 2.2.5")).toBeInTheDocument();
    expect(within(table).getByText("No candidate possible")).toBeInTheDocument();
    expect(within(table).getByText("this finding is not about a file of the project")).toBeInTheDocument();
    expect(within(table).getByText("No patch candidate")).toBeInTheDocument();
    expect(within(table).getByText(/No language model is configured/)).toBeInTheDocument();
  });

  it("opens a patch candidate: not verified, not applied, with the steps, the tests and the diff", async () => {
    render(<ResponseReportView r={report()} />);
    fireEvent.click(screen.getByRole("button", { name: "Open the candidate for src/app.py:4" }));
    const dialog = await screen.findByRole("dialog", { name: "Patch candidate" });
    expect(api.projectCandidate).toHaveBeenCalledWith("shop-ab12cd", "20261010-120000-abcd");
    expect(await within(dialog).findByText("not verified")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("It has not been applied to your folders");
    expect(dialog).toHaveTextContent("nothing here reproduces the weakness");
    expect(within(dialog).getByRole("list", { name: "Steps" })).toHaveTextContent("Tests after: the tool image has not been built yet");
    expect(dialog).toHaveTextContent("the same tests fail after the patch");
    expect(within(dialog).getByLabelText("The diff")).toHaveTextContent("return int(x)");
    expect(dialog).toHaveTextContent("Proposed by tiny-1 on 127.0.0.1 (this computer)");
    expect(dialog.textContent).not.toMatch(/\b(is|are|was) (correct|safe|ready to merge)\b/);
  });

  it("filters by class and by response", async () => {
    render(<ResponseReportView r={report()} />);
    await pickOption("Class", /^Unknown/);
    expect(screen.getByText("A header is missing")).toBeInTheDocument();
    expect(screen.queryByText("Flask session cookie leak")).toBeNull();
    await pickOption("Class", "all");
    await pickOption("Action", /^Upgrade recommended/);
    expect(screen.getByText("Flask session cookie leak")).toBeInTheDocument();
    expect(screen.queryByText("A header is missing")).toBeNull();
  });

  it("shows a composed email that was not sent, and says an empty report is not the same as no problem", () => {
    const email = { subject: "[shop] 1 findings: 1 Warning", body: "Warning: src/app.py - eval", sent: false, reason: "mail is not set up in Settings, so the message was composed and not sent" };
    const { rerender } = render(<ResponseReportView r={report({ email })} />);
    expect(screen.getByText(/Email composed, not sent: \[shop\] 1 findings/)).toBeInTheDocument();
    expect(screen.getByText(/mail is not set up in Settings/)).toBeInTheDocument();
    rerender(<ResponseReportView r={report({ findings: [], summary: { findings: 0, by_class: { Critical: 0, Warning: 0, Recommendation: 0, Unknown: 0 }, by_action: {} } })} />);
    expect(screen.getByText(/That is not the same as no problem/)).toBeInTheDocument();
  });

  it("reports a candidate that cannot be read", async () => {
    vi.mocked(api.projectCandidate).mockRejectedValue(new Error("There is no such candidate."));
    render(<ResponseReportView r={report()} />);
    fireEvent.click(screen.getByRole("button", { name: "Open the candidate for src/app.py:4" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("There is no such candidate."));
  });
});
