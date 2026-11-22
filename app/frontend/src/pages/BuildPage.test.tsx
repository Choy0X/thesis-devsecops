import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
vi.mock("../lib/api", () => ({ api: { buildProfiles: vi.fn(), builds: vi.fn(), createBuild: vi.fn(), buildDetail: vi.fn(), retryBuild: vi.fn(), repairBuildEvidence: vi.fn(), buildEvidence: vi.fn(), qualificationWorkflows: vi.fn() }, errorText: (e: Error) => e.message }));
vi.mock('../lib/useJob', () => ({useJob: () => ({attach: vi.fn(), status: 'idle', id: null, progress: null})}));
vi.mock("../lib/dataSync", () => ({ useDataVersion: () => 0 }));
vi.mock("../sections/DatasetBuild", () => ({ BuildCard: () => null, AdmissionCounts: () => null }));
import { api } from "../lib/api";
import { BuildPage } from "./BuildPage";

beforeEach(() => {
  vi.mocked(api.qualificationWorkflows).mockResolvedValue({items: [], total: 0, offset: 0, limit: 100} as any);
  vi.mocked(api.buildProfiles).mockResolvedValue({ default_profile: "js_ts_security", profiles: [{ id: "js_ts_security", label: "JS/TS security", blockers: [], counts: { source_pairs: 12, units: 10, human_reviewed: 8 } }, { id: "all_language_security", label: "All-language security", blockers: [], counts: {} }] } as any);
  vi.mocked(api.builds).mockResolvedValue({ builds: [], total: 0, limit: 100, offset: 0 } as any);
  vi.mocked(api.buildDetail).mockResolvedValue({build: null, manifest: {}, stats: {}} as any);
});
it('resets stale version filters for a new build and follows its detail outside the listed page', async () => {
  vi.mocked(api.builds).mockResolvedValue({builds: [], total: 200, limit: 100, offset: 0} as any);
  vi.mocked(api.createBuild).mockResolvedValue({build: {tag: 'new-v2', state: 'queued', profile: 'js_ts_security', counts: {}}} as any);
  vi.mocked(api.buildDetail).mockResolvedValue({build: {tag: 'new-v2', state: 'ready', profile: 'js_ts_security', counts: {}, progress: {label: 'Publication complete'}}, manifest: {}, stats: {}} as any);
  render(<MemoryRouter initialEntries={['/build?tab=versions']}><BuildPage /></MemoryRouter>);
  fireEvent.change(await screen.findByLabelText('Search dataset versions'), {target: {value: 'old-search'}});
  fireEvent.click(screen.getByRole('button', {name: 'Next'}));
  const tab = screen.getByRole('tab', {name: 'Build dataset'});
  fireEvent.mouseDown(tab, {button: 0}); fireEvent.click(tab);
  fireEvent.click(await screen.findByRole('button', {name: 'Build dataset'}));
  expect(await screen.findByText('Ready: Publication complete')).toBeInTheDocument();
  expect(screen.getByLabelText('Search dataset versions')).toHaveValue('');
  await waitFor(() => expect(api.builds).toHaveBeenLastCalledWith({limit: 100, offset: 0, search: ''}));
  expect(api.buildDetail).toHaveBeenCalledWith('new-v2');
});
it("offers two build tabs and starts the default profile without wizard settings", async () => {
  vi.mocked(api.createBuild).mockResolvedValue({ build: { id: "build-1", tag: "v1", state: "queued", profile: "js_ts_security", counts: {} } } as any);
  render(<MemoryRouter initialEntries={["/build"]}><BuildPage /></MemoryRouter>);
  expect(await screen.findByRole("radio", { name: /JS\/TS security/ })).toHaveAttribute("aria-checked", "true");
  expect(screen.getAllByRole("tab").map(x => x.textContent)).toEqual(["Build dataset", "Dataset versions"]);
  expect(screen.queryByText("Advanced options")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Build dataset" }));
  await screen.findByText("dataset_v1");
  expect(api.createBuild).toHaveBeenCalledWith("js_ts_security");
  expect(screen.getByRole("tab", { name: "Dataset versions" })).toHaveAttribute("aria-selected", "true");
});

it("shows automatic admission estimates without a human review requirement", async () => {
  const result = await api.buildProfiles();
  vi.mocked(api.buildProfiles).mockResolvedValue({ ...result, profiles: result.profiles.map(p => ({ ...p, admission_policy: 'automatic-evidence/1', blockers: [], counts: { source_pairs: 12, units: 10, automatically_eligible: null, insufficient_context: null, excluded: null }, counts_estimated: true })) } as any);
  render(<MemoryRouter initialEntries={["/build"]}><BuildPage /></MemoryRouter>);
  expect(await screen.findByText('Automatic evidence checks. Injection share 10%.')).toBeInTheDocument();
  expect(screen.getAllByText('Calculated during build')).toHaveLength(3);
  expect(screen.queryByText(/human-reviewed/i)).not.toBeInTheDocument();
  expect(screen.queryByRole('link', {name: 'Review collected data'})).not.toBeInTheDocument();
  expect(screen.getByRole('link', {name: 'Optional context improvements'})).toHaveAttribute('href', '/pairs?tab=context');
});
it("uses readable profile and state labels for selected versions and keeps raw blockers in details", async () => {
  vi.mocked(api.builds).mockResolvedValue({ builds: [{ tag: "blocked-v1", profile: "all_language_security", state: "blocked", status: "unfrozen", blockers: [{ code: "unknown_policy_condition", detail: "Internal report" }] }], total: 1 } as any);
  render(<MemoryRouter initialEntries={["/build?tab=versions"]}><BuildPage /></MemoryRouter>);
  expect(await screen.findByText("All-language security")).toBeInTheDocument();
  expect(screen.queryByText("all_language_security")).not.toBeInTheDocument();
  expect(screen.getByText("Needs attention")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "dataset_blocked-v1" }));
  expect(screen.getByText("This build needs attention. Open build details for the diagnostic report.")).toBeInTheDocument();
  expect(screen.getByText("Build details")).toBeInTheDocument();
  expect(screen.queryByRole('button', {name: 'Retry build'})).not.toBeInTheDocument();
});

it('uses backend evidence capabilities and does not allocate unchanged blocked builds', async () => {
  const build = {tag: 'blocked-v2', state: 'blocked', counts: {}, evidence_actions: {repair: {available: true}, verify: {available: true}, rebuild: {available: false, reason: 'Evidence has not changed.'}}};
  vi.mocked(api.builds).mockResolvedValue({builds: [build], total: 1} as any);
  vi.mocked(api.buildDetail).mockResolvedValue({build} as any);
  vi.mocked(api.repairBuildEvidence).mockResolvedValue({job_id: 'repair-job'});
  render(<MemoryRouter initialEntries={['/build?tab=versions']}><BuildPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', {name: 'dataset_blocked-v2'}));
  fireEvent.click(await screen.findByRole('button', {name: 'Repair evidence'}));
  await waitFor(() => expect(api.repairBuildEvidence).toHaveBeenCalledWith('blocked-v2', 'repair'));
  expect(screen.getByRole('button', {name: 'Rebuild when changed'})).toBeDisabled();
  expect(screen.getByText('Evidence has not changed.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', {name: 'Retry verification'}));
  await waitFor(() => expect(api.repairBuildEvidence).toHaveBeenCalledWith('blocked-v2', 'verify_failed'));
  expect(api.retryBuild).not.toHaveBeenCalled();
});
