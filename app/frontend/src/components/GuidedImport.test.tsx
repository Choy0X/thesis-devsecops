import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { finetuneCheckImport: vi.fn() } };
});
// The job console is replaced by a button that reports what it would launch.
vi.mock("./RunConsole", () => ({
  RunConsole: ({ label, stage, params, onDone }: { label: string; stage: string; params: any; onDone: () => void }) =>
    <button data-stage={stage} data-path={params.path} data-benchmark={params.evaluation_dataset_dir} onClick={onDone}>{label}</button>,
}));
import { ApiError, api, type ImportCheck, type ImportStep } from "../lib/api";
import { GuidedImport } from "./GuidedImport";

const step = (name: string, state: ImportStep["state"], over: Partial<ImportStep> = {}): ImportStep =>
  ({ name, title: `Title of ${name}`, what: `what ${name} looks at`, state, failures: [], fix: null, ...over });
const good: ImportCheck = { kind: "outputs", ok: true, already_imported: false, steps: [step("zip_structure", "passed"), step("manifest", "passed"), step("identity", "passed")],
  summary: { type: "trained", state: "finished", steps_done: 120, adapter_parameters: 80740352, smoke_test: false, warnings: [] } };
const bad: ImportCheck = { kind: "outputs", ok: false, already_imported: false, summary: null, steps: [step("zip_structure", "passed"),
  step("manifest", "failed", { failures: ["adapter/adapter_model.safetensors changed after the run wrote it"], fix: "Download the file again." }), step("identity", "not_checked")] };

beforeEach(() => { vi.mocked(api.finetuneCheckImport).mockReset(); });

const typePath = (v: string, label = "Path of outputs.zip") => fireEvent.change(screen.getByLabelText(label), { target: { value: v } });

