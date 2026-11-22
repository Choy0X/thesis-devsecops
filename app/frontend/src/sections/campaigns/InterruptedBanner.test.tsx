import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { campaigns: vi.fn(), campaignResume: vi.fn() } };
});
import { api } from "../../lib/api";
import { resetCampaigns } from "../../lib/campaignsStore";
import { ToastProvider } from "../../components/Toast";
import { ConfirmProvider } from "../../components/hud/AlertDialog";
import { InterruptedBanner } from "./InterruptedBanner";
import { campaign } from "./fixtures";

const show = (rows: ReturnType<typeof campaign>[], onOpen = vi.fn()) => {
  render(<ToastProvider><ConfirmProvider><InterruptedBanner campaigns={rows} onOpen={onOpen} /></ConfirmProvider></ToastProvider>);
  return onOpen;
};

beforeEach(() => {
  resetCampaigns();
  vi.mocked(api.campaigns).mockReset().mockResolvedValue({ campaigns: [], interrupted: [], owners: {} });
  vi.mocked(api.campaignResume).mockReset();
});

describe("the banner for campaigns that were interrupted", () => {
  it("shows nothing when no campaign was interrupted", () => {
    show([campaign(), campaign({ campaign_id: "cmp-2", state: "paused" })]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says what happened and that nothing restarted by itself", () => {
    show([campaign({ state: "interrupted" })]);
    const banner = screen.getByRole("alert", { name: "Interrupted campaigns" });
    expect(banner).toHaveTextContent("A campaign was interrupted");
    expect(banner).toHaveTextContent("nothing restarted by itself");
    expect(banner).toHaveTextContent("Mine the code pairs (npm), step 3 of 4, 63%");
  });

  it("resumes a campaign with one click and opens it from its id", async () => {
    vi.mocked(api.campaignResume).mockResolvedValue({ ok: true } as any);
    const onOpen = show([campaign({ state: "interrupted" }), campaign({ campaign_id: "cmp-20261010-bbbb", state: "interrupted", name: null, ecosystems: ["PyPI"] })]);
    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("2 campaigns were interrupted");
    fireEvent.click(within(banner).getByRole("button", { name: "Resume cmp-20261010-bbbb" }));
    await waitFor(() => expect(api.campaignResume).toHaveBeenCalledWith("cmp-20261010-bbbb"));
    fireEvent.click(within(banner).getByRole("button", { name: "cmp-20261010-aaaa" }));
    expect(onOpen).toHaveBeenCalledWith("cmp-20261010-aaaa");
  });

  it("leaves campaigns of the earlier kind out", () => {
    show([campaign({ state: "interrupted", legacy: true, plan: null })]);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
