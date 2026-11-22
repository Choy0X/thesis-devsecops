import type { BuildInfo, CodeSummary, Metrics, StoreSummary } from "./api";

// The path from collecting advisories to a verified result, as six steps with a state each and one recommended next action. The state is computed
// from numbers the app already has; a step whose numbers are not known yet is shown as unknown, never as done.

export type StepState = "done" | "next" | "todo" | "unknown";
export type Step = { id: string; title: string; detail: string; state: StepState; to: string; cta: string };

export type Inputs = {
  records: StoreSummary | null | undefined;
  pairs: CodeSummary | null | undefined;
  build: BuildInfo | null | undefined;
  runs: Metrics[] | null | undefined;
};

const n = (x: number | undefined | null) => (x ?? 0).toLocaleString("en-US");

type Row = { id: string; title: string; done: string; empty: string; measure: number | null | undefined; to: string; cta: string };

export function pipelineSteps({ records, pairs, build, runs }: Inputs): Step[] {
  const rows: Row[] = [
    { id: "collect", title: "Collect advisories", done: `${n(records?.total)} advisories collected`, empty: "No advisories collected yet",
      measure: records ? records.total : records, to: "/collections", cta: "Collect advisories" },
    { id: "records", title: "Records with fixes", done: `${n(records?.by_code_status?.mined)} records have mined code`, empty: "No record has mined code yet",
      measure: records ? (records.by_code_status?.mined ?? 0) : records, to: "/records", cta: "Open the records" },
    { id: "pairs", title: "Code pairs", done: `${n(pairs?.pairs?.usable)} usable pairs`, empty: "No usable code pairs yet",
      measure: pairs ? (pairs.pairs?.usable ?? 0) : pairs, to: "/pairs", cta: "Open the code pairs" },
    { id: "dataset", title: "Dataset", done: build ? `Version ${build.tag} (${build.status}), ${n(build.counts?.records)} records` : "", empty: "No dataset built yet",
      measure: build === undefined ? undefined : build ? 1 : 0, to: "/build", cta: "Build a dataset" },
    { id: "train", title: "Train and evaluate", done: `${runs?.length ?? 0} evaluated run${runs?.length === 1 ? "" : "s"}`, empty: "No evaluated run yet",
      measure: runs ? runs.length : runs, to: "/train", cta: "Open Train" },
    { id: "verify", title: "Verify fixes", done: "", empty: "Verification has not started", measure: 0, to: "/sandbox", cta: "Open the sandbox" },
  ];
  let nextFound = false;
  return rows.map((r) => {
    let state: StepState;
    if (r.measure == null) state = "unknown";
    else if (r.measure > 0) state = "done";
    else if (!nextFound) { state = "next"; nextFound = true; }
    else state = "todo";
    return { id: r.id, title: r.title, detail: state === "done" ? r.done : state === "unknown" ? "Reading…" : r.empty, state, to: r.to, cta: r.cta };
  });
}

/** The one thing to do next: the first step that is not done. */
export const recommended = (steps: Step[]): Step | null => steps.find((s) => s.state === "next") ?? null;
