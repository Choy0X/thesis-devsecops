import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { listJobs: vi.fn(), resetInventory: vi.fn(), resetStart: vi.fn(), resetStatus: vi.fn() } };
});
import { ApiError, api, type Job, type ResetCategory, type ResetInventory, type ResetStatus } from "../lib/api";
import { resetJobsList } from "../lib/jobsList";
import { HardResetDialog, HardResetPanel, selectedSize } from "./HardResetPanel";

const cat = (id: string, label: string, bytes: number, over: Partial<ResetCategory> = {}): ResetCategory => ({ id, label, note: `Note of ${label}.`, default: true, warning: null, present: true, bytes, files: 3,
  paths: [], blocked: null, databases: [], ...over });
const CATEGORIES: ResetCategory[] = [
  cat("mongo", "MongoDB databases", 1_500_000_000, { files: 2, databases: [{ name: "aivf", bytes: 1_000_000_000, documents: 12_000, collections: 20 }, { name: "aivf_scale", bytes: 500_000_000, documents: 3_000, collections: 5 }] }),
  cat("raw", "Downloaded sources", 2_500_000_000),
  cat("logs", "Logs", 0, { present: false, files: 0 }),
  cat("db_backups", "Database backups", 340),
  cat("models", "Models", 5_100_000_000, { default: false, warning: "Not ticked: every model goes." }),
  cat("tools", "Installed tools", 900_000, { default: false, warning: "Not ticked: llama.cpp must be installed again." }),
  cat("settings", "Settings and keys", 150, { default: false, warning: "Not ticked: every key and token is deleted." }),
  cat("sandbox", "Sandbox and verification pilot", 50, { blocked: "D:/elsewhere is outside the project folder, so it is not offered here." }),
];
const inventory = (over: Partial<ResetInventory> = {}): ResetInventory => ({ running: false, done: true, token: "tok1", taken: 1, progress: { category: "", files: 0, bytes: 0 }, error: null, root: "D:/p",
  categories: CATEGORIES, docker_note: "The databases live in a volume of the Docker container. The volume file does not get smaller.", confirm_word: "RESET", ...over });
const run = (over: Partial<ResetStatus> = {}): ResetStatus => ({ running: false, done: true, started: 1, steps: [], ok: true, error: null, pending: 0, categories: [], ...over });
const job = (stage: string): Job => ({ id: "j", stage, status: "running", params: {}, started: "", finished: "", error: "", log_count: 0 });
const close = vi.fn();
const show = () => render(<HardResetDialog open onClose={close} pollMs={5} />);

beforeEach(() => {
  resetJobsList();
  close.mockReset();
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.resetInventory).mockResolvedValue(inventory());
  vi.mocked(api.resetStart).mockResolvedValue(run({ done: false, running: true }));
  vi.mocked(api.resetStatus).mockResolvedValue(run({ steps: [{ id: "raw", label: "Downloaded sources", ok: true, detail: "5 items removed" }] }));
});

const row = (name: string) => screen.getByRole("checkbox", { name });
const typeWord = (v = "RESET") => fireEvent.change(screen.getByLabelText(/Type RESET to continue/), { target: { value: v } });

describe("selectedSize", () => {
  it("adds up the sizes of the chosen categories only", () => {
    expect(selectedSize(CATEGORIES, new Set(["raw", "db_backups"]))).toBe(2_500_000_340);
    expect(selectedSize(CATEGORIES, new Set())).toBe(0);
  });
});

