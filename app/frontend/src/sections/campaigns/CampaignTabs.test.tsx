import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { campaigns: vi.fn(), campaignDetail: vi.fn(), campaignEvents: vi.fn(), campaignPauseOne: vi.fn(), campaignResume: vi.fn(), campaignDelete: vi.fn(),
    campaignComputeResources: vi.fn(), campaignPerformance: vi.fn(), stopCampaignGpuOwner: vi.fn() } };
});
const jobState = { id: null as string | null, status: "running", logs: [] as any[], earlier: 0, error: "", progress: null as any, failure: null, attach: vi.fn(), showEarlier: vi.fn(), run: vi.fn(), cancel: vi.fn() };
vi.mock("../../lib/useJob", () => ({ useJob: () => jobState }));
vi.mock("../CampaignContextItems", () => ({ CampaignContextItems: () => <div>Collected items ready for review</div> }));
vi.mock('./CampaignSources', () => ({CampaignSources: () => <div>Source retry and skip decisions</div>}));
import { api } from "../../lib/api";
import { resetCampaigns } from "../../lib/campaignsStore";
import { ToastProvider } from "../../components/Toast";
import { ConfirmProvider } from "../../components/hud/AlertDialog";
import { DevModeProvider } from "../../lib/devMode";
import { CampaignDetail } from "./CampaignDetail";
import { campaign } from "./fixtures";

const step = (over: Record<string, any>) => ({ target: "npm", stage: "ingest", status: "done", started_at: "2026-10-10T10:00:00Z", finished_at: "2026-10-10T10:02:05Z", detail: {}, error: null, attempts: 1, progress: {}, ...over });
const steps = [
  step({ stage: "ingest", detail: { rows: 12345, advisories: 900 } }), step({ stage: "queue", finished_at: "2026-10-10T10:02:30Z" }),
  step({ stage: "recover", status: "skipped", detail: { skipped: "no GitHub token: link recovery needs one" } }),
  step({ stage: "mine", status: "error", finished_at: "2026-10-10T10:20:00Z", error: "ConnectionError: the network is down", attempts: 2 }),
  step({ target: "*", stage: "build", status: "pending", started_at: null, finished_at: null, attempts: 0 }),
];
const tile = (label: string) => screen.getAllByText(label).find((e) => e.tagName === "P")!.parentElement!;
const show = (path = "/campaigns?c=cmp-20261010-aaaa", dev = false) => {
  const onBack = vi.fn(), onDeleted = vi.fn();
  render(<DevModeProvider initial={dev}><ToastProvider><ConfirmProvider><MemoryRouter initialEntries={[path]}><CampaignDetail id="cmp-20261010-aaaa" onBack={onBack} onDeleted={onDeleted} /></MemoryRouter></ConfirmProvider></ToastProvider></DevModeProvider>);
  return { onBack, onDeleted };
};

beforeEach(() => {
  resetCampaigns();
  for (const fn of Object.values(api)) (fn as any).mockReset();
  jobState.attach.mockReset(); jobState.logs = []; jobState.progress = null; jobState.status = "running";
  vi.mocked(api.campaigns).mockResolvedValue({ campaigns: [], interrupted: [], owners: {} });
  vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign({ state: "partial", finished_at: "2026-10-10T10:30:00Z", resumes: 1, error: "1 step(s) failed" }), steps: steps as any, job: null });
  vi.mocked(api.campaignEvents).mockResolvedValue({ events: [], last: 0 });
  vi.mocked(api.campaignComputeResources).mockResolvedValue({gpu: {cards: []}, owner: null});
  vi.mocked(api.campaignPerformance).mockResolvedValue({counts: {}, timings: {}});
});

describe("the funnel of a draft dataset in the overview", () => {
  it("shows it under the steps when the build reported one, and not otherwise", async () => {
    const withBuild = [...steps.slice(0, 4), step({ target: "*", stage: "build", status: "done", detail: { profile: "training", rows: 22, funnel: [{ step: "usable pairs of mined records", rows: 85 }, { step: "test files excluded", rows: 56 }], duplicates_removed: 3 } })];
    vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign({ state: "done", finished_at: "2026-10-10T10:30:00Z" }), steps: withBuild as any, job: null });
    show();
    expect(await screen.findByRole("table", { name: "Where the rows of the draft dataset went" })).toBeInTheDocument();
    expect(screen.getByText("test files excluded")).toBeInTheDocument();
  });

  it("is absent for a campaign that built nothing", async () => {
    show();
    await screen.findByText("Steps done");
    expect(screen.queryByRole("table", { name: "Where the rows of the draft dataset went" })).toBeNull();
  });
});

