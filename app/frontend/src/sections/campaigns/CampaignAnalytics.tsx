import { useCallback, useEffect, useMemo, useState } from "react";
import { BarList } from "../../components/charts/BarList";
import { Curve } from "../../components/charts/Curve";
import { FlowChart } from "../../components/charts/FlowChart";
import { ErrorState, Panel, Skeleton } from "../../components/ui";
import { useWhileActive } from "../../components/ActivePage";
import { api, type CampaignAnalytics, type CampaignRow } from "../../lib/api";
import { num } from "../../lib/format";
import { CoverageBars } from "./CoverageBars";
import { stageWord } from "./campaignFormat";
import { stamp } from "./eventText";
import { StageTimeline } from "./StageTimeline";

// The charts of one campaign, each with its numbers as a table: where it is, what it added from advisory to code pair, how fast it went, what it produced, what went wrong
// and, when it writes context, how far that got. They are read from what the campaign stored while it ran, so they stay after it ends.

const items = (counts: Record<string, number> | undefined) => Object.entries(counts ?? {}).map(([label, value]) => ({ label, value }));
const hours = (t0: number, at: string) => (Date.parse(at) - t0) / 3_600_000;

export function CampaignAnalyticsTab({ campaign }: { campaign: CampaignRow }) {
  const id = campaign.campaign_id;
  const busy = campaign.state === "running" || campaign.state === "pausing";
  const [data, setData] = useState<CampaignAnalytics | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => api.campaignAnalytics(id).then((d) => { setData(d); setError(null); }).catch(setError), [id]);
  useEffect(() => { setData(null); load(); }, [load]);
  useWhileActive(load, busy ? 10_000 : 60_000);

  const speed = useMemo(() => {
    const t = data?.throughput;
    if (!t) return null;
    const t0 = Date.parse(t.buckets[0]?.at ?? t.samples[0]?.at ?? data!.generated_at);
    return {
      work: [{ name: "Commits mined", points: t.buckets.map((b) => ({ x: hours(t0, b.at), y: b.commits })) }, { name: "Usable pairs", points: t.buckets.map((b) => ({ x: hours(t0, b.at), y: b.pairs })) }],
      memory: [{ name: "Memory of the process (MB)", points: t.samples.filter((s) => s.rss_mb != null).map((s) => ({ x: hours(t0, s.at), y: s.rss_mb! })) }],
      quota: [{ name: "GitHub calls left", points: t.samples.filter((s) => s.remaining != null).map((s) => ({ x: hours(t0, s.at), y: s.remaining! })) }],
      queue: [{ name: "Commits still queued", points: t.samples.filter((s) => s.pending != null).map((s) => ({ x: hours(t0, s.at), y: s.pending! })) }],
    };
  }, [data]);

  if (error != null && !data) return <ErrorState error={error} title="Could not read the analytics" onRetry={load} />;
  if (!data) return <div className="space-y-4"><Skeleton className="h-40 w-full" /><Skeleton className="h-40 w-full" /></div>;
  const f = data.funnel;
  const t = data.throughput;
  return (
    <div className="space-y-5">
      {data.note && <p role="note" className="border border-line bg-surface-2 px-4 py-3 text-[0.82rem] text-muted">{data.note}</p>}
      <StageTimeline steps={data.timeline.steps} now={data.generated_at} />

      <section aria-labelledby="yield-h" className="space-y-3">
        <h3 id="yield-h" className="text-[0.9rem] font-semibold">Yield from advisory to code pair</h3>
        <FlowChart data={f.flow} title="What the campaign added" description={f.has_baseline ? "How many advisories it read, how many have a fix commit, how many commits it worked on, and how many usable code pairs it made." : "No starting point was recorded, so these are the totals of its ecosystems now."} />
        <Panel title="By ecosystem" desc="Before the campaign started, now, and what it added.">
          <div className="overflow-x-auto">
            <table className="w-full text-[0.8rem]">
              <caption className="sr-only">What the campaign added to each ecosystem</caption>
              <thead><tr className="text-left text-faint"><th className="py-1.5 pr-4 font-medium">Ecosystem</th><th className="py-1.5 pr-3 text-right font-medium">Advisories</th>
                <th className="py-1.5 pr-3 text-right font-medium">With a fix commit</th><th className="py-1.5 pr-3 text-right font-medium">Mined</th><th className="py-1.5 text-right font-medium">Usable pairs</th></tr></thead>
              <tbody>
                {Object.entries(f.ecosystems).map(([eco, e]) => (
                  <tr key={eco} className="border-t border-line align-top">
                    <th scope="row" className="py-2 pr-4 text-left font-medium">{eco}</th>
                    {(["advisories", "with_fix_pointer", "mined", "usable_pairs"] as const).map((k) => (
                      <td key={k} className="tnum py-2 pr-3 text-right">{num(e.now[k])}<span className="block text-[0.72rem] text-faint">+{num(e.gained[k])} from {num(e.before[k])}</span></td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </section>

      {t && speed && (
        <section aria-labelledby="speed-h" className="space-y-3">
          <h3 id="speed-h" className="text-[0.9rem] font-semibold">Speed</h3>
          <p className="tnum text-[0.82rem] text-muted">{t.per_hour ? `While mining it made about ${num(t.per_hour.commits)} commits and ${num(t.per_hour.pairs)} pairs an hour.` : "It has not mined enough yet to give a speed."}{" "}
            Each point below is {t.bucket_seconds >= 3600 ? `${Math.round(t.bucket_seconds / 3600)} hour(s)` : `${Math.round(t.bucket_seconds / 60)} minutes`} of work.</p>
          <div className="grid gap-4 md:grid-cols-2">
            <Curve title="Commits and pairs over time" description="How many commits were mined, and how many usable pairs they gave, in each stretch of time." series={speed.work} xLabel="hours" yLabel="count" />
            <Curve title="Commits still queued" description="The mining queue of its ecosystems, sampled while it ran." series={speed.queue} xLabel="hours" yLabel="commits" />
            <Curve title="GitHub calls left" description="The quota the last request saw." series={speed.quota} xLabel="hours" yLabel="calls" />
            <Curve title="Memory of the process" description="The memory the campaign's process used, sampled while it ran." series={speed.memory} xLabel="hours" yLabel="MB" />
          </div>
          {t.waits.length > 0 && (
            <Panel title="Waits" desc="Times it waited for the GitHub rate limit or between batches.">
              <ul className="space-y-1 text-[0.8rem]">{t.waits.map((w, i) => <li key={i} className="tnum">{stamp(w.start)} to {w.end ? stamp(w.end) : "now"}</li>)}</ul>
            </Panel>
          )}
        </section>
      )}

      {data.mix && (
        <section aria-labelledby="mix-h" className="space-y-3">
          <h3 id="mix-h" className="text-[0.9rem] font-semibold">What it produced</h3>
          <p className="tnum text-[0.82rem] text-muted">{num(data.mix.pairs)} usable pairs from {num(data.mix.commits)} commits.</p>
          <div className="grid gap-4 md:grid-cols-2">
            <BarList title="Language" description="Usable pairs by programming language." items={items(data.mix.language)} unit="pairs" />
            <BarList title="Severity" description="By the severity word the advisory of the CVE gives." items={items(data.mix.severity)} unit="pairs" />
            <BarList title="Weakness class" description="The most frequent CWE classes." items={items(data.mix.cwe)} unit="pairs" />
            <BarList title="Granularity" description="File, function or hunk." items={items(data.mix.granularity)} unit="pairs" />
            {Object.keys(data.mix.ecosystem).length > 1 && <BarList title="Ecosystem" items={items(data.mix.ecosystem)} unit="pairs" />}
          </div>
        </section>
      )}

      <section aria-labelledby="errors-h" className="space-y-3">
        <h3 id="errors-h" className="text-[0.9rem] font-semibold">What went wrong</h3>
        {data.errors.by_cause.length === 0 && data.errors.commits.length === 0 && data.errors.forced_kills === 0 && data.errors.interruptions === 0
          ? <p className="text-[0.82rem] text-muted">Nothing has gone wrong.</p> : (
          <div className="grid gap-4 md:grid-cols-2">
            <BarList title="Failed steps by cause" description="Why steps failed: the GitHub limit, the network, git, or something else." items={data.errors.by_cause.map((c) => ({ label: c.cause.replace(/_/g, " "), value: c.n }))} unit="steps" empty="No step failed." />
            <BarList title="Commits that gave nothing" description="Why the commits it worked on failed, had no code, or were skipped." items={data.errors.commits.map((r) => ({ label: `${r.status.replace(/_/g, " ")}: ${r.reason}`, value: r.n }))} unit="commits" empty="Every commit gave code." />
          </div>
        )}
        {(data.errors.forced_kills > 0 || data.errors.interruptions > 0) && (
          <p className="text-[0.8rem] text-muted">It was interrupted {data.errors.interruptions} time(s) and its process had to be ended {data.errors.forced_kills} time(s) because a pause took too long. Only the step in progress was done again.</p>
        )}
        {data.errors.latest.length > 0 && (
          <Panel title="Latest failures">
            <ul className="space-y-1.5 text-[0.8rem]">{data.errors.latest.slice(-5).map((e, i) => (
              <li key={i}><span className="tnum mr-2 text-faint">{stamp(e.at)}</span>{e.stage ? `${stageWord(e.stage)}${e.target && e.target !== "*" ? ` (${e.target})` : ""}: ` : ""}<span className="text-crit-soft">{e.cause?.replace(/_/g, " ")}{e.message ? `: ${e.message}` : ""}</span></li>))}</ul>
          </Panel>
        )}
      </section>

      {data.context && (
        <section aria-labelledby="context-h" className="space-y-3">
          <h3 id="context-h" className="text-[0.9rem] font-semibold">Model-written context</h3>
          <p className="tnum text-[0.82rem] text-muted">
            {data.context.model ? `With ${data.context.model}${data.context.prompt_version ? ` (prompt ${data.context.prompt_version})` : ""}: ` : ""}{num(data.context.written)} drafts written, {num(data.context.failed)} failed, of {num(data.context.candidates)} offered
            {data.context.seconds_per_pair != null ? `; about ${data.context.seconds_per_pair} seconds a pair` : ""}.
          </p>
          <CoverageBars before={data.context.coverage_before} after={data.context.coverage_after} />
        </section>
      )}
    </div>
  );
}