describe("the list", () => {
  it("measures first, with its progress, then shows every category with its exact size", async () => {
    vi.mocked(api.resetInventory).mockResolvedValueOnce(inventory({ done: false, token: null, categories: [], progress: { category: "raw", files: 1200, bytes: 4_000_000 } })).mockResolvedValue(inventory());
    show();
    expect(await screen.findByText(/Measuring what is stored/)).toHaveTextContent("raw: 1,200 files");
    const list = await screen.findByRole("list", { name: "What can be deleted" });
    expect(within(list).getAllByRole("checkbox")).toHaveLength(8);
    expect(api.resetInventory).toHaveBeenNthCalledWith(1, true);
    expect(screen.getByLabelText("Downloaded sources: size")).toHaveTextContent("2.3 GB");
    expect(screen.getByLabelText("MongoDB databases: size")).toHaveTextContent("1.4 GB");
    const dbs = screen.getByRole("list", { name: "Databases" });
    expect(dbs).toHaveTextContent("aivf_scale");
    expect(dbs).toHaveTextContent("12,000 documents");
  });

  it("ticks what is meant to be deleted by default and leaves the models, the tools and the keys unticked, each with its warning", async () => {
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    for (const name of ["MongoDB databases", "Downloaded sources", "Database backups"]) expect(row(name)).toBeChecked();
    for (const name of ["Models", "Installed tools", "Settings and keys"]) expect(row(name)).not.toBeChecked();
    expect(screen.getByText("Not ticked: every key and token is deleted.")).toBeInTheDocument();
    expect(screen.getByText("Not ticked: every model goes.")).toBeInTheDocument();
  });

  it("cannot tick a category with nothing in it, or one that is not offered, and says why", async () => {
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    expect(row("Logs")).toBeDisabled();
    expect(screen.getByText(/Nothing is stored here\. Note of Logs\./)).toBeInTheDocument();
    expect(row("Sandbox and verification pilot")).toBeDisabled();
    expect(screen.getByText(/outside the project folder, so it is not offered here/)).toBeInTheDocument();
  });

  it("totals what is chosen, ticks everything that can go with Delete everything, and goes back with Default choice", async () => {
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    expect(screen.getByText(/Chosen:/).closest("p")).toHaveTextContent("3.7 GB in 3 categories");
    fireEvent.click(screen.getByRole("button", { name: "Delete everything" }));
    for (const name of ["Models", "Installed tools", "Settings and keys", "MongoDB databases"]) expect(row(name)).toBeChecked();
    expect(row("Logs")).not.toBeChecked();
    expect(row("Sandbox and verification pilot")).not.toBeChecked();
    expect(screen.getByText(/Chosen:/).closest("p")).toHaveTextContent("8.5 GB in 6 categories");
    fireEvent.click(screen.getByRole("button", { name: "Default choice" }));
    expect(row("Models")).not.toBeChecked();
    expect(screen.getByText(/Chosen:/).closest("p")).toHaveTextContent("in 3 categories");
  });

  it("says the Docker volume does not shrink and that the code and the environments are never deleted", async () => {
    show();
    expect(await screen.findByText(/volume file does not get smaller/)).toHaveTextContent("never deleted");
  });

  it("says why the list could not be made", async () => {
    vi.mocked(api.resetInventory).mockResolvedValue(inventory({ done: false, token: null, categories: [], error: "OSError: the disk is not readable" }));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("The list could not be made: OSError: the disk is not readable");
  });
});

