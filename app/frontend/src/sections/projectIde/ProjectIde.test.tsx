import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../../components/hud/AlertDialog";
import { resetWorkspaceState } from "../../lib/workspaceState";
import { resetFileDrafts } from "../../lib/fileDrafts";
import { ProjectIde } from "./ProjectIde";
import { get, write, type ChatDetail } from "./client";

vi.mock("./Editor", () => ({ Editor: ({ text, onChange }: { text: string; onChange: (value: string) => void }) => <button aria-label="Edit test file" onClick={() => onChange(`${text} edited`)}>{text}</button> }));
vi.mock("../../components/chat/Markdown", () => ({ Markdown: ({ text }: { text: string }) => <p>{text}</p> }));
vi.mock("./client", async original => {
  const actual = await original<typeof import("./client")>();
  return { ...actual, get: vi.fn(), write: vi.fn() };
});
let after = "after-v1";
beforeEach(() => {
  localStorage.clear(); resetWorkspaceState(); resetFileDrafts(); vi.clearAllMocks(); after = "after-v1";
  vi.mocked(get).mockImplementation(async path => {
    if (path === "/api/ide/projects") return { projects: [{ id: "p", name: "Test repository", settings: { instructions: "Read tests", memory: ["Keep the API stable"] } }] } as never;
    if (path === "/api/ide/models") return { models: [{ id: "qwen", name: "Qwen2.5-Coder 7B Instruct", available: true }] } as never;
    if (path.endsWith("/runtime-status")) return { chat_id: "chat", model_id: "qwen", ready: true, state: "ready" } as never;
    if (path.endsWith("/chats")) return { chats: [{ id: "chat", title: "Review project" }, { id: "chat2", title: "Another chat" }] } as never;
    if (path.endsWith("/sources")) return { sources: [{ id: "primary", kind: "local", label: "Project folder", primary: true }] } as never;
    if (path.endsWith("/plan")) return { items: [], text: "", status: "empty" } as never;
    if (path.endsWith("/subagents")) return { subagents: [], status: "empty" } as never;
    if (path === "/api/chats/chat") return { chat: { id: "chat", title: "Review project", model: { id: "qwen", name: "Qwen2.5-Coder 7B Instruct" } }, runs: [], messages: [], permissions: [], questions: [] } as never;
    if (path === "/api/chats/chat2") return { chat: { id: "chat2", title: "Another chat" }, runs: [], messages: [], permissions: [], questions: [] } as never;
    if (path.includes("/events?")) return { events: [] } as never;
    if (path.endsWith("/files")) return { files: [{ path: "app.ts", bytes: 10 }], workspace: "D:\workspaces\chat", exclusions: [] } as never;
    if (path.includes("/file?")) return { text: "saved source", version: "saved-v1" } as never;
    if (path.endsWith("/changes")) return { changes: [{ path: "app.ts", status: "modified", before: "base", after, diff: "-before\n+after" }] } as never;
    if (path.endsWith("/checkout")) return { available: true, branch: "main", revision: "12345678", files: [], truncated: false } as never;
    if (path.endsWith("/patches")) return { candidates: [] } as never;
    if (path.endsWith("/dependencies")) return { packages: [{ id: "npm:demo", name: "demo", installed: ["1.0.0"], ecosystem: "npm", status: "locked" }] } as never;
    if (path.endsWith("/verification")) return { receipts: [] } as never;
    if (path.endsWith("/findings")) return { findings: [], coverage: [] } as never;
    throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(write).mockImplementation(async path => path.endsWith("/runtime/prepare") ? { chat_id: "chat", model_id: "qwen", ready: true, state: "ready" } as never : path.endsWith("/scans") ? { id: "scan", state: "completed" } as never : path === "/api/ide/projects/p" ? { id: "p", name: "Test repository", settings: { instructions: "Read tests", memory: ["First decision", "Second decision"] } } as never : {} as never);
});
function NavigationFixture() {
  const navigate = useNavigate(); const location = useLocation();
  return <><button onClick={() => navigate("/projects?project=p&chat=chat2")}>External chat link</button><button onClick={() => navigate("/settings?tab=runtime")}>Other page</button><button onClick={() => navigate(-1)}>Go back</button><button onClick={() => navigate(1)}>Go forward</button><output aria-label="Current route">{location.pathname}{location.search}</output><ProjectIde /></>;
}
function show(path = "/projects?project=p&chat=chat") { return render(<MemoryRouter initialEntries={[path]}><ConfirmProvider><NavigationFixture /></ConfirmProvider></MemoryRouter>); }
async function openWorkspace(name: string) {
  fireEvent.click(await screen.findByRole("button", { name: "Workspace" }));
  fireEvent.click(await within(await screen.findByRole("region", { name: "Workspace details" })).findByRole("button", { name: new RegExp("^" + name) }));
}
afterEach(() => vi.unstubAllGlobals());

it("loads another project's history while a pin is saving", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  let finishPin!: () => void;
  let finishHistory!: () => void;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/projects") return { projects: [{ id: "p", name: "Test repository" }, { id: "p2", name: "Second repository" }] } as never;
    if (path === "/api/ide/projects/p2/chats") return new Promise(resolve => { finishHistory = () => resolve({ chats: [] } as never); });
    return original(path, signal);
  });
  vi.mocked(write).mockImplementation(async () => new Promise(resolve => {
    finishPin = () => resolve({ id: "chat2", title: "Another chat", pinned: true } as never);
  }));
  show();
  fireEvent.click(await screen.findByRole("button", { name: "Resume conversation" }));
  fireEvent.click(await screen.findByRole("button", { name: "Pin Another chat" }));
  expect(finishPin).toBeTypeOf("function");
  fireEvent.click(screen.getByRole("button", { name: "Show project navigation" }));
  fireEvent.click(screen.getByRole("button", { name: "Second repository" }));
  await waitFor(() => expect(finishHistory).toBeTypeOf("function"));
  await act(async () => finishPin());
  await act(async () => finishHistory());
  expect(await screen.findByRole("heading", { name: "New conversation" })).toBeVisible();
});

