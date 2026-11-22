import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { enrichCandidates: vi.fn(), datasetOptions: vi.fn() } };
});
vi.mock("../lib/dataSync", () => ({ useDataVersion: () => 0, useDataSync: () => ({ refresh: () => undefined }) }));
import { api } from "../lib/api";
import { EnrichPanel } from "./EnrichPanel";

/** A candidates request that answers when the test says so, and notices being cancelled. */
function pending() {
  const calls: { resolve: (v: unknown) => void; signal?: AbortSignal; offset: number }[] = [];
  vi.mocked(api.enrichCandidates).mockImplementation((_f: unknown, _limit?: number, offset = 0, signal?: AbortSignal) =>
    new Promise((resolve) => { calls.push({ resolve, signal, offset }); }) as any);
  return calls;
}
const page = (total: number) => ({ total, rows: [], missing_labels: {}, counts: {}, levels: {} }) as any;

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(api.datasetOptions).mockResolvedValue({ defaults: {}, languages: [], severities: [], cwes: [], ecosystems: [] } as any);
});
afterEach(() => vi.useRealTimers());

describe("superseded candidate requests", () => {
  it("are cancelled when the filters change and their late answer is ignored", async () => {
    const calls = pending();
    render(<EnrichPanel onDone={() => undefined} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    expect(calls).toHaveLength(1);
    const first = calls[0];
    fireEvent.change(screen.getAllByRole("spinbutton")[0], { target: { value: "300" } });      // any filter change
    expect(first.signal?.aborted).toBe(true);                       // the old request was cancelled at once
    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    expect(calls).toHaveLength(2);
    await act(async () => { calls[1].resolve(page(2)); });
    await act(async () => { first.resolve(page(99)); });            // the old answer arrives late
    expect(screen.queryByText(/99/)).toBeNull();
  });
});
