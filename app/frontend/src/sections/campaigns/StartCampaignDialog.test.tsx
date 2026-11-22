import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { campaigns: vi.fn(), campaignOptions: vi.fn(), campaignValidate: vi.fn(), campaignResources: vi.fn(), campaignCreate: vi.fn() } };
});
import { api } from "../../lib/api";
import { ApiError } from "../../lib/apiError";
import { resetCampaigns } from "../../lib/campaignsStore";
import { ToastProvider } from "../../components/Toast";
import { ConfirmProvider } from "../../components/hud/AlertDialog";
import { DevModeProvider } from "../../lib/devMode";
import { StartCampaignDialog } from "./StartCampaignDialog";
import { campaign, campaignOptions, verdict } from "./fixtures";

const show = (props: Partial<React.ComponentProps<typeof StartCampaignDialog>> = {}) => {
  const onClose = vi.fn(), onStarted = vi.fn();
  render(<DevModeProvider initial={false}><ToastProvider><ConfirmProvider><StartCampaignDialog open onClose={onClose} onStarted={onStarted} {...props} /></ConfirmProvider></ToastProvider></DevModeProvider>);
  return { onClose, onStarted };
};
const next = () => fireEvent.click(screen.getByRole("button", { name: "Next" }));
const toCheck = async () => { fireEvent.click(await screen.findByLabelText(/^npm/)); next(); await screen.findByText("Stages"); next(); };

beforeEach(() => {
  resetCampaigns();
  for (const fn of Object.values(api)) (fn as any).mockReset();
  vi.mocked(api.campaigns).mockResolvedValue({ campaigns: [], interrupted: [], owners: {} });
  vi.mocked(api.campaignOptions).mockResolvedValue(campaignOptions());
  vi.mocked(api.campaignValidate).mockResolvedValue(verdict());
  vi.mocked(api.campaignResources).mockResolvedValue({ cores: 8, cpu_cap: 7, ram_total_gb: 32, ram_free_gb: 20, ram_cap_gb: 25.6, disk_free_gb: 200, jobs_running: 1, jobs_max: 4, github_token: true,
    github_per_hour: 4500, used: { cpu: 2, ram_gb: 1.5, github_per_hour: 2200 }, campaigns: [{ campaign_id: "cmp-20261010-aaaa", name: null, state: "running", declared: {} }] });
});

