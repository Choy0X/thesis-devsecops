import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { verifyPilot: vi.fn(), verifyReceipts: vi.fn(), verifyReceipt: vi.fn(), verifyCases: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() },
    sandbox: { status: vi.fn() }, wsUrl: () => "ws://test" };
});
vi.mock("./Sandbox", () => ({ Sandbox: () => <div>the containers</div> }));
import { api, sandbox, type PilotSummary } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { DevModeProvider } from "../lib/devMode";
import { SandboxPage } from "../pages/SandboxPage";
import { Verification } from "./Verification";

const summary: PilotSummary = {
  cases_attempted: 20, verified: 12, reached_a_verdict: 20, reproduced_before_the_fix: 20, seconds: 2706,
  outcomes: { verified: 12, fixed_but_functionality_failed: 1, not_reproduced: 0, acceptance_needs_network: 1 }, libraries: ["a/b", "c/d", "e/f"],
};
const receipt = (over = {}) => ({
  receipt_id: "r1", verdict: "verified", grade: "G3", reasons: [], runner: { kind: "docker", fixture_only: false, image_digest: "sha256:aa" },
  steps: { apply: { status: "passed" }, build: { status: "passed" }, security_before: { status: "failed" }, security_after: { status: "passed" }, functionality: { status: "passed" }, integrity: { status: "passed" } }, ...over,
});

const show = (dev = false, node = <Verification />) => render(<MemoryRouter><DevModeProvider initial={dev}><ConfirmProvider>{node}</ConfirmProvider></DevModeProvider></MemoryRouter>);

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(sandbox.status).mockReset();
  vi.mocked(sandbox.status).mockResolvedValue({ docker_available: true });
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.verifyCases).mockResolvedValue({ cases: [{ case_id: "pilot_expressjs_express_0867302d", library: "expressjs/express", outcome: "verified", evidence_level: "strong" }, { case_id: "pilot_ws_ws_11111111", library: "websockets/ws", outcome: "fix_did_not_pass", evidence_level: "moderate" }] });
  vi.mocked(api.verifyPilot).mockResolvedValue({ ran: true, summary });
  vi.mocked(api.verifyReceipts).mockResolvedValue({ receipts: [
    { name: "pilot_expressjs_express_0867302d", readable: true, case_id: "rec_1", verdict: "verified", grade: "G3", runner: "docker", fixture_only: false, sound: true },
    { name: "pilot_ljharb_shell-quote_aaaaaaaa", readable: true, case_id: "rec_2", verdict: "not_verified", grade: "G2", runner: "docker", fixture_only: false, sound: false },
    { name: "pilot_bad", readable: false },
  ] });
  vi.mocked(api.verifyReceipt).mockResolvedValue({ name: "x", receipt: receipt() as any, problems: [] });
});

