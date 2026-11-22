import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, wsUrl: () => "ws://test", api: { trainConfig: vi.fn(), ggufFor: vi.fn(), downloadPlan: vi.fn(), listJobs: vi.fn(), startJob: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn() } };
});
import { api, type GgufVersions } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { GgufFinder, rows } from "./GgufFinder";

const GB = 1024 ** 3;
const found = (over: Partial<GgufVersions> = {}): GgufVersions => ({ base: "Qwen/Qwen2.5-Coder-7B", how: "converted from this model", training_bytes: 15 * GB, free_bytes: 20 * GB, candidates: [
  { id: "a/m-GGUF", downloads: 900, likes: 5, gated: false, quants: { Q8_0: 7.7 * GB, Q4_K_M: 4.4 * GB } }, { id: "b/m-gguf", downloads: 100, likes: 1, gated: false, quants: { Q4_K_M: 4.5 * GB } }], ...over });
const show = () => render(<ConfirmProvider><GgufFinder /></ConfirmProvider>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.trainConfig).mockResolvedValue({ sections: [{ key: "model", title: "Base model", fields: [{ key: "model.id", name: "id", kind: "text", value: "Qwen/Qwen2.5-Coder-7B", fixed: false }] }],
    counts: { settings: 1, fixed: 0, changeable: 1 }, file_sha256: null, unresolved: [] });
  vi.mocked(api.ggufFor).mockResolvedValue(found());
  vi.mocked(api.listJobs).mockResolvedValue([]);
});

describe("rows", () => {
  it("puts every quantisation of every repository in a row, smaller first, with its size next to the training copy and whether the disk has room", () => {
    const r = rows(found({ free_bytes: 6 * GB }));
    expect(r.map((x) => `${x.repo} ${x.quant}`)).toEqual(["a/m-GGUF Q4_K_M", "a/m-GGUF Q8_0", "b/m-gguf Q4_K_M"]);
    expect(r[0]).toMatchObject({ withTraining: 15 * GB + 4.4 * GB, room: "yes" });
    expect(r[1].room).toBe("no");
    expect(rows(found({ free_bytes: null, training_bytes: null }))[0]).toMatchObject({ withTraining: null, room: "unknown" });
  });
});

describe("GgufFinder", () => {
  it("fills in the project's base model, looks for copies when asked, and shows the disk cost of each quantisation", async () => {
    show();
    const field = await screen.findByLabelText("Base model");
    await waitFor(() => expect(field).toHaveValue("Qwen/Qwen2.5-Coder-7B"));
    expect(api.ggufFor).not.toHaveBeenCalled();                                             // nothing is sent to the Hub until asked
    fireEvent.click(screen.getByRole("button", { name: "Look for GGUF versions" }));
    expect(await screen.findByText(/Repositories the Hub lists as converted from this model/)).toBeInTheDocument();
    expect(api.ggufFor).toHaveBeenCalledWith("Qwen/Qwen2.5-Coder-7B");
    expect(screen.getByText(/The training copy \(safetensors\) is 15 GB\. 20 GB is free/)).toBeInTheDocument();
    const row = screen.getAllByText("Q4_K_M", { selector: "td" })[0].closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("a/m-GGUF");
    expect(within(row).getByText("4.4 GB")).toBeInTheDocument();
    expect(within(row).getByText("19 GB")).toBeInTheDocument();
    expect(within(row).getByText("enough")).toBeInTheDocument();
  });

  it("says when a quantisation does not fit the free space and when the repositories were found only by name", async () => {
    vi.mocked(api.ggufFor).mockResolvedValue(found({ how: "found by name", free_bytes: 5 * GB }));
    show();
    fireEvent.change(await screen.findByLabelText("Base model"), { target: { value: "org/other" } });
    fireEvent.click(screen.getByRole("button", { name: "Look for GGUF versions" }));
    expect(await screen.findByText(/Repositories found by the model's name, which may be of a different model/)).toBeInTheDocument();
    expect(screen.getAllByText("not enough").length).toBeGreaterThan(0);
  });

  it("says when nothing was found, and when the Hub could not be reached", async () => {
    vi.mocked(api.ggufFor).mockResolvedValueOnce(found({ candidates: [], how: "none found" }));
    show();
    await waitFor(() => expect(screen.getByLabelText("Base model")).toHaveValue("Qwen/Qwen2.5-Coder-7B"));
    fireEvent.click(screen.getByRole("button", { name: "Look for GGUF versions" }));
    expect(await screen.findByText("No GGUF version found")).toBeInTheDocument();
    vi.mocked(api.ggufFor).mockImplementationOnce(() => Promise.reject(new Error("Hugging Face could not be reached")));
    fireEvent.click(screen.getByRole("button", { name: "Look for GGUF versions" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Hugging Face could not be reached");
  });

  it("opens the download of a repository, with its quantisations and their sizes", async () => {
    vi.mocked(api.downloadPlan).mockResolvedValue({ repo_id: "a/m-GGUF", revision: "a".repeat(40), gated: false, quants: ["Q4_K_M", "Q8_0"], quant_bytes: { Q4_K_M: 4.4 * GB, Q8_0: 7.7 * GB },
      needs_quant: true, files: 0, bytes: 0, free_bytes: 20 * GB, enough: false });
    show();
    await waitFor(() => expect(screen.getByLabelText("Base model")).toHaveValue("Qwen/Qwen2.5-Coder-7B"));
    fireEvent.click(screen.getByRole("button", { name: "Look for GGUF versions" }));
    const row = (await screen.findByText("Q8_0", { selector: "td" })).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Download" }));
    const select = await screen.findByRole("combobox", { name: "Quantisation" });
    await userEvent.setup().click(select);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["choose one", "Q4_K_M (4.4 GB)", "Q8_0 (7.7 GB)"]);
  });
});