describe("the overview of a campaign", () => {
  it("counts the steps done and failed, the time and what it was admitted with", async () => {
    show();
    await screen.findByText("Steps done");
    expect(tile("Steps done")).toHaveTextContent("3 of 5");
    expect(tile("Steps done")).toHaveTextContent("1 failed");
    expect(tile("Time")).toHaveTextContent("30:00");
    expect(tile("Time")).toHaveTextContent("resumed 1 time");
    expect(tile("Admitted with")).toHaveTextContent("2 cores");
    expect(screen.getByRole("alert")).toHaveTextContent("1 step(s) failed");
  });

  it("lists every step with its state in words, its time and what it did or why it failed", async () => {
    show();
    const table = await screen.findByRole("table", { name: "The steps of the campaign" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(5);
    expect(rows[0]).toHaveTextContent("Read the advisories");
    expect(rows[0]).toHaveTextContent("Done");
    expect(rows[0]).toHaveTextContent("2 min 5 s");
    expect(rows[0]).toHaveTextContent("rows 12,345, advisories 900");
    expect(rows[2]).toHaveTextContent("Skipped");
    expect(rows[2]).toHaveTextContent("no GitHub token");
    expect(rows[3]).toHaveTextContent("Failed");
    expect(rows[3]).toHaveTextContent("ConnectionError: the network is down");
    expect(rows[3]).toHaveTextContent("2");
    expect(rows[4]).toHaveTextContent("Whole campaign");
    expect(rows[4]).toHaveTextContent("Waiting");
  });

  it("offers Retry for a partly done campaign", async () => {
    show();
    expect(await screen.findByRole("button", { name: "Retry the failed steps" })).toBeInTheDocument();
  });
});

describe("the log of a campaign", () => {
  const events = [
    { id: 1, at: "2026-10-10T10:00:00Z", kind: "created", data: { ecosystems: ["npm"] } }, { id: 2, at: "2026-10-10T10:00:05Z", kind: "started", data: { steps: 4, resumes: 0 } },
    { id: 3, at: "2026-10-10T10:20:00Z", kind: "stage_error", data: { target: "npm", stage: "mine", cause: "network", message: "down" } },
  ];

  it("tells the story in sentences, oldest first, and marks the problems", async () => {
    vi.mocked(api.campaignEvents).mockResolvedValue({ events, last: 3 });
    show("/campaigns?c=cmp-20261010-aaaa&tab=log");
    const list = await screen.findByRole("list", { name: "Events" });
    await waitFor(() => expect(within(list).getAllByRole('listitem').map(item => item.getAttribute('data-kind'))).toEqual(['created', 'started', 'stage_error']));
    const items = within(list).getAllByRole("listitem");
    expect(items.map((i) => i.getAttribute("data-kind"))).toEqual(["created", "started", "stage_error"]);
    expect(items[0]).toHaveTextContent("2026-10-10 10:00 UTC");
    expect(items[0]).toHaveTextContent("Campaign created for npm.");
    expect(items[2]).toHaveTextContent("Problem");
    expect(items[2]).toHaveTextContent("Failed: Mine the code pairs (npm), network: down");
    expect(api.campaignEvents).toHaveBeenCalledWith("cmp-20261010-aaaa", 0, 500);
  });

  it("follows the process's job and shows its output, or says none is kept", async () => {
    jobState.logs = [{ id: 1, text: "mining 4 fix commits", tone: "plain" }, { id: 2, text: "x: failed - boom", tone: "error" }];
    show("/campaigns?c=cmp-20261010-aaaa&tab=log");
    const log = await screen.findByRole("log", { name: "Output of the process" });
    expect(within(log).getByText("mining 4 fix commits")).toBeInTheDocument();
    expect(jobState.attach).toHaveBeenCalledWith("job1");
  });

  it("says no output is kept when the run is over and nothing was written", async () => {
    jobState.status = "done";
    show("/campaigns?c=cmp-20261010-aaaa&tab=log");
    expect(await screen.findByText("No output is kept for this run.")).toBeInTheDocument();
  });

  it("says the campaign has no process yet", async () => {
    vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign({ job_id: null }), steps: [], job: null });
    show("/campaigns?c=cmp-20261010-aaaa&tab=log");
    expect(await screen.findByText("The campaign has no process yet.")).toBeInTheDocument();
  });

  it("says why the story could not be read, with a way to try again", async () => {
    vi.mocked(api.campaignEvents).mockRejectedValueOnce(new Error("down")).mockResolvedValue({ events, last: 3 });
    show("/campaigns?c=cmp-20261010-aaaa&tab=log");
    expect(await screen.findByText("Could not read the story")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Events" })).toBeInTheDocument();
  });
});

describe("the config of a campaign", () => {
  it("shows the plan as it was chosen, the stages on and off, the settings and what it was admitted with", async () => {
    show("/campaigns?c=cmp-20261010-aaaa&tab=config");
    expect(await screen.findByText("As it was chosen when the campaign started.")).toBeInTheDocument();
    expect(screen.getByText("collect")).toBeInTheDocument();
    const on = (stage: RegExp) => screen.getAllByRole("listitem").find((li) => stage.test(li.textContent ?? ""))!.getAttribute("data-on");
    expect(on(/^On\s*Mine the code pairs/)).toBe("true");
    expect(on(/^Off\s*Check the fixes/)).toBe("false");
    expect(screen.getByText("Commits mined at once").nextSibling).toHaveTextContent("4");
    expect(screen.getByText("Processor cores").nextSibling).toHaveTextContent("2");
    expect(screen.getByText("Memory").nextSibling).toHaveTextContent("1.5 GB");
  });

  it("shows the model and the profile when those stages are on, and says so for a campaign of the earlier kind", async () => {
    vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign({ plan: { ...campaign().plan!, stages: { enrich: true, build: true }, build_profile: "final", context: { profile: "all", workers: 3, cap: 500 } } }), steps: [], job: null });
    show("/campaigns?c=cmp-20261010-aaaa&tab=config");
    expect(await screen.findByText("Dataset profile")).toBeInTheDocument();
    expect(screen.getByText(/all, 3 requests at once, at most 500 pairs per ecosystem/)).toBeInTheDocument();
  });

  it("explains a campaign of the earlier kind", async () => {
    vi.mocked(api.campaignDetail).mockResolvedValue({ campaign: campaign({ legacy: true, plan: null, state: "done" }), steps: [], job: null });
    show("/campaigns?c=cmp-20261010-aaaa&tab=config");
    expect(await screen.findByText(/made by the earlier kind of campaign/)).toBeInTheDocument();
  });

  it("names the run_config file in Developer mode only", async () => {
    show("/campaigns?c=cmp-20261010-aaaa&tab=config", true);
    expect(await screen.findByText(/experiments\/campaigns\/cmp-20261010-aaaa\/run_config.json/)).toBeInTheDocument();
  });
});

