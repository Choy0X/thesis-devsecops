import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/apiError";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { finishPlan: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() }, wsUrl: () => "ws://test" };
});
import { api, type InstallPlan } from "../lib/api";
import { resetJobsList } from "../lib/jobsList";
import { resetStreams } from "../lib/jobStreams";
import { ConfirmProvider } from "./hud/AlertDialog";
import { FinishInstall } from "./FinishInstall";

const GB = 1024 ** 3;
const plan = (over: Partial<InstallPlan> = {}): InstallPlan => ({
  kind: "finish", repo_id: "Qwen/Qwen2.5-Coder-7B-Instruct", source: "hub_cache", revision: "abc", role: "chat", role_detected: true, outtype: "f16", quant: "Q4_K_M", format: "Q4_K_M", format_reason: "The usual balance.",
  format_fit: "split", parameters: 7_615_616_512, parameters_estimated: false, architecture: "Qwen2ForCausalLM", supported: true, download: null,
  convert: { intermediate_bytes: 14 * GB, final_bytes: 4.4 * GB, final_bytes_max: 4.4 * GB, peak_bytes: 18.4 * GB, fallback: false, seconds: 815, seconds_note: "An estimate." },
  scripts: { tag: "b11528", installed: false, installed_tag: null, will_fetch: true, blockers: [], note: "about 115 small files" }, disk_needed_bytes: 18.4 * GB, problems: [], ready: true, notes: [], ...over });
class FakeSocket { onerror: any; onclose: any; close() {} constructor() { setTimeout(() => { this.onerror?.(); this.onclose?.(); }, 0); } }       // a socket that never opens: the page polls

const open = (onClose = vi.fn()) => render(<ConfirmProvider><FinishInstall model={{ id: "Qwen/Qwen2.5-Coder-7B-Instruct", source: "hub_cache" }} onClose={onClose} /></ConfirmProvider>);

beforeEach(() => {
  resetJobsList();
  resetStreams();
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.finishPlan).mockResolvedValue(plan());
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => vi.unstubAllGlobals());

