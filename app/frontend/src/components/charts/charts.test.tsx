import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { lttb, type Pt } from "../../lib/lttb";
import { Curve } from "./Curve";
import { FlowChart, type Lineage } from "./FlowChart";
import { RadarChart, radarPoints } from "./RadarChart";

const wave = (n: number): Pt[] => Array.from({ length: n }, (_, i) => ({ x: i, y: Math.sin(i / 50) + (i === 12_345 ? 5 : 0) }));

describe("lttb", () => {
  it("keeps the ends and the requested number of points, and the peak a plain stride would miss", () => {
    const data = wave(20_000);
    const out = lttb(data, 600);
    expect(out).toHaveLength(600);
    expect(out[0]).toBe(data[0]);
    expect(out[out.length - 1]).toBe(data[data.length - 1]);
    expect(out.some((p) => p.x === 12_345)).toBe(true);
    expect(out.every((p, i) => i === 0 || p.x > out[i - 1].x)).toBe(true);
  });
  it("returns a short series untouched", () => {
    const data = wave(10);
    expect(lttb(data, 600)).toBe(data);
  });
  it("is fast enough for 20,000 points", () => {
    const data = wave(20_000);
    const t = performance.now();
    lttb(data, 600);
    expect(performance.now() - t).toBeLessThan(50);
  });
});

describe("RadarChart", () => {
  const axes = [{ label: "javascript", value: 40 }, { label: "python", value: 20 }, { label: "go", value: 10 }];
  it("puts the largest value on the outer ring and the others in proportion", () => {
    const p = radarPoints([40, 20, 10], 40, 100, 0, 0);
    expect(Math.hypot(...p[0])).toBeCloseTo(100, 0);
    expect(Math.hypot(...p[1])).toBeCloseTo(50, 0);
  });
  it("gives every corner a name and a value for a screen reader, and the same numbers as a table", () => {
    render(<RadarChart title="Languages" axes={axes} unit="records" />);
    expect(screen.getByRole("img", { name: "javascript: 40 records" })).toHaveAttribute("tabindex", "0");
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "Languages" });
    expect(within(table).getByText("javascript")).toBeInTheDocument();
    expect(within(table).getByText("57%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View as chart" }));
    expect(screen.getByRole("group", { name: /Languages: javascript 40/ })).toBeInTheDocument();
  });
  it("shows a table instead of a radar for fewer than three categories, and says so for none", () => {
    const { rerender } = render(<RadarChart title="Mix" axes={axes.slice(0, 2)} />);
    expect(screen.getByText(/needs at least three categories/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Mix" })).toBeInTheDocument();
    rerender(<RadarChart title="Mix" axes={[]} />);
    expect(screen.getByText("There is nothing to draw yet.")).toBeInTheDocument();
  });
});

describe("FlowChart", () => {
  const data: Lineage = {
    stages: [
      { id: "a", label: "Advisories", unit: "advisories", count: 100, lost: [] },
      { id: "b", label: "With a fix commit", unit: "advisories", count: 60, lost: [{ id: "n", label: "No fix commit known", count: 40 }] },
      { id: "c", label: "Usable pairs", unit: "pairs", count: 30, lost: [{ id: "t", label: "Test files", count: 5 }, { id: "z", label: "Zero", count: 0 }], reasons_overlap: true },
    ],
    set_aside: { malware: 7, withdrawn: 0 },
  };
  it("says in words how many reached each stage and why the rest dropped out", () => {
    render(<FlowChart data={data} />);
    expect(screen.getByRole("listitem", { name: "With a fix commit: 60 advisories, 40 dropped out" })).toBeInTheDocument();
    expect(screen.getByText(/no fix commit known/)).toBeInTheDocument();
    expect(screen.getByText(/a pair can have more than one reason/)).toBeInTheDocument();
    expect(screen.queryByText(/zero/)).toBeNull();
    expect(screen.getByText(/Not counted above: malware 7\./)).toBeInTheDocument();
  });
  it("has the same numbers as a table", () => {
    render(<FlowChart data={data} />);
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "From advisory to dataset" });
    expect(within(table).getByText("No fix commit known 40")).toBeInTheDocument();
    expect(within(table).getAllByRole("row")).toHaveLength(4);
  });
});

describe("Curve", () => {
  it("draws at most the points asked for from a series of 20,000, and at most four series", () => {
    const series = ["a", "b", "c", "d", "e"].map((name) => ({ name, points: wave(20_000) }));
    const { container } = render(<Curve title="Loss" series={series} maxPoints={500} />);
    const lines = container.querySelectorAll("polyline");
    expect(lines).toHaveLength(4);
    expect(lines[0].getAttribute("points")!.split(" ")).toHaveLength(500);
    expect(screen.getByText("1 more not drawn")).toBeInTheDocument();
    expect(new Set(Array.from(lines).map((l) => l.getAttribute("stroke-dasharray")))).toHaveProperty("size", 4);
  });
  it("describes itself for a screen reader and offers the table", () => {
    render(<Curve title="Loss" series={[{ name: "train", points: [{ x: 0, y: 1 }, { x: 1, y: 0.5 }] }]} />);
    expect(screen.getByRole("img", { name: /Loss\. train: 2 points, last 0\.5/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    expect(within(screen.getByRole("table", { name: "Loss" })).getByText("train")).toBeInTheDocument();
  });
  it("says so when there is nothing to draw", () => {
    render(<Curve title="Loss" series={[{ name: "train", points: [] }]} />);
    expect(screen.getByText("There are no points to draw yet.")).toBeInTheDocument();
  });
});
