import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { campaigns: vi.fn() } };
});
import { api } from "../../lib/api";
import { resetCampaigns } from "../../lib/campaignsStore";
import { StartCampaignEntry } from "./StartCampaignEntry";
import { campaign } from "./fixtures";

function Where() { const l = useLocation(); return <output aria-label="address">{l.pathname}{l.search}</output>; }
const show = () => render(<MemoryRouter initialEntries={["/collections"]}><StartCampaignEntry /><Where /></MemoryRouter>);
const listing = (rows: ReturnType<typeof campaign>[]) => ({ campaigns: rows, interrupted: [], owners: {} });

beforeEach(() => { resetCampaigns(); vi.mocked(api.campaigns).mockReset(); });

describe("the way from the Collections page to the campaigns", () => {
  it("says how many campaigns are working", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign(), campaign({ campaign_id: "b", state: "pausing" }), campaign({ campaign_id: "c", state: "interrupted" }), campaign({ campaign_id: "d", state: "done" })]));
    show();
    expect(await screen.findByText("Campaigns: 2 running, 1 interrupted.")).toBeInTheDocument();
  });

  it("says when there are none, or none running", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([]));
    const { unmount } = show();
    expect(await screen.findByText("Campaigns: none yet.")).toBeInTheDocument();
    unmount();
    resetCampaigns();
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign({ state: "done" })]));
    show();
    expect(await screen.findByText("Campaigns: none running.")).toBeInTheDocument();
  });

  it("opens the start dialog on the Campaigns page, or the Campaigns page itself", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([]));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Start a campaign" }));
    expect(screen.getByLabelText("address")).toHaveTextContent("/campaigns?start=");
  });

  it("opens the list of campaigns from its other button", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([]));
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Open the campaigns" }));
    expect(screen.getByLabelText("address")).toHaveTextContent(/^\/campaigns$/);
  });
});
