import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { JobProgress } from "../lib/api";
import { ProgressStrip } from "./ProgressStrip";

const MB = 1024 ** 2;
const strip = (p: JobProgress, live = true) => render(<ProgressStrip p={p} live={live} />);

describe("ProgressStrip", () => {
  it("draws the bar as 40 segments in the console look, with a straight frame and no rounded corners", () => {
    const { container } = strip({ label: "Downloading", processed: 25, total: 100 });
    const bar = screen.getByRole("progressbar", { name: "Downloading" });
    expect(bar).toHaveAttribute("aria-valuenow", "25");
    expect(bar).toHaveAttribute("aria-valuetext", "25 percent");
    expect(bar.querySelectorAll(".progress-seg")).toHaveLength(40);
    expect(bar.querySelectorAll('[data-filled="true"]')).toHaveLength(10);
    expect(container.innerHTML).not.toMatch(/rounded/);
  });

  it("has no bar when the total is not known, and says what is done so far", () => {
    strip({ label: "Reading", unit: "files", processed: 1200 });
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.getByText("1,200 files so far")).toBeInTheDocument();
  });

  it("says the title in a status region that is not the percentage, so the percentage is not announced every time it moves", () => {
    strip({ label: "Downloading org/m", processed: 10, total: 100 });
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Downloading org/m");
    expect(status).not.toHaveTextContent("10");
  });

  it("shows a transfer: sizes, percent, speed, time left, files done, the file now fetched and where it continued from", () => {
    strip({ label: "Downloading org/m", unit: "bytes", processed: 4 * MB, total: 8 * MB, rate: 2 * MB, eta: 2, files: 5, files_total: 7, current: "model.safetensors", resumed_from: 2 * MB });
    expect(screen.getByText(/4\.0 MB \/ 8\.0 MB/)).toBeInTheDocument();
    expect(screen.getByText(/50\.0%/)).toBeInTheDocument();
    expect(screen.getByText("2.0 MB/s · 0:02 left")).toBeInTheDocument();
    expect(screen.getByText("5 of 7 files")).toBeInTheDocument();
    expect(screen.getByText("model.safetensors")).toBeInTheDocument();
    expect(screen.getByText("continued from 2.0 MB")).toBeInTheDocument();
  });

  it("says file, not files, for one, and shows nothing about files when the job does not count them", () => {
    const { unmount } = strip({ label: "x", unit: "bytes", processed: 1, total: 2, files: 0, files_total: 1 });
    expect(screen.getByText("0 of 1 file")).toBeInTheDocument();
    unmount();
    strip({ label: "x", unit: "bytes", processed: 1, total: 2 });
    expect(screen.queryByText(/ of \d+ files?/)).toBeNull();
  });

  it("does not show speed or time left once the job is over", () => {
    strip({ label: "Done", unit: "bytes", processed: 8 * MB, total: 8 * MB, rate: 2 * MB, eta: 0 }, false);
    expect(screen.queryByText(/MB\/s/)).toBeNull();
    expect(screen.queryByText(/left/)).toBeNull();
  });

  it("shows which step of several it is", () => {
    strip({ label: "Reading the dump", step: 2, steps: 3, processed: 1, total: 4 });
    expect(screen.getByText("Step 2 of 3")).toBeInTheDocument();
  });

  it("warns, in the warning colours, while rate limited and counts down to when it resumes", () => {
    const { container } = strip({ state: "rate_limited", until: Date.now() / 1000 + 90, reason: "GitHub asked to wait" });
    expect(screen.getByText("GitHub rate limit reached")).toHaveClass("text-warn");
    expect(container.firstElementChild).toHaveClass("border-warn-line", "bg-warn-tint");
    expect(screen.getByText(/Resumes at/)).toBeInTheDocument();
    expect(screen.getByText(/GitHub asked to wait/)).toBeInTheDocument();
  });

  it("shows a percentage only for a job that reports one, not 37 / 100 and not a second percentage", () => {
    strip({ label: "Converting org/m to Q4_K_M", unit: "percent", processed: 36.6, total: 100, step: 3, steps: 3 });
    expect(screen.getByText("37%")).toBeInTheDocument();
    expect(screen.queryByText(/\/ 100/)).toBeNull();
    expect(screen.queryByText(/36\.6%/)).toBeNull();
    expect(screen.getByRole("progressbar", { name: "Converting org/m to Q4_K_M" })).toHaveAttribute("aria-valuenow", "37");
  });

  it("has no frame of its own when it sits inside another panel", () => {
    const { container } = render(<ProgressStrip p={{ label: "x", processed: 1, total: 2 }} live bare />);
    expect(container.firstElementChild).not.toHaveClass("border");
    expect(container.firstElementChild).not.toHaveClass("mt-3");
  });

  it("shows counts and the API quota", () => {
    strip({ state: "mining", batch: 2, batches: 5, counts: { new_pairs: 1200, skipped_rows: 3 }, quota: { remaining: 4000, limit: 5000, reset: 0 } });
    expect(screen.getByText("Batch 2 of 5")).toBeInTheDocument();
    expect(screen.getByText("new pairs: 1,200")).toBeInTheDocument();
    expect(screen.getByText("skipped rows: 3")).toBeInTheDocument();
    expect(screen.getByText(/GitHub API quota: 4,000 \/ 5,000/)).toBeInTheDocument();
  });

  it("says nothing about how long ago it last moved while the number is fresh, so a healthy job is not cluttered with it", () => {
    strip({ label: "Reading", processed: 5, total: 10, ts: Date.now() / 1000 - 2 });
    expect(screen.queryByText(/last moved/)).toBeNull();
  });

  it("says how long ago it last moved once the number has sat still a while, so a real wait is never silently indistinguishable from a stuck one", () => {
    strip({ label: "npm: putting the dump together", processed: 230_327, total: 230_330, ts: Date.now() / 1000 - 40 });
    expect(screen.getByText(/last moved 0:4\d ago/)).toBeInTheDocument();
  });

  it("flags it once it has really sat still a long while, not just a slow moment", () => {
    strip({ label: "x", processed: 1, total: 2, ts: Date.now() / 1000 - 150 });
    const stale = screen.getByText(/last moved 2:3\d ago/);
    expect(stale).toHaveClass("text-warn");
  });

  it("is not shown at all for a job that is not live (a finished or a stored one), even with an old timestamp", () => {
    render(<ProgressStrip p={{ label: "x", processed: 1, total: 2, ts: Date.now() / 1000 - 300 }} live={false} />);
    expect(screen.queryByText(/last moved/)).toBeNull();
  });
});
