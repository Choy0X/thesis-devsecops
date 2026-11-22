import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as apiModule from "../../lib/api";
import type { AgentSession, AgentSessionParams, EngineRemediation, EngineRun, EngineTask } from "../../lib/api";
import { AgentStatusPanel } from "./AgentStatusPanel";
import { TaskPanel } from "./TaskPanel";
import { budgetWords, checkWords, remediationWords, stepGood, stopWords } from "./engineWords";

const task = (over: Partial<EngineTask> = {}): EngineTask => ({ id: "t_1", objective: "Fix the path traversal in read_public_file.", mode: "build", state: "completed", constraints: [], plan: [], observations: [],
  files_changed: [], validations: [], attempts: [], remediations: [], completion: { status: "open", reason: "" }, ...over });
const fix = (over: Partial<EngineRemediation> = {}): EngineRemediation => ({ id: "r_1", status: "verified", reason: "verified", grade: "G3", test: { path: "tests/security/test_traversal.py", sha256: "a".repeat(64) },
  steps: { apply: "passed", build: "passed", security_before: "failed", security_after: "passed", functionality: "passed", integrity: "passed" }, patch: { files: ["src/files/serve.py"], lines: 6, sha256: "b".repeat(64) },
  receipt: "c".repeat(64), ...over });