class TestEventSource extends EventTarget {
  close = vi.fn();
  constructor(readonly url: string) { super(); }
  snapshot(value: ChatDetail) { this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(value) })); }
}

function mockChatStreams() {
  const streams: TestEventSource[] = [];
  vi.stubGlobal("EventSource", vi.fn(function (url: string) {
    const stream = new TestEventSource(url);
    streams.push(stream);
    return stream;
  }));
  return streams;
}

it("renders live assistant and tool updates without letting an older detail request replace them", async () => {
  const streams = mockChatStreams();
  const original = vi.mocked(get).getMockImplementation()!;
  const saved = await original("/api/chats/chat") as ChatDetail;
  let finish!: (value: ChatDetail) => void;
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/chats/chat" ? await new Promise<ChatDetail>(resolve => { finish = resolve; }) as never : original(path, signal));
  show();
  await waitFor(() => {
    expect(finish).toBeTypeOf("function");
    expect(streams[0]?.url).toBe("/api/chats/chat/stream");
  });
  const runs = [{ id: "live-run", kind: "turn", state: "running" }];
  const info = { id: "assistant", role: "assistant" };
  act(() => streams[0].snapshot({ ...saved, runs, messages: [{ info, parts: [{ type: "text", text: "Reading" }] }] }));
  expect(await screen.findByText("Reading", { exact: true })).toBeVisible();
  const composer = screen.getByRole("textbox", { name: "Message agent" });
  fireEvent.change(composer, { target: { value: "A follow-up draft" } });
  act(() => streams[0].snapshot({
    ...saved, runs: [{ ...runs[0], state: "waiting_approval" }], permissions: [{ id: "permit", permission: "run tests" }],
    messages: [{ info, parts: [{ type: "text", text: "Reading project files" }, { type: "tool", tool: "read", state: { status: "running" } }] }],
  }));
  expect(screen.getByText("Reading project files")).toBeVisible();
  expect(screen.getByText("Read file")).toHaveTextContent("running");
  expect(screen.getByRole("button", { name: "Approve once" })).toBeEnabled();
  await act(async () => finish(saved));
  expect(screen.getByText("Reading project files")).toBeVisible();
  act(() => streams[0].snapshot({ ...saved, runs: [{ ...runs[0], state: "completed" }], messages: [{ info, parts: [
    { type: "text", text: "Project review complete" }, { type: "tool", tool: "read", state: { status: "completed" } },
  ] }] }));
  expect(screen.getByText("Project review complete")).toBeVisible();
  expect(screen.getByText("Read file")).toHaveTextContent("completed");
  expect(screen.queryByRole("button", { name: "Approve once" })).not.toBeInTheDocument();
  expect(composer).toHaveValue("A follow-up draft");
});

it("polls after a stream error, accepts reconnect snapshots, and closes old streams", async () => {
  const streams = mockChatStreams();
  const original = vi.mocked(get).getMockImplementation()!;
  const saved = await original("/api/chats/chat") as ChatDetail;
  const info = { id: "assistant", role: "assistant" };
  const snapshot = (text: string): ChatDetail => ({ ...saved, messages: [{ info, parts: [{ type: "text", text }] }] });
  const view = show();
  await screen.findByRole("heading", { name: "Review project" });
  const first = streams[0];
  act(() => first.snapshot({ ...snapshot("Live text"), runs: [{ id: "scan", state: "running", kind: "scan", payload: { scope: "dependencies", dependency_mode: "versions" } }] }));
  expect(screen.getByText("Live text")).toBeVisible();
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/chats/chat") return snapshot("Text from fallback polling") as never;
    if (path === "/api/chats/chat/events?after=0") return { events: [{ sequence: 1, type: "scan.progress", payload: { phase: "versions", completed: 2, total: 3 } }] } as never;
    return original(path, signal);
  });
  expect(await screen.findByText("2 of 3 packages checked", {}, { timeout: 3500 })).toBeVisible();
  expect(screen.getByText("Live text")).toBeVisible();
  act(() => first.dispatchEvent(new Event("error")));
  expect(await screen.findByText("Text from fallback polling", {}, { timeout: 3500 })).toBeVisible();
  act(() => first.snapshot(snapshot("Live after reconnect")));
  expect(screen.getByText("Live after reconnect")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "External chat link", hidden: true }));
  await screen.findByRole("heading", { name: "Another chat" });
  expect(first.close).toHaveBeenCalledOnce();
  expect(streams[1]?.url).toBe("/api/chats/chat2/stream");
  act(() => first.snapshot(snapshot("Late traffic from the old chat")));
  expect(screen.queryByText("Late traffic from the old chat")).not.toBeInTheDocument();
  view.unmount();
  expect(streams[1].close).toHaveBeenCalledOnce();
}, 8000);

function showEmptyProject() {
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/ide/projects/p/chats" ? { chats: [] } as never : original(path, signal));
  return show("/projects?project=p&view=terminal");
}

function enterCommand(input: HTMLElement, command: string) {
  fireEvent.change(input, { target: { value: command } });
  fireEvent.keyDown(input, { key: "Enter" });
}

it("lists projects and their active runs without listing conversations in the sidebar", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/projects") return { projects: [{ id: "p", name: "Test repository" }, { id: "payments", name: "Payments service", has_active_run: true }] } as never;
    if (path.endsWith("/chats")) return { chats: [{ id: "chat", title: "Review project" }, { id: "second", title: "Second task" }, { id: "old", title: "Old task" }] } as never;
    return original(path, signal);
  });
  show();
  fireEvent.click(screen.getByRole("button", { name: "Show project navigation" }));
  const payments = await screen.findByRole("button", { name: "Payments service" });
  const navigation = screen.getByRole("navigation");
  expect(within(navigation).getAllByRole("button")).toHaveLength(3);
  expect(within(navigation).queryByText("Review project")).not.toBeInTheDocument();
  expect(within(navigation).getByRole("button", { name: "Test repository" })).toHaveAttribute("aria-current", "true");
  expect(payments).toHaveAccessibleDescription("Active run");
});

