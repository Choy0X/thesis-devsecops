import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { campaigns: vi.fn(), campaignPauseOne: vi.fn(), campaignResume: vi.fn(), campaignDelete: vi.fn() } };
});
import { api } from "../../lib/api";
import { ApiError } from "../../lib/apiError";
import { resetCampaigns } from "../../lib/campaignsStore";
import { ToastProvider } from "../../components/Toast";
import { ConfirmProvider } from "../../components/hud/AlertDialog";
import { CampaignList } from "./CampaignList";
import { campaign } from "./fixtures";

const show = (rows = [campaign()], onOpen = vi.fn()) => {
  render(<ToastProvider><ConfirmProvider><CampaignList campaigns={rows} onOpen={onOpen} /></ConfirmProvider></ToastProvider>);
  return onOpen;
};

beforeEach(() => {
  resetCampaigns();
  for (const fn of Object.values(api)) (fn as any).mockReset();
  vi.mocked(api.campaigns).mockResolvedValue({ campaigns: [], interrupted: [], owners: {} });
});

describe("the campaigns table", () => {
  it("shows each campaign with its id, name, ecosystems, state, progress and time", () => {
    show([campaign({ ecosystems: ["npm", "PyPI", "Go"], finished_at: null })]);
    const table = screen.getByRole("table", { name: "Campaigns" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Campaign", "Name", "Ecosystems", "State", "Progress", "Time", ""]);
    expect(within(table).getByText("cmp-20261010-aaaa")).toBeInTheDocument();
    expect(within(table).getByText("npm first")).toBeInTheDocument();
    expect(within(table).getByText("npm")).toBeInTheDocument();
    expect(within(table).getByText("+1")).toBeInTheDocument();
    expect(within(table).getByText("Running")).toBeInTheDocument();
    const bar = within(table).getByRole("progressbar", { name: /cmp-20261010-aaaa: Mine the code pairs \(npm\), step 3 of 4/ });
    expect(bar).toHaveAttribute("aria-valuetext", "63 percent");
    expect(within(table).getByText("63%")).toBeInTheDocument();
  });

  it("offers the buttons that fit each state", () => {
    show([campaign({ campaign_id: "cmp-run" }), campaign({ campaign_id: "cmp-paused", state: "paused" }), campaign({ campaign_id: "cmp-part", state: "partial" }),
          campaign({ campaign_id: "cmp-done", state: "done" }), campaign({ campaign_id: "cmp-stopping", state: "pausing" })]);
    const buttons = (id: string) => screen.queryAllByRole("button").map((b) => b.getAttribute("aria-label")).filter((l) => l?.endsWith(id));
    expect(buttons("cmp-run")).toEqual(["Pause cmp-run"]);
    expect(buttons("cmp-paused")).toEqual(["Resume cmp-paused", "Delete cmp-paused"]);
    expect(buttons("cmp-part")).toEqual(["Retry the failed steps cmp-part", "Delete cmp-part"]);
    expect(buttons("cmp-done")).toEqual(["Delete cmp-done"]);
    expect(buttons("cmp-stopping")).toEqual([]);
  });

  it("opens a campaign when its row is clicked, but not when a button in it is", async () => {
    vi.mocked(api.campaignPauseOne).mockResolvedValue({ ok: true } as any);
    const onOpen = show();
    fireEvent.click(screen.getByRole("button", { name: "Pause cmp-20261010-aaaa" }));
    await waitFor(() => expect(api.campaignPauseOne).toHaveBeenCalledWith("cmp-20261010-aaaa"));
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("npm first"));
    expect(onOpen).toHaveBeenCalledWith("cmp-20261010-aaaa");
  });

  it("asks a running campaign to pause and says what that means", async () => {
    vi.mocked(api.campaignPauseOne).mockResolvedValue({ ok: true } as any);
    show();
    fireEvent.click(screen.getByRole("button", { name: "Pause cmp-20261010-aaaa" }));
    expect(await screen.findByText(/stops at the next safe point, saves where it is and frees the processor and memory/)).toBeInTheDocument();
    await waitFor(() => expect(api.campaigns).toHaveBeenCalled());
  });

  it("lists the reasons when a resume is refused for lack of room", async () => {
    vi.mocked(api.campaignResume).mockRejectedValue(new ApiError({ status: 409, title: "Not enough room to resume this campaign", hint: "Pause or wait for a running campaign, or choose less.",
      reasons: [{ code: "ram_free", text: "This campaign needs about 2.4 GB of memory, but only 1.5 GB is free now.", numbers: { needs: 2.4, free: 1.5 } }, { code: "disk", text: "The disk has 3 GB free." }] }));
    show([campaign({ state: "paused" })]);
    fireEvent.click(screen.getByRole("button", { name: "Resume cmp-20261010-aaaa" }));
    const dialog = await screen.findByRole("dialog", { name: "Not enough room to resume this campaign" });
    expect(within(dialog).getByText(/needs about 2.4 GB of memory/)).toBeInTheDocument();
    expect(within(dialog).getByText("needs 2.4 · free 1.5")).toBeInTheDocument();
    expect(within(dialog).getByText("The disk has 3 GB free.")).toBeInTheDocument();
    expect(within(dialog).getByText("Pause or wait for a running campaign, or choose less.")).toBeInTheDocument();
    const closers = within(dialog).getAllByRole("button", { name: "Close" });
    fireEvent.click(closers[closers.length - 1]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("shows a refusal without reasons as a toast", async () => {
    vi.mocked(api.campaignResume).mockRejectedValue(new ApiError({ status: 409, title: "Campaign x is running, so it cannot be resumed" }));
    show([campaign({ state: "interrupted" })]);
    fireEvent.click(screen.getByRole("button", { name: "Resume cmp-20261010-aaaa" }));
    expect(await screen.findByText(/Could not resume: Campaign x is running/)).toBeInTheDocument();
  });

  it("asks before deleting, says what goes and what stays, and deletes only when confirmed", async () => {
    vi.mocked(api.campaignDelete).mockResolvedValue({ ok: true, deleted: "cmp-20261010-aaaa", kept: "" });
    show([campaign({ state: "paused" })]);
    fireEvent.click(screen.getByRole("button", { name: "Delete cmp-20261010-aaaa" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("Everything it produced stays");
    expect(ask).toHaveTextContent("The ecosystems it holds become free");
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.campaignDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete cmp-20261010-aaaa" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete the campaign" }));
    await waitFor(() => expect(api.campaignDelete).toHaveBeenCalledWith("cmp-20261010-aaaa"));
  });

  it("marks a campaign of the earlier kind and shows no progress for it", () => {
    show([campaign({ campaign_id: "campaign-20260101-000000", legacy: true, state: "done", plan: null, progress: {} })]);
    expect(screen.getByText("earlier kind")).toBeInTheDocument();
    expect(screen.getByText("not recorded")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("button", { name: /Pause|Resume/ })).toBeNull();
  });
});
