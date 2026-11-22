import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { Conversation } from "./Conversation";
import type { ChatDetail } from "./client";

vi.mock("../../components/chat/Markdown", () => ({ Markdown: ({ text }: { text: string }) => <p>{text}</p> }));
function setup(dirtyCount = 0, detail: ChatDetail | null = null) {
  const onSend = vi.fn(async (): Promise<void> => undefined);
  const onScan = vi.fn(async (): Promise<void> => undefined);
  const onCancel = vi.fn();
  const onAnswer = vi.fn(async (): Promise<void> => undefined);
  render(<Conversation detail={detail} models={[{ id: "qwen", name: "Qwen2.5-Coder 7B Instruct" }]} modelId="qwen" onModel={vi.fn()} dirtyCount={dirtyCount} busy={false} onSend={onSend} onScan={onScan} onCancel={onCancel} onPermission={vi.fn()} onAnswer={onAnswer} />);
  const input = screen.getByRole("textbox", { name: "Message agent" });
  return { input, onSend, onScan, onCancel, onAnswer };
}

it("uses Build by default and /plan for one read-only request", async () => {
  const { input, onSend } = setup();
  const send = (value: string) => {
    fireEvent.change(input, { target: { value } });
    fireEvent.keyDown(input, { key: "Enter" });
  };
  expect(screen.queryByRole("group", { name: "Agent mode" })).toBeNull();
  send("/plan");
  expect(screen.getByText("Planning next message")).toBeVisible();
  expect(onSend).not.toHaveBeenCalled();
  send("Keep the public API");
  await waitFor(() => expect(screen.queryByText("Planning next message")).toBeNull());
  expect(onSend).toHaveBeenLastCalledWith("Keep the public API", false, "plan");
  send("Implement the agreed change");
  await waitFor(() => expect(input).toHaveValue(""));
  expect(onSend).toHaveBeenLastCalledWith("Implement the agreed change", false, "build");
  send("/plan Add a regression");
  expect(onSend).toHaveBeenLastCalledWith("Add a regression", false, "plan");
  await waitFor(() => expect(input).toHaveValue(""));
});

it("asks a recommended planning question above the composer and accepts a custom answer", async () => {
  const user = userEvent.setup();
  const { input, onAnswer } = setup(0, { chat: { id: "planning", title: "Plan" }, runs: [{ id: "run", kind: "turn", state: "waiting_input" }],
    messages: [], permissions: [], questions: [{ id: "question", questions: [{ question: "Which retry strategy?", options: [
      { label: "Reuse existing policy", description: "Keeps one implementation.", recommended: true },
      { label: "Add a dedicated policy", description: "Lets this route differ." },
    ] }] }] });
  const question = screen.getByRole("form", { name: "Planning question" });
  expect(question.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.getByText("Recommended")).toBeVisible();
  expect(screen.getAllByRole("radio").slice(-1)[0]).toHaveAccessibleName("Write my own answer");
  await user.click(screen.getByRole("radio", { name: "Reuse existing policy" }));
  await user.click(screen.getByRole("radio", { name: "Write my own answer" }));
  expect(screen.getByRole("button", { name: "Send answer" })).toBeDisabled();
  await user.type(screen.getByRole("textbox", { name: /Custom answer/ }), "Use explicit deadlines");
  await user.click(screen.getByRole("button", { name: "Send answer" }));
  expect(onAnswer).toHaveBeenCalledWith("question", [["Use explicit deadlines"]]);
});

it("runs a complete dependency menu command as soon as it is chosen", async () => {
  const { input, onScan, onSend } = setup();
  fireEvent.change(input, { target: { value: "/scan dependencies" } });
  fireEvent.click(screen.getByRole("menuitem", { name: /Check versions and known advisories/ }));
  await waitFor(() => expect(onScan).toHaveBeenCalledWith({ kind: "scan", scope: "dependencies", dependency_mode: "versions" }));
  expect(onSend).not.toHaveBeenCalled();
  expect(input).toHaveValue("");
  expect(screen.queryByRole("button", { name: "Composer actions" })).toBeNull();
});