it("opens a project's last conversation when selecting it", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/projects") return { projects: [{ id: "p", name: "Test repository" }, { id: "payments", name: "Payments service" }] } as never;
    if (path === "/api/ide/projects/payments/chats") return { chats: [{ id: "newest", title: "Review invoices" }, { id: "payment-chat", title: "Review checkout" }] } as never;
    if (path === "/api/chats/payment-chat") return { chat: { id: "payment-chat", title: "Review checkout" }, runs: [], messages: [], permissions: [], questions: [] } as never;
    return original(path, signal);
  });
  localStorage.setItem("project-last-chat.payments", "payment-chat");
  show();
  fireEvent.click(screen.getByRole("button", { name: "Show project navigation" }));
  fireEvent.click(await screen.findByRole("button", { name: "Payments service" }));
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent(/project=payments.*chat=payment-chat/));
  expect(await screen.findByRole("heading", { name: "Review checkout" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Go back" }));
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("project=p&chat=chat"));
});

it("opens an empty terminal without preparing a model or creating a chat", async () => {
  showEmptyProject();
  expect(await screen.findByRole("textbox", { name: "Message agent" })).toHaveValue("");
  expect(write).not.toHaveBeenCalled();
});

it("can reselect an Ollama tag whose installed digest changed", async () => {
  const before = { id: "qwen", tag: "qwen:latest", name: "Qwen", digest: "old", kind: "local" };
  const after = { ...before, digest: "updated" };
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/models") return { models: [after] } as never;
    if (path === "/api/chats/chat") return { chat: { id: "chat", title: "Review project", model: before }, runs: [], messages: [], permissions: [], questions: [] } as never;
    return original(path, signal);
  });
  show("/projects?project=p&chat=chat&view=detect");
  await screen.findByRole("heading", { name: "Review project" });
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Agent model" }), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: "Qwen / qwen:latest" }));
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/ide/models/prepare", { model: after, context: 8192 }));
});

it("creates a conversation on the first message after model and agent preparation", async () => {
  const model = { id: "qwen", name: "Qwen2.5-Coder 7B Instruct" };
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/chats/new" ? { chat: { id: "new", title: "Review the route", model }, runs: [], messages: [], permissions: [], questions: [] } as never : original(path, signal));
  vi.mocked(write).mockImplementation(async path => {
    if (path === "/api/ide/models/prepare") return { id: "prepared", state: "ready", model } as never;
    if (path === "/api/ide/projects/p/chats") return { id: "new", title: "New chat", model } as never;
    if (path === "/api/chats/new/runtime/prepare") return { chat_id: "new", model_id: "qwen", ready: true, state: "ready" } as never;
    return {} as never;
  });
  showEmptyProject();
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(composer, { target: { value: "Review the route" } });
  fireEvent.keyDown(composer, { key: "Enter" });
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent(/project=p.*chat=new/));
  expect(vi.mocked(write).mock.calls.slice(0, 4).map(([path]) => path)).toEqual([
    "/api/ide/models/prepare", "/api/ide/projects/p/chats", "/api/chats/new/runtime/prepare", "/api/chats/new/turns",
  ]);
  expect(write).toHaveBeenCalledWith("/api/chats/new/turns", expect.objectContaining({ text: "Review the route", mode: "build" }));
  expect(await screen.findByRole("textbox", { name: "Message agent" })).toHaveValue("");
});

it("resumes another conversation while leaving the active run intact", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/chats/chat") return { chat: { id: "chat", title: "Review project" }, runs: [{ id: "run", state: "running" }], messages: [], permissions: [], questions: [] } as never;
    return original(path, signal);
  });
  show();
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  expect(screen.getByRole("button", { name: "Stop active run" })).toBeInTheDocument();
  enterCommand(composer, "/resume another");
  const filter = await screen.findByRole("combobox", { name: "Filter conversations" });
  expect(filter).toHaveValue("another");
  fireEvent.keyDown(filter, { key: "Enter" });
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("project=p&chat=chat2"));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message agent" })).toHaveFocus());
  expect(write).not.toHaveBeenCalled();
});

it("opens a new terminal in the current project without creating a conversation", async () => {
  show();
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  enterCommand(composer, "/new");
  await waitFor(() => expect(screen.getByLabelText("Current route")).not.toHaveTextContent("chat="));
  expect(screen.getByLabelText("Current route")).toHaveTextContent("project=p");
  expect(await screen.findByRole("textbox", { name: "Message agent" })).toHaveValue("");
  expect(write).not.toHaveBeenCalled();
});

