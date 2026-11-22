import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { tipOf } from "../test/hint";
import type { Metrics } from "../lib/api";
import { MetricRow } from "./Viz";

const m = { tag: "t", n: 9, accuracy: 0.6, macro_f1: 0.5, per_class: {}, critical_false_negative_rate: 0.25, critical_total: 8, critical_missed: 2,
  split: "val", confusion_matrix: [], confusion_matrix_labels: [] } as unknown as Metrics;

describe("MetricRow", () => {
  it("shows the Critical rate with its denominator, the error type and the split", async () => {
    render(<MetricRow m={m} />);
    expect(screen.getByText("Critical missed")).toBeInTheDocument();
    expect(screen.getByText("25.0%")).toBeInTheDocument();
    const counts = screen.getByText("2 of 8 Critical missed");
    expect(await tipOf(counts)).toContain("lower severity");
    expect(screen.getByText("n (val split)")).toBeInTheDocument();
  });

  it("says when the counts are not exact", () => {
    render(<MetricRow m={{ ...m, critical_total: undefined, critical_missed: undefined, confusion_matrix: [[1, 0, 0], [0, 1, 0], [1, 2, 1]], confusion_matrix_labels: ["Recommendation", "Warning", "Critical"] }} />);
    expect(screen.getByText(/Critical missed \(answered items only\)/)).toBeInTheDocument();
  });
});
