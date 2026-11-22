import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ScanHistory } from "./ScanHistory";

it("shows determinate dependency progress for the matching run and exposes its stop action", () => {
  const onCancel = vi.fn();
  render(<ScanHistory runs={[{ id: "scan", kind: "scan", state: "running", payload: { scope: "dependencies", dependency_mode: "versions" } }]}
    progress={{ runId: "scan", payload: { phase: "versions", completed: 2, total: 9, package_name: "demo", affected: 1 } }} onCancel={onCancel} />);
  expect(screen.getByRole("progressbar", { name: "Dependency check progress" })).toHaveAttribute("aria-valuenow", "2");
  expect(screen.getByText("2 of 9 packages checked")).toBeVisible();
  expect(screen.getByText("demo")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Stop check" }));
  expect(onCancel).toHaveBeenCalledOnce();
});

it("shows inventory and workspace preparation as indeterminate, without carrying over another run's counts", () => {
  const runs = [{ id: "current", kind: "scan", state: "running", payload: { scope: "project_code" } }];
  const { rerender } = render(<ScanHistory runs={runs} activity={{ run_id: "current", phase: "preparing_workspace" }}
    progress={{ runId: "previous", payload: { phase: "static_analysis", completed: 4, total: 6 } }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Preparing the workspace");
  expect(screen.getByRole("progressbar", { name: "Code scan progress" })).not.toHaveAttribute("aria-valuenow");
  rerender(<ScanHistory runs={runs} progress={{ runId: "current", payload: { phase: "inventory" } }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Finding project files");
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
});

it("shows a queued scan without offering to stop a different active turn", () => {
  render(<ScanHistory runs={[{ id: "turn", kind: "turn", state: "running" },
    { id: "scan", kind: "scan", state: "queued", payload: { scope: "project_code" } }]} onCancel={vi.fn()} />);
  expect(screen.getByRole("status")).toHaveTextContent("Waiting to run");
  expect(screen.queryByRole("button", { name: "Stop scan" })).not.toBeInTheDocument();
});

it("shows code scan counts and puts candidate evidence behind an expandable finding", () => {
  render(<ScanHistory runs={[{ id: "scan", kind: "scan", state: "completed", payload: { scope: "project_code" }, result: {
    summary: { scanned_files: 2, skipped_entries: 1, cut_short: true },
    findings: [{ id: "candidate-1", file: "src/check.py", line: 12, rule: "fixture-rule", title: "Review this call", severity: "high", snippet: "call()" }],
    skipped: { unsupported_type: 1 }, coverage: { limits_reached: true },
  } }]} />);
  expect(screen.getByText("Partial coverage")).toBeVisible();
  expect(within(screen.getByRole("group", { name: "Code scan results" })).getByText("1", { selector: "strong" })).toBeVisible();
  const finding = screen.getByText("Review this call").closest("details");
  expect(finding).not.toHaveAttribute("open");
  expect(finding).toHaveTextContent("src/check.py:12");
  fireEvent.click(screen.getByText("Review this call"));
  expect(finding).toHaveAttribute("open");
  expect(screen.getByText("call()")).toBeVisible();
});

it("keeps affected, clear and unknown version evidence distinct", () => {
  render(<ScanHistory runs={[{ id: "scan", kind: "scan", state: "completed", payload: { scope: "dependencies", dependency_mode: "versions" }, result: {
    packages: [
      { id: "demo", name: "demo", resolution: "locked", resolved: ["1.1.0"], checks: { "1.1.0": {
        status: "affected", lookup: { status: "fresh" }, advisories: [{ id: "GHSA-test", summary: "Fixture advisory" }],
      } }, recommendation: { version: "1.2.0" } },
      { id: "clear", name: "clear", resolved: ["1.0.0"], checks: { "1.0.0": { status: "no_known_vulnerabilities", lookup: { status: "fresh" } } } },
      { id: "unknown", name: "unknown" },
    ],
  } }]} />);
  const advisory = screen.getByRole("link", { name: "GHSA-test" });
  expect(advisory).toBeVisible();
  expect(advisory).toHaveAttribute("href", "https://osv.dev/vulnerability/GHSA-test");
  expect(screen.getByText("Suggested version")).toBeVisible();
  expect(screen.getByText("No advisory match", { selector: "dt" })).toBeVisible();
  expect(screen.getByText("Inconclusive", { selector: "dt" })).toBeVisible();
});