it("resumes a cancelled draft with its configured context and permits another explicit memory retry", async () => {
  const configured = { id: "qwen-short", name: "Qwen, shorter context", context: 4096 };
  const shorter = { id: "qwen-minimum", name: "Qwen, minimum context", context: 2048 };
  const original = vi.mocked(get).getMockImplementation()!;
  let created = false, requestAccepted = false, boots = 0, prepares = 0;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/projects/p/chats") return { chats: created ? [{ id: "provisional", title: "New chat", model: configured }] : [] } as never;
    if (path === "/api/chats/provisional" && requestAccepted) return { chat: { id: "provisional", title: "Check the handler", model: shorter }, runs: [{ id: "run", state: "queued" }], messages: [], permissions: [], questions: [] } as never;
    return original(path, signal);
  });
  vi.mocked(write).mockImplementation(async path => {
    if (path === "/api/ide/models/prepare") {
      prepares += 1;
      if (prepares === 2) return { id: "memory-check", state: "failed", error: { code: "does_not_fit", message: "Free memory or shorten the context" } } as never;
      return { id: "loaded", state: "ready", model: prepares === 1 ? configured : shorter } as never;
    }
    if (path === "/api/ide/projects/p/chats") { created = true; return { id: "provisional", title: "New chat", model: configured } as never; }
    if (path === "/api/chats/provisional/model") return { id: "provisional", title: "New chat", model: shorter } as never;
    if (path === "/api/chats/provisional/runtime/prepare") return { chat_id: "provisional", model_id: ++boots > 1 ? shorter.id : configured.id, ready: boots > 1, state: boots > 1 ? "ready" : "failed", error: { message: "Agent unavailable" } } as never;
    if (path.endsWith("/turns")) { requestAccepted = true; return { id: "run", state: "queued" } as never; }
    throw new Error(`Unexpected write ${path}`);
  });

  const view = show("/projects?project=p&view=terminal");
  enterCommand(await screen.findByRole("textbox", { name: "Message agent" }), "Check the handler");
  expect(await screen.findByText("Agent unavailable")).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  await waitFor(() => expect(composer).toHaveValue("Check the handler"));
  view.unmount();

  show("/projects?project=p&view=terminal");
  const resumedDraft = await screen.findByRole("textbox", { name: "Message agent" });
  expect(resumedDraft).toHaveValue("Check the handler");
  expect(screen.getByLabelText("Current route")).not.toHaveTextContent("chat=");
  fireEvent.keyDown(resumedDraft, { key: "Enter" });
  fireEvent.click(await screen.findByRole("button", { name: "Retry with 2,048 tokens" }));
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("chat=provisional"));
  expect(vi.mocked(write).mock.calls.filter(([path]) => path.endsWith("/chats"))).toHaveLength(1);
  expect(write).toHaveBeenLastCalledWith("/api/chats/provisional/turns", expect.objectContaining({ text: "Check the handler" }));
  expect(write).toHaveBeenCalledWith("/api/ide/models/prepare", { model: configured, context: 4096 });
  expect(write).toHaveBeenCalledWith("/api/chats/provisional/model", { model: shorter, preparation_id: "loaded", expected_model_id: configured.id });
});

it("reuses the first submission key if the server accepted a turn before its response was lost", async () => {
  const model = { id: "qwen", name: "Qwen", context: 8192 };
  const original = vi.mocked(get).getMockImplementation()!;
  const accepted = new Set<string>();
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/chats/retry" ? { chat: { id: "retry", title: "Check input validation", model }, runs: [{ id: "run", state: "queued" }], messages: [], permissions: [], questions: [] } as never : original(path, signal));
  vi.mocked(write).mockImplementation(async (path, body) => {
    if (path === "/api/ide/models/prepare") return { id: "loaded", state: "ready", model } as never;
    if (path === "/api/ide/projects/p/chats") return { id: "retry", title: "New chat", model } as never;
    if (path === "/api/chats/retry/runtime/prepare") return { chat_id: "retry", model_id: model.id, ready: true, state: "ready" } as never;
    if (path === "/api/chats/retry/turns") {
      const key = (body as { request_key: string }).request_key;
      if (!accepted.has(key)) { accepted.add(key); throw new Error("Connection closed before receiving the response"); }
      return { id: "run", state: "queued" } as never;
    }
    throw new Error(`Unexpected write ${path}`);
  });

  showEmptyProject();
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  enterCommand(composer, "Check input validation");
  expect(await screen.findByText("Connection closed before receiving the response")).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
  fireEvent.keyDown(composer, { key: "Enter" });
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("chat=retry"));
  expect(accepted.size).toBe(1);
  expect(vi.mocked(write).mock.calls.filter(([path]) => path.endsWith("/chats"))).toHaveLength(1);
});

it("opens archived history only by explicit selection and keeps it read-only", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  const archive = { id: "archived", title: "Older review", archived: true, model: { id: "qwen", name: "Qwen" } };
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/projects/p/chats") return { chats: [archive, { id: "chat2", title: "Another chat" }] } as never;
    if (path === "/api/chats/archived") return { chat: archive, runs: [], messages: [], permissions: [], questions: [] } as never;
    return original(path, signal);
  });
  localStorage.setItem("project-last-chat.p", "archived");
  show("/projects?project=p&view=terminal");
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  expect(screen.getByLabelText("Current route")).toHaveTextContent("chat=chat2");
  enterCommand(composer, "/resume older");
  const filter = await screen.findByRole("combobox", { name: "Filter conversations" });
  fireEvent.keyDown(filter, { key: "Enter" });
  expect(await screen.findByText("Archived conversation. History is read-only.")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Agent model" })).toBeDisabled();
  expect(get).not.toHaveBeenCalledWith("/api/chats/archived/runtime-status", expect.anything());
  expect(get).not.toHaveBeenCalledWith("/api/chats/archived/changes", expect.anything());
  enterCommand(screen.getByRole("textbox", { name: "Message agent" }), "Do not send");
  expect(write).not.toHaveBeenCalled();
});

it("opens project creation in a dialog with no editable source path", async () => {
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.click(screen.getByRole("button", { name: "Show project navigation" }));
  fireEvent.click(screen.getByRole("button", { name: "Add project" }));
  const dialog = screen.getByRole("dialog", { name: "Create project" });
  expect(within(dialog).getAllByRole("textbox")).toHaveLength(1);
  expect(within(dialog).getByRole("textbox", { name: "Project name" })).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Browse folders" })).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(write).not.toHaveBeenCalled();
});

