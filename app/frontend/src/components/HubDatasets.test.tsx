import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { hubDatasetSearch: vi.fn(), hubDatasetFiles: vi.fn(), hubDatasetFetch: vi.fn(), externalCheck: vi.fn(), externalDiscard: vi.fn() } };
});
// The job console is replaced by a button that launches and then reports the job as done, so the picker's own behaviour is what is tested.
vi.mock("./RunConsole", () => ({
  RunConsole: ({ label, launch, onDone, params }: { label: string; launch: (p: any) => Promise<unknown>; onDone: () => void; params: any }) =>
    <button onClick={async () => { await launch(params); onDone(); }}>{label}</button>,
}));
import { ApiError, api, type ExternalCheck, type HubDatasetFiles } from "../lib/api";
import { HubDatasets } from "./HubDatasets";
import { ImportDataset } from "./ImportDataset";
import { ConfirmProvider } from "./hud/AlertDialog";

const SHA = "c".repeat(40);
const files = (over: Partial<HubDatasetFiles> = {}): HubDatasetFiles => ({
  id: "o/set", revision: SHA, gated: false, license: "mit", limit_bytes: 512 * 1048576, total_importable: 3,
  files: [{ path: "data/small.jsonl", size_bytes: 2048, within_limit: true }, { path: "data/mid.parquet", size_bytes: 5 * 1048576, within_limit: true }, { path: "data/huge.csv", size_bytes: 900 * 1048576, within_limit: false }], ...over });
const report: ExternalCheck = { file: "small.jsonl", filename: "small.jsonl", format: "jsonl", rows: 24, valid_rows: 24, rows_with_problems: 0, problems_total: 0, problems: [], warnings: [],
  tasks: { discovery: 12 }, splits: { train: 20, val: 4 }, families: 6, ok: true } as ExternalCheck;

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.hubDatasetSearch).mockResolvedValue({ query: "vuln", datasets: [
    { id: "o/set", downloads: 1200, likes: 3, gated: false, license: "mit", updated: null }, { id: "o/closed", downloads: 10, likes: 0, gated: true, license: null, updated: null }] });
  vi.mocked(api.hubDatasetFiles).mockResolvedValue(files());
  vi.mocked(api.hubDatasetFetch).mockResolvedValue({ id: "job1", stage: "fetch_hub_dataset", upload_id: "u".repeat(32) });
  vi.mocked(api.externalCheck).mockResolvedValue(report);
  vi.mocked(api.externalDiscard).mockResolvedValue({ discarded: true });
});

const search = async () => {
  fireEvent.change(screen.getByLabelText("Search Hugging Face datasets"), { target: { value: "vuln" } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  return screen.findByRole("list", { name: "Datasets found" });
};

describe("HubDatasets", () => {
  it("searches, lists the datasets with their downloads and licence, and fetches nothing by looking", async () => {
    render(<HubDatasets onFetched={vi.fn()} />);
    const list = await search();
    expect(api.hubDatasetSearch).toHaveBeenCalledWith("vuln");
    expect(within(list).getByText("o/set").closest("li")).toHaveTextContent("1,200 downloads, mit");
    expect(within(list).getByText("o/closed").closest("li")).toHaveTextContent("no licence listed, gated");
    fireEvent.click(screen.getByRole("button", { name: "Show the files of o/set" }));
    await screen.findByRole("list", { name: "Files that can be imported" });
    expect(api.hubDatasetFetch).not.toHaveBeenCalled();
  });

  it("says what a file costs before it is fetched, refuses one over the limit, and fetches the chosen file at the commit it was listed at", async () => {
    const onFetched = vi.fn();
    render(<HubDatasets onFetched={onFetched} />);
    await search();
    fireEvent.click(screen.getByRole("button", { name: "Show the files of o/set" }));
    const list = await screen.findByRole("list", { name: "Files that can be imported" });
    expect(screen.queryByRole("button", { name: "Fetch this file" })).toBeNull();                       // nothing chosen yet
    expect(within(list).getByLabelText("data/huge.csv")).toBeDisabled();
    expect(within(list).getByText(/900.0 MB, over the 512 MB limit/)).toBeInTheDocument();
    fireEvent.click(within(list).getByLabelText("data/mid.parquet"));
    expect(screen.getByText((_, el) => el?.tagName === "P" && /Fetch data\/mid.parquet \(5.0 MB\) from o\/set, licence mit/.test(el.textContent ?? ""))).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Fetch this file" }));
    await waitFor(() => expect(api.hubDatasetFetch).toHaveBeenCalledWith("o/set", SHA, "data/mid.parquet"));
    await waitFor(() => expect(onFetched).toHaveBeenCalledWith({ upload_id: "u".repeat(32), filename: "mid.parquet", size_bytes: 5 * 1048576 }));
  });

  it("does not offer a gated dataset's file and warns about a missing licence", async () => {
    vi.mocked(api.hubDatasetFiles).mockResolvedValue(files({ gated: true, license: null }));
    render(<HubDatasets onFetched={vi.fn()} />);
    await search();
    fireEvent.click(screen.getByRole("button", { name: "Show the files of o/set" }));
    await screen.findByText(/This dataset is gated/);
    expect(screen.getByText(/lists no licence for this dataset/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("data/small.jsonl"));
    expect(screen.queryByRole("button", { name: "Fetch this file" })).toBeNull();
  });

  it("says when the Hub cannot be reached instead of listing nothing, and when a dataset has no importable file", async () => {
    vi.mocked(api.hubDatasetSearch).mockRejectedValue(new ApiError({ status: 502, title: "Hugging Face could not be reached, so the search has no answer." }));
    render(<HubDatasets onFetched={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be reached");
    vi.mocked(api.hubDatasetSearch).mockResolvedValue({ query: "x", datasets: [{ id: "o/none", downloads: null, likes: null, gated: false, license: "mit", updated: null }] });
    vi.mocked(api.hubDatasetFiles).mockResolvedValue(files({ files: [], total_importable: 0 }));
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "Show the files of o/none" }));
    expect(await screen.findByText(/no JSONL, Parquet or CSV file that can be imported/)).toBeInTheDocument();
  });

  it("goes back from the files to the list", async () => {
    render(<HubDatasets onFetched={vi.fn()} />);
    await search();
    fireEvent.click(screen.getByRole("button", { name: "Show the files of o/set" }));
    await screen.findByRole("list", { name: "Files that can be imported" });
    fireEvent.click(screen.getByRole("button", { name: "Back to the list" }));
    expect(await screen.findByRole("list", { name: "Datasets found" })).toBeInTheDocument();
  });
});

describe("in the import dialog", () => {
  it("a fetched file is checked in words like a file of one's own, and the dataset gets a name from the file", async () => {
    render(<ConfirmProvider><ImportDataset onImported={vi.fn()} /></ConfirmProvider>);
    fireEvent.click(screen.getByRole("button", { name: "Import a file" }));
    fireEvent.click(await screen.findByRole("button", { name: "Find a dataset on Hugging Face" }));
    await search();
    fireEvent.click(screen.getByRole("button", { name: "Show the files of o/set" }));
    fireEvent.click(await screen.findByLabelText("data/small.jsonl"));
    fireEvent.click(screen.getByRole("button", { name: "Fetch this file" }));
    await waitFor(() => expect(api.externalCheck).toHaveBeenCalledWith("u".repeat(32)));
    expect(await screen.findByLabelText("Name of the dataset")).toHaveValue("small");
    expect(screen.getByText(/24 rows, 24 usable/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Find a dataset on Hugging Face" })).toBeNull();
  });
});
