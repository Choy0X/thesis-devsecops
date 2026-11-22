import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickOption } from "../test/select";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { projects: vi.fn(), projectScans: vi.fn(), projectScan: vi.fn(), respondProject: vi.fn(), projectCandidate: vi.fn() } };
});
vi.mock("../components/RunConsole", () => ({
  RunConsole: ({ label, launch, disabled, onDone }: any) => <button type="button" disabled={disabled} onClick={() => launch?.({}).then(() => onDone?.())}>{label}</button>,
}));
import { api, type Project, type ResponseReport } from "../lib/api";
import { Response } from "./Response";

const project = (id: string, name: string) => ({ id, name } as Project);
const empty: ResponseReport = { schema: "project-scan/3", kind: "response", scan_id: "20261010-120500-abcd", project_id: "shop-ab12cd", started: "x", finished: "2026-10-10T12:05:00Z", scope: "dependencies", mode: "log", depth: "quick",
  options: { assist: false, allow_remote: false, test: true }, email: null, notes: [], findings: [], summary: { findings: 0, by_class: { Critical: 0, Warning: 0, Recommendation: 0, Unknown: 0 }, by_action: {} }, words: "w" };

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.projects).mockResolvedValue({ projects: [project("shop-ab12cd", "The shop"), project("blog-cd34ef", "The blog")], statement: "s" });
  vi.mocked(api.projectScans).mockResolvedValue({ scans: [] });
  vi.mocked(api.respondProject).mockResolvedValue({ id: "j1", stage: "respond_project" });
});

describe("the response orchestrator page", () => {
  it("asks for a project first when there is none", async () => {
    vi.mocked(api.projects).mockResolvedValue({ projects: [], statement: "s" });
    render(<Response />);
    expect(await screen.findByText("No project yet")).toBeInTheDocument();
  });

  it("offers the scope, the depth for code, and the mode, each with what it does and does not do", async () => {
    render(<Response />);
    const scope = await screen.findByRole("group", { name: "What to look at" });
    expect(within(scope).getByRole("radio", { name: /^Both.*one report/ })).toBeChecked();
    const mode = screen.getByRole("group", { name: "What to do about the findings" });
    expect(within(mode).getByRole("radio", { name: /^Log.*Nothing leaves this computer/ })).toBeChecked();
    expect(within(mode).getByRole("radio", { name: /^Email.*only if mail is set up in Settings/ })).not.toBeChecked();
    expect(within(mode).getByRole("radio", { name: /^Patch candidates.*Nothing is applied to your folders/ })).not.toBeChecked();
    expect(screen.getByRole("group", { name: "How deep to scan the code" })).toBeInTheDocument();
    fireEvent.click(within(scope).getByRole("radio", { name: /^Dependencies/ }));
    expect(screen.queryByRole("group", { name: "How deep to scan the code" })).toBeNull();
  });

  it("starts a run with the choices made, and nothing is sent to a model that is not here unless that is ticked", async () => {
    render(<Response />);
    await screen.findByRole("group", { name: "What to look at" });
    await pickOption("Project", "The blog");
    fireEvent.click(screen.getByRole("radio", { name: /^Dependencies/ }));
    fireEvent.click(screen.getByRole("radio", { name: /^Patch candidates/ }));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(api.respondProject).toHaveBeenCalledWith("blog-cd34ef", { scope: "dependencies", mode: "auto-patch", depth: "quick", assist: false, allow_remote: false, start_command: "", port: null }));
    fireEvent.click(screen.getByLabelText(/Estimate a class for a finding that has no score/));
    fireEvent.click(screen.getByLabelText(/The model may be one that is not on this computer/));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(api.respondProject).toHaveBeenLastCalledWith("blog-cd34ef", expect.objectContaining({ assist: true, allow_remote: true })));
  });

  it("asks how the project starts only for the full code scan, and refuses a port that is not a port", async () => {
    render(<Response />);
    await screen.findByRole("group", { name: "What to look at" });
    expect(screen.queryByLabelText("Start command")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: /^Full: in the sandbox/ }));
    fireEvent.change(screen.getByLabelText("Start command"), { target: { value: "python app.py" } });
    fireEvent.change(screen.getByLabelText("Port"), { target: { value: "80" } });
    expect(screen.getByText("A port is a whole number from 1024 to 65535.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Port"), { target: { value: "8000" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(api.respondProject).toHaveBeenCalledWith("shop-ab12cd", expect.objectContaining({ depth: "full", start_command: "python app.py", port: 8000 })));
  });

  it("shows the newest report of the project, or says there is none", async () => {
    render(<Response />);
    expect(await screen.findByText("This project has had no response run yet.")).toBeInTheDocument();
    vi.mocked(api.projectScans).mockResolvedValue({ scans: [{ scan_id: "20261010-120500-abcd", kind: "response", finished: "x", summary: empty.summary }] });
    vi.mocked(api.projectScan).mockResolvedValue(empty);
    await pickOption("Project", "The blog");
    expect(await screen.findByText(/Nothing was found by the checks that ran/)).toBeInTheDocument();
    expect(api.projectScan).toHaveBeenCalledWith("blog-cd34ef", "20261010-120500-abcd");
  });
});
