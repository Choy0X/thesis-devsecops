import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Job } from "../lib/api";
import { JobStrip, STRIP_JOBS, length, recent, seconds } from "./JobStrip";

const T0 = Date.parse("2026-10-09T08:00:00Z");
const job = (id: string, status: string, startMin: number, secs: number, stage = "mine_code"): Job => ({
  id, stage, status, params: {}, started: new Date(T0 + startMin * 60_000).toISOString(),
  finished: status === "running" ? "" : new Date(T0 + startMin * 60_000 + secs * 1000).toISOString(), error: "", log_count: 0,
});
const NOW = T0 + 60 * 60_000;

describe("JobStrip", () => {
  it("shows nothing for fewer than two jobs", () => {
    const { container } = render(<JobStrip jobs={[job("a", "done", 0, 10)]} now={NOW} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists the jobs oldest first and says state and length in words for each", () => {
    render(<JobStrip jobs={[job("c", "error", 20, 5, "ingest"), job("a", "done", 0, 125), job("b", "interrupted", 10, 30)]} now={NOW} />);
    const items = screen.getAllByRole("listitem");
    expect(items.map((i) => i.getAttribute("aria-label"))).toEqual(["Mine code pairs, finished, 2 min 5 s", "Mine code pairs, cut off, 30 s", "OSV ingest, failed, 5 s"]);
    expect(items[0]).toHaveAttribute("tabindex", "0");
  });

  it("tells failed, cut off and stopped apart without colour", () => {
    render(<JobStrip jobs={[job("a", "error", 0, 5), job("b", "interrupted", 1, 5), job("c", "cancelled", 2, 5), job("d", "done", 3, 5), job("e", "running", 4, 0)]} now={NOW} />);
    const cls = (i: number) => screen.getAllByRole("listitem")[i].className;
    expect(screen.getAllByRole("listitem")[0].style.backgroundImage).toContain("repeating-linear-gradient");
    expect(cls(1)).toContain("border-dotted");
    expect(cls(2)).toContain("border-dashed");
    expect(screen.getAllByRole("listitem")[3].style.backgroundImage).toBe("");
    expect(cls(4)).toContain("border-r-0");
    expect(screen.getByText("open end: running")).toBeInTheDocument();
  });

  it("makes a longer job a wider block on a logarithmic scale", () => {
    render(<JobStrip jobs={[job("a", "done", 0, 2), job("b", "done", 5, 3600)]} now={NOW} />);
    const [short, long] = screen.getAllByRole("listitem").map((i) => Number(i.style.flexGrow));
    expect(long).toBeGreaterThan(short);
    expect(long / short).toBeLessThan(10);
  });

  it("keeps only the most recent jobs and ignores one without a start time", () => {
    const many = Array.from({ length: 40 }, (_, i) => job(`j${i}`, "done", i, 5));
    const list = recent([...many, { ...job("x", "done", 0, 1), started: "" }]);
    expect(list).toHaveLength(STRIP_JOBS);
    expect(list[list.length - 1].id).toBe("j39");
    expect(list.some((j) => j.id === "x")).toBe(false);
  });

  it("measures a running job up to now and formats a length", () => {
    expect(seconds(job("r", "running", 0, 0), T0 + 90_000)).toBe(90);
    expect(length(59)).toBe("59 s");
    expect(length(3 * 3600 + 120)).toBe("3 h 2 min");
  });
});