describe("step 1: the ecosystems", () => {
  it("lists every dump with what can be done with it and who holds it", async () => {
    show();
    expect(await screen.findByLabelText(/^npm/)).not.toBeChecked();
    expect(screen.getByLabelText(/^Ubuntu/)).toBeEnabled();
    expect(screen.getByText("reads only")).toBeInTheDocument();
    expect(screen.getByText(/has no mining adapter/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Go/)).toBeDisabled();
    expect(screen.getByText("held by cmp-20261010-zzzz")).toBeInTheDocument();
    expect(screen.getByText("read was cut short")).toBeInTheDocument();
    expect(screen.getByText("381.5 MB")).toBeInTheDocument();
  });

  it("cannot go on until an ecosystem is chosen, and chooses all the free ones on request", async () => {
    show();
    const nextButton = await screen.findByRole("button", { name: "Next" });
    expect(nextButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Choose all" }));
    expect(screen.getByLabelText(/^npm/)).toBeChecked();
    expect(screen.getByLabelText(/^PyPI/)).toBeChecked();
    expect(screen.getByLabelText(/^Ubuntu/)).toBeChecked();
    expect(screen.getByLabelText(/^Go/)).not.toBeChecked();
    expect(nextButton).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(nextButton).toBeDisabled();
  });

  it("starts with the ecosystems it was opened with, except one that is held", async () => {
    show({ initialEcosystems: ["PyPI", "Go", "Atlantis"] });
    expect(await screen.findByLabelText(/^PyPI/)).toBeChecked();
    expect(screen.getByLabelText(/^Go/)).not.toBeChecked();
    expect(screen.getByLabelText(/^npm/)).not.toBeChecked();
  });

  it("says there is nothing to work on when no dump is here", async () => {
    vi.mocked(api.campaignOptions).mockResolvedValue(campaignOptions({ targets: [] }));
    show();
    expect(await screen.findByText("There is no dump on this computer yet")).toBeInTheDocument();
  });

  it("says why the options could not be read, with a way to try again", async () => {
    vi.mocked(api.campaignOptions).mockRejectedValueOnce(new Error("down")).mockResolvedValue(campaignOptions());
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read what a campaign can use");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText(/^npm/)).toBeInTheDocument();
  });
});

describe("step 2: the stages and settings", () => {
  it("starts from the Collect preset with its stages on, and shows the settings of those stages", async () => {
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next();
    expect(await screen.findByLabelText("Collect")).toBeChecked();
    const on = (name: RegExp) => (screen.getAllByRole("checkbox") as HTMLInputElement[]).filter((c) => name.test(c.closest("label")?.textContent ?? "")).map((c) => c.checked);
    expect(on(/^ingest/)).toEqual([true]);
    expect(on(/^verify/)).toEqual([false]);
    expect(screen.getByLabelText(/^Commits mined at once/)).toHaveValue("4");
    expect(screen.getByLabelText(/^Most records to recover/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^GitHub requests at once/)).toHaveValue("8");
    expect(screen.queryByLabelText(/^Registries asked at once/)).toBeNull();
    expect(screen.getByText(/Model-written context is a separate manual step/)).toBeInTheDocument();
  });

  it("switches the stages with a preset, and calls any other set Custom", async () => {
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next();
    fireEvent.click(await screen.findByLabelText("Full with model-written context"));
    expect(screen.getByLabelText(/^Registries asked at once/)).toBeInTheDocument();
    expect(screen.getAllByText(/Model-written context/).length).toBeGreaterThan(0);
    expect(screen.getByLabelText(/^Requests at once/)).toHaveValue("2");
    fireEvent.click(screen.getByRole("checkbox", { name: /^verify/ }));
    expect(screen.getByRole("radio", { name: "Custom" })).toBeChecked();
    fireEvent.click(screen.getByLabelText("Ingest only"));
    expect(screen.queryByLabelText(/^Commits mined at once/)).toBeNull();
    expect(screen.getByLabelText(/^Ask OSV for a newer dump first/)).toBeInTheDocument();
  });

  it("offers the dataset profile only when the build stage is on", async () => {
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next();
    expect(screen.queryByText("Draft dataset")).toBeNull();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Build a draft dataset/ }));
    expect(screen.getByText("Draft dataset")).toBeInTheDocument();
    expect(screen.getByLabelText(/Training \(recommended\)/)).toBeChecked();
    fireEvent.click(screen.getByLabelText(/Final model/));
    expect(screen.getByLabelText(/Final model/)).toBeChecked();
  });

  it("cannot go on with every stage off", async () => {
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next();
    for (const box of (await screen.findAllByRole("checkbox")).filter((c) => (c as HTMLInputElement).checked)) fireEvent.click(box);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });
});