describe("FinishInstall", () => {
  it("names the model, plans the conversion from the model's own folder and says what it will do", async () => {
    open();
    const dialog = await screen.findByRole("dialog", { name: "Finish installing Qwen2.5-Coder-7B-Instruct" });
    expect(await within(dialog).findByText("The usual balance.")).toBeInTheDocument();
    expect(api.finishPlan).toHaveBeenCalledWith("Qwen/Qwen2.5-Coder-7B-Instruct", "hub_cache", "");
    expect(within(dialog).getByText("Qwen/Qwen2.5-Coder-7B-Instruct")).toBeInTheDocument();
    expect(within(dialog).getByText(/The files it was downloaded as are kept/)).toBeInTheDocument();
    expect(within(dialog).getByText(/about 14 minutes on the processor/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Convert it" })).toBeEnabled();
  });

  it("does not ask what the model is: it says what it will be set up as, and the choice is there only after Change", async () => {
    open();
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("The usual balance.");
    expect(within(dialog).getByText("Will be set up as a language model.")).toBeInTheDocument();
    expect(within(dialog).getByText(/Found from the name and the files of the model/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("radio", { name: "Language model" })).toBeNull();
    expect(within(dialog).queryByRole("radiogroup", { name: "Use it as" })).toBeNull();
    expect(api.finishPlan).toHaveBeenCalledTimes(1);                         // nothing was planned again for a question that was not asked
  });

  it("plans again when the person changes the role after pressing Change", async () => {
    open();
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("The usual balance.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Change what it is used as" }));
    expect(within(dialog).getByRole("radio", { name: "Language model" })).toBeChecked();
    vi.mocked(api.finishPlan).mockResolvedValue(plan({ role: "embedding", role_detected: false, outtype: "q8_0", quant: null, format: "Q8_0" }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "Embedding model" }));
    await waitFor(() => expect(api.finishPlan).toHaveBeenLastCalledWith("Qwen/Qwen2.5-Coder-7B-Instruct", "hub_cache", "embedding"));
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: "Embedding model" })).toBeChecked());
    expect(within(dialog).queryByText(/Found from the name/)).toBeNull();
  });

  it("asks first with what will be done, and starts the install without the download, from the model that is here, with the role and the format it showed", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "install_model", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({ id: "j1", stage: "install_model", status: "running", params: { repo_id: "Qwen/Qwen2.5-Coder-7B-Instruct" }, logs: [], log_start: 0, progress: null } as any);
    open();
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("The usual balance.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Convert it" }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("This fetches llama.cpp b11528's converter scripts from github.com");
    expect(ask).toHaveTextContent("converts the model on this computer's processor to Q4_K_M GGUF");
    expect(ask).toHaveTextContent("The downloaded files are kept.");
    expect(api.startJob).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole("button", { name: "Convert it" }));
    await waitFor(() => expect(api.startJob).toHaveBeenCalledWith("install_model", { repo_id: "Qwen/Qwen2.5-Coder-7B-Instruct", quant: "Q4_K_M", source: "hub_cache", skip_download: true, role: "chat" }));
    expect(await within(dialog).findByLabelText("Install of Qwen/Qwen2.5-Coder-7B-Instruct")).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).queryByRole("button", { name: "Change what it is used as" })).toBeNull());       // what it is set up as is settled once it runs
    expect(within(dialog).getByText("Will be set up as a language model.")).toBeInTheDocument();
  });

  it("once the conversion is done, the role choice and the plan are gone, not left stale beside a Convert it that cannot be pressed again", async () => {
    vi.mocked(api.startJob).mockResolvedValue({ id: "j1", stage: "install_model", status: "running", logs: [], progress: null } as any);
    vi.mocked(api.getJob).mockResolvedValue({
      id: "j1", stage: "install_model", status: "done", params: { repo_id: "Qwen/Qwen2.5-Coder-7B-Instruct" }, logs: ["Finished"], log_start: 0,
      progress: { state: "done", unit: "percent", processed: 100, total: 100, phase: "convert" },
    } as any);
    open();
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("The usual balance.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Convert it" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Convert it" }));
    expect(await within(dialog).findByText("Ready to run", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(within(dialog).queryByText("The usual balance.")).toBeNull();
    expect(within(dialog).queryByText("Will be set up as a language model.")).toBeNull();
    expect(within(dialog).getByText("Converted.")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Convert it" })).toBeNull();
  });

  it("starts nothing when the person says no", async () => {
    open();
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("The usual balance.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Convert it" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.startJob).not.toHaveBeenCalled();
  });

  it("cannot be started while the plan finds something wrong, and says what", async () => {
    vi.mocked(api.finishPlan).mockResolvedValue(plan({ ready: false, problems: ["The llama.cpp program is not installed, and the converter has to match its release."] }));
    open();
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The llama.cpp program is not installed");
    expect(within(dialog).getByRole("button", { name: "Convert it" })).toBeDisabled();
    expect(within(dialog).getByText("This cannot be done yet: see above.")).toBeInTheDocument();
  });

  it("says why the plan could not be made", async () => {
    vi.mocked(api.finishPlan).mockRejectedValue(new ApiError({ status: 400, title: "Qwen/x holds GGUF files already: it is ready to run." }));
    open();
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("holds GGUF files already");
    expect(within(dialog).queryByRole("button", { name: "Convert it" })).toBeNull();
  });

  it("closes when asked, and does not stop what runs", async () => {
    const closed = vi.fn();
    open(closed);
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("The usual balance.");
    fireEvent.click(within(dialog).getByRole("button", { name: /Close/i }));
    expect(closed).toHaveBeenCalled();
    expect(api.cancelJob).not.toHaveBeenCalled();
  });
});
