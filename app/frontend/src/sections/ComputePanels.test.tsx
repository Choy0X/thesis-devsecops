import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { compute: vi.fn(), computePythons: vi.fn(), putSettings: vi.fn(), storageInfo: vi.fn(), openFolder: vi.fn(), pickFolder: vi.fn(), pickFolderResult: vi.fn() } };
});
import { api, type ComputeInfo, type ComputePythons, type Settings } from "../lib/api";
import { choose } from "../test/select";
import { ToastProvider } from "../components/Toast";
import { ModelsFolderPanel } from "../components/ModelsFolderPanel";
import { ComputePanel, StoragePanel, pythonOptions, workerOptions } from "./ComputePanels";

const info = (over: Partial<ComputeInfo> = {}): ComputeInfo => ({ physical_cores: 6, logical_cores: 12, ram_gb: 16, ram_free_gb: 8, cpu_workers: 5, auto_cpu_workers: 5, max_cpu_workers: 16, gpu: { available: false, reason: "no card" }, ...over });
const GPU_ENV = "D:/code/.venv-gpu/Scripts/python.exe";
const APP_ENV = "D:/code/.venv/Scripts/python.exe";
const pythons = (over: Partial<ComputePythons> = {}): ComputePythons => ({
  automatic: GPU_ENV, chosen: "",
  pythons: [{ path: GPU_ENV, label: ".venv-gpu", cuda: true, torch: "2.4.0", cuda_build: "12.4", name: "GTX 1660", total_mb: 6144 },
    { path: APP_ENV, label: ".venv", cuda: false, reason: "torch is not installed in it" }], ...over,
});
const settings = (over: Record<string, unknown> = {}) => ({ cpu_workers: "auto", gpu_python: "", interim_dir: "", models_dir: "",
  storage: { path: "data/interim", in_effect: "data/interim", restart_required: false, exists: true, free_gb: 100, problems: [] },
  models: { default: "D:/code/data/models", in_effect: "D:/code/data/models", saved: "D:/code/data/models", exists: true }, ...over }) as unknown as Settings;
const onUpdated = vi.fn();
const wrap = (ui: React.ReactElement) => render(<ToastProvider>{ui}</ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  onUpdated.mockReset();
  vi.mocked(api.compute).mockResolvedValue(info());
  vi.mocked(api.computePythons).mockResolvedValue(pythons());
  vi.mocked(api.putSettings).mockImplementation(async (patch) => settings(patch));
});

describe("the options", () => {
  it("offers Automatic with its number, then 1 up to the logical cores, and keeps a saved number beyond them", () => {
    const base = workerOptions(info(), "auto");
    expect(base[0]).toEqual({ value: "auto", label: "Automatic (5)" });
    expect(base.map((o) => o.value).slice(1)).toEqual(Array.from({ length: 12 }, (_, i) => String(i + 1)));
    const big = workerOptions(info(), "40");
    expect(big[big.length - 1]).toEqual({ value: "40", label: "40" });
    expect(workerOptions(null, "auto")).toEqual([{ value: "auto", label: "Automatic" }, { value: "1", label: "1" }]);
  });

  it("offers Automatic first, the interpreters with the reason beside those that cannot drive the card, and a saved path that was not found", () => {
    const rows = pythonOptions(pythons(), "D:/gone/python.exe");
    expect(rows[0]).toMatchObject({ value: "", label: "Automatic: .venv-gpu" });
    expect(rows[1]).toMatchObject({ value: GPU_ENV, disabled: false, hint: "torch 2.4.0, CUDA 12.4, GTX 1660" });
    expect(rows[2]).toMatchObject({ value: APP_ENV, disabled: true, hint: "torch is not installed in it" });
    expect(rows[3]).toMatchObject({ value: "D:/gone/python.exe", label: "D:/gone/python.exe (not found)" });
    expect(pythonOptions(pythons({ automatic: null }), "")[0].label).toBe("Automatic: none found next to the code");
    expect(pythonOptions(null, "")[0].label).toBe("Automatic");
  });
});

