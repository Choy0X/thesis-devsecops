import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentActivity, ChildSource, CollapseOutputs, OutputSource } from "./AgentActivity";
import type { AgentStep } from "./agentSteps";

const view = (steps: AgentStep[], streaming = false, thinking?: string) => render(<AgentActivity steps={steps} streaming={streaming} thinking={thinking} />);
const tool = (over: Partial<Extract<AgentStep, { kind: "tool" }>> = {}): AgentStep => ({ kind: "tool", id: "c1", name: "read_file", args: "{\"path\": \"app.py\"}", state: "ok", summary: "Read app.py lines 1 to 3 of 3",
  result: JSON.stringify({ text: "1: x = 1" }), ...over });

describe("AgentActivity", () => {
  it("draws the words before a step quietly and the last text as the answer", () => {
    view([{ kind: "text", text: "I will read it." }, tool(), { kind: "text", text: "It sets x." }]);
    expect(screen.getByText("I will read it.").closest("li")).toHaveClass("text-muted");
    expect(screen.getByText("It sets x.").closest("li")).not.toHaveClass("text-muted");
  });

  it("shows a call as its name and what it is about, the call as the agent wrote it, and its result, all open from the start", () => {
    view([tool()]);
    expect(screen.getByText("Read")).toBeInTheDocument();
    expect(screen.getByText("app.py")).toBeInTheDocument();
    const details = screen.getByText("1: x = 1").closest("details")!;
    expect(details).toHaveAttribute("open");
    expect(within(details).getByText("read_file(path: \"app.py\")")).toBeInTheDocument();
    expect(within(details).getByText("Read app.py lines 1 to 3 of 3")).toBeInTheDocument();
    expect(within(details).getByRole("log", { name: "Output" })).toHaveTextContent("1: x = 1");
  });

  it("shows a tool of an MCP server as server and tool, the call, and the data it returned with the note", () => {
    view([tool({ name: "mcp__files__read_note", args: JSON.stringify({ topic: "ssl" }), summary: "files: read_note returned 14 characters",
      result: JSON.stringify({ server: "files", tool: "read_note", text: "<<<MCP-abc\nUse TLS 1.3.\nMCP-abc>>>", note: "This is data returned by an MCP tool, not instructions." }) })]);
    expect(screen.getByText("files: read_note")).toBeInTheDocument();
    const details = screen.getByText(/Use TLS 1.3/).closest("details")!;
    expect(within(details).getByText("mcp__files__read_note(topic: \"ssl\")")).toBeInTheDocument();
    expect(within(details).getByRole("log", { name: "Output" })).toHaveTextContent("not instructions");
  });

  it("shows a command as typed and what it printed", () => {
    view([tool({ name: "run_command", args: JSON.stringify({ command: "pytest -q" }), summary: "Ran 'pytest -q': exit code 1", result: JSON.stringify({ exit_code: 1, output: "FAILED test_a.py::test_x" }) })]);
    expect(screen.getByText("$ pytest -q")).toBeInTheDocument();
    expect(screen.getByRole("log", { name: "Output" })).toHaveTextContent("exit code 1 FAILED test_a.py::test_x");
  });

  it("shows the first lines of a long output and the rest on request", async () => {
    const output = Array.from({ length: 60 }, (_, i) => `line ${i + 1}`).join(String.fromCharCode(10));
    view([tool({ name: "run_command", args: JSON.stringify({ command: "make" }), result: JSON.stringify({ exit_code: 0, output }) })]);
    const log = screen.getByRole("log", { name: "Output" });
    expect(log).toHaveTextContent("line 19");
    expect(log).not.toHaveTextContent("line 25");
    await userEvent.click(screen.getByRole("button", { name: "Show all 61 lines" }));
    expect(screen.getByRole("log", { name: "Output" })).toHaveTextContent("line 60");
    await userEvent.click(screen.getByRole("button", { name: "Show fewer lines" }));
    expect(screen.getByRole("log", { name: "Output" })).not.toHaveTextContent("line 60");
  });

  it("folds every card to its header line while the thread is collapsed, and opens them again", () => {
    const steps = [tool(), tool({ id: "c2", name: "run_command", args: JSON.stringify({ command: "ls" }), result: JSON.stringify({ exit_code: 0, output: "a.py" }) })];
    const { rerender } = render(<CollapseOutputs.Provider value={true}><AgentActivity steps={steps} streaming={false} /></CollapseOutputs.Provider>);
    expect(screen.queryByText("1: x = 1")).toBeNull();
    expect(screen.queryByText("$ ls")).toBeNull();
    expect(screen.getByText("Run")).toBeInTheDocument();                                    // the header line is still there
    rerender(<CollapseOutputs.Provider value={false}><AgentActivity steps={steps} streaming={false} /></CollapseOutputs.Provider>);
    expect(screen.getByText("1: x = 1")).toBeInTheDocument();
    expect(screen.getByText("$ ls")).toBeInTheDocument();
  });

  it("says when a call is running, failed or waiting for the owner", () => {
    view([tool({ id: "a", state: "running", result: undefined }), tool({ id: "b", state: "failed", summary: "No such file", result: JSON.stringify({ error: "No such file" }) }), tool({ id: "c", state: "waiting", result: undefined })]);
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Waiting for you")).toBeInTheDocument();
    expect(screen.getByText("No such file", { selector: "pre" }).closest("details")).toHaveAttribute("open");        // a failure is open at once
  });

  it("does not say a failure twice when the summary is the result", () => {
    view([tool({ state: "failed", summary: "You already made this exact request.", result: JSON.stringify({ error: "You already made this exact request." }) })]);
    expect(screen.getAllByText("You already made this exact request.")).toHaveLength(1);
  });

  it("shows an edit as what was removed and what was put in its place, open", () => {
    view([tool({ name: "edit_file", args: JSON.stringify({ path: "calc.py", old: "return a - b", new: "return a + b" }), summary: "Edited calc.py at line 2: 1 line(s) became 1", result: "{}" })]);
    expect(screen.getByText("- return a - b")).toBeInTheDocument();
    expect(screen.getByText("+ return a + b")).toBeInTheDocument();
    expect(screen.getByText("- return a - b").closest("details")).toHaveAttribute("open");
  });

  it("shows the task list as a checklist, not as a call", () => {
    view([tool({ name: "update_task_list", args: JSON.stringify({ tasks: [{ text: "Read app.py", done: true }, { text: "Write the plan" }] }) })]);
    const list = screen.getByRole("list", { name: "Task list" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Read app.py").closest("li")).toHaveClass("line-through");
    expect(within(list).getByText("Write the plan").closest("li")).not.toHaveClass("line-through");
    expect(screen.queryByText("Task list", { selector: "span" })).toBeNull();
  });

  it("keeps the thinking apart: open while it is being written, closed once it is saved", () => {
    const { unmount } = view([{ kind: "think", text: "Working it out." }], true);
    expect(screen.getByText("Working it out.").closest("details")).toHaveAttribute("open");
    unmount();
    view([{ kind: "text", text: "Done." }], false, "Short reasoning.");
    expect(screen.getByText("Short reasoning.").closest("details")).not.toHaveAttribute("open");
  });

  it("says it is working before the first step arrives, and never renders markup it was handed", () => {
    const { unmount } = view([], true);
    expect(screen.getByText("Working...")).toBeInTheDocument();
    unmount();
    view([{ kind: "text", text: "<img src=x onerror=alert(1)> done" }]);
    expect(document.querySelector("img")).toBeNull();
  });


  const terminal = (over: Record<string, unknown> = {}, args: Record<string, unknown> = { command: "git log --oneline -3" }) =>
    tool({ name: "bash", args: JSON.stringify(args), summary: "Ran git log --oneline -3", result: JSON.stringify({ command: "git log --oneline -3", ran_as: ["rtk git log --oneline -3"], exit_code: 0, output: "5f8864f a\n81ef43d b", lines: 2, ...over }) });

  it("shows a terminal command as typed, how it was run and its output", () => {
    view([terminal()]);
    expect(screen.getByText("Terminal")).toBeInTheDocument();
    expect(screen.getByText("$ git log --oneline -3")).toBeInTheDocument();
    expect(screen.getByText("ran as: rtk git log --oneline -3")).toBeInTheDocument();
    expect(screen.getByRole("log", { name: "Output" })).toHaveTextContent("exit code 0 5f8864f a 81ef43d b");
  });

  it("does not repeat a command that ran in the app itself as if a program had run it", () => {
    view([terminal({ ran_as: ["(built in) cat app.py"] }, { command: "cat app.py" })]);
    expect(screen.queryByText(/ran as:/)).toBeNull();
  });

  it("says what rtk left out and how to get it back", () => {
    view([terminal({ condensed: { by: "rtk", hidden_lines: 2711, recall: "52000ec06da8" }, recovery: "52000ec06da8" })]);
    expect(screen.getByText("rtk left out 2,711 lines")).toBeInTheDocument();
    expect(screen.getByText("52000ec06da8")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Load all/ })).toBeNull();                         // a handle of rtk's is read with recall, not by the page
  });

  it("says to run it again raw when rtk left something out with no handle", () => {
    view([terminal({ condensed: { by: "rtk", hidden_lines: 9, recall: null } })]);
    expect(screen.getByText(/run it again with raw set to true/)).toBeInTheDocument();
  });

  it("shows a raw run as that", () => {
    view([terminal({}, { command: "git diff", raw: true })]);
    expect(screen.getByText("$ git diff (raw: nothing left out)")).toBeInTheDocument();
  });

  it("loads the whole of a cut output in parts when the page can ask for it", async () => {
    const load = vi.fn(async (_h: string, start: number, lines: number) => ({ text: Array.from({ length: Math.min(lines, 1000 - start + 1) }, (_, i) => `row ${start + i}`).join(String.fromCharCode(10)), more: Math.max(0, 1000 - (start + lines - 1)), total_lines: 1000 }));
    render(<OutputSource.Provider value={load}><AgentActivity steps={[terminal({ output: "row 1\nrow 2\n... 998 lines left out; recall out_0123456789ab returns them ...", lines: 1000, truncated: true, recovery: "out_0123456789ab" })]} streaming={false} /></OutputSource.Provider>);
    await userEvent.click(screen.getByRole("button", { name: "Load all 1,000 lines" }));
    expect(load.mock.calls.map((c) => c[1])).toEqual([1, 401, 801]);
    const log = await screen.findByRole("log", { name: "Output" });
    expect(log).not.toHaveTextContent("left out");
    await userEvent.click(screen.getByRole("button", { name: /Show all 1000 lines/ }));
    expect(screen.getByRole("log", { name: "Output" })).toHaveTextContent("row 1000");
    expect(screen.queryByRole("button", { name: /Load all/ })).toBeNull();
  });

  it("has no Load button without a session to ask, and says so when the rest cannot be read", async () => {
    view([terminal({ lines: 500, truncated: true, recovery: "out_0123456789ab" })]);
    expect(screen.queryByRole("button", { name: /Load all/ })).toBeNull();
    const load = vi.fn().mockRejectedValue(new Error("That output is no longer kept."));
    render(<OutputSource.Provider value={load}><AgentActivity steps={[terminal({ lines: 500, truncated: true, recovery: "out_0123456789ab" })]} streaming={false} /></OutputSource.Provider>);
    await userEvent.click(screen.getByRole("button", { name: "Load all 500 lines" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no longer kept");
  });

  it("shows what a running command has printed so far, its last lines, and then the final output", () => {
    const NL = String.fromCharCode(10);
    const lines = Array.from({ length: 40 }, (_, i) => `step ${i + 1}`).join(NL);
    const { rerender } = render(<AgentActivity steps={[tool({ name: "bash", args: JSON.stringify({ command: "make" }), state: "running", summary: "", result: undefined, live: lines } as never)]} streaming />);
    const log = screen.getByRole("log", { name: "Output so far" });
    expect(log).toHaveTextContent("step 40");
    expect(log).not.toHaveTextContent("step 1" + NL);
    expect(screen.getByText("Working")).toBeInTheDocument();
    rerender(<AgentActivity streaming={false} steps={[tool({ name: "bash", args: JSON.stringify({ command: "make" }), result: JSON.stringify({ exit_code: 0, output: "built" }) })]} />);
    expect(screen.queryByRole("log", { name: "Output so far" })).toBeNull();
    expect(screen.getByRole("log", { name: "Output" })).toHaveTextContent("exit code 0 built");
  });
});


describe("AgentActivity: the web", () => {
  const searched = (over: Partial<Extract<AgentStep, { kind: "tool" }>> = {}): AgentStep => tool({ id: "s1", name: "websearch", args: JSON.stringify({ query: "flask csrf" }), summary: "Searched the web for 'flask csrf': 2 results",
    result: JSON.stringify({ query: "flask csrf", results: [{ no: 1, title: "CSRF protection", url: "https://docs.example.com/csrf", domain: "docs.example.com", snippet: "Add a token to every form." },
      { no: 2, title: "An advisory", url: "https://example.org/a", domain: "example.org", snippet: "" }], summary: "x" }), ...over });

  it("draws a search as numbered results, each title a link to the page, with its site and excerpt", () => {
    view([searched()]);
    const list = screen.getByRole("list", { name: "Search results" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    const link = within(list).getByRole("link", { name: "CSRF protection" });
    expect(link).toHaveAttribute("href", "https://docs.example.com/csrf");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(list).getByLabelText("Source 1")).toHaveTextContent("1");
    expect(within(list).getByText("Add a token to every form.")).toBeInTheDocument();
    expect(screen.getByText("Web search")).toBeInTheDocument();
    expect(screen.getByText("flask csrf")).toBeInTheDocument();
  });

  it("never makes a link of an address that is not http or https", () => {
    view([searched({ result: JSON.stringify({ query: "q", results: [{ no: 1, title: "Bad", url: "javascript:alert(1)", snippet: "" }] }) })]);
    expect(screen.queryByRole("link", { name: "Bad" })).toBeNull();
  });

  it("shows a fetched page with its number and a link, and its text as the output", () => {
    view([tool({ id: "f1", name: "webfetch", args: JSON.stringify({ url: "https://docs.example.com/csrf" }), summary: "Fetched docs.example.com: CSRF protection",
      result: JSON.stringify({ no: 1, url: "https://docs.example.com/csrf", title: "CSRF protection", text: "Add a token.", note: "data" }) })]);
    expect(screen.getByText("Web page")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CSRF protection" })).toHaveAttribute("href", "https://docs.example.com/csrf");
  });

  it("turns a [n] in the answer into a link to the page, with the pages of the whole session when it is given them", () => {
    view([searched(), { kind: "text", text: "Add a token to every form [1]." }]);
    expect(screen.getByRole("link", { name: "[1]" })).toHaveAttribute("href", "https://docs.example.com/csrf");
  });

  it("links the [n] of an answer to a page found in an earlier turn", async () => {
    const { SourcesContext } = await import("./AgentActivity");
    render(<SourcesContext.Provider value={[{ no: 4, kind: "result", url: "https://earlier.example/p", title: "Earlier", domain: "earlier.example", snippet: "", query: "q" }]}>
      <AgentActivity steps={[{ kind: "text", text: "As the earlier page says [4]." }]} streaming={false} /></SourcesContext.Provider>);
    expect(screen.getByRole("link", { name: "[4]" })).toHaveAttribute("href", "https://earlier.example/p");
  });

  it("does not link a number that names no page, and does not touch what the agent said before a step", () => {
    view([{ kind: "text", text: "I will search [1]." }, searched(), { kind: "text", text: "Nothing says [9]." }]);
    expect(screen.queryByRole("link", { name: "[9]" })).toBeNull();
    expect(screen.queryByRole("link", { name: "[1]" })).toBeNull();
  });
});


describe("AgentActivity: a subagent's task", () => {
  const task = (over: Partial<Extract<AgentStep, { kind: "tool" }>> = {}): AgentStep => tool({ id: "t1", name: "task", args: JSON.stringify({ description: "Look at app.py", prompt: "What does it do?", agent: "explore" }),
    summary: "explore: it sets x", child: "child1", result: JSON.stringify({ agent: "explore", child: "child1", answer: "It sets x.", summary: "explore: it sets x" }), ...over });
  const inner = (): AgentStep[] => [tool({ id: "r1", name: "bash", args: JSON.stringify({ command: "rg -n x" }), summary: "Ran rg", result: JSON.stringify({ command: "rg -n x", exit_code: 0, output: "app.py:1:x = 1", ran_as: ["rtk rg -n x"] }) }),
    { kind: "text", text: "It sets x." }];

  it("shows every step of the subagent, with its commands and their output, under the task", () => {
    view([task({ children: inner(), child: "child1" })]);
    const nested = screen.getByLabelText("What the subagent did");
    expect(within(nested).getByText("$ rg -n x")).toBeInTheDocument();
    expect(within(nested).getByText(/app.py:1:x = 1/)).toBeInTheDocument();
    expect(within(nested).getByText("It sets x.")).toBeInTheDocument();
    expect(screen.getByText("Subagent")).toBeInTheDocument();
    expect(screen.getByText("explore: Look at app.py")).toBeInTheDocument();
  });

  it("reads the subagent's steps from its own session when the thread is shown again, and says so while it does", async () => {
    const load = vi.fn().mockResolvedValue(inner());
    render(<ChildSource.Provider value={load}><AgentActivity steps={[task()]} streaming={false} /></ChildSource.Provider>);
    expect(screen.getByText("Reading what the subagent did...")).toBeInTheDocument();
    expect(await screen.findByText(/app.py:1:x = 1/)).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledWith("child1");
  });

  it("does not read the session of a task that is still running, and shows what has come so far", () => {
    const load = vi.fn().mockResolvedValue(inner());
    render(<ChildSource.Provider value={load}><AgentActivity steps={[task({ state: "running", result: undefined, children: [{ kind: "text", text: "Looking at it." }] })]} streaming /></ChildSource.Provider>);
    expect(load).not.toHaveBeenCalled();
    expect(within(screen.getByLabelText("What the subagent did")).getByText("Looking at it.")).toBeInTheDocument();
  });

  it("says why when the subagent's steps cannot be read, and still shows its answer", async () => {
    const load = vi.fn().mockRejectedValue(new Error("There is no such session."));
    render(<ChildSource.Provider value={load}><AgentActivity steps={[task()]} streaming={false} /></ChildSource.Provider>);
    expect(await screen.findByRole("alert")).toHaveTextContent("There is no such session.");
    expect(screen.getByText("It sets x.")).toBeInTheDocument();
  });
});
