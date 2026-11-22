import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, api: { dataStamp: vi.fn() }, clearRequestCache: vi.fn() };
});
import { api } from "./api";
import { DataSyncProvider, SyncButton, resetSyncStatus, useDataSync, useDataVersion } from "./dataSync";

let renders = 0;
function Panel() {
  const v = useDataVersion(0);
  const sync = useDataSync();
  renders++;
  return <button onClick={sync.refresh}>panel v{v}</button>;
}

const stamp = (value: string, running: string[] = []) => vi.mocked(api.dataStamp).mockResolvedValue({ stamp: value, running });
async function tick(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }

beforeEach(() => { vi.useFakeTimers(); renders = 0; resetSyncStatus(); vi.mocked(api.dataStamp).mockReset(); });
afterEach(() => vi.useRealTimers());

describe("DataSyncProvider", () => {
  it("renders its panels again only when the data really changed", async () => {
    stamp("a");
    render(<DataSyncProvider enabled><Panel /></DataSyncProvider>);
    await tick(10);
    const afterFirstPoll = renders;
    await tick(16_000);                                              // two more polls, same stamp
    await tick(16_000);
    expect(vi.mocked(api.dataStamp).mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(renders).toBe(afterFirstPoll);
    stamp("b");
    await tick(16_000);
    await tick(10);                                                  // the panel's own throttle timer runs after the render
    expect(renders).toBeGreaterThan(afterFirstPoll);
    expect(screen.getByRole("button")).toHaveTextContent("panel v1");
  });

  it("reloads once for a manual refresh even when the stamp changed at the same time", async () => {
    stamp("a");
    render(<DataSyncProvider enabled><Panel /></DataSyncProvider>);
    await tick(10);
    stamp("b");                                                      // a job finished and wrote data
    fireEvent.click(screen.getByRole("button"));
    await tick(2000);
    expect(screen.getByRole("button")).toHaveTextContent("panel v1");     // one reload, not two
    await tick(16_000);
    expect(screen.getByRole("button")).toHaveTextContent("panel v1");
  });

  it("polls only while enabled", async () => {
    stamp("a");
    const { rerender } = render(<DataSyncProvider enabled={false}><Panel /></DataSyncProvider>);
    await tick(40_000);
    expect(api.dataStamp).not.toHaveBeenCalled();
    rerender(<DataSyncProvider enabled><Panel /></DataSyncProvider>);
    await tick(10);
    expect(api.dataStamp).toHaveBeenCalledTimes(1);
  });
});

describe("SyncButton", () => {
  it("shows the running stages and the last check without re-rendering the panels", async () => {
    stamp("a", ["mine_code"]);
    render(<DataSyncProvider enabled><SyncButton /><Panel /></DataSyncProvider>);
    await tick(10);
    expect(screen.getByText(/live · mining/)).toBeInTheDocument();
    const before = renders;
    stamp("a", ["mine_code", "ingest"]);
    await tick(4000);
    expect(screen.getByText(/live · mining, ingest/)).toBeInTheDocument();
    expect(renders).toBe(before);
  });

  it("shows when it last checked", async () => {
    stamp("a");
    render(<DataSyncProvider enabled><SyncButton /></DataSyncProvider>);
    expect(screen.getByText("syncing…")).toBeInTheDocument();
    await tick(10);
    expect(screen.getByText(/^synced /)).toBeInTheDocument();
  });
});
