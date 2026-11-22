import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { choose } from "../test/select";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { builds: vi.fn(), lineage: vi.fn(), mix: vi.fn(), reviewSummary: vi.fn(), splitCheck: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: () => "ws://test" };
});
vi.mock("./Review", () => ({ Review: () => <div>the review queue</div> }));
import { api, type BuildInfo } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { BuildWizard, injectionValid } from "./BuildWizard";
import { Stepper } from "../components/hud/Stepper";

const build = (tag: string, status: string): BuildInfo => ({
  tag, status, path: `data/processed/dataset_${tag}`, counts: { records: 120, groups: 40, views: 300, duplicates: 7 },
  tier_mix: { unreviewed: 10, auto_checked: 100, llm_adjudicated: 0, human_reviewed: 10 }, versions: {}, blocked: {}, files: {},
});

const show = (builds: BuildInfo[] = [], start?: string) => {
  vi.mocked(api.builds).mockResolvedValue({ builds, next_version: "v2", next_versions: {} });
  return render(<MemoryRouter><ConfirmProvider><BuildWizard start={start} onOther={() => undefined} /></ConfirmProvider></MemoryRouter>);
};
const next = () => fireEvent.click(screen.getByRole("button", { name: "Next" }));

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.reviewSummary).mockResolvedValue({ rows: 500, candidates: 480, auto_checked: 300, tiers: {}, failed: {}, not_evaluated: [], decisions: 12, audit_id: null, rubric_version: "rubric_v1", audit_error_rate: "unestimated" });
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.lineage).mockResolvedValue({ stages: [{ id: "advisories", label: "Vulnerability advisories", unit: "advisories", count: 90, lost: [] }] });
  vi.mocked(api.mix).mockResolvedValue({ scope: "dataset_preview", total: 3, language: { a: 1, b: 1, c: 1 }, ecosystem: { npm: 3 }, severity: null });
  vi.mocked(api.splitCheck).mockResolvedValue({ split_versions: [] });
});

describe("Stepper", () => {
  it("marks the current step, and lets only reached steps be chosen", () => {
    const onSelect = vi.fn();
    render(<Stepper steps={[{ id: "a", label: "First" }, { id: "b", label: "Second" }, { id: "c", label: "Third", optional: true }]} current={1} reached={1} onSelect={onSelect} />);
    expect(screen.getByRole("button", { name: /Second/ })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("button", { name: /First.*done/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Third \(optional\)/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /First/ }));
    expect(onSelect).toHaveBeenCalledWith(0);
  });
});

describe("BuildWizard", () => {
  it("starts with the task and the counts of the source", async () => {
    show();
    expect(await screen.findByText("Reviewed dataset")).toBeInTheDocument();
    expect(await screen.findByText("480")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Task and source/ })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("button", { name: /Readiness and checks/ })).toBeDisabled();
  });

  it("keeps the settings when going back and forth", async () => {
    show();
    await screen.findByText("Reviewed dataset");
    next();
    await choose(userEvent.setup(), await screen.findByRole("combobox", { name: "Lowest review tier" }), "human reviewed");
    fireEvent.change(screen.getByLabelText("Share with an injection variant"), { target: { value: "0.25" } });
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await screen.findByText("Reviewed dataset");
    next();
    expect(await screen.findByRole("combobox", { name: "Lowest review tier" })).toHaveTextContent("human reviewed");
    expect(screen.getByLabelText("Share with an injection variant")).toHaveValue("0.25");
  });

  it("does not go on while the injection share is not a number from 0 to 1", async () => {
    show();
    await screen.findByText("Reviewed dataset");
    next();
    fireEvent.change(await screen.findByLabelText("Share with an injection variant"), { target: { value: "1.5" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a number from 0 to 1.");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(injectionValid("0")).toBe(true);
    expect(injectionValid("")).toBe(false);
    expect(injectionValid("1.01")).toBe(false);
  });

  it("allows direct immutable publishing without a preview build", async () => {
    show([]);
    await screen.findByText("Reviewed dataset");
    next(); next();
    expect(await screen.findByText('Security readiness')).toBeInTheDocument();
    expect(screen.queryByRole('button', {name: /Build a preview/})).toBeNull();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("shows the preview's counts and tier mix, and the review queue as an optional step", async () => {
    show([build("v1", "frozen"), build("preview", "preview")]);
    await screen.findByText("Reviewed dataset");
    next(); next();
    expect(await screen.findByText(/dataset_preview/)).toBeInTheDocument();
    expect(await screen.findByRole("listitem", { name: "Vulnerability advisories: 90 advisories" })).toBeInTheDocument();
    expect(api.mix).toHaveBeenCalledWith("preview");
    expect(screen.getByText("120")).toBeInTheDocument();
    next();
    expect(await screen.findByText("the review queue")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Review \(optional\)/ })).toHaveAttribute("aria-current", "step");
  });

  it("asks before it freezes, then starts the build with the chosen settings", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "build_reviewed_dataset", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "build_reviewed_dataset", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show([build("preview", "preview")], "freeze");
    expect(await screen.findByRole("heading", { name: /Freeze as dataset_v2/ })).toBeInTheDocument();
    expect(screen.getByText("human reviewed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Build and freeze" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("dataset_v2");
    expect(api.startJob).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Build and freeze" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("build_reviewed_dataset", { freeze: true, injection_rate: 0.1, min_tier: "human_reviewed" }));
    vi.unstubAllGlobals();
  });

  it("opens on the step named in the address", async () => {
    show([build("preview", "preview")], "review");
    expect(await screen.findByText("the review queue")).toBeInTheDocument();
  });
});