describe("GuidedImport", () => {
  it('keeps partial portable trials experimental and uses their separate import job', async () => {
    vi.mocked(api.finetuneCheckImport).mockResolvedValue({...good, kind: 'trial' as any,
      summary: {state: 'partial', total_budget_minutes: 30, experimental: true,
        counts_as_full_training: false, security_evaluation: false}});
    render(<GuidedImport run="trial" kind={'trial' as any} onDone={vi.fn()} />);
    typePath('C:/trial_outputs.zip', 'Path of trial_outputs.zip');
    fireEvent.click(screen.getByRole('button', {name: 'Check the file'}));
    expect((await screen.findByText(/partial trial/i)).closest('p')).toHaveTextContent('30 minutes');
    expect(screen.getByText(/does not count as full training or security evaluation/i)).toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Import trial'})).toHaveAttribute('data-stage', 'import_trial_outputs');
  });
  it('checks and imports an independent benchmark and invalidates a stale benchmark check', async () => {
    vi.mocked(api.finetuneCheckImport).mockResolvedValue({...good, kind: 'eval'});
    render(<GuidedImport run="pilot" kind="eval" onDone={vi.fn()} />);
    typePath('C:/evaluation.zip', 'Path of eval_outputs.zip');
    fireEvent.change(screen.getByLabelText('Independent benchmark directory (optional)'), {target: {value: 'C:/reviewed benchmark'}});
    fireEvent.click(screen.getByRole('button', {name: 'Check the file'}));
    await screen.findByText('Every check passed.');
    expect(api.finetuneCheckImport).toHaveBeenCalledWith('pilot', 'eval', 'C:/evaluation.zip', 'C:/reviewed benchmark');
    expect(screen.getByRole('button', {name: 'Import and score'})).toHaveAttribute('data-benchmark', 'C:/reviewed benchmark');
    fireEvent.change(screen.getByLabelText('Independent benchmark directory (optional)'), {target: {value: 'C:/different benchmark'}});
    expect(screen.queryByRole('button', {name: 'Import and score'})).not.toBeInTheDocument();
  });
  it("starts with the file, offers no import before a check, and checks only on request", async () => {
    render(<GuidedImport run="pilot" kind="outputs" onDone={vi.fn()} />);
    const steps = screen.getByRole("list", { name: "Steps to import outputs.zip" });
    expect(within(steps).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["1. Find the file", "2. Check it", "3. Import it"]);
    expect(screen.getByRole("button", { name: "Check the file" })).toBeDisabled();
    typePath('  "C:/Users/me/outputs.zip" ');
    expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
    expect(api.finetuneCheckImport).not.toHaveBeenCalled();
  });

  it("shows every check with its state, then offers the import for a file that passed, with a path cleaned of quotes", async () => {
    vi.mocked(api.finetuneCheckImport).mockResolvedValue(good);
    const onDone = vi.fn();
    render(<GuidedImport run="pilot" kind="outputs" onDone={onDone} />);
    typePath('"C:/Users/me/outputs.zip"');
    fireEvent.click(screen.getByRole("button", { name: "Check the file" }));
    const list = await screen.findByRole("list", { name: "Checks" });
    expect(api.finetuneCheckImport).toHaveBeenCalledWith("pilot", "outputs", "C:/Users/me/outputs.zip");
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText("Every check passed.")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.tagName === "P" && /It is a trained run .finished, 120 steps. with an adapter of 80,740,352 parameters\./.test(el.textContent ?? ""))).toBeInTheDocument();
    const go = screen.getByRole("button", { name: "Import" });
    expect(go).toHaveAttribute("data-stage", "import_finetune_outputs");
    expect(go).toHaveAttribute("data-path", "C:/Users/me/outputs.zip");
    fireEvent.click(go);
    expect(onDone).toHaveBeenCalled();
  });

  it("names the failed check, its reasons and what to do, marks the later ones as not checked and offers no import", async () => {
    vi.mocked(api.finetuneCheckImport).mockResolvedValue(bad);
    render(<GuidedImport run="pilot" kind="outputs" onDone={vi.fn()} />);
    typePath("C:/x.zip");
    fireEvent.click(screen.getByRole("button", { name: "Check the file" }));
    expect(await screen.findByText("The file was rejected: 1 check failed, so nothing can be imported from it.")).toBeInTheDocument();
    const failed = screen.getByText("Title of manifest").closest("li") as HTMLElement;
    expect(failed).toHaveTextContent("failed");
    expect(within(failed).getByText("adapter/adapter_model.safetensors changed after the run wrote it")).toBeInTheDocument();
    expect(failed).toHaveTextContent("What to do: Download the file again.");
    expect(failed).toHaveTextContent("This check looks at: what manifest looks at");
    expect(screen.getByText("Title of identity").closest("li")).toHaveTextContent("not checked");
    expect(screen.getByText("Title of zip_structure").closest("li")).toHaveTextContent("passed");
    expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
  });

  it("forgets a check when the path changes, and says when the same file was imported before", async () => {
    vi.mocked(api.finetuneCheckImport).mockResolvedValue({ ...good, already_imported: true });
    render(<GuidedImport run="pilot" kind="outputs" onDone={vi.fn()} />);
    typePath("C:/a.zip");
    fireEvent.click(screen.getByRole("button", { name: "Check the file" }));
    await screen.findByText(/imported before; importing it again changes nothing/);
    typePath("C:/b.zip");
    expect(screen.queryByText("Every check passed.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
  });

  it("uses the evaluation import and its words for an evaluation, and says what a plain error was", async () => {
    vi.mocked(api.finetuneCheckImport).mockResolvedValueOnce({ kind: "eval", ok: true, already_imported: false, steps: [step("zip_structure", "passed")],
      summary: { baseline: "B0", arm: "base", role: "selection", attempts: 120, excluded: 2, counts_as_evaluation: true, smoke_test: false, partial: false } });
    render(<GuidedImport run="pilot" kind="eval" onDone={vi.fn()} />);
    typePath("C:/e.zip", "Path of eval_outputs.zip");
    fireEvent.click(screen.getByRole("button", { name: "Check the file" }));
    expect(await screen.findByText(/It is the evaluation of/)).toHaveTextContent("B0 (base, selection role): 120 answers, 2 views excluded. It counts as an evaluation of that control.");
    expect(screen.getByRole("button", { name: "Import and score" })).toHaveAttribute("data-stage", "import_finetune_eval");
    vi.mocked(api.finetuneCheckImport).mockRejectedValueOnce(new ApiError({ status: 400, title: "That file is not there. Check the path of the zip file." }));
    typePath("C:/missing.zip", "Path of eval_outputs.zip");
    fireEvent.click(screen.getByRole("button", { name: "Check the file" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That file is not there");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Import and score" })).toBeNull());
  });
});
