import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DevModeProvider } from "../lib/devMode";
import { VerificationTimeline, judge, type Receipt } from "./VerificationTimeline";

const ok = (status = "passed") => ({ status: status as "passed" });
const verified: Receipt = {
  receipt_id: "abc123", verdict: "verified", grade: "G3", reasons: [], runner: { kind: "docker", fixture_only: false, image_digest: "sha256:ff" },
  steps: { apply: ok(), build: ok(), security_before: { status: "failed", detail: "AssertionError: injected" }, security_after: ok(), functionality: ok(), integrity: ok() },
};
const show = (r: Receipt, dev = false) => render(<DevModeProvider initial={dev}><VerificationTimeline receipt={r} /></DevModeProvider>);

describe("judge", () => {
  it("expects the security check to fail before the patch and every other step to pass", () => {
    expect(judge("security_before", "failed")).toBe("as_required");
    expect(judge("security_before", "passed")).toBe("not_as_required");
    expect(judge("apply", "passed")).toBe("as_required");
    expect(judge("apply", "failed")).toBe("not_as_required");
    expect(judge("build", "not_run")).toBe("not_run");
    expect(judge("integrity", "env_error")).toBe("env_error");
  });
});

describe("VerificationTimeline", () => {
  it("shows six steps in order, a reproduced vulnerability as required, and the verdict with its grade", () => {
    show(verified);
    expect(screen.getByRole("status")).toHaveTextContent("Verified");
    expect(screen.getByRole("status")).toHaveTextContent("Grade G3");
    const steps = within(screen.getByRole("list", { name: "Verification steps" })).getAllByRole("listitem");
    expect(steps).toHaveLength(6);
    expect(steps[2]).toHaveTextContent("The vulnerability shows before the patch (As required)");
    expect(steps[2]).toHaveTextContent("so the vulnerability was reproduced");
    expect(steps.every((s) => s.getAttribute("data-state") === "as_required")).toBe(true);
  });

  it("says plainly when the vulnerability was not reproduced before the patch", () => {
    show({ ...verified, verdict: "not_verified", grade: "none", reasons: ["security_before: the vulnerability was not reproduced before the patch"],
      steps: { ...verified.steps, security_before: ok("passed") } });
    expect(screen.getByRole("status")).toHaveTextContent("Not verified");
    expect(screen.getByText(/nothing can be shown to be fixed/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Reasons" })).toHaveTextContent("not reproduced before the patch");
  });

  it("never calls an environment failure a failure of the patch", () => {
    show({ ...verified, verdict: "env_error", grade: "none", steps: { ...verified.steps, build: { status: "env_error" }, security_after: { status: "not_run" }, functionality: { status: "not_run" }, integrity: { status: "not_run" } } });
    expect(screen.getByRole("status")).toHaveTextContent("Could not be decided");
    expect(screen.getAllByText(/Environment problem/)).toHaveLength(1);
    expect(screen.getAllByText("This step was not reached.")).toHaveLength(3);
  });

  it("marks a test run that was not in a container", () => {
    show({ ...verified, runner: { kind: "fixture_inprocess", fixture_only: true } });
    expect(screen.getByText(/cannot count as evidence for a real record/)).toBeInTheDocument();
  });

  it("keeps the receipt, the runner and the raw detail for Developer mode", () => {
    const { unmount } = show(verified, false);
    expect(screen.queryByText(/receipt abc123/)).toBeNull();
    expect(screen.queryByText(/AssertionError/)).toBeNull();
    unmount();
    show(verified, true);
    expect(screen.getByText(/receipt abc123 · runner docker · sha256:ff/)).toBeInTheDocument();
    expect(screen.getByText(/AssertionError: injected/)).toBeInTheDocument();
  });
});
