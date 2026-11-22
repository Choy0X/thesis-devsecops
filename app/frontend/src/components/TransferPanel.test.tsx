import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiError } from "../lib/apiError";
import { errorSentence, finishedWords, stateWord, TransferPanel, transferState } from "./TransferPanel";

const MB = 1024 ** 2;
const rep = { state: "downloading", label: "Downloading org/m", unit: "bytes" as const, processed: 4 * MB, total: 8 * MB, rate: 2 * MB, eta: 2, files: 5, files_total: 7, current: "model.safetensors" };

describe("transferState", () => {
  it("is planning until the job has reported something to show", () => {
    expect(transferState("running", null)).toBe("planning");
    expect(transferState("running", undefined)).toBe("planning");
    expect(transferState("running", { state: "starting" })).toBe("planning");
    expect(transferState("idle", null)).toBe("planning");
  });

  it("is downloading once the job reports that or a total", () => {
    expect(transferState("running", { state: "downloading" })).toBe("downloading");
    expect(transferState("running", { processed: 1, total: 10 })).toBe("downloading");
  });

  it("follows the end of the job: finished, stopped, failed", () => {
    expect(transferState("done", rep)).toBe("finished");
    expect(transferState("cancelled", rep)).toBe("stopped");
    expect(transferState("error", rep)).toBe("failed");
    expect(transferState("interrupted", null)).toBe("failed");
  });
});

describe("finishedWords", () => {
  it("says the size, the files, the time and the speed", () => {
    expect(finishedWords({ total: 6.2 * MB, files_total: 7, seconds: 3, unit: "bytes" })).toBe("6.2 MB in 7 files, took 3.0 s (2.1 MB/s).");
  });

  it("leaves the speed out when it took under a second, and writes a long time as minutes", () => {
    expect(finishedWords({ total: 500 * 1024, files_total: 1, seconds: 0.4 })).toBe("500 kB in 1 file, took 0.4 s.");
    expect(finishedWords({ total: 3 * 1024 ** 3, files_total: 4, seconds: 150 })).toBe("3.0 GB in 4 files, took 2:30 (20.5 MB/s).");
  });

  it("says what it can when the job did not say how long it took, or said nothing", () => {
    expect(finishedWords({ total: 2 * MB, files_total: 2 })).toBe("2.0 MB in 2 files arrived.");
    expect(finishedWords({ processed: 3 * MB })).toBe("3.0 MB arrived.");
    expect(finishedWords({})).toBe("The model arrived.");
    expect(finishedWords(null)).toBe("The download is finished.");
  });
});

