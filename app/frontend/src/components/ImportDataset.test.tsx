import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { externalUpload: vi.fn(), externalCheck: vi.fn(), externalImport: vi.fn(), externalDiscard: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { ApiError, api, type ExternalCheck } from "../lib/api";
import { ConfirmProvider } from "./hud/AlertDialog";
import { resetStreams } from "../lib/jobStreams";
import { ImportDataset, nameFrom } from "./ImportDataset";

class InertSocket { onopen: any; onmessage: any; onclose: any; onerror: any; constructor(public url: string) {} close() {} send() {} }

const report = (over: Partial<ExternalCheck> = {}): ExternalCheck => ({ file: "data.jsonl", filename: "data.jsonl", format: "jsonl", rows: 24, valid_rows: 24, rows_with_problems: 0, problems_total: 0, problems: [], warnings: [],
  tasks: { discovery: 12, repair: 6, condition_assessment: 6 }, splits: { train: 20, val: 4 }, families: 6, ok: true, ...over });
const file = (name = "My Data.jsonl") => new File(['{"task":"discovery"}\n'], name, { type: "application/json" });
const show = (onImported = vi.fn()) => { render(<ConfirmProvider><ImportDataset onImported={onImported} /></ConfirmProvider>); return onImported; };
const choose = async (f: File) => {
  fireEvent.click(screen.getByRole("button", { name: "Import a file" }));
  const input = await screen.findByLabelText("File");
  fireEvent.change(input, { target: { files: [f] } });
};

beforeEach(() => {
  vi.stubGlobal("WebSocket", InertSocket);
  resetStreams();
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.externalUpload).mockResolvedValue({ upload_id: "u".repeat(32), filename: "My Data.jsonl", size_bytes: 3 * 1048576, sha256: "a".repeat(64) });
  vi.mocked(api.externalCheck).mockResolvedValue(report());
  vi.mocked(api.externalDiscard).mockResolvedValue({ discarded: true });
  vi.mocked(api.listJobs).mockResolvedValue([]);
});
afterEach(() => { vi.unstubAllGlobals(); resetStreams(); });

describe("nameFrom", () => {
  it("makes a name of lower-case letters, digits and underscores from a file name", () => {
    expect(nameFrom("My Data.jsonl")).toBe("my_data");
    expect(nameFrom("export (v2).final.csv")).toBe("export_v2_final");
    expect(nameFrom("___.csv")).toBe("data");
    expect(nameFrom("x".repeat(80) + ".csv")).toHaveLength(40);
  });
});

describe("ImportDataset", () => {
  it("sends the file, shows what is in it, offers a name made from the file's and says what the import is", async () => {
    show();
    await choose(file());
    expect(await screen.findByText(/24 rows, 24 usable/)).toBeInTheDocument();
    expect(screen.getByText(/discovery 12, repair 6, condition_assessment 6; train 20, val 4; 6 families\./)).toBeInTheDocument();
    expect(vi.mocked(api.externalUpload).mock.calls[0][0].name).toBe("My Data.jsonl");
    expect(api.externalCheck).toHaveBeenCalledWith("u".repeat(32));
    expect(screen.getByLabelText("Name of the dataset")).toHaveValue("my_data");
    expect(screen.getByText(/It will be called dataset_ext_my_data\./)).toBeInTheDocument();
    expect(screen.getByText(/never called reviewed or verified, and a run on it is not\s+comparable with a run on a frozen version/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("lists the problems in words and offers no import for a file that has one", async () => {
    vi.mocked(api.externalCheck).mockResolvedValue(report({ ok: false, valid_rows: 22, rows_with_problems: 2, problems_total: 60, problems: ["Row 3: 'task' is 'nothing'; it must be one of discovery.", "Row 9: 'response' is missing."] }));
    show();
    await choose(file());
    const list = await screen.findByRole("list", { name: "Problems" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Row 3: 'task' is 'nothing'; it must be one of discovery.", "Row 9: 'response' is missing."]);
    expect(screen.getByRole("alert")).toHaveTextContent("60 problems found, so nothing can be imported from this file (the first 2 are listed)");
    expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
    expect(screen.queryByLabelText("Name of the dataset")).toBeNull();
  });

  it("says what a run on the data would lack, as a warning that does not stop the import", async () => {
    vi.mocked(api.externalCheck).mockResolvedValue(report({ warnings: ["There is no row for repair. Training needs at least one row of each of discovery, condition_assessment, repair, so a run on this data is refused until they are added."] }));
    show();
    await choose(file());
    expect(await screen.findByRole("list", { name: "Warnings" })).toHaveTextContent("There is no row for repair");
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("says why the file was refused when it cannot be sent or is the wrong kind", async () => {
    vi.mocked(api.externalUpload).mockRejectedValue(new ApiError({ status: 400, title: "run.exe is not a JSONL, Parquet or CSV file." }));
    show();
    await choose(file("run.exe"));
    expect(await screen.findByRole("alert")).toHaveTextContent("run.exe is not a JSONL, Parquet or CSV file.");
    expect(api.externalCheck).not.toHaveBeenCalled();
  });

  it("keeps the import disabled for a name the server would refuse and cleans the name as it is typed", async () => {
    show();
    await choose(file());
    const name = await screen.findByLabelText("Name of the dataset");
    fireEvent.change(name, { target: { value: "Bad Name!" } });
    expect(name).toHaveValue("badname");
    fireEvent.change(name, { target: { value: "" } });
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
    fireEvent.change(name, { target: { value: "ok_name_2" } });
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("starts the import job with the upload and the name", async () => {
    vi.mocked(api.externalImport).mockResolvedValue({ id: "job7", stage: "import_external", tag: "ext_my_data" });
    vi.mocked(api.getJob).mockResolvedValue({ id: "job7", stage: "import_external", status: "running", logs: [] } as any);
    show();
    await choose(file());
    await screen.findByLabelText("Name of the dataset");
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(api.externalImport).toHaveBeenCalledWith("u".repeat(32), "my_data"));
  });

  it("throws the uploaded file away when the dialog is closed without importing it", async () => {
    show();
    await choose(file());
    await screen.findByLabelText("Name of the dataset");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(api.externalDiscard).toHaveBeenCalledWith("u".repeat(32)));
  });
});
