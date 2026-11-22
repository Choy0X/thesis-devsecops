import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { servingDerived: vi.fn(), servingDerivedPin: vi.fn(), servingDerivedTrim: vi.fn(), putSettings: vi.fn() } };
});
import { api, type DerivedCache, type DerivedEntry } from "../lib/api";
import { notifyChanged } from "../lib/changes";
import { ConfirmProvider } from "./hud/AlertDialog";
import { ToastProvider } from "./Toast";
import { DerivedCachePanel } from "./DerivedCachePanel";

const GB = 1024 ** 3;
const NOW = Date.now() / 1000;
const entry = (name: string, over: Partial<DerivedEntry> = {}): DerivedEntry => ({ name, path: `D:/m/converted/${name}`, bytes: 2 * GB, files: 2, last_used: NOW - 3 * 86400, used: true, pinned: false,
  label: `org/${name}, Q4_K_M`, in_use: null, settings_roles: [], adapters: 0, ...over });
const cache = (over: Partial<DerivedCache> = {}): DerivedCache => ({ path: "D:/m/converted", budget_gb: 0, used_bytes: 4 * GB, entries: [entry("a"), entry("b", { used: false, adapters: 2 })], ...over });

const show = () => render(<ToastProvider><ConfirmProvider><DerivedCachePanel /></ConfirmProvider></ToastProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.servingDerived).mockResolvedValue(cache());
  vi.mocked(api.servingDerivedPin).mockResolvedValue({ name: "a", pinned: true });
  vi.mocked(api.servingDerivedTrim).mockResolvedValue({ removed: ["a"], freed_bytes: 2 * GB, message: "" });
  vi.mocked(api.putSettings).mockResolvedValue({} as any);
});

describe("DerivedCachePanel", () => {
  it("lists the converted models with their size, last load and the adapters made for them", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Converted models" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("org/a, Q4_K_M")).toBeInTheDocument();
    expect(within(rows[0]).getByText("2.0 GB")).toBeInTheDocument();
    expect(within(rows[0]).getByText("3 days ago")).toBeInTheDocument();
    expect(within(rows[1]).getByText("not yet")).toBeInTheDocument();
    expect(within(rows[1]).getByText("2")).toBeInTheDocument();
    expect(screen.getByText(/4\.0 GB in 2 models\. With no limit, nothing is ever deleted\./)).toBeInTheDocument();
  });

  it("says what keeps a model from being deleted", async () => {
    vi.mocked(api.servingDerived).mockResolvedValue(cache({ entries: [entry("a", { pinned: true }), entry("b", { in_use: "the model gateway has it loaded" }), entry("c", { settings_roles: ["the language model", "the embedding model"] }), entry("d")] }));
    show();
    const table = await screen.findByRole("table", { name: "Converted models" });
    const kept = within(table).getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[4].textContent);
    expect(kept).toEqual(["Pinned", "Loaded now", "Chosen for the language model and the embedding model", ""]);
  });

  it("pins and unpins by name and reads the list again", async () => {
    vi.mocked(api.servingDerived).mockResolvedValueOnce(cache()).mockResolvedValue(cache({ entries: [entry("a", { pinned: true }), entry("b", { used: false })] }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Pin org/a, Q4_K_M" }));
    await waitFor(() => expect(api.servingDerivedPin).toHaveBeenCalledWith("a", true));
    const unpin = await screen.findByRole("button", { name: "Unpin org/a, Q4_K_M" });
    expect(unpin).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(unpin);
    await waitFor(() => expect(api.servingDerivedPin).toHaveBeenCalledWith("a", false));
  });

  it("saves the budget as typed, keeps it to digits and a point, and says what no limit means", async () => {
    show();
    const input = await screen.findByLabelText("Most the converted models may take, in GB");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    fireEvent.change(input, { target: { value: "1x2.5" } });
    expect(input).toHaveValue("12.5");
    fireEvent.click(save);
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ derived_cache_gb: "12.5" }));
    expect(await screen.findByText("The budget is saved.")).toBeInTheDocument();
  });

  it("clears the limit when the budget is emptied or zero", async () => {
    vi.mocked(api.servingDerived).mockResolvedValue(cache({ budget_gb: 10 }));
    show();
    const input = await screen.findByLabelText("Most the converted models may take, in GB");
    expect(input).toHaveValue("10");
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ derived_cache_gb: "" }));
    expect(await screen.findByText("There is no limit now: nothing is deleted.")).toBeInTheDocument();
  });

  it("says a budget that was refused", async () => {
    vi.mocked(api.putSettings).mockRejectedValue(new Error("The most the converted models may take: a number of gigabytes from 0 to 100000"));
    show();
    fireEvent.change(await screen.findByLabelText("Most the converted models may take, in GB"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Not saved: .*a number of gigabytes/)).toBeInTheDocument();
  });

  it("offers to free space only when there is a budget and the cache is over it", async () => {
    vi.mocked(api.servingDerived).mockResolvedValue(cache({ budget_gb: 10 }));
    const { unmount } = show();
    expect(await screen.findByRole("button", { name: "Free space now" })).toBeDisabled();
    expect(screen.queryByText(/holds more than its limit/)).not.toBeInTheDocument();
    unmount();
    vi.mocked(api.servingDerived).mockResolvedValue(cache({ budget_gb: 3 }));
    show();
    expect(await screen.findByRole("button", { name: "Free space now" })).toBeEnabled();
    expect(screen.getByText(/holds more than its limit/)).toBeInTheDocument();
    expect(screen.getByText(/The limit is 3 GB\./)).toBeInTheDocument();
  });

  it("with no budget there is no button to free space", async () => {
    show();
    await screen.findByRole("table", { name: "Converted models" });
    expect(screen.queryByRole("button", { name: "Free space now" })).not.toBeInTheDocument();
  });

  it("asks before it deletes, deletes nothing when the answer is to keep them, and says what it freed", async () => {
    vi.mocked(api.servingDerived).mockResolvedValue(cache({ budget_gb: 3 }));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Free space now" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/pinned model, a loaded one and one that Settings chooses are kept/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep them" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.servingDerivedTrim).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Free space now" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete the oldest" }));
    await waitFor(() => expect(api.servingDerivedTrim).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Deleted 1 converted model, 2.0 GB.")).toBeInTheDocument();
  });

  it("says the reason when the cache could not be trimmed", async () => {
    vi.mocked(api.servingDerived).mockResolvedValue(cache({ budget_gb: 3 }));
    vi.mocked(api.servingDerivedTrim).mockRejectedValue(new Error("Only 1.0 GB of it can be freed. Nothing was deleted."));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Free space now" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete the oldest" }));
    expect(await screen.findByText(/Nothing was deleted\./)).toBeInTheDocument();
  });

  it("is empty with a hint when nothing was converted", async () => {
    vi.mocked(api.servingDerived).mockResolvedValue(cache({ entries: [], used_bytes: 0 }));
    show();
    expect(await screen.findByText("No converted model")).toBeInTheDocument();
    expect(screen.getByText(/Nothing has been converted here yet\./)).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Converted models" })).not.toBeInTheDocument();
  });

  it("says when the list cannot be read", async () => {
    vi.mocked(api.servingDerived).mockRejectedValue(new Error("down"));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("down");
  });

  it("reads the list again when a model is installed or deleted anywhere in the app", async () => {
    show();
    await screen.findByRole("table", { name: "Converted models" });
    expect(api.servingDerived).toHaveBeenCalledTimes(1);
    notifyChanged("models");
    await waitFor(() => expect(api.servingDerived).toHaveBeenCalledTimes(2));
  });
});