describe("TransferPanel", () => {
  it("names the model and the state, and says it is getting ready while it plans", () => {
    render(<TransferPanel model="org/m" status="running" progress={null} />);
    expect(screen.getByRole("heading", { name: "org/m" })).toBeInTheDocument();
    expect(screen.getByText("Planning")).toBeInTheDocument();
    expect(screen.getByText(/getting ready to fetch it/)).toBeInTheDocument();
    expect(screen.getByLabelText("Download of org/m")).toHaveAttribute("data-state", "planning");
  });

  it("shows the bar, sizes, speed, time left, files and the file being fetched while it downloads", () => {
    render(<TransferPanel model="org/m" status="running" progress={rep} />);
    expect(screen.getByText("Downloading")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", String(4 * MB));
    expect(screen.getByText(/4\.0 MB \/ 8\.0 MB/)).toBeInTheDocument();
    expect(screen.getByText(/50\.0%/)).toBeInTheDocument();
    expect(screen.getByText("2.0 MB/s · 0:02 left")).toBeInTheDocument();
    expect(screen.getByText("5 of 7 files")).toBeInTheDocument();
    expect(screen.getByText("model.safetensors")).toBeInTheDocument();
  });

  it("does not draw a second frame for the strip inside it", () => {
    const { container } = render(<TransferPanel model="org/m" status="running" progress={rep} />);
    expect(container.querySelectorAll("section")).toHaveLength(1);
    expect(container.querySelector("section > div > div")).not.toHaveClass("border");
  });

  it("says what arrived, how long it took, and where the model is, when it is finished", () => {
    render(<TransferPanel model="org/m" status="done" progress={{ ...rep, processed: 8 * MB, total: 8 * MB, seconds: 4, files: 7, files_total: 7 }} />);
    expect(screen.getByText("Finished")).toBeInTheDocument();
    expect(screen.getByText("8.0 MB in 7 files, took 4.0 s (2.0 MB/s).")).toBeInTheDocument();
    expect(screen.getByText(/listed under Installed models/)).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("says that what was fetched is kept when it was stopped", () => {
    render(<TransferPanel model="org/m" status="cancelled" progress={rep} />);
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getByText(/What was fetched is kept/)).toBeInTheDocument();
    expect(screen.getByText(/Continue the download/)).toBeInTheDocument();
  });

  it("gives the reason in words when it failed, and the error a start was refused with", () => {
    const { rerender } = render(<TransferPanel model="org/m" status="error" error="not enough disk space: 3.0 GB are needed" />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The download failed: not enough disk space: 3.0 GB are needed");
    rerender(<TransferPanel model="org/m" status="error" failure={new ApiError({ status: 409, title: "too many jobs are running", hint: "Wait for one to finish." })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("too many jobs are running");
    expect(screen.getByText("Wait for one to finish.")).toBeInTheDocument();
    rerender(<TransferPanel model="org/m" status="interrupted" />);
    expect(screen.getByText("The download stopped with an error.")).toBeInTheDocument();
  });
});


describe("TransferPanel for an install", () => {
  const install = { state: "converting", label: "Converting org/m to Q4_K_M", unit: "percent" as const, processed: 37, total: 100, step: 3, steps: 3, phase: "convert" };

  it("says which step an install is on while it runs, and names the panel an install", () => {
    const { rerender } = render(<TransferPanel model="org/m" status="running" progress={{ ...install, phase: "scripts", unit: "files", processed: 40, total: 115, step: 1 }} install />);
    expect(screen.getByLabelText("Install of org/m")).toBeInTheDocument();
    expect(screen.getByText("Fetching scripts")).toBeInTheDocument();
    rerender(<TransferPanel model="org/m" status="running" progress={{ ...install, phase: "download", unit: "bytes", processed: MB, total: 8 * MB, step: 2 }} install />);
    expect(screen.getByText("Downloading")).toBeInTheDocument();
    rerender(<TransferPanel model="org/m" status="running" progress={install} install />);
    expect(screen.getByText("Converting")).toBeInTheDocument();
    expect(screen.getByText("Step 3 of 3")).toBeInTheDocument();
    expect(screen.getByText("37%")).toBeInTheDocument();
    expect(screen.queryByText(/37 \/ 100/)).toBeNull();
  });

  it("says Installing for a step it does not know and Planning before the first report", () => {
    const { rerender } = render(<TransferPanel model="org/m" status="running" progress={{ ...install, phase: "something" }} install />);
    expect(screen.getByText("Installing")).toBeInTheDocument();
    rerender(<TransferPanel model="org/m" status="running" progress={null} install />);
    expect(screen.getByText("Planning")).toBeInTheDocument();
    expect(screen.getByText(/Checking what the install needs/)).toBeInTheDocument();
  });

  it("says that the model is ready to run, as what, where it is listed and that the files are kept", () => {
    const { rerender } = render(<TransferPanel model="org/m" status="done" progress={{ ...install, state: "done", processed: 100 }} install role="chat" />);
    expect(screen.getByText("Ready to run")).toBeInTheDocument();
    expect(screen.getByText("org/m is converted and ready to run as a language model. Settings lists it by its name and format.")).toBeInTheDocument();
    expect(screen.getByText(/The downloaded files are kept/)).toBeInTheDocument();
    rerender(<TransferPanel model="org/m" status="done" progress={null} install role="embedding" />);
    expect(screen.getByText(/ready to run as an embedding model/)).toBeInTheDocument();
    rerender(<TransferPanel model="org/m" status="done" progress={null} install />);
    expect(screen.getByText("org/m is converted and ready to run. Settings lists it by its name and format.")).toBeInTheDocument();
  });

  it("says install, not download, when an install is stopped or fails", () => {
    const { rerender } = render(<TransferPanel model="org/m" status="cancelled" progress={install} install />);
    expect(screen.getByText(/Continue the install, picks up from there/)).toBeInTheDocument();
    rerender(<TransferPanel model="org/m" status="error" error="The converter does not list the architecture X." install />);
    expect(screen.getByRole("alert")).toHaveTextContent("The install failed: The converter does not list the architecture X.");
    rerender(<TransferPanel model="org/m" status="interrupted" install />);
    expect(screen.getByText("The install stopped with an error.")).toBeInTheDocument();
  });

  it("keeps the words of a download for a download", () => {
    render(<TransferPanel model="org/m" status="done" progress={{ unit: "bytes", processed: 8 * MB, total: 8 * MB, files_total: 4, seconds: 4 }} />);
    expect(screen.getByText("Finished")).toBeInTheDocument();
    expect(screen.getByLabelText("Download of org/m")).toBeInTheDocument();
    expect(screen.queryByText(/ready to run/)).toBeNull();
  });
});

describe("stateWord", () => {
  it("is the state of a download, and for an install the step, with Ready to run at the end", () => {
    expect(stateWord("downloading", { phase: "convert" }, false)).toEqual(["Downloading", "text-brand-strong"]);
    expect(stateWord("downloading", { phase: "convert" }, true)).toEqual(["Converting", "text-brand-strong"]);
    expect(stateWord("finished", null, true)).toEqual(["Ready to run", "text-ok"]);
    expect(stateWord("finished", null, false)).toEqual(["Finished", "text-ok"]);
    expect(stateWord("stopped", null, true)).toEqual(["Stopped", "text-warn"]);
  });
});

describe("errorSentence", () => {
  it("is the last line of what a job printed, without the blank lines and the spaces around it", () => {
    expect(errorSentence("Writing:  50%|## | 2.4M/4.9M\nWriting: 100%|## | 4.9M/4.9M\nThe quantisation failed (exit code 1): failed to open the file.\n\n  ")).toBe("The quantisation failed (exit code 1): failed to open the file.");
    expect(errorSentence("one line")).toBe("one line");
    expect(errorSentence("a\r\nb\r\n")).toBe("b");
    expect(errorSentence("")).toBe("");
    expect(errorSentence(undefined)).toBe("");
  });

  it("is what the panel says failed, for a download and an install, and not the terminal's tail", () => {
    const tail = "Writing:  50%|## | 2.4M/4.9M\nINFO:hf-to-gguf:Model successfully exported\nThe quantisation failed (exit code 1): failed to open the file.";
    const { rerender } = render(<TransferPanel model="org/m" status="error" error={tail} install />);
    expect(screen.getByRole("alert")).toHaveTextContent("The install failed: The quantisation failed (exit code 1): failed to open the file.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Writing:");
    rerender(<TransferPanel model="org/m" status="error" error={tail} />);
    expect(screen.getByRole("alert")).toHaveTextContent("The download failed: The quantisation failed");
    rerender(<TransferPanel model="org/m" status="error" error={"\n \n"} install />);
    expect(screen.getByText("The install stopped with an error.")).toBeInTheDocument();
  });
});