describe("ComputePanel", () => {
  it("saves the number of worker processes at once", async () => {
    const user = userEvent.setup();
    wrap(<ComputePanel s={settings()} onUpdated={onUpdated} />);
    const trigger = await screen.findByRole("combobox", { name: "Worker processes" });
    await waitFor(() => expect(trigger).toHaveTextContent("Automatic (5)"));
    await choose(user, trigger, "8");
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ cpu_workers: "8" }));
    expect(onUpdated).toHaveBeenCalled();
  });

  it("lists the interpreters found, will not let one without CUDA be chosen, and saves the choice", async () => {
    const user = userEvent.setup();
    wrap(<ComputePanel s={settings()} onUpdated={onUpdated} />);
    const trigger = await screen.findByRole("combobox", { name: "GPU Python" });
    await waitFor(() => expect(api.computePythons).toHaveBeenCalledWith(false));
    await user.click(trigger);
    expect(await screen.findByRole("option", { name: /^\.venv(?!-)/ })).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("option", { name: /^\.venv-gpu/ }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ gpu_python: GPU_ENV }));
  });

  it("goes back to Automatic by saving an empty path", async () => {
    const user = userEvent.setup();
    wrap(<ComputePanel s={settings({ gpu_python: GPU_ENV })} onUpdated={onUpdated} />);
    const trigger = await screen.findByRole("combobox", { name: "GPU Python" });
    await choose(user, trigger, /^Automatic/);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ gpu_python: "" }));
  });

  it("looks again at the machine and at the interpreters when asked", async () => {
    wrap(<ComputePanel s={settings()} onUpdated={onUpdated} />);
    fireEvent.click(await screen.findByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.computePythons).toHaveBeenLastCalledWith(true));
    expect(api.compute).toHaveBeenCalledTimes(2);
  });

  it("says in words what is saved when it cannot be", async () => {
    const user = userEvent.setup();
    vi.mocked(api.putSettings).mockRejectedValue(new Error("that Python does not exist"));
    wrap(<ComputePanel s={settings()} onUpdated={onUpdated} />);
    await choose(user, await screen.findByRole("combobox", { name: "Worker processes" }), "2");
    expect(await screen.findByText(/Not saved: that Python does not exist/)).toBeInTheDocument();
    expect(onUpdated).not.toHaveBeenCalled();
  });
});

describe("StoragePanel", () => {
  it("checks a folder before it is saved, and saves with the word that a restart is needed", async () => {
    vi.mocked(api.storageInfo).mockResolvedValue({ path: "E:/fast", in_effect: "data/interim", restart_required: true, exists: true, free_gb: 300, problems: [] });
    wrap(<StoragePanel s={settings()} onUpdated={onUpdated} />);
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "E:/fast" } });
    fireEvent.click(screen.getByRole("button", { name: "Check folder" }));
    expect(await screen.findByText("300 GB")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ interim_dir: "E:/fast" }));
    expect(await screen.findByText("Data folder saved. Restart the app to use it.")).toBeInTheDocument();
  });

  it("opens the folder in use, and says why when it cannot", async () => {
    vi.mocked(api.openFolder).mockResolvedValue({ ok: false, detail: "C:/x does not exist yet." });
    wrap(<StoragePanel s={settings()} onUpdated={onUpdated} />);
    fireEvent.click(screen.getByRole("button", { name: "Open the folder in use" }));
    await waitFor(() => expect(api.openFolder).toHaveBeenCalledWith("interim"));
    expect(await screen.findByText("C:/x does not exist yet.")).toBeInTheDocument();
  });
});

describe("ModelsFolderPanel", () => {
  const panel = (s: Settings) => wrap(<ModelsFolderPanel s={s} onUpdated={onUpdated} />);

  it("shows the real default path, as the hint of an empty field and as a line, and what is in use", () => {
    panel(settings());
    expect(screen.getByLabelText("Folder")).toHaveAttribute("placeholder", "D:/code/data/models");
    const dl = screen.getByText("Default").closest("dl") as HTMLElement;
    expect(within(dl).getAllByText("D:/code/data/models")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Reset to default" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("says when the folder in use is not made yet", () => {
    panel(settings({ models: { default: "D:/code/data/models", in_effect: "D:/code/data/models", saved: "D:/code/data/models", exists: false } }));
    expect(screen.getByText(/\(not made yet\)/)).toBeInTheDocument();
  });

  it("saves a folder, and goes back to the default with one press", async () => {
    panel(settings({ models_dir: "E:/models" }));
    expect(screen.getByLabelText("Folder")).toHaveValue("E:/models");
    fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ models_dir: "" }));
    expect(await screen.findByText("The default models folder is used.")).toBeInTheDocument();
    expect(screen.getByLabelText("Folder")).toHaveValue("");
  });

  it("opens the folder in the file manager", async () => {
    vi.mocked(api.openFolder).mockResolvedValue({ ok: true, path: "D:/code/data/models" });
    panel(settings());
    fireEvent.click(screen.getByRole("button", { name: "Open the folder in use" }));
    await waitFor(() => expect(api.openFolder).toHaveBeenCalledWith("models"));
  });

  it("fills the field with the folder the window chose", async () => {
    vi.mocked(api.pickFolder).mockResolvedValue({ ok: true, id: "p1" });
    vi.mocked(api.pickFolderResult).mockResolvedValue({ state: "chosen", path: "F:/big-disk/models" });
    panel(settings());
    fireEvent.click(screen.getByRole("button", { name: "Browse for a folder" }));
    await waitFor(() => expect(screen.getByLabelText("Folder")).toHaveValue("F:/big-disk/models"));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});
