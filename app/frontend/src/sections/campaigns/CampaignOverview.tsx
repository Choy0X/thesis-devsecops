import { Panel } from "../../components/ui";
import type { CampaignDetail } from "../../lib/api";
import { num } from "../../lib/format";
import { elapsed, STEP_WORDS, stageWord } from "./campaignFormat";
import { DatasetFunnel } from "./DatasetFunnel";
import { detailSummary, stamp } from "./eventText";

// The first tab: how many steps are done, what is in each step, and what the campaign was admitted with.

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="border border-line bg-surface-2 px-4 py-3">
      <p className="text-[0.72rem] uppercase tracking-wider text-faint">{label}</p>
      <p className="tnum mt-0.5 text-[1.15rem] font-semibold">{value}</p>
      {note && <p className="text-[0.74rem] text-muted">{note}</p>}
    </div>
  );
}

const seconds = (start: string | null, end: string | null) => (start ? Math.max(0, Math.round(((end ? Date.parse(end) : Date.now()) - Date.parse(start)) / 1000)) : null);
const span = (s: number | null) => (s == null ? "—" : s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`);

export function CampaignOverview({ detail }: { detail: CampaignDetail }) {
  const { campaign: c, steps } = detail;
  const done = steps.filter((s) => ["done", "quota", "skipped"].includes(s.status)).length;
  const failed = steps.filter((s) => s.status === "error").length;
  const d = c.declared ?? {};
  return (
    <div className="space-y-5">
      {c.error && <p role="alert" className="border border-crit-line px-4 py-3 text-[0.84rem] text-crit-soft">{c.error}</p>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Tile label="Steps done" value={`${done} of ${steps.length}`} note={failed ? `${failed} failed` : c.legacy ? "earlier kind: steps only" : undefined} />
        <Tile label="Time" value={elapsed(c) ?? "—"} note={c.resumes ? `resumed ${c.resumes} time${c.resumes === 1 ? "" : "s"}` : "not resumed"} />
        <Tile label="Started" value={stamp(c.started_at).slice(5, 16) || "—"} note={c.finished_at ? `ended ${stamp(c.finished_at).slice(0, 16)}` : undefined} />
        <Tile label="Admitted with" value={d.cpu != null ? `${d.cpu} core${d.cpu === 1 ? "" : "s"}` : "—"} note={d.ram_gb != null ? `${d.ram_gb} GB, ${num(d.github_per_hour ?? 0)} calls an hour` : undefined} />
      </div>
      <Panel title="Steps" desc="Each ecosystem takes its stages in order. A step that stopped at the quota is continued by a larger quota; a step that failed is tried again when the campaign is resumed.">
        <div className="overflow-x-auto">
          <table className="w-full text-[0.8rem]">
            <caption className="sr-only">The steps of the campaign</caption>
            <thead><tr className="text-left text-faint">
              <th className="py-1.5 pr-4 font-medium">Ecosystem</th><th className="py-1.5 pr-4 font-medium">Stage</th><th className="py-1.5 pr-4 font-medium">State</th>
              <th className="py-1.5 pr-4 text-right font-medium">Time</th><th className="py-1.5 pr-4 text-right font-medium">Tries</th><th className="py-1.5 font-medium">Result</th>
            </tr></thead>
            <tbody>
              {steps.map((s) => (
                <tr key={`${s.target}|${s.stage}`} className="border-t border-line align-top">
                  <td className="py-2 pr-4">{s.target === "*" ? "Whole campaign" : s.target}</td>
                  <td className="py-2 pr-4">{stageWord(s.stage)}</td>
                  <td className="py-2 pr-4"><span data-status={s.status} className={s.status === "error" ? "font-medium text-crit-soft" : s.status === "running" ? "font-medium text-brand-strong" : "text-muted"}>{STEP_WORDS[s.status]}</span></td>
                  <td className="tnum py-2 pr-4 text-right text-muted">{span(seconds(s.started_at, s.finished_at))}</td>
                  <td className="tnum py-2 pr-4 text-right text-muted">{s.attempts || "—"}</td>
                  <td className="py-2 text-muted">{s.error ? <span className="font-mono text-[0.72rem] text-crit-soft">{s.error}</span> : detailSummary(s.detail) || "—"}</td>
                </tr>
              ))}
              {steps.length === 0 && <tr><td colSpan={6} className="py-6 text-center text-muted">No steps were recorded.</td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>
      {steps.filter((s) => Array.isArray(s.detail?.funnel) && s.detail.funnel.length > 0).map((s) => (
        <Panel key={`funnel|${s.target}`} title="Where the rows of the draft dataset went" desc="Each filter of the build, in order: the rows it left and the rows it dropped. A pair that fails one is not looked at by the next.">
          <DatasetFunnel detail={s.detail as any} />
        </Panel>
      ))}
    </div>
  );
}