it("opens the first server-ordered conversation when the saved conversation is missing", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/projects/p/chats") return { chats: [{ id: "chat2", title: "Another chat" }, { id: "chat", title: "Review project" }] } as never;
    return original(path, signal);
  });
  localStorage.setItem("project-last-chat.p", "missing");
  show("/projects?project=p");
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent(/project=p.*chat=chat2/));
  expect(localStorage.getItem("project-last-chat.p")).toBe("chat2");
});
it("does not scan dependency source until packages are explicitly selected", async () => {
  show();
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(composer, { target: { value: "/scan dependencies source" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(await screen.findByText("Select packages for source review")).toBeInTheDocument();
  expect(write).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Review selected packages" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "Review source for demo" }));
  fireEvent.click(screen.getByRole("button", { name: "Review 1 packages" }));
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/scans", expect.objectContaining({ scope: "dependencies", dependency_mode: "source", selected_package_ids: ["npm:demo"] })));
});
it("integrates only hashes marked reviewed after showing the real diff", async () => {
  show();
  await openWorkspace("Changes");
  fireEvent.click(screen.getByRole("button", { name: /Agent workspace/ }));
  const integrate = await screen.findByRole("button", { name: "Integrate reviewed files" });
  expect(integrate).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: /modified app.ts/ }));
  const diff = screen.getByRole("region", { name: "Unified diff" });
  expect(diff).toHaveTextContent("-before");
  expect(diff).toHaveTextContent("+after");
  fireEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
  fireEvent.click(integrate);
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/integrations", { paths: ["app.ts"], review: { "app.ts": "after-v1" } }));
});
it("inherits the app theme without a separate IDE palette switch", async () => {
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  expect(screen.queryByRole("button", { name: /IDE theme/ })).not.toBeInTheDocument();
  expect(document.querySelector(".project-ide")).not.toHaveAttribute("data-ide-theme");
});
it("loads and saves curated memory as separate entries", async () => {
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.click(await screen.findByRole("button", { name: "Project settings" }));
  fireEvent.click(screen.getByRole("button", { name: "Agent context" }));
  const memory = screen.getByRole("textbox", { name: "Memory entry 1" });
  expect(memory).toHaveValue("Keep the API stable");
  fireEvent.change(memory, { target: { value: "First decision" } });
  fireEvent.click(screen.getByRole("button", { name: "Add memory entry" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Memory entry 2" }), { target: { value: "Second decision" } });
  fireEvent.click(screen.getByRole("button", { name: "General" }));
  fireEvent.click(screen.getByRole("button", { name: "Agent context" }));
  expect(screen.getByRole("textbox", { name: "Memory entry 2" })).toHaveValue("Second decision");
  fireEvent.click(screen.getByRole("button", { name: "Save project settings" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Project settings saved.");
  expect(write).toHaveBeenCalledWith("/api/ide/projects/p", { memory: ["First decision", "Second decision"] }, "PATCH");
  expect(screen.getByRole("button", { name: "Save project settings" })).toBeDisabled();
});
it("keeps an unsaved project name until the user confirms discard", async () => {
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  const trigger = screen.getByRole("button", { name: "Project settings" });
  fireEvent.click(await screen.findByRole("button", { name: "Project settings" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), { target: { value: "Draft name" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  let confirm = await screen.findByRole("alertdialog", { name: "Discard changes?" });
  fireEvent.click(within(confirm).getByRole("button", { name: "Keep editing" }));
  expect(screen.getByRole("textbox", { name: "Project name" })).toHaveValue("Draft name");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  confirm = await screen.findByRole("alertdialog", { name: "Discard changes?" });
  fireEvent.click(within(confirm).getByRole("button", { name: "Discard changes" }));
  await waitFor(() => expect(trigger).toHaveFocus());
  fireEvent.click(await screen.findByRole("button", { name: "Project settings" }));
  expect(screen.getByRole("textbox", { name: "Project name" })).toHaveValue("Test repository");
});
it("requires explicit version trust before enabling an extension for this project", async () => {
  const originalGet = vi.mocked(get).getMockImplementation()!;
  const originalWrite = vi.mocked(write).getMockImplementation()!;
  const extension = { id: "skill-1", name: "Review guide", kind: "skill", version: 2, hash: "abc123", source: "D:/skills/review/SKILL.md", trusted: false, enabled_projects: [] };
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/ide/extensions" ? { extensions: [extension] } as never : originalGet(path, signal));
  vi.mocked(write).mockImplementation(async (path, body, method) => {
    if (path.endsWith("/trust")) return { ...extension, trusted: true } as never;
    if (path === "/api/ide/projects/p/extensions/skill-1") return { ...extension, trusted: true, enabled_projects: ["p"] } as never;
    return originalWrite(path, body, method);
  });
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.click(await screen.findByRole("button", { name: "Project settings" }));
  fireEvent.click(screen.getByRole("button", { name: "Extensions" }));
  const review = await screen.findByRole("button", { name: "Review trust" });
  expect(screen.queryByRole("button", { name: "Enable for project" })).not.toBeInTheDocument();
  fireEvent.click(review);
  expect(screen.getByText("abc123")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Trust version 2" }));
  const enable = await screen.findByRole("button", { name: "Enable for project" });
  expect(write).toHaveBeenCalledWith("/api/ide/extensions/skill-1/trust", { version: 2, sha256: "abc123" });
  expect(write).not.toHaveBeenCalledWith("/api/ide/projects/p/extensions/skill-1", expect.anything());
  fireEvent.click(enable);
  expect(await screen.findByRole("button", { name: "Disable for project" })).toBeEnabled();
  expect(write).toHaveBeenCalledWith("/api/ide/projects/p/extensions/skill-1", { enabled: true });
});
it("follows external chat links and browser history while remaining mounted", async () => {
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.click(screen.getByRole("button", { name: "External chat link" }));
  await waitFor(() => expect(get).toHaveBeenCalledWith("/api/chats/chat2", expect.any(AbortSignal)));
  expect(screen.getByLabelText("Current route")).toHaveTextContent("chat=chat2");
  fireEvent.click(screen.getByRole("button", { name: "Go back" }));
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("chat=chat"));
  expect(screen.getByLabelText("Current route")).not.toHaveTextContent("chat=chat2");
  fireEvent.click(screen.getByRole("button", { name: "Go forward" }));
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("chat=chat2"));
});
it("does not rewrite another route while the IDE remains mounted", async () => {
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.click(screen.getByRole("button", { name: "Other page" }));
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("/settings?tab=runtime"));
  fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  expect(screen.queryByRole("dialog", { name: "Commands" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Current route")).toHaveTextContent("/settings?tab=runtime");
});
it("keeps scans in Ctrl+K and gives workspace actions their own panel", async () => {
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  const header = within(screen.getByRole("banner"));
  expect(header.getByRole("heading", { name: "Review project" })).toBeVisible();
  expect(screen.queryByRole("region", { name: "Source editor" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "Terminal" })).not.toBeInTheDocument();
  expect(header.queryByRole("button", { name: "Actions" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Open plan" })).not.toBeInTheDocument();
  fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  const commands = within(await screen.findByRole("dialog", { name: "Commands" }));
  for (const name of [/^Check dependencies /, /^Scan code /]) {
    expect(commands.getByRole("button", { name })).toBeVisible();
  }
  expect(commands.getAllByRole("button").filter(button => button.textContent?.trim())).toHaveLength(2);
  fireEvent.keyDown(commands.getByRole("searchbox"), { key: "Escape" });
  await openWorkspace("Plan");
  expect(await screen.findByText("No plan yet")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Close plan" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Workspace" })).toHaveFocus());
});

it.each(["changes", "sources"])("keeps failed %s lookups distinct from successful empty results and allows retry", async panel => {
  const original = vi.mocked(get).getMockImplementation()!;
  let fail = true;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path.endsWith(`/${panel}`) && fail) throw new Error(`${panel} lookup unavailable`);
    return original(path, signal);
  });
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  await openWorkspace(panel === "changes" ? "Changes" : "Sources");
  if (panel === "changes") fireEvent.click(screen.getByRole("button", { name: /Agent workspace/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent(`${panel} lookup unavailable`);
  expect(screen.queryByText("No changes yet")).not.toBeInTheDocument();
  fail = false; fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

it("creates no chat until the selected model has finished loading", async () => {
  let finish!: (value: unknown) => void;
  const model = { id: "qwen", name: "Qwen2.5-Coder 7B Instruct", context: 8192 };
  const original = vi.mocked(write).getMockImplementation()!;
  vi.mocked(write).mockImplementation(async (path, body, method) => {
    if (path === "/api/ide/models/prepare") return await new Promise<unknown>(resolve => { finish = resolve; }) as never;
    if (path === "/api/ide/projects/p/chats") return { id: "new", title: "New chat", model } as never;
    return original(path, body, method);
  });
  showEmptyProject();
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(composer, { target: { value: "First project message" } });
  fireEvent.keyDown(composer, { key: "Enter" });
  expect(await screen.findByRole("dialog", { name: "Loading selected model" })).toBeInTheDocument();
  expect(write).not.toHaveBeenCalledWith("/api/ide/projects/p/chats", expect.anything());
  await act(async () => { finish({ id: "prepared", state: "ready", model }); });
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/ide/projects/p/chats", { title: "New chat", model, preparation_id: "prepared" }));
  expect(vi.mocked(write).mock.calls.filter(([path]) => path.endsWith("/chats"))).toHaveLength(1);
});

it("offers installed models for a new chat, defaults to Settings, and loads the user's choice", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  const models = [{ id: "small", name: "Example/Small-Coder, Q4_K_M", available: true }, { id: "other", name: "Other/Code-Model, Q8_0", available: true, selected: true }];
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/ide/models" ? { models } as never : original(path, signal));
  vi.mocked(write).mockImplementation(async () => await new Promise<never>(() => {}));
  showEmptyProject();
  const select = await screen.findByRole("combobox", { name: "Agent model" });
  expect(select).toHaveTextContent(models[1].name);
  expect(write).not.toHaveBeenCalled();
  fireEvent.keyDown(select, { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: models[0].name }));
  const composer = screen.getByRole("textbox", { name: "Message agent" });
  fireEvent.change(composer, { target: { value: "First project message" } });
  fireEvent.keyDown(composer, { key: "Enter" });
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/ide/models/prepare", { model: models[0], context: 8192 }));
});

it("does not send a prompt when model preparation fails", async () => {
  const original = vi.mocked(write).getMockImplementation()!;
  const originalGet = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path.endsWith("/model-status") ? { ready: false } as never : originalGet(path, signal));
  vi.mocked(write).mockImplementation(async (path, body, method) => path === "/api/ide/models/prepare" ? { id: "load", state: "failed", error: { code: "does_not_fit", message: "Free memory before loading this model." } } as never : original(path, body, method));
  show(); const input = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Fix this function" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Free memory");
  expect(write).not.toHaveBeenCalledWith("/api/chats/chat/turns", expect.anything());
  fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
  await waitFor(() => expect(input).toHaveValue("Fix this function"));
});

it("switches an existing chat from the composer only after the chosen model loads and keeps the draft", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  const other = { id: "small", name: "Small coding model", available: true };
  let switched = false, finish!: (value: unknown) => void;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/models") return { models: [{ id: "qwen", name: "Qwen2.5-Coder 7B Instruct" }, other] } as never;
    if (path === "/api/chats/chat" && switched) return { chat: { id: "chat", title: "Review project", model: other }, runs: [], messages: [], permissions: [], questions: [] } as never;
    return original(path, signal);
  });
  vi.mocked(write).mockImplementation(async (path) => {
    if (path === "/api/ide/models/prepare") return await new Promise<unknown>(resolve => { finish = resolve; }) as never;
    if (path === "/api/chats/chat/model") { switched = true; return { id: "chat", title: "Review project", model: other } as never; }
    if (path.endsWith("/runtime/prepare")) return { chat_id: "chat", model_id: other.id, ready: true, state: "ready" } as never;
    return {} as never;
  });
  show(); const input = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Keep this draft" } });
  const select = screen.getByRole("combobox", { name: "Agent model" });
  expect(select).not.toBeDisabled(); fireEvent.keyDown(select, { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: other.name }));
  expect(await screen.findByRole("dialog", { name: "Switching model" })).toBeInTheDocument();
  expect(write).not.toHaveBeenCalledWith("/api/chats/chat/model", expect.anything());
  await act(async () => { finish({ id: "loaded", state: "ready", model: other }); });
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/model", { model: other, preparation_id: "loaded", expected_model_id: "qwen" }));
  expect(input).toHaveValue("Keep this draft");
  expect(select).toHaveTextContent(other.name);
});

it("sends immediately when the chat model is already ready without reopening its load dialog", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path.endsWith("/model-status") ? { ready: true, model_id: "qwen" } as never : original(path, signal));
  show(); const input = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Explain the route" } }); fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/turns", expect.objectContaining({ text: "Explain the route" })));
  expect(write).not.toHaveBeenCalledWith("/api/ide/models/prepare", expect.anything());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("acknowledges an accepted message even if the following conversation refresh fails", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  let submitted = false;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path.endsWith("/model-status")) return { ready: true, model_id: "qwen" } as never;
    if (path === "/api/chats/chat" && submitted) throw new Error("Refresh unavailable");
    return original(path, signal);
  });
  vi.mocked(write).mockImplementation(async path => { if (path.endsWith("/runtime/prepare")) return { chat_id: "chat", model_id: "qwen", ready: true, state: "ready" } as never; submitted = true; return { id: "accepted", state: "queued" } as never; });
  show(); const input = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Implement once" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(input).toHaveValue(""));
  expect(screen.getByRole("alert")).toHaveTextContent("Your request was submitted");
  expect(vi.mocked(write).mock.calls.filter(([path]) => path.endsWith("/turns"))).toHaveLength(1);
});

