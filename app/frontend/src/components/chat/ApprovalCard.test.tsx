import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AgentPendingCall } from "../../lib/api";
import { ApprovalCard } from "./ApprovalCard";

const call = (over: Partial<AgentPendingCall> = {}): AgentPendingCall => ({
  id: "c1", name: "run_command", arguments: JSON.stringify({ command: "pytest -q tests" }), asked_at: "2026-10-16T10:00:00Z",
  permission: { action: "ask", permission: "bash", value: "pytest -q tests", reason: "Running a command changes things; the owner decides.", source: "mode", always: ["pytest *"] }, ...over,
});

describe("ApprovalCard", () => {
  it("shows what is asked, why, and what Allow always would cover", () => {
    render(<ApprovalCard call={call()} busy={false} onApprove={() => {}} onDeny={() => {}} />);
    expect(screen.getByRole("alertdialog", { name: "Approval needed" })).toBeTruthy();
    expect(screen.getByText(/pytest -q tests/)).toBeTruthy();
    expect(screen.getByText(/Running a command changes things/)).toBeTruthy();
    expect(screen.getByText("pytest *")).toBeTruthy();
  });

  it("approves once, for the session or for the project", () => {
    const onApprove = vi.fn();
    render(<ApprovalCard call={call()} busy={false} onApprove={onApprove} onDeny={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Allow for this session" }));
    fireEvent.click(screen.getByRole("button", { name: "Allow for this project" }));
    expect(onApprove.mock.calls).toEqual([[], ["session"], ["project"]]);
  });

  it("offers no Allow always for a call that can only be allowed once", () => {
    const once = call({ permission: { action: "ask", permission: "bash", value: "rm x", reason: "", source: "engine", always: [] } });
    render(<ApprovalCard call={once} busy={false} onApprove={() => {}} onDeny={() => {}} />);
    expect(screen.queryByRole("button", { name: "Allow for this session" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Allow for this project" })).toBeNull();
    expect(screen.queryByText(/Allow always would cover/)).toBeNull();
  });

  it("still works for a call recorded before permissions existed", () => {
    const old = call({ permission: undefined });
    render(<ApprovalCard call={old} busy={false} onApprove={() => {}} onDeny={() => {}} />);
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Allow for this session" })).toBeNull();
  });

  it("denies at once, or with a note to the agent", () => {
    const onDeny = vi.fn();
    render(<ApprovalCard call={call()} busy={false} onApprove={() => {}} onDeny={onDeny} />);
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    expect(onDeny).toHaveBeenLastCalledWith();
    fireEvent.click(screen.getByRole("button", { name: "Deny with a note" }));
    fireEvent.change(screen.getByLabelText("What should the agent do instead?"), { target: { value: "  use make test  " } });
    fireEvent.click(screen.getByRole("button", { name: "Send note and deny" }));
    expect(onDeny).toHaveBeenLastCalledWith("use make test");
  });

  it("shows an edit as what goes and what comes", () => {
    const edit = call({ name: "edit_file", arguments: JSON.stringify({ path: "a.py", old_string: "x = 1", new_string: "x = 2" }), permission: undefined });
    render(<ApprovalCard call={edit} busy={false} onApprove={() => {}} onDeny={() => {}} />);
    expect(screen.getByText(/a\.py/)).toBeTruthy();
  });

  it("disables every choice while busy", () => {
    render(<ApprovalCard call={call()} busy onApprove={() => {}} onDeny={() => {}} />);
    for (const b of screen.getAllByRole("button")) expect((b as HTMLButtonElement).disabled).toBe(true);
  });
});
