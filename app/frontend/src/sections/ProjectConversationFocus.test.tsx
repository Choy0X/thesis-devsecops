import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual, api: {
      projects: vi.fn(), projectChats: vi.fn(), projectChatGet: vi.fn(), projectChatCreate: vi.fn(), projectChatSend: vi.fn(), projectChatEndpoint: vi.fn(),
      servingStatus: vi.fn(), servingStatusLive: vi.fn(), servingUnload: vi.fn(), chatGpuView: vi.fn(),
      agentSessions: vi.fn(), agentSessionCreate: vi.fn(), agentSessionGet: vi.fn(), agentApplies: vi.fn(), agentCheckpoints: vi.fn(), agentCapabilities: vi.fn(), agentDiff: vi.fn(), agentModels: vi.fn(),
      agentSessionFork: vi.fn(), agentSessionExportUrl: (id: string, sid: string, format: string) => `/export/${id}/${sid}?format=${format}`,
    },
  };
});
import { api, ApiError, type ChatEndpoint, type Project } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { ProjectConversationFocus } from "./ProjectConversationFocus";

const project = (id: string, name: string) => ({ id, name, folders: [], languages: {}, ecosystems: [], created_at: "x", updated_at: "x",
  authorization: { acknowledged: true, at: "x", statement: "x" }, last_scan: null, instructions: "" }) as unknown as Project;
const local: ChatEndpoint = { configured: true, model: "m", host: "127.0.0.1", local: true, provider: "managed" };

const show = (over: Partial<React.ComponentProps<typeof ProjectConversationFocus>> = {}) => {
  const onBack = vi.fn(); const onTabChange = vi.fn();
  render(<ToastProvider><ConfirmProvider><ProjectConversationFocus projectId="shop-ab12cd" initialTab="chat" onBack={onBack} onTabChange={onTabChange} {...over} /></ConfirmProvider></ToastProvider>);
  return { onBack, onTabChange };
};

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.projects).mockResolvedValue({ projects: [project("other-111111", "Other"), project("shop-ab12cd", "The shop")], statement: "" });
  vi.mocked(api.projectChats).mockResolvedValue({ chats: [], damaged: 0 });
  vi.mocked(api.projectChatEndpoint).mockResolvedValue(local);
  vi.mocked(api.servingStatus).mockResolvedValue({ slots: {} } as any);
  vi.mocked(api.servingStatusLive).mockResolvedValue({ slots: {} } as any);
  vi.mocked(api.chatGpuView).mockResolvedValue({});
  vi.mocked(api.agentSessions).mockResolvedValue({ sessions: [], damaged: 0 });
  vi.mocked(api.agentApplies).mockResolvedValue({ applies: [] });
  vi.mocked(api.agentCheckpoints).mockResolvedValue({ checkpoints: [] });
  vi.mocked(api.agentCapabilities).mockResolvedValue({ plan: { max_rounds: 20, tools: [] }, manual: { max_rounds: 40, tools: [], confirm_tools: [] }, auto: { max_rounds: 60, max_seconds: 1800, tools: [] } });
  vi.mocked(api.agentDiff).mockResolvedValue({ files: [] });
  vi.mocked(api.agentModels).mockResolvedValue({ models: [] });
});