describe("Verification", () => {
  it("states what the pilot found in numbers and words, leaving out outcomes that did not happen", async () => {
    show();
    expect(await screen.findByText("12 of 20")).toBeInTheDocument();
    expect(screen.getByText(/from 3 libraries, in about 45 minutes/)).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Outcomes" });
    expect(within(list).getByText(/Verified: the vulnerability showed before the fix/)).toBeInTheDocument();
    expect(within(list).getByText("The security test needs the network, which the container does not have")).toBeInTheDocument();
    expect(within(list).queryByText(/did not show on the old code/)).toBeNull();
    expect(screen.getByText(/measures the method on real fixes/)).toBeInTheDocument();
  });

  it("says when no pilot has been run and that running one is a deliberate step", async () => {
    vi.mocked(api.verifyPilot).mockResolvedValue({ ran: false, summary: null });
    vi.mocked(api.verifyReceipts).mockResolvedValue({ receipts: [] });
    show();
    expect(await screen.findByText("No pilot has been run yet")).toBeInTheDocument();
    expect(screen.getByText("No receipts yet")).toBeInTheDocument();
  });

  it("lists the receipts with the case name, the result in words and whether the receipt is intact", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Verification receipts" });
    expect(within(table).getByText("expressjs_express")).toBeInTheDocument();
    expect(within(table).getByText("Verified")).toBeInTheDocument();
    expect(within(table).getByText("Intact")).toBeInTheDocument();
    expect(within(table).getByText("Does not match its content")).toBeInTheDocument();
    expect(within(table).getByText("Cannot be read")).toBeInTheDocument();
  });

  it("opens a receipt as the six-step timeline", async () => {
    show();
    fireEvent.click(await screen.findByText("expressjs_express"));
    const dialog = await screen.findByRole("dialog", { name: "Verification receipt" });
    expect(await within(dialog).findByText(/The vulnerability shows before the patch/)).toBeInTheDocument();
    expect(within(dialog).getAllByRole("listitem").length).toBeGreaterThanOrEqual(6);
    expect(api.verifyReceipt).toHaveBeenCalledWith("pilot_expressjs_express_0867302d");
  });

  it("warns that a receipt that does not match its content cannot be trusted, with the details in Developer mode only", async () => {
    vi.mocked(api.verifyReceipt).mockResolvedValue({ name: "x", receipt: receipt() as any, problems: ["grade is 'G2' but the steps give 'G3'"] });
    const { unmount } = show(false);
    fireEvent.click(await screen.findByText("expressjs_express"));
    expect(await screen.findByText(/cannot be trusted/)).toBeInTheDocument();
    expect(screen.queryByText(/grade is 'G2'/)).toBeNull();
    unmount();
    show(true);
    fireEvent.click(await screen.findByText("expressjs_express"));
    expect(await screen.findByText(/grade is 'G2' but the steps give 'G3'/)).toBeInTheDocument();
  });

  it("is a tab of the sandbox page next to the containers", async () => {
    render(<MemoryRouter initialEntries={["/?tab=verification"]}><DevModeProvider initial={false}><SandboxPage /></DevModeProvider></MemoryRouter>);
    expect(await screen.findByText("Verification pilot")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Containers" })).toBeInTheDocument();
  });

  it("offers to verify a saved case again, says what it needs, and asks before it starts", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "verify_case", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "verify_case", status: "running", logs: [], progress: null } as any);
    class Quiet { onerror: any; onclose: any; onmessage: any; onopen: any; close() {} }
    vi.stubGlobal("WebSocket", Quiet);
    show();
    const panel = (await screen.findByText("Verify a case again")).closest("section") as HTMLElement;
    const user = userEvent.setup();
    const select = within(panel).getByRole("combobox", { name: "Case to verify" });
    expect(select).toHaveTextContent("expressjs/express, expressjs_express_0867302d (last: verified)");
    await user.click(select);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual([
      "expressjs/express, expressjs_express_0867302d (last: verified)", "websockets/ws, ws_ws_11111111 (last: fix did not pass)"]);
    await user.click(screen.getByRole("option", { name: /websockets\/ws/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "Verify again" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("no network for the checks");
    expect(ask).toHaveTextContent("does not change the saved pilot");
    expect(api.startJob).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Verify again" }));
    await vi.waitFor(() => expect(api.startJob).toHaveBeenCalledWith("verify_case", { case_id: "pilot_ws_ws_11111111" }));
  });

  it("does not let a case start while Docker is not running, and says so", async () => {
    vi.mocked(sandbox.status).mockResolvedValue({ docker_available: false });
    show();
    const panel = (await screen.findByText("Verify a case again")).closest("section") as HTMLElement;
    expect(await within(panel).findByText(/Docker is not running/)).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Verify again" })).toBeDisabled();
  });

  it("says when there is no saved case to run again, and marks a run again in the receipts table", async () => {
    vi.mocked(api.verifyCases).mockResolvedValue({ cases: [] });
    vi.mocked(api.verifyReceipts).mockResolvedValue({ receipts: [{ name: "verify_case~pilot_ws_ws_11111111", run: "verify_case", readable: true, case_id: "x", verdict: "verified", grade: "G3", runner: "docker", fixture_only: false, sound: true }] });
    show();
    expect(await screen.findByText("There are no saved cases to run again.")).toBeInTheDocument();
    expect(screen.getByText("(run again)")).toBeInTheDocument();
    expect(screen.getByText(/ws_ws/)).toBeInTheDocument();
  });
});