describe("step 3: the check and the start", () => {
  it("judges the plan that was chosen and shows what runs and what it needs", async () => {
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next();
    await screen.findByText("Stages");
    fireEvent.change(screen.getByLabelText(/^Commits mined at once/), { target: { value: "6" } });
    next();
    expect(await screen.findByText("This plan can start now.")).toBeInTheDocument();
    expect(api.campaignValidate).toHaveBeenLastCalledWith(expect.objectContaining({ ecosystems: ["npm"], preset: "collect", knobs: expect.objectContaining({ mine_workers: 6 }) }));
    expect(screen.getByText("ingest, queue, recover, mine")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "What the campaign needs and what is free" });
    expect(within(table).getByText("Processor cores")).toBeInTheDocument();
    expect(within(table).getAllByText("Fits").length).toBe(5);
    expect(screen.getByText(/Campaigns running now: cmp-20261010-aaaa/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start the campaign" })).toBeEnabled();
  });

  it("lists what is wrong, with the sentences of the server, and does not let the campaign start", async () => {
    vi.mocked(api.campaignValidate).mockResolvedValue(verdict({ ok: false,
      errors: [{ code: "ram_free", text: "This campaign needs about 2.4 GB of memory, but only 1.5 GB is free now.", source: "machine" }, { code: "no_dump", text: "Go has no dump on this computer.", source: "plan" }],
      warnings: [{ code: "github_no_token", text: "There is no GitHub token, so commits are fetched with git only." }],
      needs: { cpu: 2, ram_gb: 2.4, github_per_hour: 2800, disk_gb: 15, context: false, build: false }, free: { cpu: 5, ram_gb: 1.5, github_per_hour: 4500, disk_gb: 180, jobs: 4 } }));
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next(); await screen.findByText("Stages"); next();
    expect(await screen.findByText("This plan cannot start yet.")).toBeInTheDocument();
    const problems = screen.getByRole("list", { name: "Problems" });
    expect(within(problems).getByText(/needs about 2.4 GB of memory/)).toBeInTheDocument();
    expect(within(problems).getByText(/Go has no dump/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Notes" })).toHaveTextContent("no GitHub token");
    expect(screen.getByText("Short by 0.9 GB")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start the campaign" })).toBeDisabled();
  });

  it("judges the plan again when a choice changes after going back", async () => {
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next(); await screen.findByText("Stages"); next();
    await screen.findByText("This plan can start now.");
    expect(api.campaignValidate).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(await screen.findByLabelText("Collect and verify"));
    next();
    await waitFor(() => expect(api.campaignValidate).toHaveBeenCalledTimes(2));
    expect(api.campaignValidate).toHaveBeenLastCalledWith(expect.objectContaining({ preset: "collect_verify" }));
  });

  it("starts the campaign that was judged and tells the page which one", async () => {
    vi.mocked(api.campaignCreate).mockResolvedValue({ ok: true, campaign: campaign({ campaign_id: "cmp-20261010-cccc" }), job: {} as any, warnings: [] });
    const { onStarted } = show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next(); await screen.findByText("Stages"); next();
    await screen.findByText("This plan can start now.");
    fireEvent.click(screen.getByRole("button", { name: "Start the campaign" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith("cmp-20261010-cccc"));
    const judged = vi.mocked(api.campaignValidate).mock.calls;
    expect(api.campaignCreate).toHaveBeenCalledWith(judged[judged.length - 1][0]);
    expect(await screen.findByText("Campaign cmp-20261010-cccc started.")).toBeInTheDocument();
  });

  it("shows the reasons when the start is refused after all, and keeps the dialog open", async () => {
    vi.mocked(api.campaignCreate).mockRejectedValue(new ApiError({ status: 409, title: "Not enough room to start this campaign",
      reasons: [{ code: "cpu", text: "This campaign needs 2 processor cores and the campaigns running use 4 of the 5 that can be spared." }] }));
    const { onStarted, onClose } = show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next(); await screen.findByText("Stages"); next();
    await screen.findByText("This plan can start now.");
    fireEvent.click(screen.getByRole("button", { name: "Start the campaign" }));
    expect(await screen.findByText(/use 4 of the 5 that can be spared/)).toBeInTheDocument();
    expect(onStarted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("says why the check could not be made, with a way to try again", async () => {
    vi.mocked(api.campaignValidate).mockRejectedValueOnce(new Error("down")).mockResolvedValue(verdict());
    show({ initialEcosystems: ["npm"] });
    await screen.findByLabelText(/^npm/);
    next(); await screen.findByText("Stages"); next();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not check the plan");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("This plan can start now.")).toBeInTheDocument();
  });

  it("can be cancelled from any step", async () => {
    const { onClose } = show();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalled();
  });
});
