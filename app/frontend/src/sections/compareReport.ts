import type { ConfigDiff, CweCell, CweData, DatasetFacts, DecisionCell, DiscoveryReport, EvalShared, RunCompare } from "../lib/api";
import { num, pct } from "../lib/format";

// The pure parts of the Compare view: what a row of a table says, and the comparison as a Markdown report. Nothing here draws or fetches, so it is tested on its own.

export const LETTERS = ["A", "B", "C", "D"] as const;

/** The discovery numbers of the arm a run is about, or null when it has none. */
export function discoveryOf(run: EvalShared): DiscoveryReport | null {
  const arm = run.run_info?.arm && run.arms[run.run_info.arm] ? run.run_info.arm : Object.keys(run.arms)[0];
  const d = arm ? run.arms[arm]?.generated_decision : undefined;
  return d && (d as { overall?: unknown }).overall ? (d as DiscoveryReport) : null;
}

/** Which split a run was measured on, in a few words, with what that means for a reader. */
export function splitShort(run: EvalShared): string {
  const split = run.split, role = run.run_info?.role;
  if (!split) return "split not recorded";
  return split === "val" ? `validation split${role ? `, ${role} role` : ""}; used to choose the checkpoint, so not a held-out result` : `${split} split`;
}

const share = (n: number, d: number) => (d > 0 ? pct(n / d) : "no cases");

/** "71% (5 of 7)", or "too few (n)" when the cell has too few cases to say, or "no cases". */
export function foundText(c: DecisionCell | undefined): string {
  if (!c) return "not measured";
  const v = c.vulnerable;
  return v.n === 0 ? "no cases" : v.insufficient ? `too few (${v.n})` : `${share(v.detected, v.n)} (${v.detected} of ${v.n})`;
}
export function alarmsText(c: DecisionCell | undefined): string {
  if (!c) return "not measured";
  const n = c.negative;
  return n.n === 0 ? "no cases" : n.insufficient ? `too few (${n.n})` : `${share(n.false_positives, n.n)} (${n.false_positives} of ${n.n})`;
}
export function insufficientText(c: DecisionCell | undefined): string {
  if (!c) return "not measured";
  const i = c.insufficient_context;
  return i.n === 0 ? "no cases" : `${share(i.correct, i.n)} (${i.correct} of ${i.n})`;
}

/** What a run found and how often it raised a false alarm in one weakness class: "71.4% (5 of 7)", "too few (n)" under the run's own minimum, or "no cases". */
export function cweText(cell: CweCell | null | undefined, minCell: number): { found: string; alarms: string } {
  if (!cell) return { found: "no data", alarms: "no data" };
  const one = (hit: number, n: number) => (n === 0 ? "no cases" : n < minCell ? `too few (${n})` : `${share(hit, n)} (${hit} of ${n})`);
  return { found: one(cell.detected, cell.vulnerable), alarms: one(cell.false_positives, cell.negatives) };
}

/** The keys of a breakdown that any of the runs has, in order. */
export function keysOf(reports: (DiscoveryReport | null)[], by: "by_language" | "by_ecosystem"): string[] {
  return [...new Set(reports.flatMap((r) => (r ? Object.keys(r[by] ?? {}) : [])))].sort();
}

/** The confusion matrices of runs on one grid: the truth labels and the outcomes that any run has. */
export function gridOf(reports: (DiscoveryReport | null)[]): { truths: string[]; outcomes: string[] } {
  const matrices = reports.map((r) => r?.overall.confusion ?? {});
  const truths = [...new Set(matrices.flatMap((m) => Object.keys(m)))].sort();
  const outcomes = [...new Set(matrices.flatMap((m) => Object.values(m).flatMap((row) => Object.keys(row))))].sort();
  return { truths, outcomes };
}

/** A count and, for a run other than the first, its change from the first run's. */
export function withDelta(value: number, reference: number | null): string {
  if (reference === null) return String(value);
  const d = value - reference;
  return d === 0 ? `${value} (same)` : `${value} (${d > 0 ? "+" : ""}${d})`;
}