const run = (over: Partial<EngineRun> = {}): EngineRun => ({ id: "r_x", n: 1, state: "completed", stop_reason: "answered", budget: { rounds: 40, seconds: 1800, tool_calls: 150 },
  spent: { rounds: 7, tool_calls: 12, tokens: 9000, active_seconds: 65 }, started_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:01:00Z", ...over });

describe("the words", () => {
  it("says why a run stopped in a sentence, also for a reason it does not know", () => {
    expect(stopWords("no_progress")).toBe("Stopped: the same failure kept repeating");
    expect(stopWords("error:session_too_large")).toBe("Stopped by an error (session too large)");
    expect(stopWords("something_new")).toBe("something new");
    expect(stopWords(null)).toBe("");
  });

  it("says what a run has used of what it was given, only for the limits it has", () => {
    expect(budgetWords(run())).toBe("7 of 40 rounds, 12 of 150 tool calls, 1 min 5 s of 30 min 0 s");
    expect(budgetWords(run({ budget: { rounds: 20 } }))).toBe("7 of 20 rounds");
    expect(budgetWords(null)).toBe("");
  });

  it("reads a failing step as good only where failing is the right result", () => {
    expect(stepGood("security_before", "failed")).toBe(true);
    expect(stepGood("security_before", "passed")).toBe(false);
    expect(stepGood("security_after", "passed")).toBe(true);
    expect(stepGood("build", undefined)).toBe(false);
  });

  it("gives every reason a fix can be unverified a sentence", () => {
    for (const reason of ["no_code_change", "test_not_new", "test_does_not_use_project", "test_passes_before_patch", "test_not_runnable", "fix_does_not_pass_the_test", "patch_does_not_apply", "build_failed", "project_tests_failed", "integrity", "unsupported", "env_error"]) {
      expect(remediationWords({ status: "unverified", reason })).not.toBe(reason.replace(/_/g, " "));
    }
    expect(remediationWords({ status: "verified", reason: "verified" })).toMatch(/failed on the original code and passes on the fix/);
  });

  it("describes a check by who ran it and how it ended", () => {
    expect(checkWords({ id: "v", call: "c", cmd: "pytest -q", kind: "test", class: "passed" })).toBe("Test run passed");
    expect(checkWords({ id: "v", call: "c", cmd: "pytest -q", kind: "test", class: "validation_failed", readable: "AssertionError: 3 != 4", by: "engine" })).toBe("Test run failed (run by the engine): AssertionError: 3 != 4");
    expect(checkWords({ id: "v", call: "c", cmd: "verify_fix tests/t.py", kind: "test", class: "validation_env", env_reason: "no daemon" })).toBe("Fix verification could not run here: no daemon");
  });
});

describe("TaskPanel", () => {
  it("shows the state, the objective and, from the run, why it stopped and what it used", () => {
    render(<TaskPanel task={task({ completion: { status: "complete", reason: "" } })} run={run({ stop_reason: "max_rounds", state: "exhausted" })} />);
    expect(screen.getByText("Complete")).toBeInTheDocument();
    expect(screen.getByText("Ran out of budget")).toBeInTheDocument();
    expect(screen.getByText("Ran out of rounds.")).toBeInTheDocument();
    expect(screen.getByText(/Used 7 of 40 rounds/)).toBeInTheDocument();
    expect(screen.getByText("Fix the path traversal in read_public_file.")).toBeInTheDocument();
  });

  it("leaves out the sections that have nothing to say", () => {
    render(<TaskPanel task={task()} />);
    for (const title of ["Plan", "Checks", "Fix checks", "Files changed", "Ruled out by you", "What the engine noticed"]) expect(screen.queryByText(title)).toBeNull();
    expect(screen.getByText("In progress")).toBeInTheDocument();
  });

  it("lists the plan with the state of each step in words and the protected paths of a constraint", () => {
    render(<TaskPanel task={task({ constraints: [{ text: "Do not change the tests.", protect: ["tests/*"] }], plan: [
      { id: "s1", text: "Read the file", state: "done" }, { id: "s2", text: "Write the check", state: "active" }, { id: "s3", text: "Skip this", state: "skipped" }] })} />);
    const steps = within(screen.getByLabelText("Plan steps")).getAllByRole("listitem");
    expect(steps).toHaveLength(3);
    expect(steps[0]).toHaveTextContent("Done: Read the file");
    expect(steps[1]).toHaveTextContent("Doing: Write the check");
    expect(screen.getByText("Do not change the tests.")).toBeInTheDocument();
    expect(screen.getByText(/Not written: tests\/\*/)).toBeInTheDocument();
  });

  it("shows the engine's own scope rule apart from what the owner ruled out, and never calls it the owner's", () => {
    const rule = { text: "A fix changes the code that has the fault, not the project's documents.", protect: ["README*", "*/README*", "CHANGELOG*", "*/CHANGELOG*", "*.md", "*.rst", "docs/*", "*/docs/*"], implicit: true };
    render(<TaskPanel task={task({ constraints: [rule] })} />);
    expect(screen.queryByText("Ruled out by you")).toBeNull();
    expect(screen.getByText("Left alone by the engine")).toBeInTheDocument();
    expect(screen.getByText(/Not written: README\*, \*\/README\*, CHANGELOG\*, \*\/CHANGELOG\*, \*\.md, \*\.rst and 2 more/)).toBeInTheDocument();
  });

  it("lists the owner's rules and the engine's rule in their own sections when a task has both", () => {
    render(<TaskPanel task={task({ constraints: [{ text: "Do not change the tests.", protect: ["tests/*"] }, { text: "A fix changes code.", protect: ["README*"], implicit: true }] })} />);
    expect(within(screen.getByText("Ruled out by you").closest("section") ?? document.body).getByText("Do not change the tests.")).toBeInTheDocument();
    expect(screen.getByText("Left alone by the engine")).toBeInTheDocument();
    expect(screen.getByText("A fix changes code.")).toBeInTheDocument();
  });

  it("marks a verified fix in words, with the grade, the test, the files and the receipt", () => {
    render(<TaskPanel task={task({ remediations: [fix()] })} />);
    const card = screen.getByLabelText("Fix check: verified");
    expect(within(card).getByText(/Verified \(grade G3\)/)).toBeInTheDocument();
    expect(within(card).getByText(/Test: tests\/security\/test_traversal.py/)).toBeInTheDocument();
    expect(within(card).getByText(/Changed to fix it: src\/files\/serve.py/)).toBeInTheDocument();
    expect(within(card).getByText(/Receipt cccccccccccc/)).toBeInTheDocument();
    const steps = within(card).getByLabelText("Steps of the check");
    expect(within(steps).getByText("Test fails before: yes")).toBeInTheDocument();
    expect(within(steps).getAllByRole("listitem")).toHaveLength(6);
  });

  it("lists the review notes of a fix as advice", () => {
    render(<TaskPanel task={task({ remediations: [fix({ review: ["adds an md5 or sha1 digest", "is a large patch (250 added lines)"] })] })} />);
    const notes = within(screen.getByLabelText("Review notes")).getAllByRole("listitem");
    expect(notes).toHaveLength(2);
    expect(notes[0]).toHaveTextContent("The patch adds an md5 or sha1 digest");
    expect(screen.getByText(/advice, not part of the verdict/)).toBeInTheDocument();
  });

  it("says so when files were changed after a fix was verified", () => {
    render(<TaskPanel task={task({ remediations: [fix({ stale: true })] })} />);
    const card = screen.getByLabelText("Fix check: verified");
    expect(within(card).getByText(/Verified \(grade G3\), then the files changed/)).toBeInTheDocument();
    expect(within(card).getByText(/Check again before relying on it/)).toBeInTheDocument();
  });

  it("marks an unverified fix with its reason, and a check that could not run differently from one that failed", () => {
    render(<TaskPanel task={task({ remediations: [
      fix({ id: "r_2", status: "unverified", reason: "test_passes_before_patch", grade: "none", steps: { security_before: "passed" }, note: "Ran 1 test OK" }),
      fix({ id: "r_3", status: "unverified", reason: "env_error", grade: "none", steps: {}, note: "docker is not usable", finding: { description: "path traversal", source: "claimed" } })] })} />);
    const cards = screen.getAllByLabelText("Fix check: unverified");
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent("The check could not run here, so nothing was verified.");
    expect(cards[0]).toHaveTextContent("the model's own description");
    expect(cards[1]).toHaveTextContent("The test already passes on the original code, so it does not show the problem.");
    expect(cards[1]).toHaveTextContent("Test fails before: no");
    expect(cards[1]).toHaveTextContent("Ran 1 test OK");
  });

  it("lists the checks with their outcome and the files changed", () => {
    render(<TaskPanel task={task({ files_changed: [{ path: "src/a.py", sha_before: "x", sha_after: "y" }], validations: [
      { id: "v1", call: "c1", cmd: "pytest -q", kind: "test", class: "validation_failed", readable: "FAILED tests/test_a.py::test_x" },
      { id: "v2", call: "c2", cmd: "pytest -q", kind: "test", class: "passed" }] })} />);
    const checks = within(screen.getByLabelText("Checks")).getAllByRole("listitem");
    expect(checks[0]).toHaveTextContent("fail");
    expect(checks[0]).toHaveTextContent("Test run failed: FAILED tests/test_a.py::test_x");
    expect(checks[1]).toHaveTextContent("pass");
    expect(within(screen.getByLabelText("Files changed")).getByText("src/a.py")).toBeInTheDocument();
  });
});

const PARAMS: AgentSessionParams = { temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false, auto_approve: false };
const session = (over: Partial<AgentSession> = {}): AgentSession => ({ schema: "agent_session/1", id: "s1", project_id: "p1", mode: "auto", title: "", instructions: "", params: PARAMS,
  created_at: "2026-10-18T00:00:00.000Z", updated_at: "2026-10-18T00:00:00.000Z", status: "idle", transcript: [], task_list: [], pending_call: null, rounds: 0, ...over });

describe("AgentStatusPanel tabs", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows no tabs for a session without an engine task, and nothing at all on a fresh one", () => {
    const { container } = render(<AgentStatusPanel session={session()} mode="auto" messages={[]} diff={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("offers the Task tab when the session has a task, and shows it with the newest run", async () => {
    const runs = vi.spyOn(apiModule.api, "agentRuns").mockResolvedValue({ runs: [run({ stop_reason: "no_progress", state: "exhausted" })] });
    render(<AgentStatusPanel session={session({ task: task({ completion: { status: "unverified", reason: "files were changed in this run and no check passed on them" } }) })} mode="auto" messages={[]} diff={null} />);
    expect(screen.getByRole("tab", { name: "Session" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByLabelText("Task")).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Task" }));
    expect(screen.getByRole("tab", { name: "Task" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Unverified")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Stopped: the same failure kept repeating.")).toBeInTheDocument());
    expect(runs).toHaveBeenCalledWith("p1", "s1");
    await userEvent.click(screen.getByRole("tab", { name: "Session" }));
    expect(screen.queryByLabelText("Task")).toBeNull();
  });

  it("still shows the task when the run cannot be read", async () => {
    vi.spyOn(apiModule.api, "agentRuns").mockRejectedValue(new Error("down"));
    render(<AgentStatusPanel session={session({ task: task() })} mode="auto" messages={[]} diff={null} />);
    await userEvent.click(screen.getByRole("tab", { name: "Task" }));
    expect(await screen.findByText("In progress")).toBeInTheDocument();
    expect(screen.getByText("Fix the path traversal in read_public_file.")).toBeInTheDocument();
  });
});
