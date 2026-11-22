import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { campaigns: vi.fn(), campaignDetail: vi.fn(), campaignPauseOne: vi.fn(), campaignResume: vi.fn(), campaignDelete: vi.fn(),
    campaignComputeResources: vi.fn(), campaignPerformance: vi.fn() } };
});
import { api } from "../lib/api";
import { resetCampaigns } from "../lib/campaignsStore";
import { ToastProvider } from "../components/Toast";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { DevModeProvider } from "../lib/devMode";
import { CampaignsPage } from "./CampaignsPage";
import { campaign } from "../sections/campaigns/fixtures";

function Where() { const l = useLocation(); return <output aria-label="address">{l.pathname}{l.search}</output>; }
const show = (path = "/campaigns") => render(
  <DevModeProvider initial={false}><ToastProvider><ConfirmProvider><MemoryRouter initialEntries={[path]}><CampaignsPage /><Where /></MemoryRouter></ConfirmProvider></ToastProvider></DevModeProvider>);
const listing = (rows: ReturnType<typeof campaign>[]) => ({ campaigns: rows, interrupted: rows.filter((r) => r.state === "interrupted").map((r) => r.campaign_id), owners: {} });

beforeEach(() => {
  resetCampaigns();
  for (const fn of Object.values(api)) (fn as any).mockReset();
  vi.mocked(api.campaignComputeResources).mockResolvedValue({ gpu: { cards: [] }, owner: null });
  vi.mocked(api.campaignPerformance).mockResolvedValue({ counts: {}, timings: {} });
});

describe("the Campaigns page", () => {
  it("says there are no campaigns yet", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([]));
    show();
    expect(await screen.findByText("No campaigns yet")).toBeInTheDocument();
  });

  it("lists the campaigns", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign(), campaign({ campaign_id: "cmp-20261010-bbbb", name: "PyPI", state: "done", ecosystems: ["PyPI"] })]));
    show();
    expect(await screen.findByRole("table", { name: "Campaigns" })).toBeInTheDocument();
    expect(screen.getByText("cmp-20261010-bbbb")).toBeInTheDocument();
  });

  it("says why the campaigns could not be read, with a way to try again", async () => {
    vi.mocked(api.campaigns).mockRejectedValueOnce(new Error("The store is locked.")).mockResolvedValue(listing([]));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the campaigns");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No campaigns yet")).toBeInTheDocument();
  });

  it("puts the banner for interrupted campaigns above the list", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign({ state: "interrupted" })]));
    show();
    expect(await screen.findByRole("alert", { name: "Interrupted campaigns" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Campaigns" })).toBeInTheDocument();
  });

  it("opens a campaign in the address and comes back to the list", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([campaign()]));
    vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign(), steps: [], job: null });
    show();
    fireEvent.click(await screen.findByText("npm first"));
    await waitFor(() => expect(screen.getByLabelText("address")).toHaveTextContent("/campaigns?c=cmp-20261010-aaaa"));
    expect(await screen.findByRole("heading", { name: /npm first/ })).toBeInTheDocument();
    expect(api.campaignDetail).toHaveBeenCalledWith("cmp-20261010-aaaa");
    fireEvent.click(screen.getByRole("button", { name: "All campaigns" }));
    await waitFor(() => expect(screen.getByLabelText("address")).toHaveTextContent(/^\/campaigns$/));
    expect(await screen.findByRole("table", { name: "Campaigns" })).toBeInTheDocument();
  });

  it("opens a campaign straight from an address, and says when it cannot be read", async () => {
    vi.mocked(api.campaignDetail).mockRejectedValue(new Error("No campaign cmp-nothing"));
    vi.mocked(api.campaigns).mockResolvedValue(listing([]));
    show("/campaigns?c=cmp-nothing");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the campaign");
  });

  it("shows a campaign with its state, where it is and the buttons for it", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([]));
    vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign({ state: "paused" }), steps: [], job: null });
    show("/campaigns?c=cmp-20261010-aaaa");
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("Mine the code pairs (npm), step 3 of 4")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
  });

  it("returns to the list after the campaign it shows is deleted", async () => {
    vi.mocked(api.campaigns).mockResolvedValue(listing([]));
    vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign({ state: "paused" }), steps: [], job: null });
    vi.mocked(api.campaignDelete).mockResolvedValue({ ok: true, deleted: "cmp-20261010-aaaa", kept: "" });
    show("/campaigns?c=cmp-20261010-aaaa");
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete the campaign" }));
    await waitFor(() => expect(screen.getByLabelText("address")).toHaveTextContent(/^\/campaigns$/));
  });
});