/** A setting's value as a person reads it. */
export const shown = (v: unknown): string => (v === undefined || v === null ? "not set" : Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v));

/** The datasets' facts that differ, as rows with both values (the tag, the status, the counts, the examples of each task, the review tiers and the imported file are compared one by one). */
export function datasetRows(a: DatasetFacts | null, b: DatasetFacts | null): { field: string; a: string; b: string }[] {
  if (!a || !b) return [];
  const rows: { field: string; a: string; b: string }[] = [];
  const add = (field: string, x: unknown, y: unknown) => { if (JSON.stringify(x ?? null) !== JSON.stringify(y ?? null)) rows.push({ field, a: shown(x), b: shown(y) }); };
  const union = (x: object, y: object) => [...new Set([...Object.keys(x), ...Object.keys(y)])].sort();
  add("dataset", a.tag, b.tag);
  add("status", a.status, b.status);
  for (const k of union(a.counts, b.counts).filter((k) => typeof a.counts[k] !== "object" && typeof b.counts[k] !== "object")) add(k.replace(/_/g, " "), a.counts[k], b.counts[k]);
  for (const k of union(a.counts.views_by_task ?? {}, b.counts.views_by_task ?? {})) add(`examples of ${k.replace(/_/g, " ")}`, a.counts.views_by_task?.[k], b.counts.views_by_task?.[k]);
  for (const k of union(a.tier_mix, b.tier_mix)) add(`${k.replace(/_/g, " ")} rows`, a.tier_mix[k], b.tier_mix[k]);
  add("imported file", a.external?.original_file, b.external?.original_file);
  return rows;
}

const HASH = /\b[0-9a-f]{40,64}\b/g;
/** A hash shown as a word, unless Developer mode is on. */
export const hideHashes = (text: string, developer: boolean): string => (developer ? text : text.replace(HASH, "(hash)"));