describe("ProjectConversationFocus", () => {
  it("names the project in its heading and shows its chat with the list and the settings open, and a way back", async () => {
    const { onBack } = show();
    expect(await screen.findByRole("heading", { level: 1, name: "The shop" })).toBeInTheDocument();
    expect(await screen.findByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "This conversation's settings" })).toBeInTheDocument();
    expect(api.projectChats).toHaveBeenCalledWith("shop-ab12cd");
    fireEvent.click(screen.getByRole("button", { name: "Back to the project" }));
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1));
  });

  it("says so, with a way back, when the project is not there", async () => {
    const { onBack } = show({ projectId: "gone-000000" });
    expect(await screen.findByRole("alert")).toHaveTextContent("There is no such project.");
    fireEvent.click(screen.getByRole("button", { name: "Back to the projects" }));
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("heading", { level: 1, name: "Conversation" })).toBeInTheDocument();
    expect(api.projectChats).not.toHaveBeenCalled();
  });

  it("says why the projects could not be read", async () => {
    vi.mocked(api.projects).mockRejectedValue(new ApiError({ status: 500, title: "The projects folder is unreadable." }));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("The projects folder is unreadable.");
  });

  it("shows a required unloading dialog before leaving when a model it loaded is still loaded, and only then goes back", async () => {
    vi.mocked(api.servingStatusLive).mockResolvedValue({ slots: { llm: { state: "ready" } } } as any);
    let resolveUnload: () => void = () => undefined;
    vi.mocked(api.servingUnload).mockReturnValue(new Promise((r) => { resolveUnload = () => r({ unloaded: ["llm"], status: {} } as any); }));
    const { onBack } = show();
    await screen.findByRole("heading", { level: 1, name: "The shop" });
    fireEvent.click(screen.getByRole("button", { name: "Back to the project" }));
    expect(await screen.findByRole("dialog", { name: "Unloading the model" })).toBeInTheDocument();
    expect(onBack).not.toHaveBeenCalled();
    resolveUnload();
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1));
  });

  it("shows the Normal Chat and Agentic Mode tabs, chat active first by default", async () => {
    show();
    await screen.findByRole("heading", { level: 1, name: "The shop" });
    expect(screen.getByRole("tab", { name: "Normal Chat" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Agentic Mode" })).toHaveAttribute("aria-selected", "false");
  });

  it("switching to the inactive tab shows its own empty draft, without creating a chat or a session", async () => {
    show();
    await screen.findByRole("navigation", { name: "Conversations" });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Agentic Mode" }), { button: 0 });
    expect(await screen.findByRole("combobox", { name: "Mode" })).toBeInTheDocument();
    expect(api.agentSessionCreate).not.toHaveBeenCalled();
    expect(api.projectChatCreate).not.toHaveBeenCalled();
  });

  it("switching tabs remounts the active chat and reloads its saved history", async () => {
    vi.mocked(api.projectChats).mockResolvedValue({ chats: [{ id: "c1", title: "Existing", preview: "hi", updated_at: "x", created_at: "x", message_count: 1 } as any], damaged: 0 });
    vi.mocked(api.projectChatGet).mockResolvedValue({ id: "c1", title: "Existing", created_at: "x", updated_at: "x",
      params: { system: "", temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false }, messages: [{ id: "m1", role: "user", content: "hi there", created_at: "x" }] } as any);
    show();
    await screen.findByText("hi there");
    expect(api.projectChatGet).toHaveBeenCalledTimes(1);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Agentic Mode" }), { button: 0 });
    await screen.findByRole("combobox", { name: "Mode" });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Normal Chat" }), { button: 0 });
    expect(await screen.findByText("hi there")).toBeInTheDocument();
    expect(api.projectChatGet).toHaveBeenCalledTimes(2);
  });

  it("a deep link with focus=agent and a session id opens directly on the agent tab with that session", async () => {
    vi.mocked(api.agentSessions).mockResolvedValue({ sessions: [{ id: "s1", mode: "plan", status: "idle", created_at: "x", updated_at: "x", rounds: 0, message_count: 0, pending_call: null } as any], damaged: 0 });
    vi.mocked(api.agentSessionGet).mockResolvedValue({ schema: "agent_session/1", id: "s1", project_id: "shop-ab12cd", mode: "plan", instructions: "look around",
      created_at: "x", updated_at: "x", status: "idle", transcript: [], task_list: [], pending_call: null, rounds: 0 } as any);
    show({ initialTab: "agent", initialSessionId: "s1" });
    await screen.findByRole("heading", { level: 1, name: "The shop" });
    expect(screen.getByRole("tab", { name: "Agentic Mode" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Instructions: look around")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Conversations" })).toBeNull();     // the chat pane was never visited, so never mounted
  });
});