describe("starting", () => {
  it("keeps Delete disabled until the word is typed exactly and something is chosen", async () => {
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    const button = screen.getByRole("button", { name: "Delete what is chosen" });
    expect(button).toBeDisabled();
    for (const v of ["reset", "RESE", "RESET now", ""]) { typeWord(v); expect(button).toBeDisabled(); }
    typeWord("RESET");
    expect(button).toBeEnabled();
    for (const name of ["MongoDB databases", "Downloaded sources", "Database backups"]) fireEvent.click(row(name));
    expect(button).toBeDisabled();
  });

  it("will not start while a job runs, and names the job", async () => {
    vi.mocked(api.listJobs).mockResolvedValue([job("mine_code")]);
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Mine code pairs"));
    typeWord();
    expect(screen.getByRole("button", { name: "Delete what is chosen" })).toBeDisabled();
  });

  it("sends the token, the chosen categories, the word and the backup choice, and follows the steps to the end", async () => {
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Back up the databases first" }));
    fireEvent.click(row("Models"));
    typeWord();
    fireEvent.click(screen.getByRole("button", { name: "Delete what is chosen" }));
    await waitFor(() => expect(api.resetStart).toHaveBeenCalledWith({ token: "tok1", categories: ["mongo", "raw", "db_backups", "models"], confirm: "RESET", backup: true }));
    const steps = await screen.findByRole("list", { name: "Steps of the reset" });
    await waitFor(() => expect(steps).toHaveTextContent("5 items removed"));
    expect(await screen.findByText(/Done\. The app is empty now\./)).toBeInTheDocument();
  });

  it("offers the backup only when the databases are chosen, and does not send it otherwise", async () => {
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    expect(screen.getByRole("checkbox", { name: "Back up the databases first" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Back up the databases first" }));
    fireEvent.click(row("MongoDB databases"));
    expect(screen.queryByRole("checkbox", { name: "Back up the databases first" })).toBeNull();
    typeWord();
    fireEvent.click(screen.getByRole("button", { name: "Delete what is chosen" }));
    await waitFor(() => expect(api.resetStart).toHaveBeenCalledWith(expect.objectContaining({ backup: false, categories: ["raw", "db_backups"] })));
  });

  it("says what could not be deleted yet, and offers to restart the app", async () => {
    vi.mocked(api.resetStatus).mockResolvedValue(run({ ok: false, pending: 2, steps: [{ id: "logs", label: "Logs", ok: false, detail: "2 items removed; 2 could not be removed and will be tried again at the next start" }] }));
    const seen: string[] = [];
    window.addEventListener("app-reboot-request", () => seen.push("reboot"), { once: true });
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    typeWord();
    fireEvent.click(screen.getByRole("button", { name: "Delete what is chosen" }));
    expect(await screen.findByText(/Done, with problems/)).toHaveTextContent("2 items are in use and will be deleted at the next start");
    fireEvent.click(screen.getByRole("button", { name: "Restart the app" }));
    expect(seen).toEqual(["reboot"]);
    expect(close).toHaveBeenCalled();
  });

  it("measures again when the server says the list is no longer the current one", async () => {
    vi.mocked(api.resetStart).mockRejectedValue(new ApiError({ status: 409, title: "The list on the page is not the current one.", code: "stale" }));
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    typeWord();
    fireEvent.click(screen.getByRole("button", { name: "Delete what is chosen" }));
    expect(await screen.findByText(/was out of date\. It has been measured again; choose again\./)).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(api.resetInventory).mock.calls.filter((c) => c[0] === true).length).toBe(2));
    expect(api.resetStatus).not.toHaveBeenCalled();
  });

  it("shows the refusal of the server in its words and starts nothing", async () => {
    vi.mocked(api.resetStart).mockRejectedValue(new ApiError({ status: 409, title: "Jobs are running (campaign_run).", code: "jobs_running" }));
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    typeWord();
    fireEvent.click(screen.getByRole("button", { name: "Delete what is chosen" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Jobs are running (campaign_run).");
    expect(api.resetStatus).not.toHaveBeenCalled();
  });

  it("cancels without starting anything", async () => {
    show();
    await screen.findByRole("list", { name: "What can be deleted" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(close).toHaveBeenCalled();
    expect(api.resetStart).not.toHaveBeenCalled();
  });
});

describe("HardResetPanel", () => {
  it("opens the dialog from a button and says what is never touched", async () => {
    render(<HardResetPanel />);
    expect(screen.getByText(/never ticked for you/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Hard reset..." })); });
    expect(await screen.findByRole("dialog", { name: "Hard reset" })).toBeInTheDocument();
    expect(api.resetInventory).toHaveBeenCalledWith(true);
  });
});