/** The comparison as a Markdown report: the runs, the split of each, whether they may be compared, the verdicts and the tables. Hashes are left out unless Developer mode is on. */
export function buildReport(runs: EvalShared[], outs: (RunCompare | null)[], developer: boolean, diffs: (ConfigDiff | null)[] = [], cwe: CweData | null = null): string {
  const reports = runs.map(discoveryOf);
  const lines: string[] = ["# Comparison of evaluation runs", ""];
  runs.forEach((r, i) => {
    const info = r.run_info;
    lines.push(`* Run ${LETTERS[i]}: ${r.tag}${info?.baseline ? `, baseline ${info.baseline}` : ""}${info?.arm ? `, arm ${info.arm}` : ""}${info?.model ? `, model ${info.model}` : ""}. Measured on the ${splitShort(r)}.`);
  });
  lines.push("");
  outs.forEach((out, i) => {
    if (!out) return;
    lines.push(`## Run A against run ${LETTERS[i + 1]}`, "");
    if (!out.guard.comparable) {
      lines.push("These runs are not comparable, so no verdict is given:", ...out.guard.blockers.map((b) => `* ${b.reason} (${b.field}: A ${JSON.stringify(b.a)}, B ${JSON.stringify(b.b)})`), "");
    } else if (out.stats) {
      const s = out.stats;
      lines.push(`${s.verdict.replace(/_/g, " ")}. ${s.reason}`, "", `${num(s.items)} items answered by both; only A right ${num(s.only_a)}, only B right ${num(s.only_b)}.`, "");
    } else if (out.stats_note) {
      lines.push(out.stats_note, "");
    }
    for (const w of out.warnings) lines.push(`Note: ${w}`, "");
    const diff = diffs[i];
    if (diff) {
      const letter = LETTERS[i + 1];
      lines.push(`### What differs between run A and run ${letter}`, "");
      for (const [l, side] of [["A", diff.a], [letter, diff.b]] as const) if (!side.available) lines.push(`The configuration of run ${l} could not be found: ${side.reason ?? "unknown"}`, "");
      if (diff.config.compared) {
        if (diff.config.differing.length === 0) lines.push(`Trained with the same ${num(diff.config.same)} settings.`, "");
        else lines.push("| Setting | Run A | Run " + letter + " |", "|---|---|---|", ...diff.config.differing.map((d) => `| ${d.key} | ${shown(d.a)} | ${shown(d.b)} |`), "", `${num(diff.config.differing.length)} settings differ; ${num(diff.config.same)} are the same.`, "");
      }
      if (diff.dataset.a && diff.dataset.b) {
        if (diff.dataset.same) lines.push(`Both runs were trained on the same dataset: dataset_${diff.dataset.a.tag}.`, "");
        else {
          lines.push(`Different datasets: dataset_${diff.dataset.a.tag} against dataset_${diff.dataset.b.tag}.`, "");
          const rows = datasetRows(diff.dataset.a, diff.dataset.b);
          if (rows.length) lines.push("| What | Run A | Run " + letter + " |", "|---|---|---|", ...rows.map((r) => `| ${r.field} | ${r.a} | ${r.b} |`), "");
        }
      }
    }
  });
  const table = (title: string, head: string[], rows: string[][]) => {
    lines.push(`## ${title}`, "", `| ${head.join(" | ")} |`, `|${head.map(() => "---").join("|")}|`, ...rows.map((r) => `| ${r.join(" | ")} |`), "");
  };
  const cols = runs.map((_, i) => `Run ${LETTERS[i]}`);
  table("Headline", ["Measure", ...cols], [
    ["Cases", ...reports.map((d) => (d ? num(d.overall.n) : "not measured"))],
    ["Vulnerable found", ...reports.map((d) => foundText(d?.overall))],
    ["False alarms", ...reports.map((d) => alarmsText(d?.overall))],
    ["Insufficient context handled", ...reports.map((d) => insufficientText(d?.overall))],
    ["Abstained", ...reports.map((d) => (d ? pct(d.overall.abstention_rate) : "not measured"))],
  ]);
  for (const [by, title] of [["by_language", "By language"], ["by_ecosystem", "By ecosystem"]] as const) {
    const keys = keysOf(reports, by);
    if (keys.length) table(title, [title.replace("By ", ""), ...cols], keys.map((k) => [k, ...reports.map((d) => `found ${foundText(d?.[by]?.[k])}; false alarms ${alarmsText(d?.[by]?.[k])}`)]));
  }
  const { truths, outcomes } = gridOf(reports);
  if (truths.length) {
    runs.forEach((r, i) => {
      const m = reports[i]?.overall.confusion ?? {}, ref = reports[0]?.overall.confusion ?? {};
      table(`Confusion matrix, run ${LETTERS[i]}${i ? " (change from run A in brackets)" : ""}`, ["Truth / outcome", ...outcomes],
        truths.map((t) => [t, ...outcomes.map((o) => withDelta(m[t]?.[o] ?? 0, i ? ref[t]?.[o] ?? 0 : null))]));
    });
  }
  if (!cwe) lines.push("The table by weakness class (CWE) could not be read.", "");
  else {
    if (cwe.cwes.length) {
      table("By weakness class (CWE)", ["Weakness class", ...cols], cwe.cwes.map((c) => [c.cwe, ...c.cells.map((cell, i) => { const t = cweText(cell, cwe.runs[i]?.min_cell ?? 20); return `found ${t.found}; false alarms ${t.alarms}`; })]));
    }
    for (const [i, r] of cwe.runs.entries()) if (!r.available) lines.push(`Run ${LETTERS[i]} has no weakness classes: ${r.reason ?? "unknown"}.`, "");
    for (const [i, r] of cwe.runs.entries()) if (r.available && r.unmatched) lines.push(`Run ${LETTERS[i]}: ${num(r.unmatched)} of its ${num(r.items)} cases are not in its dataset's views and are left out.`, "");
    lines.push(cwe.note, "");
  }
  return hideHashes(lines.join("\n"), developer);
}