it("cancels a model switch without applying a late preparation result or losing the draft", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  const other = { id: "small", name: "Small coding model", available: true };
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/ide/models" ? { models: [{ id: "qwen", name: "Qwen2.5-Coder 7B Instruct" }, other] } as never : original(path, signal));
  let finish!: (value: unknown) => void;
  vi.mocked(write).mockImplementation(async () => await new Promise<unknown>(resolve => { finish = resolve; }) as never);
  show(); const input = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Keep my draft" } });
  const select = screen.getByRole("combobox", { name: "Agent model" });
  fireEvent.keyDown(select, { key: "ArrowDown" }); fireEvent.click(await screen.findByRole("option", { name: other.name }));
  fireEvent.click(await screen.findByRole("button", { name: "Cancel switch" }));
  await act(async () => { finish({ id: "late", state: "ready", model: other }); });
  expect(write).not.toHaveBeenCalledWith("/api/chats/chat/model", expect.anything());
  expect(select).toHaveTextContent("Qwen2.5-Coder"); expect(input).toHaveValue("Keep my draft");
});

it("does not let a poll started before a model switch restore the previous model", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  const other = { id: "small", name: "Small coding model", available: true };
  let reads = 0, changed = false, finish!: (value: unknown) => void;
  vi.mocked(get).mockImplementation(async (path, signal) => {
    if (path === "/api/ide/models") return { models: [{ id: "qwen", name: "Qwen2.5-Coder 7B Instruct" }, other] } as never;
    if (path === "/api/chats/chat") {
      if (++reads === 2) return await new Promise<unknown>(resolve => { finish = resolve; }) as never;
      if (changed) return { chat: { id: "chat", title: "Review project", model: other }, messages: [], runs: [], permissions: [], questions: [] } as never;
    }
    return original(path, signal);
  });
  vi.mocked(write).mockImplementation(async path => {
    if (path === "/api/ide/models/prepare") return { id: "loaded", state: "ready", model: other } as never;
    if (path.endsWith("/runtime/prepare")) return { chat_id: "chat", model_id: other.id, ready: true, state: "ready" } as never;
    changed = true; return { id: "chat", title: "Review project", model: other } as never;
  });
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  await waitFor(() => expect(finish).toBeTypeOf("function"), { timeout: 3500 });
  const select = screen.getByRole("combobox", { name: "Agent model" });
  fireEvent.keyDown(select, { key: "ArrowDown" }); fireEvent.click(await screen.findByRole("option", { name: other.name }));
  await waitFor(() => expect(select).toHaveTextContent(other.name));
  await act(async () => { finish(await original("/api/chats/chat")); });
  expect(select).toHaveTextContent(other.name);
});

