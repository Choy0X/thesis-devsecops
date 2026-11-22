import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { chooseNow } from "../test/select";
import { ApiError } from "../lib/apiError";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { datasetOptions: vi.fn(), datasetPreview: vi.fn(), builds: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn() } };
});
vi.mock("../lib/dataSync", () => ({ useDataVersion: () => 0 }));
vi.mock("../components/Toast", () => ({ useToast: () => ({ success: () => undefined, error: () => undefined }) }));
import { api } from "../lib/api";
import { DatasetBuilder } from "./DatasetBuilder";

const DEFAULTS = { languages: [], severities: [], cwes: [], ecosystems: [], date_from: null, date_to: null, val_share: 0.05, train_all: false,
  safe_ratio: 2, include_untouched: false, max_chars: 8000, profile: "custom", granularity: "function", max_lines: 300, min_lines: 1, per_cve_cap: 5, collapse_backports: true, attribution: "" };
const options = { records_mined: 10, profiles: [], split: { splits: ["train", "val"], val_share: 0.05, train_all: false, rule: "" },
  window: { since: null, collected_at: null, earliest: null, latest: null }, languages: { python: 5 }, ecosystems: {}, cwes: {}, severities: {}, defaults: DEFAULTS };
const preview = { records_mined: 10, funnel: [], rows: 77, records: 3, cves: 3, commits: 3, split_counts: { train: 70, val: 7 }, split_cves: {}, by_language: {},
  by_severity: {}, top_cwe: {}, by_granularity: {}, llm_enriched: 0, warnings: [] };

/** Preview requests that answer when the test says so, and notice being cancelled. */
function previews() {
  const calls: { resolve: (v: unknown) => void; reject: (e: unknown) => void; signal?: AbortSignal }[] = [];
  vi.mocked(api.datasetPreview).mockImplementation((_f: unknown, signal?: AbortSignal) =>
    new Promise((resolve, reject) => { calls.push({ resolve, reject, signal }); }) as any);
  return calls;
}
const settle = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(10); }); await act(async () => { await vi.advanceTimersByTimeAsync(400); }); };   // options load first, then the debounce

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(api.datasetOptions).mockResolvedValue(options as any);
  vi.mocked(api.builds).mockResolvedValue({ builds: [], next_versions: { dataset: "v1", code_pairs: "v1" } } as any);
  vi.mocked(api.listJobs).mockResolvedValue([]);
});
afterEach(() => vi.useRealTimers());

describe("the choices about which unit is the vulnerable one", () => {
  it("asks for rows whose unit is named, and lets the owner keep every copy of a fix", async () => {
    const calls = previews();
    render(<DatasetBuilder sum={null} onDone={() => undefined} />);
    await settle();
    await act(async () => { calls[0].resolve(preview); });
    fireEvent.click(screen.getByRole("button", { name: /^Advanced options/ }));
    const unit = screen.getByRole("combobox", { name: "Which unit is the vulnerable one" });
    expect(unit).toHaveTextContent("all rows (the unit is not checked)");
    const collapse = screen.getByRole("checkbox", { name: /cherry-picked to several release branches/ }) as HTMLInputElement;
    expect(collapse.checked).toBe(true);
    chooseNow(unit, "named only");          // the test runs on fake timers
    fireEvent.click(collapse);
    await settle();
    const all = vi.mocked(api.datasetPreview).mock.calls;
    const last = all[all.length - 1][0] as Record<string, unknown>;
    expect(last.attribution).toBe("decision");
    expect(last.collapse_backports).toBe(false);
    expect(last.profile).toBe("custom");
  });
});

describe("the detection set's safe samples", () => {
  it("leaves out the functions the fix did not touch unless the owner asks for them as weak, unverified samples", async () => {
    const calls = previews();
    render(<DatasetBuilder sum={null} onDone={() => undefined} />);
    await settle();
    await act(async () => { calls[0].resolve(preview); });
    fireEvent.click(screen.getByRole("button", { name: /^Advanced options/ }));
    const box = screen.getByRole("checkbox", { name: /did not touch .*unverified/ }) as HTMLInputElement;
    expect(box.checked).toBe(false);
    fireEvent.click(box);
    await settle();
    const all = vi.mocked(api.datasetPreview).mock.calls;
    expect((all[all.length - 1][0] as Record<string, unknown>).include_untouched).toBe(true);
  });
});

describe("the preview of the dataset a build would make", () => {
  it("shows that it is computing, then the answer", async () => {
    const calls = previews();
    render(<DatasetBuilder sum={null} onDone={() => undefined} />);
    await settle();
    expect(screen.getByRole("status")).toHaveTextContent("Computing the preview");
    await act(async () => { calls[0].resolve(preview); });
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("77")).toBeInTheDocument();
  });

  it("keeps one request open: a newer choice cancels the older one and only the newer answer is shown", async () => {
    const calls = previews();
    render(<DatasetBuilder sum={null} onDone={() => undefined} />);
    await settle();
    expect(calls).toHaveLength(1);
    fireEvent.click(screen.getByText("python"));                    // any scope choice
    expect(calls[0].signal?.aborted).toBe(true);
    await settle();
    expect(calls).toHaveLength(2);
    await act(async () => { calls[1].resolve({ ...preview, rows: 111 }); });
    await act(async () => { calls[0].resolve({ ...preview, rows: 999 }); });          // the old answer arrives late
    expect(screen.getByText("111")).toBeInTheDocument();
    expect(screen.queryByText("999")).toBeNull();
    expect(calls.filter((c) => !c.signal?.aborted)).toHaveLength(1);
  });

  it("cancels the open request when the page is left", async () => {
    const calls = previews();
    const { unmount } = render(<DatasetBuilder sum={null} onDone={() => undefined} />);
    await settle();
    unmount();
    expect(calls[0].signal?.aborted).toBe(true);
  });

  it("says why it failed and offers to try again", async () => {
    const calls = previews();
    render(<DatasetBuilder sum={null} onDone={() => undefined} />);
    await settle();
    await act(async () => { calls[0].reject(new ApiError({ status: 500, title: "The app hit an error", detail: "disk is full" })); });
    expect(screen.getByRole("alert")).toHaveTextContent("The preview could not be computed");
    fireEvent.click(screen.getByRole("button", { name: /try again|retry/i }));
    await settle();
    expect(calls).toHaveLength(2);
  });
});
