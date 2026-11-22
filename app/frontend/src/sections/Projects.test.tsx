import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { projects: vi.fn(), projectLook: vi.fn(), createProject: vi.fn(), deleteProject: vi.fn(), projectAudit: vi.fn(), projectScans: vi.fn(), projectScan: vi.fn(),
    projectCapabilities: vi.fn(), scanProjectDeps: vi.fn(), projectChatEndpoint: vi.fn(), servingStatus: vi.fn(), projectChats: vi.fn(), projectUpdates: vi.fn(), pickFolder: vi.fn(), pickFolderResult: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { api, type Project } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { PICK_POLL_MS, Projects, languageText, when } from "./Projects";

const STATEMENT = "I own this code, or I am authorised to test it. The app may read it and copy it into a sandbox to analyse it.";
const project = (over: Partial<Project> = {}): Project => ({
  id: "shop-ab12cd", name: "The shop", folders: [{ path: "C:\\code\\shop", languages: { Python: 12, JavaScript: 3 }, ecosystems: ["PyPI", "npm"], manifests: {}, files_seen: 15, truncated: false }],
  languages: { Python: 12, JavaScript: 3 }, ecosystems: ["PyPI", "npm"], created_at: "2026-10-09T10:00:00Z", updated_at: "2026-10-09T10:00:00Z",
  authorization: { acknowledged: true, at: "2026-10-09T10:00:05Z", statement: STATEMENT }, last_scan: null, instructions: "", ...over });

const show = () => render(<ToastProvider><ConfirmProvider><Projects /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.projects).mockResolvedValue({ projects: [project()], statement: STATEMENT });
  vi.mocked(api.projectLook).mockResolvedValue({ ok: true, path: "C:\\code\\shop", languages: { Python: 12 }, ecosystems: ["PyPI"], manifests: {}, files_seen: 12, truncated: false,
    suggested_name: "Acme Shop", name_source: "package.json" });
  vi.mocked(api.projectAudit).mockResolvedValue({ entries: [] });
  vi.mocked(api.projectScans).mockResolvedValue({ scans: [] });
  vi.mocked(api.projectCapabilities).mockResolvedValue({ capabilities: [], ecosystems: {} });
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.projectChatEndpoint).mockResolvedValue({ configured: false, model: null, host: "", local: false, provider: "" });
  vi.mocked(api.projectChats).mockResolvedValue({ chats: [], damaged: 0 });
  vi.mocked(api.servingStatus).mockResolvedValue({ slots: {} } as any);
  vi.mocked(api.projectUpdates).mockResolvedValue({ updates: [] });
});

describe("the words", () => {
  it("lists the main languages with their file counts, and says when there are none", () => {
    expect(languageText({ Python: 12, JavaScript: 3 })).toBe("Python (12), JavaScript (3)");
    expect(languageText({ A: 1, B: 1, C: 1, D: 1, E: 1 })).toBe("A (1), B (1), C (1), D (1)");
    expect(languageText({})).toBe("no source files found");
    expect(when("2026-10-09T10:00:05Z")).toBe("2026-10-09 10:00");
  });
});