it("keeps archived conversations read-only", () => {
  const { input, onSend } = setup(0, { chat: { id: "archived", title: "Archived", archived: true }, runs: [], messages: [], permissions: [], questions: [] });
  fireEvent.change(input, { target: { value: "/plan Change the route" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
});

it("uses a simple composer in the new conversation", () => {
  const { input } = setup();
  expect(input).toHaveAttribute("placeholder", "Message the agent or type / for commands");
  expect(screen.getByRole("heading", { name: "New conversation" })).toBeVisible();
});

it("submits no request for incomplete slash commands and advances a local command menu", () => {
  const { input, onSend, onScan } = setup();
  fireEvent.change(input, { target: { value: "/scan" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled(); expect(onScan).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("menuitem", { name: /Review dependencies/ }));
  expect(input).toHaveValue("/scan dependencies");
  expect(screen.getByRole("menuitem", { name: /Select packages for source review/ })).toBeInTheDocument();
});
it("keeps suggestions visible while typing a command and runs the keyboard selection", async () => {
  const user = userEvent.setup();
  const { input, onSend, onScan } = setup();
  await user.type(input, "/sc");
  expect(screen.getAllByRole("menuitem")).toHaveLength(1);
  expect(screen.getByRole("menuitem", { name: /Review project code or dependencies/ })).toBeVisible();

  await user.type(input, "an dep");
  expect(screen.getAllByRole("menuitem")).toHaveLength(1);
  expect(screen.getByRole("menuitem", { name: /\/scan dependencies Review dependencies/ })).toBeVisible();

  await user.type(input, "endencies  V");
  const versions = screen.getByRole("menuitem", { name: /Check versions and known advisories/ });
  expect(screen.getAllByRole("menuitem")).toHaveLength(1);
  expect(onScan).not.toHaveBeenCalled();
  await user.keyboard("{ArrowDown}");
  expect(versions).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(onScan).toHaveBeenCalledOnce();
  expect(onScan).toHaveBeenCalledWith({ kind: "scan", scope: "dependencies", dependency_mode: "versions" });
  expect(onSend).not.toHaveBeenCalled();
  expect(input).toHaveValue("");
});
it("renders source search evidence with its file, line range and coverage", () => {
  setup(0, { chat: { id: "chat", title: "Search" }, runs: [], permissions: [], questions: [], messages: [{
    info: { id: "agent", role: "assistant" }, parts: [{ type: "tool", tool: "search_project", state: { status: "completed", output: {
      hits: [{ path: "src/billing.ts", start_line: 18, end_line: 20, excerpt: "function refundOrder() {}" }],
      complete: false, omissions: ["Index is warming"],
    } } }],
  }] });
  expect(screen.getByText("Search project")).toBeInTheDocument();
  expect(screen.getByText("src/billing.ts:18-20")).toBeInTheDocument();
  expect(screen.getByText("function refundOrder() {}")).toBeInTheDocument();
  expect(screen.getByText("Index is warming")).toBeInTheDocument();
});
it("shows usage without sending invalid slash input", () => {
  const { input, onSend, onScan } = setup();
  fireEvent.change(input, { target: { value: "/scan dependencies wrong" } });
  fireEvent.click(screen.getByRole("button", { name: "Run command" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Use /scan code");
  expect(onSend).not.toHaveBeenCalled(); expect(onScan).not.toHaveBeenCalled();
});

it("shows terminal command choices and help without sending a message", () => {
  const { input, onSend, onScan } = setup();
  fireEvent.change(input, { target: { value: "/" } });
  expect(screen.getByRole("menu", { name: "Terminal commands" })).toBeInTheDocument();
  fireEvent.change(input, { target: { value: "/help" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(screen.getByRole("status")).toHaveTextContent("/resume [title], /new, /scan code");
  expect(screen.getByRole("status")).toHaveTextContent("/scan model");
  expect(onSend).not.toHaveBeenCalled();
  expect(onScan).not.toHaveBeenCalled();
});
it("Escape cancels a slash command and returns focus to the composer", () => {
  const { input, onScan } = setup();
  fireEvent.change(input, { target: { value: "/scan dependencies" } });
  fireEvent.keyDown(input, { key: "Escape" });
  expect(input).toHaveValue(""); expect(screen.queryByRole("menu")).not.toBeInTheDocument(); expect(onScan).not.toHaveBeenCalled();
});
it("sends source review as structured selection intent", async () => {
  const { input, onScan, onSend } = setup();
  fireEvent.change(input, { target: { value: "/scan dependencies source" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(onScan).toHaveBeenCalledWith({ kind: "scan", scope: "dependencies", dependency_mode: "source" }));
  expect(onSend).not.toHaveBeenCalled();
});
it("starts the rebuilt model review from the composer", async () => {
  const { input, onScan, onSend } = setup();
  fireEvent.change(input, { target: { value: "/scan model" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(onScan).toHaveBeenCalledWith({ kind: "scan", scope: "model_review" }));
  expect(onSend).not.toHaveBeenCalled();
});

it("renders model review findings with quote and gate state", () => {
  setup(0, { chat: { id: "chat", title: "Review" }, messages: [], permissions: [], questions: [], runs: [{
    id: "scan", state: "completed", kind: "scan", payload: { scope: "model_review" }, result: {
      status: "partial", reviewed: 2, skipped: [{ function: "lookup", reason: "invalid_answer" }], findings: [{
        path: "policy.py", line: 9, class: "Missing Authorization", quote: "return allowed",
        explanation: "The permission check is absent.", gate_ok: true, gate_reasons: [],
      }],
    },
  }] });
  expect(screen.getByText("v40 source review")).toBeInTheDocument();
  expect(screen.getByText("policy.py:9")).toBeInTheDocument();
  expect(screen.getByText("return allowed")).toBeInTheDocument();
  expect(screen.getByText("Evidence passed")).toBeInTheDocument();
  expect(screen.getByText("2 functions reviewed, 1 skipped.")).toBeInTheDocument();
});
it("renders a two-pass model review with its confirmation state, exposure and counts", () => {
  setup(0, { chat: { id: "chat", title: "Review" }, messages: [], permissions: [], questions: [], runs: [{
    id: "scan", state: "completed", kind: "scan", payload: { scope: "model_review" }, result: {
      version: 2, status: "complete", reviewed: 5, skipped: [],
      coverage: { units: 5, reviewed: 5, by_kind: { function: 4, module: 1 }, confirmed: 1, unconfirmed: 1, rejected: 3, unlocated: 0, invalid_answers: 0 },
      findings: [
        { path: "relay/ws.go", line: 178, class: "Uncontrolled Resource Consumption", quote: "payload := make([]byte, plen)", state: "confirmed",
          explanation: "The frame length comes from the network.", gate_ok: true, gate_reasons: [], function: "readFrame",
          exposure: { distance: 0, reason: "reads a network connection" }, confirmation: { state: "confirmed", reason: "The class-scoped re-check reported the same place." } },
        { path: "pac.go", line: 42, class: "Cross-site Scripting", quote: "fmt.Fprintf(w, ...)", state: "unconfirmed",
          explanation: "Output is formatted.", gate_ok: true, gate_reasons: [], exposure: { distance: null, reason: "no path from external input was found in this project" } },
      ],
    },
  }] });
  expect(screen.getByText("relay/ws.go:178")).toBeInTheDocument();
  expect(screen.getAllByText("Confirmed").length).toBeGreaterThan(0);
  expect(screen.getAllByText("Needs review").length).toBeGreaterThan(0);
  expect(screen.getByText("Ruled out on re-check")).toBeInTheDocument();
  expect(screen.getByText("5 of 5")).toBeInTheDocument();
  expect(screen.getByText(/reads a network connection/)).toBeInTheDocument();
  expect(screen.queryByText("Evidence passed")).toBeNull();
});
it("submits a pending scan only once across repeated Enter and click events", async () => {
  const { input, onScan } = setup();
  let complete!: () => void;
  onScan.mockImplementationOnce(() => new Promise<void>(resolve => { complete = resolve; }));
  fireEvent.change(input, { target: { value: "/scan code" } });
  fireEvent.keyDown(input, { key: "Enter" });
  fireEvent.keyDown(input, { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(onScan).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  complete();
  await waitFor(() => expect(input).toHaveValue(""));
});
it("submits a pending turn only once and preserves newly typed draft text", async () => {
  const { input, onSend } = setup();
  let complete!: () => void;
  onSend.mockImplementationOnce(() => new Promise<void>(resolve => { complete = resolve; }));
  fireEvent.change(input, { target: { value: "First turn" } });
  fireEvent.keyDown(input, { key: "Enter" });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onSend).toHaveBeenCalledOnce();
  fireEvent.change(input, { target: { value: "Next turn draft" } });
  complete();
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).not.toBeDisabled());
  expect(input).toHaveValue("Next turn draft");
});
it("requires an explicit saved-file decision when unsaved buffers exist", async () => {
  const { input, onSend } = setup(2);
  fireEvent.change(input, { target: { value: "Explain this function" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(onSend).not.toHaveBeenCalled(); expect(screen.getByText("2 unsaved files")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Send saved files" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("Explain this function", false, "build"));
});
it("saves dirty buffers before submitting when Save and send is selected", async () => {
  const { input, onSend } = setup(1);
  fireEvent.change(input, { target: { value: "Implement this" } });
  fireEvent.keyDown(input, { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Save and send" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("Implement this", true, "build"));
});
it("keeps the message available when saving fails", async () => {
  const { input, onSend } = setup(1);
  onSend.mockRejectedValueOnce(new Error("File changed on disk"));
  fireEvent.change(input, { target: { value: "Use my edits" } });
  fireEvent.keyDown(input, { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Save and send" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("File changed on disk");
  expect(input).toHaveValue("Use my edits");
});
it("shows waiting approval state and a stop control for the actual active run", () => {
  const detail: ChatDetail = { chat: { id: "chat", title: "Fix" }, runs: [{ id: "run", state: "waiting_approval" }], messages: [], permissions: [{ id: "perm", permission: "bash", patterns: ["npm test"] }], questions: [] };
  const { onCancel } = setup(0, detail);
  expect(screen.getByText("waiting approval")).toBeInTheDocument();
  expect(screen.getByText("Permission required: bash")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Stop active run" })); expect(onCancel).toHaveBeenCalledOnce();
});
it("shows the observed startup phase instead of calling a claimed run queued", () => {
  const detail: ChatDetail = { chat: { id: "chat", title: "Review" }, messages: [], permissions: [], questions: [], runs: [{ id: "run", state: "running", phase: "starting_runtime" }], activity: { run_id: "run", phase: "starting_runtime", message: "Starting the coding runtime...", started_at: new Date().toISOString() } };
  setup(0, detail);
  expect(screen.getByRole("status")).toHaveTextContent("Starting the coding runtime...");
  expect(screen.getByRole("combobox", { name: "Agent model" })).toBeDisabled();
  expect(screen.queryByText("queued")).not.toBeInTheDocument();
});
it("keeps scan request and completion evidence in conversations with no native messages", () => {
  const detail: ChatDetail = { chat: { id: "chat", title: "Review" }, messages: [], permissions: [], questions: [], runs: [{ id: "scan", kind: "scan", state: "completed", payload: { scope: "dependencies", dependency_mode: "versions" }, result: { status: "incomplete", packages: [{ name: "demo" }], coverage: { analyzed: 0, limitations: ["Registry unavailable"] } } }] };
  setup(0, detail);
  expect(screen.getByText("Dependency check")).toBeInTheDocument();
  expect(screen.getByText("Partial coverage")).toBeInTheDocument();
  expect(screen.getByText("demo")).toBeInTheDocument();
  expect(screen.getByText(/Registry unavailable/)).toBeInTheDocument();
});

it("names a removed model and offers refresh and add in the model menu", async () => {
  const onModel = vi.fn();
  const onRefreshModels = vi.fn();
  const gone = { id: "aivf/reviewer-v40-preflight:latest", tag: "aivf/reviewer-v40-preflight:latest", name: "Reviewer V40 Preflight" };
  const detail: ChatDetail = { chat: { id: "chat", title: "Review", model: gone }, messages: [], permissions: [], questions: [], runs: [] };
  render(<Conversation detail={detail} models={[{ id: "aivf/reviewer-v40:latest", name: "AIVF Reviewer v40", tag: "aivf/reviewer-v40:latest" }]} modelId="" onModel={onModel}
    onRefreshModels={onRefreshModels} dirtyCount={0} busy={false} onSend={vi.fn(async () => undefined)} onScan={vi.fn(async () => undefined)} onCancel={vi.fn()} onPermission={vi.fn()} onAnswer={vi.fn(async () => undefined)} />);
  expect(screen.getByText(/used Reviewer V40 Preflight, which is no longer installed in Ollama/)).toBeVisible();
  const open = () => fireEvent.keyDown(screen.getByRole("combobox", { name: "Agent model" }), { key: "ArrowDown" });
  open();
  expect(await screen.findByRole("option", { name: /Reviewer V40 Preflight .*\(not installed\)/ })).toHaveAttribute("aria-disabled", "true");
  fireEvent.click(screen.getByRole("option", { name: "Refresh model list" }));
  expect(onRefreshModels).toHaveBeenCalledTimes(1);
  expect(onModel).not.toHaveBeenCalled();
  open();
  fireEvent.click(await screen.findByRole("option", { name: "AIVF Reviewer v40 / aivf/reviewer-v40:latest" }));
  expect(onModel).toHaveBeenCalledWith("aivf/reviewer-v40:latest");
  open();
  fireEvent.click(await screen.findByRole("option", { name: "Add model..." }));
  expect(await screen.findByRole("dialog", { name: "Add model to Ollama" })).toBeInTheDocument();
  expect(onModel).toHaveBeenCalledTimes(1);
});