it("does not send an unsent draft after navigating away during model preparation", async () => {
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path.endsWith("/model-status") ? { ready: false } as never : original(path, signal));
  let finish!: (value: unknown) => void;
  vi.mocked(write).mockImplementation(async () => await new Promise<unknown>(resolve => { finish = resolve; }) as never);
  show(); const input = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Do not send after leaving" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByRole("dialog", { name: "Loading selected model" });
  fireEvent.click(screen.getByRole("button", { name: "Other page", hidden: true }));
  await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("/settings"));
  await act(async () => { finish({ id: "late", state: "ready", model: { id: "qwen", name: "Qwen2.5-Coder 7B Instruct" } }); });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(write).not.toHaveBeenCalledWith("/api/chats/chat/turns", expect.anything());
  expect(input).toHaveValue("Do not send after leaving");
});

it("prepares the agent when opening a chat and keeps Send disabled until it is ready", async () => {
  const originalGet = vi.mocked(get).getMockImplementation()!;
  const originalWrite = vi.mocked(write).getMockImplementation()!;
  let ready = false, release!: (value: unknown) => void;
  vi.mocked(get).mockImplementation(async (path, signal) => path.endsWith("/runtime-status") ? { chat_id: "chat", model_id: "qwen", ready, state: ready ? "ready" : "queued" } as never : originalGet(path, signal));
  vi.mocked(write).mockImplementation(async (path, body, method) => path.endsWith("/runtime/prepare") ? await new Promise<unknown>(resolve => { release = resolve; }) as never : originalWrite(path, body, method));
  show(); const input = await screen.findByRole("textbox", { name: "Message agent" });
  await waitFor(() => expect(release).toBeTypeOf("function"));
  fireEvent.change(input, { target: { value: "Hello" } });
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  expect(write).not.toHaveBeenCalledWith("/api/chats/chat/turns", expect.anything());
  ready = true;
  await act(async () => { release({ chat_id: "chat", model_id: "qwen", ready: true, state: "ready" }); });
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).not.toBeDisabled());
  expect(input).toHaveValue("Hello");
});

