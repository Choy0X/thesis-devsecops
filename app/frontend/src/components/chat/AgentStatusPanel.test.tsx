import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AgentDiffFile, AgentSession, AgentSessionParams } from "../../lib/api";
import type { ViewMessage } from "./MessageView";
import { AgentStatusPanel, usageTotalWords } from "./AgentStatusPanel";

const PARAMS: AgentSessionParams = { temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false, auto_approve: false };
const session = (over: Partial<AgentSession> = {}): AgentSession => ({ schema: "agent_session/1", id: "s1", project_id: "p1", mode: "manual", title: "", instructions: "",
  params: PARAMS, created_at: "2026-10-16T00:00:00.000Z", updated_at: "2026-10-16T00:00:00.000Z", status: "idle", transcript: [], task_list: [], pending_call: null, rounds: 0, ...over });
const msg = (id: string, role: ViewMessage["role"], content: string, over: Partial<ViewMessage> = {}): ViewMessage => ({ id, role, content, created_at: "2026-10-16T00:00:00.000Z", ...over });

describe("usageTotalWords", () => {
  it("says what the session has cost, and nothing when no round has reported", () => {
    expect(usageTotalWords({ completion_tokens: 3210, seconds: 72, rounds: 14, last_prompt_tokens: 5120 })).toBe("3,210 tokens written in 1 min 12 s over 14 rounds. About 44.6 tokens a second. The newest request was 5,120 tokens.");
    expect(usageTotalWords({ completion_tokens: 1, seconds: 4.5, rounds: 1 })).toBe("1 token written in 4.5 s over 1 round. About 0.2 tokens a second.");
    expect(usageTotalWords({ completion_tokens: 40, rounds: 2 })).toBe("40 tokens written over 2 rounds.");
    expect(usageTotalWords(undefined)).toBe("");
    expect(usageTotalWords({})).toBe("");
  });
});

describe("AgentStatusPanel usage", () => {
  it("shows the session's total under its own heading, even when there is nothing else to show", () => {
    render(<AgentStatusPanel session={session({ usage_total: { completion_tokens: 80, seconds: 10, rounds: 2, last_prompt_tokens: 1200 } })} mode="plan" messages={[]} diff={null} />);
    expect(screen.getByText("Usage")).toBeInTheDocument();
    expect(screen.getByText(/80 tokens written in 10 s over 2 rounds/)).toBeInTheDocument();
  });
});

