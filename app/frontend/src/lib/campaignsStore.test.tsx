import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, api: { campaigns: vi.fn() } };
});
import { api } from "./api";
import { campaign } from "../sections/campaigns/fixtures";
import { refreshCampaigns, resetCampaigns, useCampaigns } from "./campaignsStore";

function Watch({ onRender }: { onRender?: () => void }) {
  const { campaigns, loaded, error } = useCampaigns();
  onRender?.();
  return <p>{loaded ? `${campaigns.length} campaigns` : "loading"}{error ? " (error)" : ""} {campaigns.map((c) => c.state).join(",")}</p>;
}
const listing = (rows: ReturnType<typeof campaign>[]) => ({ campaigns: rows, interrupted: rows.filter((r) => r.state === "interrupted").map((r) => r.campaign_id), owners: {} });

beforeEach(() => { vi.useFakeTimers(); resetCampaigns(); vi.mocked(api.campaigns).mockReset(); });
afterEach(() => { resetCampaigns(); vi.useRealTimers(); });

describe("the list of campaigns", () => {
  it("is asked for once for every component that shows it, and again every few seconds while a campaign works", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign()]));
    render(<><Watch /><Watch /></>);
    expect(screen.getAllByText("loading")).toHaveLength(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(screen.getAllByText("1 campaigns running")).toHaveLength(2);
    expect(api.campaigns).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(api.campaigns).toHaveBeenCalledTimes(2);
  });

  it("is asked slowly when nothing is running", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign({ state: "done" })]));
    render(<Watch />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.campaigns).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
    expect(api.campaigns).toHaveBeenCalledTimes(2);
  });

  it("does not draw again when the answer did not change", async () => {
    vi.mocked(api.campaigns).mockImplementation(async () => listing([campaign()]));
    let renders = 0;
    render(<Watch onRender={() => { renders += 1; }} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    const after = renders;
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(api.campaigns).toHaveBeenCalledTimes(2);
    expect(renders).toBe(after);
  });

  it("keeps the rows it has when a poll fails, and says so", async () => {
    vi.mocked(api.campaigns).mockResolvedValueOnce(listing([campaign()])).mockRejectedValueOnce(new Error("down")).mockResolvedValue(listing([campaign({ state: "paused" })]));
    render(<Watch />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(screen.getByText(/1 campaigns \(error\) running/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(screen.getByText("1 campaigns paused")).toBeInTheDocument();
  });

  it("is read again at once when a campaign was just started", async () => {
    vi.mocked(api.campaigns).mockResolvedValueOnce(listing([])).mockResolvedValue(listing([campaign()]));
    render(<Watch />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(screen.getByText(/0 campaigns/)).toBeInTheDocument();
    await act(async () => { await refreshCampaigns(); });
    expect(screen.getByText("1 campaigns running")).toBeInTheDocument();
  });

  it("stops asking when nothing shows it any more", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign()]));
    const { unmount } = render(<Watch />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(api.campaigns).toHaveBeenCalledTimes(1);
  });
});