describe("Projects", () => {
  it("lists the projects as cards with their folders, languages, packages and scan state", async () => {
    show();
    const card = (await screen.findByText("The shop")).closest("li") as HTMLElement;
    expect(card).toHaveTextContent("C:\\code\\shop");
    expect(card).toHaveTextContent("Python (12), JavaScript (3)");
    expect(card).toHaveTextContent("Packages: PyPI, npm");
    expect(card).toHaveTextContent("not scanned yet");
  });

  it("says no project yet when there is none", async () => {
    vi.mocked(api.projects).mockResolvedValue({ projects: [], statement: STATEMENT });
    show();
    expect(await screen.findByText("No project yet")).toBeInTheDocument();
  });

  it("adds a project only after the folder is looked at and the confirmation is ticked, and shows what the folder holds", async () => {
    const added = project({ id: "new-aaaaaa", name: "New one" });
    vi.mocked(api.createProject).mockImplementation(async () => { vi.mocked(api.projects).mockResolvedValue({ projects: [added, project()], statement: STATEMENT }); return added; });
    show();
    await screen.findByText("The shop");
    fireEvent.click(screen.getByRole("button", { name: /Add a project/ }));
    const dialog = await screen.findByRole("dialog", { name: "Add a project" });
    const create = within(dialog).getByRole("button", { name: "Add the project" });
    expect(create).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("Project name"), { target: { value: " New one " } });
    fireEvent.change(within(dialog).getByLabelText("Source folder"), { target: { value: "C:\\code\\shop" } });
    expect(await within(dialog).findByText(/Python \(12\); packages: PyPI\./)).toBeInTheDocument();
    expect(create).toBeDisabled();                                                              // not yet confirmed
    expect(within(dialog).getByText(STATEMENT)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByLabelText("I own this code or am authorised to test it"));
    await waitFor(() => expect(create).toBeEnabled());
    fireEvent.click(create);
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({ name: "New one", folders: ["C:\\code\\shop"], authorized: true }));
    expect(await screen.findByText(/Confirmed on 2026-10-09 10:00 that you own the code or may test it\./)).toBeInTheDocument();
  });

  it("says why a folder cannot be used, and why the server refused a project", async () => {
    vi.mocked(api.projectLook).mockResolvedValue({ ok: false, reason: "C:\\nope does not exist, or cannot be read." });
    vi.mocked(api.createProject).mockImplementation(() => Promise.reject(new Error("The same folder is listed twice.")));
    show();
    fireEvent.click(await screen.findByRole("button", { name: /Add a project/ }));
    const dialog = await screen.findByRole("dialog", { name: "Add a project" });
    fireEvent.change(within(dialog).getByLabelText("Project name"), { target: { value: "x" } });
    fireEvent.change(within(dialog).getByLabelText("Source folder"), { target: { value: "C:\\nope" } });
    expect(await within(dialog).findByText("C:\\nope does not exist, or cannot be read.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByLabelText("I own this code or am authorised to test it"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add the project" }));
    expect(await within(dialog).findByText("The same folder is listed twice.")).toBeInTheDocument();
  });

  it("fills the folder from the operating system's folder window, waits while it is open and keeps the typed path when the person cancels", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.pickFolder).mockResolvedValue({ ok: true, id: "pick1" });
      vi.mocked(api.pickFolderResult).mockResolvedValueOnce({ state: "waiting", path: null }).mockResolvedValueOnce({ state: "chosen", path: "C:\code\picked" });
      show();
      fireEvent.click(await screen.findByRole("button", { name: /Add a project/ }));
      const dialog = await screen.findByRole("dialog", { name: "Add a project" });
      fireEvent.change(within(dialog).getByLabelText("Source folder"), { target: { value: "C:\typed" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Browse for a folder" }));
      await vi.advanceTimersByTimeAsync(PICK_POLL_MS * 3);
      await waitFor(() => expect(within(dialog).getByLabelText("Source folder")).toHaveValue("C:\code\picked"));
      expect(api.pickFolder).toHaveBeenCalledWith("Choose the folder of the project");
      vi.mocked(api.pickFolderResult).mockResolvedValue({ state: "cancelled", path: null });
      fireEvent.click(within(dialog).getByRole("button", { name: "Browse for a folder" }));
      await vi.advanceTimersByTimeAsync(PICK_POLL_MS * 2);
      expect(within(dialog).getByLabelText("Source folder")).toHaveValue("C:\code\picked");
    } finally {
      vi.useRealTimers();
    }
  });

  it("says to type the path where the app has no window of its own", async () => {
    vi.mocked(api.pickFolder).mockResolvedValue({ ok: false, detail: "this copy of the app has no window of its own, so a folder cannot be chosen here; type the path" });
    show();
    fireEvent.click(await screen.findByRole("button", { name: /Add a project/ }));
    const dialog = await screen.findByRole("dialog", { name: "Add a project" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Browse for a folder" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent("no window of its own");
    expect(api.pickFolderResult).not.toHaveBeenCalled();
  });

  const openAdd = async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: /Add a project/ }));
    return screen.findByRole("dialog", { name: "Add a project" });
  };
  const chooseFolder = async (dialog: HTMLElement, path = "C:\\code\\shop") => {
    fireEvent.change(within(dialog).getByLabelText("Source folder"), { target: { value: path } });
    await within(dialog).findByText(/Python \(12\); packages: PyPI\./);
  };

  it("takes one source folder only", async () => {
    const dialog = await openAdd();
    expect(within(dialog).getAllByLabelText("Source folder")).toHaveLength(1);
    expect(within(dialog).queryByRole("button", { name: "Add another folder" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Remove this folder" })).toBeNull();
  });

  it("suggests a name once the folder is looked at, says where it came from, and adds the project under it", async () => {
    vi.mocked(api.createProject).mockResolvedValue(project({ id: "new-aaaaaa", name: "Acme Shop" }));
    const dialog = await openAdd();
    const name = within(dialog).getByLabelText("Project name") as HTMLInputElement;
    expect(name.value).toBe("");
    expect(within(dialog).getByText("Name (optional)")).toBeInTheDocument();
    await chooseFolder(dialog);
    await waitFor(() => expect(name.value).toBe("Acme Shop"));
    expect(within(dialog).getByText("Suggested from package.json.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByLabelText("I own this code or am authorised to test it"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add the project" }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({ name: "Acme Shop", folders: ["C:\\code\\shop"], authorized: true }));
  });

  it("needs no name to add the project: an empty name is sent empty and the server suggests one", async () => {
    vi.mocked(api.createProject).mockResolvedValue(project({ id: "new-aaaaaa", name: "Acme Shop" }));
    const dialog = await openAdd();
    await chooseFolder(dialog);
    const name = within(dialog).getByLabelText("Project name") as HTMLInputElement;
    await waitFor(() => expect(name.value).toBe("Acme Shop"));
    fireEvent.change(name, { target: { value: "" } });
    expect(within(dialog).getByText("Left empty: a name is made from the folder when the project is added.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByLabelText("I own this code or am authorised to test it"));
    const create = within(dialog).getByRole("button", { name: "Add the project" });
    expect(create).toBeEnabled();
    fireEvent.click(create);
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({ name: "", folders: ["C:\\code\\shop"], authorized: true }));
  });

  it("never replaces a name the person typed, and offers the suggestion back", async () => {
    const dialog = await openAdd();
    const name = within(dialog).getByLabelText("Project name") as HTMLInputElement;
    fireEvent.change(name, { target: { value: "My own name" } });
    await chooseFolder(dialog);
    expect(name.value).toBe("My own name");
    vi.mocked(api.projectLook).mockResolvedValue({ ok: true, path: "C:\\code\\other", languages: { Python: 1 }, ecosystems: ["PyPI"], manifests: {}, files_seen: 1, truncated: false, suggested_name: "Other One", name_source: "the folder name" });
    fireEvent.change(within(dialog).getByLabelText("Source folder"), { target: { value: "C:\\code\\other" } });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: 'Use "Other One" again' })).toBeInTheDocument());
    expect(name.value).toBe("My own name");
    fireEvent.click(within(dialog).getByRole("button", { name: 'Use "Other One" again' }));
    expect(name.value).toBe("Other One");
    expect(within(dialog).getByText("Suggested from the folder name.")).toBeInTheDocument();
  });

  it("forgets what was entered when the dialog is closed, a cleared name included, so the next time a name is suggested again", async () => {
    const dialog = await openAdd();
    await chooseFolder(dialog);
    const name = within(dialog).getByLabelText("Project name") as HTMLInputElement;
    await waitFor(() => expect(name.value).toBe("Acme Shop"));
    fireEvent.change(name, { target: { value: "" } });                                         // cleared on purpose: the name now counts as typed
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add a project" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: /Add a project/ }));
    const again = await screen.findByRole("dialog", { name: "Add a project" });
    expect((within(again).getByLabelText("Source folder") as HTMLInputElement).value).toBe("");
    expect((within(again).getByLabelText("Project name") as HTMLInputElement).value).toBe("");
    await chooseFolder(again);
    await waitFor(() => expect((within(again).getByLabelText("Project name") as HTMLInputElement).value).toBe("Acme Shop"));
  });

  it("follows the folder with the suggestion while the name is untouched, and drops it when the folder is cleared", async () => {
    const dialog = await openAdd();
    const name = within(dialog).getByLabelText("Project name") as HTMLInputElement;
    await chooseFolder(dialog);
    await waitFor(() => expect(name.value).toBe("Acme Shop"));
    fireEvent.change(within(dialog).getByLabelText("Source folder"), { target: { value: "" } });
    await waitFor(() => expect(name.value).toBe(""));
  });

  it("opens a project: its folders, what cannot be done yet and why, and the audit log", async () => {
    vi.mocked(api.projectAudit).mockResolvedValue({ entries: [{ at: "2026-10-09T10:00:05Z", tool: "project", summary: "created with 1 source folder", target: "C:\\code\\shop", started: "x", ended: null, exit_status: null, evidence: null }] });
    show();
    fireEvent.click(await screen.findByRole("button", { name: /The shop/ }));
    expect(await screen.findByText(/Packages: PyPI, npm\./)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Scan the dependencies" })).toBeEnabled();
    fireEvent.click(screen.getByRole("radio", { name: /^Scan codebase/ }));
    expect(await screen.findByRole("button", { name: "Scan the code" })).toBeEnabled();
    expect(await screen.findByText("created with 1 source folder")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export" })).toHaveAttribute("href", "/api/projects/shop-ab12cd/audit/export");
  });

  it("offers the three actions and the modes of the two scans, each with what it does and does not do", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: /The shop/ }));
    const actions = await screen.findByRole("group", { name: "What to do with this project" });
    expect(within(actions).getByRole("radio", { name: /^Chat.*never touches the real files/ })).not.toBeChecked();
    expect(within(actions).getByRole("radio", { name: /^Scan dependencies.*Your own code is not analysed/ })).toBeChecked();
    expect(within(actions).getByRole("radio", { name: /^Scan codebase.*The dependencies are not checked/ })).not.toBeChecked();
    expect(await screen.findByRole("button", { name: "Scan the dependencies" })).toBeInTheDocument();

    fireEvent.click(within(actions).getByRole("radio", { name: /^Scan dependencies/ }));
    const modes = await screen.findByRole("group", { name: "How far to go" });
    expect(within(modes).getByRole("radio", { name: /^Dependencies only.*Nothing is changed and nothing is run/ })).toBeChecked();
    expect(within(modes).getByRole("radio", { name: /^Dependencies and code check.*only after you confirm the list of files/ })).not.toBeChecked();
    expect(screen.queryByText("Update dependencies")).toBeNull();
    fireEvent.click(within(modes).getByRole("radio", { name: /^Dependencies and code check/ }));
    expect(await screen.findByText("Update dependencies")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Scan the dependencies" })).toBeInTheDocument();

    fireEvent.click(within(actions).getByRole("radio", { name: /^Scan codebase/ }));
    const depths = await screen.findByRole("group", { name: "How deep to go" });
    expect(within(depths).getByRole("radio", { name: /^Quick: rules only.*No Docker needed/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Scan the code" })).toBeInTheDocument();
    fireEvent.click(within(depths).getByRole("radio", { name: /^Full: in the sandbox.*Needs Docker and the tool image/ }));
    expect(await screen.findByRole("button", { name: "Scan in the sandbox" })).toBeInTheDocument();
    expect(screen.getByLabelText("Start command")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Scan the code" })).toBeNull();
  });

  it("asks before removing a project and says the source folders are not touched", async () => {
    vi.mocked(api.deleteProject).mockResolvedValue({ ok: true });
    show();
    fireEvent.click(await screen.findByRole("button", { name: /The shop/ }));
    await screen.findByText(/Packages: PyPI, npm\./);
    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Its source folders are not touched");
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep it" }));
    await new Promise((r) => setTimeout(r, 30));
    expect(api.deleteProject).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove the project" }));
    await waitFor(() => expect(api.deleteProject).toHaveBeenCalledWith("shop-ab12cd"));
    expect(await screen.findByRole("list", { name: "Projects" })).toBeInTheDocument();
  });

  it("says when the projects cannot be read", async () => {
    vi.mocked(api.projects).mockImplementation(() => Promise.reject(new Error("offline")));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("The projects could not be read: offline");
  });
});

describe("Projects and the chat that can take the whole window", () => {
  it("opens the page of the project an address names", async () => {
    render(<ToastProvider><ConfirmProvider><Projects initialOpen="shop-ab12cd" /></ConfirmProvider></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "The shop" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All projects" })).toBeInTheDocument();
  });

  it("sends the Chat choice straight to the page that can give it the whole window, instead of showing it on this page", async () => {
    const onFocusChat = vi.fn();
    render(<ToastProvider><ConfirmProvider><Projects initialOpen="shop-ab12cd" onFocusChat={onFocusChat} /></ConfirmProvider></ToastProvider>);
    await screen.findByRole("heading", { name: "The shop" });
    fireEvent.click(screen.getByRole("radio", { name: /^Chat/ }));
    expect(onFocusChat).toHaveBeenCalledWith("shop-ab12cd");
    expect(screen.queryByLabelText("Your question")).toBeNull();
  });

  it("does nothing when the page holding it cannot open the focused chat", async () => {
    render(<ToastProvider><ConfirmProvider><Projects initialOpen="shop-ab12cd" /></ConfirmProvider></ToastProvider>);
    await screen.findByRole("heading", { name: "The shop" });
    fireEvent.click(screen.getByRole("radio", { name: /^Chat/ }));
    expect(screen.getByRole("radio", { name: /^Scan dependencies/ })).toBeChecked();
  });
});
