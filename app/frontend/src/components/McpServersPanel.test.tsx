import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { agentMcp: vi.fn(), agentMcpSave: vi.fn(), agentMcpCheck: vi.fn(), projects: vi.fn() } };
});
import { api, type McpServer, type Project } from "../lib/api";
import { McpServersPanel, parseEnv } from "./McpServersPanel";
import { ToastProvider } from "./Toast";

const server = (over: Partial<McpServer> = {}): McpServer => ({ name: "files", command: ["npx", "-y", "srv", "/work"], env_names: ["API_KEY"], enabled: true, timeout: 60, ...over });
const limits = { servers: 8, timeout: 60 };
const show = () => render(<ToastProvider><McpServersPanel /></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.projects).mockResolvedValue({ projects: [{ id: "p1", name: "Shop" } as Project], statement: "" });
});

describe("parseEnv", () => {
  it("reads NAME=value as a value, a bare NAME as keep what is saved, and ignores blank lines", () => {
    expect(parseEnv("A=1\n\nB\n  C = x=y \n")).toEqual({ A: "1", B: null, C: " x=y" });
  });
});

describe("McpServersPanel", () => {
  it("lists the saved servers with their command and only the names of their variables", async () => {
    vi.mocked(api.agentMcp).mockResolvedValue({ servers: [server()], limits });
    show();
    const list = await screen.findByRole("list", { name: "MCP servers" });
    expect((within(list).getByLabelText("Server 1: name") as HTMLInputElement).value).toBe("files");
    expect((within(list).getByLabelText(/Server 1: command/) as HTMLTextAreaElement).value).toBe("npx\n-y\nsrv\n/work");
    expect((within(list).getByLabelText("Server 1: variables") as HTMLTextAreaElement).value).toBe("API_KEY");
    expect(screen.getByText(/never shown again once it is saved/)).toBeTruthy();
  });

  it("says plainly when nothing is set up", async () => {
    vi.mocked(api.agentMcp).mockResolvedValue({ servers: [], limits });
    show();
    expect(await screen.findByText(/the agent has only its own tools/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save the servers" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("saves a new server with its command as words and its variables as values, and one that keeps its saved value as null", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentMcp).mockResolvedValue({ servers: [server()], limits });
    vi.mocked(api.agentMcpSave).mockResolvedValue({ servers: [server(), server({ name: "git", command: ["uvx", "mcp-git"], env_names: ["TOKEN"] })], limits });
    show();
    await screen.findByRole("list", { name: "MCP servers" });
    await user.click(screen.getByRole("button", { name: "Add a server" }));
    await user.type(screen.getByLabelText("Server 2: name"), "git");
    await user.type(screen.getByLabelText(/Server 2: command/), "uvx{Enter}mcp-git");
    await user.type(screen.getByLabelText("Server 2: variables"), "TOKEN=abc=def");
    await user.click(screen.getByRole("button", { name: "Save the servers" }));
    await waitFor(() => expect(api.agentMcpSave).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.agentMcpSave).mock.calls[0][0]).toEqual({
      files: { command: ["npx", "-y", "srv", "/work"], env: { API_KEY: null }, enabled: true, timeout: 60 },
      git: { command: ["uvx", "mcp-git"], env: { TOKEN: "abc=def" }, enabled: true, timeout: 60 },
    });
    expect(await screen.findByText("Saved the servers.")).toBeTruthy();
  });

  it("turns a server off and removes another", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentMcp).mockResolvedValue({ servers: [server(), server({ name: "git", env_names: [] })], limits });
    vi.mocked(api.agentMcpSave).mockResolvedValue({ servers: [server({ enabled: false })], limits });
    show();
    await screen.findByRole("list", { name: "MCP servers" });
    await user.click(screen.getByRole("switch", { name: "Server 1: on" }));
    await user.click(screen.getByRole("button", { name: "Remove server 2" }));
    await user.click(screen.getByRole("button", { name: "Save the servers" }));
    await waitFor(() => expect(api.agentMcpSave).toHaveBeenCalled());
    expect(Object.keys(vi.mocked(api.agentMcpSave).mock.calls[0][0])).toEqual(["files"]);
    expect(vi.mocked(api.agentMcpSave).mock.calls[0][0].files.enabled).toBe(false);
  });

  it("says why a save was refused and keeps what was typed", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentMcp).mockResolvedValue({ servers: [], limits });
    vi.mocked(api.agentMcpSave).mockRejectedValue(new Error("The server name 'Bad' must be lower case letters"));
    show();
    await user.click(await screen.findByRole("button", { name: "Add a server" }));
    await user.type(screen.getByLabelText("Server 1: name"), "Bad");
    await user.type(screen.getByLabelText(/Server 1: command/), "x");
    await user.click(screen.getByRole("button", { name: "Save the servers" }));
    expect(await screen.findByText(/Not saved: .*lower case letters/)).toBeTruthy();
    expect((screen.getByLabelText("Server 1: name") as HTMLInputElement).value).toBe("Bad");
  });

  it("checks the servers in a project's sandbox and shows which came up and why one did not", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentMcp).mockResolvedValue({ servers: [server(), server({ name: "gone" })], limits });
    vi.mocked(api.agentMcpCheck).mockResolvedValue({ servers: [{ name: "files", ok: true, tools: 3, error: "" }, { name: "gone", ok: false, tools: 0, error: "gone could not be started: no such file" }], tools: [] });
    show();
    await screen.findByRole("list", { name: "MCP servers" });
    await user.click(screen.getByRole("button", { name: "Check the servers" }));
    await waitFor(() => expect(api.agentMcpCheck).toHaveBeenCalledWith("p1"));
    const result = await screen.findByRole("list", { name: "Check result" });
    expect(result.textContent).toContain("files: 3 tools");
    expect(result.textContent).toContain("gone: gone could not be started: no such file");
  });

  it("does not check while there are unsaved changes", async () => {
    const user = userEvent.setup();
    vi.mocked(api.agentMcp).mockResolvedValue({ servers: [server()], limits });
    show();
    await screen.findByRole("list", { name: "MCP servers" });
    await user.type(screen.getByLabelText("Server 1: variables"), "{Enter}X=1");
    expect((screen.getByRole("button", { name: "Check the servers" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Save first.")).toBeTruthy();
  });
});
