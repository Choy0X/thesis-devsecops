import { describe, expect, it } from "vitest";
import type { Metrics } from "./api";
import { criticalCountText, criticalMiss } from "./critical";

const base = { tag: "t", n: 10, accuracy: 0.5, macro_f1: 0.4, per_class: {}, confusion_matrix: [[3, 0, 0], [1, 2, 0], [1, 1, 2]],
  confusion_matrix_labels: ["Recommendation", "Warning", "Critical"] } as unknown as Metrics;

describe("criticalMiss", () => {
  it("uses the recorded counts", () => {
    const c = criticalMiss({ ...base, critical_false_negative_rate: 0.5, critical_total: 4, critical_missed: 2 });
    expect(c).toEqual({ rate: 0.5, missed: 2, total: 4, exact: true });
    expect(criticalCountText(c)).toBe("2 of 4 Critical missed");
  });

  it("falls back to the confusion matrix for older runs and says so", () => {
    const c = criticalMiss({ ...base, critical_false_negative_rate: 0.5 });
    expect(c).toEqual({ rate: 0.5, missed: 2, total: 4, exact: false });
    expect(criticalCountText(c)).toBe("2 of 4 Critical missed (answered items only)");
  });

  it("is null without a rate", () => {
    const c = criticalMiss({ ...base, critical_false_negative_rate: null });
    expect(c).toBeNull();
    expect(criticalCountText(c)).toBe("no Critical items in this run");
  });

  it("does not invent counts when there is no matrix", () => {
    const c = criticalMiss({ ...base, confusion_matrix: [], critical_false_negative_rate: 0.25 });
    expect(criticalCountText(c)).toBe("counts not recorded");
  });
});
