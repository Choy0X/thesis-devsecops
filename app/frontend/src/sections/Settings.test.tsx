import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { getSettings: vi.fn(), testConnection: vi.fn(), compute: vi.fn(), storageInfo: vi.fn(), putSettings: vi.fn(), localModels: vi.fn(), computePythons: vi.fn(), openFolder: vi.fn(), listJobs: vi.fn(), settingsMigrations: vi.fn(), acknowledgeSettingsMigrations: vi.fn(), dbStatus: vi.fn(), revealSecret: vi.fn(), agentPermissions: vi.fn(), agentToolchain: vi.fn(), agentMcp: vi.fn(), projects: vi.fn() } };
});
import { api } from "../lib/api";
import { choose } from "../test/select";
import { ToastProvider } from "../components/Toast";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { DevModeProvider } from "../lib/devMode";
import { Settings, type General } from "./Settings";
import { SettingsPage } from "../pages/SettingsPage";

const settings = { llm_base_url: "http://localhost:11434/v1", llm_api_key: "", llm_api_key_set: true, llm_api_key_hint: "...abcd", llm_model: "qwen",
  nvd_api_key: "", github_token: "", smtp_host: "", smtp_port: "", smtp_user: "", smtp_password: "", smtp_from: "", smtp_to: "", cpu_workers: "auto",
  interim_dir: "", database_dir: "", database_storage: { path: "data/interim/database", in_effect: "data/interim/database", restart_required: false, exists: true, free_gb: 100, problems: [] }, window_frame: "",
  storage: { path: "data/interim", in_effect: "data/interim", restart_required: false, exists: true, free_gb: 100, problems: [] }, gpu_python: "" } as any;

const show = (dev = false, group: General = "engines") => render(<MemoryRouter><DevModeProvider initial={dev}><ToastProvider><ConfirmProvider><Settings group={group} /></ConfirmProvider></ToastProvider></DevModeProvider></MemoryRouter>);
const page = (entry = "/settings") => render(<MemoryRouter initialEntries={[entry]}><DevModeProvider initial={false}><ToastProvider><ConfirmProvider><SettingsPage /></ConfirmProvider></ToastProvider></DevModeProvider></MemoryRouter>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.getSettings).mockResolvedValue(settings);
  vi.mocked(api.compute).mockResolvedValue({ physical_cores: 6, logical_cores: 12, ram_gb: 16, ram_free_gb: 8, cpu_workers: 5, auto_cpu_workers: 5, max_cpu_workers: 16, gpu: { available: false } } as any);
  vi.mocked(api.storageInfo).mockResolvedValue({} as any);
  vi.mocked(api.computePythons).mockResolvedValue({ pythons: [], automatic: null, chosen: "" });
  vi.mocked(api.localModels).mockResolvedValue({ models: [] } as any);
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.settingsMigrations).mockResolvedValue({ migrations: [] });
  vi.mocked(api.agentPermissions).mockResolvedValue({ rules: [] });
  vi.mocked(api.agentToolchain).mockResolvedValue({ platform: "windows-x86_64", tools: [] });
  vi.mocked(api.agentMcp).mockResolvedValue({ servers: [], limits: { servers: 8, timeout: 60 } });
  vi.mocked(api.projects).mockResolvedValue({ projects: [], statement: "" });
});