it.each([false, true])("retries agent initialization without duplicating a chat or changing its bound context (memory failure: %s)", async memoryFailure => {
  const originalWrite = vi.mocked(write).getMockImplementation()!;
  const originalGet = vi.mocked(get).getMockImplementation()!;
  const model = { id: "qwen", name: "Qwen2.5-Coder 7B Instruct" };
  let creates = 0, boots = 0, loads = 0;
  vi.mocked(get).mockImplementation(async (path, signal) => path === "/api/chats/new" ? { chat: { id: "new", title: "New chat", model }, messages: [], runs: [], permissions: [], questions: [] } as never : originalGet(path, signal));
  vi.mocked(write).mockImplementation(async (path, body, method) => {
    if (path === "/api/ide/models/prepare") return ++loads > 1 && memoryFailure ? { id: "retry", state: "failed", model, error: { code: "does_not_fit", message: "Not enough memory" } } as never : { id: "prepared", state: "ready", model } as never;
    if (path === "/api/ide/projects/p/chats") { creates++; return { id: "new", title: "New chat", model } as never; }
    if (path.endsWith("/runtime/prepare")) return { chat_id: "new", model_id: "qwen", ready: ++boots > 1, state: boots > 1 ? "ready" : "failed", error: { message: "Tools did not start" } } as never;
    return originalWrite(path, body, method);
  });
  showEmptyProject();
  const composer = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(composer, { target: { value: "First project message" } });
  fireEvent.keyDown(composer, { key: "Enter" });
  expect(await screen.findByRole("alert")).toHaveTextContent("Tools did not start");
  fireEvent.click(screen.getByRole("button", { name: "Retry loading" }));
  if (memoryFailure) {
    expect(await screen.findByRole("alert")).toHaveTextContent("Not enough memory");
    expect(screen.queryByRole("button", { name: /Retry with/ })).not.toBeInTheDocument();
    expect(creates).toBe(1); expect(boots).toBe(1); return;
  }
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(creates).toBe(1); expect(boots).toBe(2);
});

it("shows a failed preparation without automatically retrying it", async () => {
  const originalGet = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path.endsWith("/runtime-status") ? { chat_id: "chat", model_id: "qwen", ready: false, state: "failed", message: "Tools did not start" } as never : originalGet(path, signal));
  show(); await screen.findByRole("textbox", { name: "Message agent" });
  const retry = await screen.findByRole("button", { name: "Retry agent preparation" });
  expect(write).not.toHaveBeenCalled();
  fireEvent.click(retry);
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/runtime/prepare"));
});





it("counts unsaved workspace files and saves them before sending when asked", async () => {
  // Review F2: the composer knows about unsaved drawer drafts, and Save and send writes them first.
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path.endsWith("/model-status") ? { ready: true, model_id: "qwen" } as never : original(path, signal));
  vi.mocked(write).mockImplementation(async (path, body) => path.endsWith("/runtime/prepare") ? { chat_id: "chat", model_id: "qwen", ready: true, state: "ready" } as never
    : path.endsWith("/file") ? { ...(body as object), version: "saved-v2" } as never : {} as never);
  show();
  fireEvent.click(await screen.findByRole("button", { name: "Workspace files" }));
  fireEvent.click(await screen.findByRole("button", { name: /app\.ts/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Edit test file" }));
  fireEvent.click(screen.getByRole("button", { name: "Close workspace files" }));
  const input = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Use my edit" } }); fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(await screen.findByText("1 unsaved file")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save and send" }));
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/turns", expect.objectContaining({ text: "Use my edit" })));
  expect(write).toHaveBeenCalledWith("/api/chats/chat/file", { path: "app.ts", text: "saved source edited", version: "saved-v1" }, "PUT");
  const order = vi.mocked(write).mock.calls.map(([path]) => path);
  expect(order.indexOf("/api/chats/chat/file")).toBeLessThan(order.indexOf("/api/chats/chat/turns"));
});

it("cancels agent preparation when the conversation changes and says the message was not sent", async () => {
  // Review F4: switching conversations during preparation must not drop the message silently or block the next chat.
  const original = vi.mocked(get).getMockImplementation()!;
  vi.mocked(get).mockImplementation(async (path, signal) => path.endsWith("/model-status") ? { ready: true, model_id: "qwen" } as never
    : path.endsWith("/runtime-status") ? { chat_id: "chat", model_id: "qwen", ready: false, state: "loading", message: "Starting the agent" } as never : original(path, signal));
  let prepares = 0;
  vi.mocked(write).mockImplementation(async path => path.endsWith("/runtime/prepare")
    ? { chat_id: "chat", model_id: "qwen", ready: ++prepares === 1, state: prepares === 1 ? "ready" : "loading" } as never : {} as never);
  show();
  const input = await screen.findByRole("textbox", { name: "Message agent" });
  await waitFor(() => expect(prepares).toBe(1));
  fireEvent.change(input, { target: { value: "Keep this message" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(prepares).toBe(2));
  fireEvent.click(screen.getByRole("button", { name: "External chat link" }));
  expect(await screen.findByText(/was not sent because you switched conversations/)).toBeInTheDocument();
  const next = await screen.findByRole("textbox", { name: "Message agent" });
  fireEvent.change(next, { target: { value: "Second chat" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
  expect(write).not.toHaveBeenCalledWith("/api/chats/chat/turns", expect.anything());
});