describe("the tabs", () => {
  it('replaces the generic retry button with source decisions when collection checks finished', async () => {
    vi.mocked(api.campaignDetail).mockResolvedValue({
      campaign: campaign({state: 'partial', summary: {unresolved_items: 40}}),
      steps: [step({stage: 'mine'}), step({stage: 'verify'})] as any, job: null,
      context_review: {ready: true, reason: null, unresolved_sources: 40, drafts_only: true},
    });
    show('/campaigns?c=cmp-20261010-aaaa&tab=context');
    expect(await screen.findByText('Source retry and skip decisions')).toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Retry the failed steps'})).toBeNull();
  });
  it("allows backend-approved review of a partial campaign and keeps missing evidence visible", async () => {
    vi.mocked(api.campaignDetail).mockResolvedValue({
      campaign: campaign({ state: 'partial', summary: { unresolved_items: 40 } }),
      steps: steps as any, job: null,
      context_review: { ready: true, reason: null, unresolved_sources: 40, drafts_only: true },
    });
    show('/campaigns?c=cmp-20261010-aaaa&tab=context');
    expect(await screen.findByText('Collected items ready for review')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('40 source commits remain unresolved');
    expect(screen.getByRole('status')).toHaveTextContent('automatic evidence checks');
  });

  it("keeps context blocked when collection or cleanup is incomplete", async () => {
    vi.mocked(api.campaignDetail).mockResolvedValue({
      campaign: campaign({ state: 'partial' }), steps: steps as any, job: null,
      context_review: { ready: false, reason: 'Finish collection checks before context review.', unresolved_sources: 40, drafts_only: true },
    });
    show('/campaigns?c=cmp-20261010-aaaa&tab=context');
    expect(await screen.findByText('Finish collection checks before context review.')).toBeInTheDocument();
    expect(screen.queryByText('Collected items ready for review')).toBeNull();
  });

  it("move between the overview, the log and the config, and keep the tab in the address", async () => {
    show();
    await screen.findByRole("table", { name: "The steps of the campaign" });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Config" }), { button: 0 });
    expect(await screen.findByText("As it was chosen when the campaign started.")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Overview" }), { button: 0 });
    await waitFor(() => expect(screen.getByRole("table", { name: "The steps of the campaign" })).toBeInTheDocument());
  });
});