describe("Settings", () => {
  it("has a menu of seven groups with the current one marked, and shows one group at a time", async () => {
    const user = userEvent.setup();
    page();
    const nav = screen.getByRole("navigation", { name: "Settings groups" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(["Projects & tools", "Skills", "Data sources", "Notifications", "Storage and compute", "App and data", "Developer"]);
    expect(within(nav).getByRole("link", { name: "Projects & tools" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("heading", { name: "Command tools" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /retrieval|embed/i })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Language model" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Model providers" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "NVD" })).toBeNull();                              // the other groups are not on the page
    expect(screen.queryByRole("heading", { name: "Models folder" })).toBeNull();
    await user.click(within(nav).getByRole("link", { name: "Data sources" }));
    expect(await screen.findByRole("heading", { name: "NVD" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Data sources" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Projects & tools" })).not.toHaveAttribute("aria-current");
    expect(screen.queryByRole("heading", { name: "Command tools" })).toBeNull();
  });

  it("opens the group named in the address, and the first group for an address that names none", async () => {
    page("/settings?tab=app");
    expect(await screen.findByRole("heading", { name: "Restart and quit" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "App and data" })).toHaveAttribute("aria-current", "page");
    document.body.innerHTML = "";
    page("/settings?tab=general");                                                                 // the name of a tab of an older version
    expect(await screen.findByRole("heading", { name: "Command tools" })).toBeInTheDocument();
  });

  it("has the Developer group: the switch of Developer mode and the window frame, in cards like the others", async () => {
    vi.mocked(api.dbStatus).mockRejectedValue(new Error("The database is not running."));
    page("/settings?tab=developer");
    expect(await screen.findByRole("switch", { name: "Developer mode is off" })).toBeInTheDocument();
    expect(await screen.findByRole("combobox", { name: "Frame" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Developer" })).toHaveAttribute("aria-current", "page");
  });

  it("has a Hugging Face token panel whose connection test can name a model to check", async () => {
    show(false, "sources");
    await screen.findByRole("heading", { name: "Hugging Face" });
    const panel = screen.getByText("Hugging Face").closest("section") as HTMLElement;
    expect(within(panel).getByText("Access token")).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Test connection" }));
    const dialog = await screen.findByRole("dialog", { name: "Test Hugging Face" });
    expect(within(dialog).getByText("Model to check access to (optional)")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Check access" })).toBeInTheDocument();
  });

  it("saves the models folder and says so", async () => {
    vi.mocked(api.putSettings).mockResolvedValue({ ...settings, models_dir: "D:/models" });
    show(false, "storage");
    await screen.findByRole("heading", { name: "Models folder" });
    const panel = screen.getByText("Models folder", { selector: "h3" }).closest("section") as HTMLElement;
    const field = within(panel).getByLabelText("Folder");
    const save = within(panel).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    fireEvent.change(field, { target: { value: " D:/models " } });
    fireEvent.click(save);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ models_dir: "D:/models" }));
    expect(await screen.findByText("Models folder saved.")).toBeInTheDocument();
  });

  it("saves the embedded records folder and explains the deliberate restart", async () => {
    vi.mocked(api.putSettings).mockResolvedValue({ ...settings, database_dir: "D:/records" });
    show(false, "storage");
    const title = await screen.findByRole("heading", { name: "Database records folder" });
    const panel = title.closest("section") as HTMLElement;
    fireEvent.change(within(panel).getByLabelText("Database records folder"), { target: { value: "D:/records" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ database_dir: "D:/records" }));
    expect(await screen.findByText("Database records folder saved. Restart the app to use it.")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /restart/i })).toBeNull();
  });

  it("saves the hardware acceleration switches and the jobs at once, and says the window changes at the next start", async () => {
    const user = userEvent.setup();
    vi.mocked(api.putSettings).mockResolvedValue({ ...settings, compute_gpu: "off", ui_gpu: "off", max_jobs: "2" });
    show(false, "storage");
    await screen.findByRole("heading", { name: "Performance" });
    const panel = screen.getByText("Performance").closest("section") as HTMLElement;
    const save = within(panel).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    expect(within(panel).queryByText(/The window changes at the next start/)).toBeNull();
    await choose(user, within(panel).getByRole("combobox", { name: "Graphics card for compute" }), "Off");
    expect(within(panel).getByText(/a training or evaluation on the card is refused, a model is served on the processor/)).toBeInTheDocument();
    await choose(user, within(panel).getByRole("combobox", { name: "Graphics card for the window" }), "Off");
    expect(within(panel).getByText(/The window changes at the next start of the app/)).toBeInTheDocument();
    await choose(user, within(panel).getByRole("combobox", { name: "Jobs at once" }), "2");
    fireEvent.click(save);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ compute_gpu: "off", ui_gpu: "off", max_jobs: "2" }));
    expect(await screen.findByText("Saved. The window changes at the next start.")).toBeInTheDocument();
    expect(within(panel).getByText(/The window changes at the next start of the app/)).toBeInTheDocument();
  });

  it("tells the result of a connection test in plain words and keeps the raw answer for Developer mode", async () => {
    vi.mocked(api.testConnection).mockResolvedValue({ ok: true, detail: "The advisory source answered in 0.4 s.", response: "{\"vulnerabilities\":[]}" });
    show(false, "sources");
    const panel = (await screen.findByRole("heading", { name: "NVD" })).closest("section") as HTMLElement;
    fireEvent.click(within(panel).getByRole("button", { name: "Test connection" }));
    const dialog = await screen.findByRole("dialog", { name: "Test NVD" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Query NVD" }));
    expect(await within(dialog).findByText(/It works\. The advisory source answered in 0\.4 s\./)).toBeInTheDocument();
    expect(within(dialog).queryByText("Technical details")).toBeNull();
    expect(within(dialog).queryByText(/vulnerabilities/)).toBeNull();
  });

  it("shows the raw answer under Technical details in Developer mode", async () => {
    vi.mocked(api.testConnection).mockResolvedValue({ ok: false, detail: "The server refused the key.", response: "401 {\"error\":\"bad key\"}" });
    show(true, "sources");
    const panel = (await screen.findByRole("heading", { name: "NVD" })).closest("section") as HTMLElement;
    fireEvent.click(within(panel).getByRole("button", { name: "Test connection" }));
    const dialog = await screen.findByRole("dialog", { name: "Test NVD" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Query NVD" }));
    expect(await within(dialog).findByText(/It did not work\. The server refused the key\./)).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByText("Technical details")).toBeInTheDocument());
    expect(within(dialog).getByText(/bad key/)).toBeInTheDocument();
  });
});