describe("AgentStatusPanel", () => {
  it("renders nothing at all on a fresh session with nothing to say yet", () => {
    const { container } = render(<AgentStatusPanel session={null} mode="plan" messages={[]} diff={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("omits Changes in Plan mode even when a diff was somehow fetched, since Plan never writes anything", () => {
    const diff: AgentDiffFile[] = [{ file: "/work/shop/app.py", folder: "shop", rel: "app.py", label: "app.py", kind: "changed", sha256_before: "a", sha256_after: "b", diff: "" }];
    render(<AgentStatusPanel session={session()} mode="plan" messages={[]} diff={diff} />);
    expect(screen.queryByText("Changes")).toBeNull();
  });

  it("shows Changes with a badge per file's kind in Manual/Auto", () => {
    const diff: AgentDiffFile[] = [
      { file: "/work/shop/new.py", folder: "shop", rel: "new.py", label: "new.py", kind: "added", sha256_before: null, sha256_after: "x", diff: "" },
      { file: "/work/shop/app.py", folder: "shop", rel: "app.py", label: "app.py", kind: "changed", sha256_before: "a", sha256_after: "b", diff: "" },
      { file: "/work/shop/old.py", folder: "shop", rel: "old.py", label: "old.py", kind: "deleted", sha256_before: "c", sha256_after: null, diff: "" },
    ];
    render(<AgentStatusPanel session={session()} mode="manual" messages={[]} diff={diff} />);
    const section = screen.getByText("Changes").closest("div")!;
    expect(within(section).getByText("new.py")).toBeInTheDocument();
    expect(within(section).getByText("added")).toBeInTheDocument();
    expect(within(section).getByText("changed")).toBeInTheDocument();
    expect(within(section).getByText("deleted")).toBeInTheDocument();
  });

  it("shows the last instruction and the standing instructions under Working on", () => {
    const messages = [msg("u1", "user", "first"), msg("a1", "assistant", "ok"), msg("u2", "user", "second")];
    render(<AgentStatusPanel session={session({ instructions: "Be careful." })} mode="manual" messages={messages} diff={null} />);
    const section = screen.getByText("Working on").closest("div")!;
    expect(within(section).getByText("Instructions: Be careful.")).toBeInTheDocument();
    expect(within(section).getByText("second")).toBeInTheDocument();
    expect(within(section).queryByText("first")).toBeNull();
  });

  it("shows the task list under Subagents, done and active", () => {
    const s = session({ task_list: [{ text: "Scan the project", done: true }, { text: "Write the fix", done: false }] });
    render(<AgentStatusPanel session={s} mode="manual" messages={[]} diff={null} />);
    const list = screen.getByRole("list", { name: "Task list" });
    expect(within(list).getByText("Done: Scan the project")).toBeInTheDocument();
    expect(within(list).getByText("Active: Write the fix")).toBeInTheDocument();
  });

  const web = (steps: unknown[], content = ""): ViewMessage => ({ id: "w", role: "assistant", content, created_at: "2026-10-16T00:00:00.000Z", steps: steps as ViewMessage["steps"] });
  const searched = { kind: "tool", id: "s1", name: "websearch", args: "{}", state: "ok", summary: "", result: JSON.stringify({ query: "q", results: [
    { no: 1, title: "The guide", url: "https://docs.example.com/guide", domain: "docs.example.com", snippet: "How it works" }, { no: 2, title: "An advisory", url: "https://example.org/a", domain: "example.org", snippet: "" }] }) };

  it("lists under Sources only the pages found on the web, as numbered cards with a link out, and not the files read or commands run", () => {
    const messages = [msg("u1", "user", "go"), web([{ kind: "tool", id: "r", name: "read_file", args: "{}", state: "ok", summary: "Read app.py", result: "{}" }, searched,
      { kind: "tool", id: "b", name: "bash", args: "{}", state: "ok", summary: "Ran ls", result: "{}" }])];
    render(<AgentStatusPanel session={session()} mode="manual" messages={messages} diff={null} />);
    const list = screen.getByRole("list", { name: "Sources" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    const link = within(list).getByRole("link", { name: "The guide" });
    expect(link).toHaveAttribute("href", "https://docs.example.com/guide");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(list).getByLabelText("Source 1")).toHaveTextContent("1");
    expect(within(list).getByText("How it works")).toBeInTheDocument();
    expect(screen.queryByText("Read app.py")).toBeNull();
    expect(screen.queryByText("Ran ls")).toBeNull();
  });

  it("says which pages were read and which the answer cites", () => {
    const fetched = { kind: "tool", id: "f1", name: "webfetch", args: "{}", state: "ok", summary: "", result: JSON.stringify({ no: 2, url: "https://example.org/a", title: "An advisory, read", text: "..." }) };
    const messages = [msg("u1", "user", "go"), web([searched, fetched, { kind: "text", text: "It works like this [1]." }])];
    render(<AgentStatusPanel session={session()} mode="plan" messages={messages} diff={null} />);
    const list = screen.getByRole("list", { name: "Sources" });
    expect(within(list).getByText("docs.example.com - cited in the answer")).toBeInTheDocument();
    expect(within(list).getByText("example.org - read")).toBeInTheDocument();
  });

  it("has no Sources at all when nothing was searched, even after many tool calls", () => {
    const messages = [msg("u1", "user", "go"), web([{ kind: "tool", id: "r", name: "read_file", args: "{}", state: "ok", summary: "Read app.py", result: "{}" }])];
    render(<AgentStatusPanel session={session()} mode="manual" messages={messages} diff={null} />);
    expect(screen.queryByText("Sources")).toBeNull();
  });
});
