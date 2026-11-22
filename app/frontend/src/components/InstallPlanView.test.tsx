import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { InstallPlan } from "../lib/api";
import { aboutTime, InstallPlanView, installQuestion } from "./InstallPlanView";

const GB = 1024 ** 3;
const MB = 1024 ** 2;

const plan = (over: Partial<InstallPlan> = {}): InstallPlan => ({
  kind: "convert", repo_id: "org/m", revision: "d".repeat(40), role: "chat", role_detected: true, outtype: "f16", quant: "Q4_K_M", format: "Q4_K_M", format_reason: "Q4_K_M: 4.4 GB, the usual balance.", format_fit: "split",
  parameters: 7_615_616_512, parameters_estimated: false, architecture: "Qwen2ForCausalLM", supported: null,
  download: { files: 4, bytes: 15 * GB, partial_bytes: 0, free_bytes: 120 * GB, enough: true, gated: false, token_set: false },
  convert: { intermediate_bytes: 14 * GB, final_bytes: 4.4 * GB, final_bytes_max: 4.4 * GB, peak_bytes: 18.4 * GB, fallback: false, seconds: 815, seconds_note: "An estimate: a model of 0.5 billion parameters took 53 s here and this one has 7.6 billion; nothing larger was timed." },
  scripts: { tag: "b11528", installed: false, installed_tag: null, will_fetch: true, blockers: [], note: "about 115 small files, under 2 MB, counted for llama.cpp b11528" },
  disk_needed_bytes: 33.4 * GB, problems: [], ready: true, notes: [], ...over });

describe("aboutTime", () => {
  it("rounds an estimate the way an estimate should be told", () => {
    expect([aboutTime(53), aboutTime(4), aboutTime(89), aboutTime(100), aboutTime(815), aboutTime(5300), aboutTime(5400), aboutTime(9000)]).toEqual(
      ["about 50 seconds", "about 10 seconds", "about 90 seconds", "about 2 minutes", "about 14 minutes", "about 88 minutes", "about 1.5 hours", "about 2.5 hours"]);
  });
});

describe("installQuestion", () => {
  it("says what is fetched and made, what it needs, and that the files are kept", () => {
    const q = installQuestion(plan());
    expect(q).toBe("This downloads 4 files, 15 GB, from huggingface.co; fetches llama.cpp b11528's converter scripts from github.com (about 115 small files, under 2 MB, counted for llama.cpp b11528); "
      + "and converts the model on this computer's processor to Q4_K_M GGUF (about 4.4 GB; about 14 minutes, an estimate). It needs about 33 GB of free disk space. The downloaded files are kept.");
  });

  it("leaves out the scripts when they are in place and the download when the model is here", () => {
    const here = installQuestion(plan({ kind: "finish", download: null, scripts: { tag: "b11528", installed: true, installed_tag: "b11528", will_fetch: false, blockers: [], note: null }, disk_needed_bytes: 18.4 * GB }));
    expect(here).toBe("This converts the model on this computer's processor to Q4_K_M GGUF (about 4.4 GB; about 14 minutes, an estimate). It needs about 18 GB of free disk space. The downloaded files are kept.");
  });

  it("says how much of the download is already there, and adds what it is given at the end", () => {
    const q = installQuestion(plan({ download: { files: 1, bytes: 6 * MB, partial_bytes: 2 * MB, free_bytes: GB, enough: true, gated: false, token_set: false }, scripts: null }), " The model is gated.");
    expect(q).toContain("downloads 1 file, 6.0 MB, from huggingface.co (2.0 MB of it is already there)");
    expect(q.endsWith("The downloaded files are kept. The model is gated.")).toBe(true);
  });
});

describe("InstallPlanView", () => {
  it("shows the format and why, the download, the conversion with what its time is built on, the disk and the scripts", () => {
    render(<InstallPlanView plan={plan()} />);
    expect(screen.getByText("Q4_K_M", { selector: "span.font-medium" })).toBeInTheDocument();
    expect(screen.getByText(/\(4\.4 GB\)/)).toBeInTheDocument();
    expect(screen.getByText("Q4_K_M: 4.4 GB, the usual balance.")).toBeInTheDocument();
    expect(screen.getByText(/15 GB in 4 files/)).toBeInTheDocument();
    expect(screen.getByText(/about 14 minutes on the processor/)).toBeInTheDocument();
    expect(screen.getByText(/took 53 s here and this one has 7\.6 billion; nothing larger was timed/)).toBeInTheDocument();
    expect(screen.getByText(/about 33 GB while it runs; 120 GB is free/)).toBeInTheDocument();
    expect(screen.getByText(/llama\.cpp b11528's converter scripts are fetched first from github\.com/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says that the scripts are in place when they are, and leaves out the download for a model that is already here", () => {
    render(<InstallPlanView plan={plan({ kind: "finish", download: null, scripts: { tag: "b11528", installed: true, installed_tag: "b11528", will_fetch: false, blockers: [], note: null } })} />);
    expect(screen.getByText(/converter scripts are already installed/)).toBeInTheDocument();
    expect(screen.queryByText("Download")).toBeNull();
    expect(screen.getByText(/about 33 GB while it runs$/)).toBeInTheDocument();
  });

  it("says that some layers fall back to a larger format", () => {
    render(<InstallPlanView plan={plan({ convert: { ...plan().convert!, fallback: true, final_bytes_max: 7 * GB } })} />);
    expect(screen.getByText(/up to 7\.0 GB because some layers fall back to a larger format/)).toBeInTheDocument();
  });

  it("lists every reason it cannot be done in an alert, and the notes under the facts", () => {
    render(<InstallPlanView plan={plan({ ready: false, problems: ["The llama.cpp program is not installed.", "The install needs about 33 GB free and 2.0 GB is."], notes: ["This repository also holds GGUF files (Q4_K_M)."] })} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The llama.cpp program is not installed.");
    expect(alert).toHaveTextContent("33 GB free and 2.0 GB is");
    expect(screen.getByText("This repository also holds GGUF files (Q4_K_M).")).toBeInTheDocument();
  });

  it("shows no facts for a repository that is not converted, only what there is to say", () => {
    render(<InstallPlanView plan={plan({ kind: "other", convert: null, scripts: null, ready: false, problems: ["This repository holds neither safetensors nor GGUF weights."] })} />);
    expect(screen.queryByText("Conversion")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("neither safetensors nor GGUF");
  });
});
