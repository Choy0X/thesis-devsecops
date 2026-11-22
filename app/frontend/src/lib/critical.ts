import type { Metrics } from "./api";

export type CriticalMiss = { rate: number; missed: number; total: number; exact: boolean };

/**
 * The headline safety number with its denominator: Critical vulnerabilities that were missed (predicted as a lower
 * severity, or not answered) out of all Critical items. Runs scored before the counts were recorded fall back to the
 * answered Critical items from the confusion matrix and are marked inexact.
 */
export function criticalMiss(m: Metrics): CriticalMiss | null {
  const rate = m.critical_false_negative_rate;
  if (rate == null) return null;
  if (m.critical_total != null && m.critical_missed != null) {
    return { rate, missed: m.critical_missed, total: m.critical_total, exact: true };
  }
  const row = m.confusion_matrix?.[m.confusion_matrix_labels?.indexOf("Critical")];
  if (!row) return { rate, missed: NaN, total: NaN, exact: false };
  const total = row.reduce((a, b) => a + b, 0);
  return { rate, missed: Math.round(rate * total), total, exact: false };
}

/** "3 of 12 missed" (or the best available text when the counts are not exact). */
export function criticalCountText(c: CriticalMiss | null): string {
  if (!c) return "no Critical items in this run";
  if (Number.isNaN(c.total)) return "counts not recorded";
  return `${c.missed} of ${c.total} Critical missed${c.exact ? "" : " (answered items only)"}`;
}

export const CRITICAL_MEANING = "A Critical vulnerability counts as missed when the model predicts a lower severity or gives no answer.";
