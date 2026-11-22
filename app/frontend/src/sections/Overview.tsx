import { useEffect, useState } from "react";
import { ArrowRight, Boxes, Check, Circle, CircleDot, FolderGit2, HelpCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type BuildInfo, type CodeSummary, type Metrics, type StoreSummary } from "../lib/api";
import { Panel, Skeleton, EmptyState, ErrorState } from "../components/ui";
import { Icon } from "../components/Icon";
import { num, pct } from "../lib/format";
import { CRITICAL_MEANING, criticalCountText, criticalMiss } from "../lib/critical";
import { useOnActivate } from "../components/ActivePage";
import { Dev } from "../lib/devMode";
import { useJobs } from "../lib/jobsList";
import { stageLabel } from "../lib/stageLabels";
import { pipelineSteps, recommended, type Step } from "../lib/pipelineSteps";
import { DevicePanel } from "../components/DevicePanel";
import { MixPanel } from "../components/charts/Panels";
import { Hint } from "../components/kit";

// The tiers of review in words a person uses; the stored names stay in Developer mode.
const TIER: Record<string, string> = { auto_checked: "Checked by rules", llm_adjudicated: "Model-adjudicated", human_reviewed: "Human reviewed", unreviewed: "Unreviewed" };

const ICON: Record<Step["state"], JSX.Element> = {
  done: <Check size={14} aria-hidden />, next: <CircleDot size={14} aria-hidden />, todo: <Circle size={14} aria-hidden />, unknown: <HelpCircle size={14} aria-hidden />,
};
const STATE_WORD: Record<Step["state"], string> = { done: "Done", next: "Next", todo: "Not yet", unknown: "Reading" };

export function Overview({ go }: { go: (id: string) => void }) {
  const [records, setRecords] = useState<StoreSummary | null | undefined>(undefined);
  const [pairs, setPairs] = useState<CodeSummary | null | undefined>(undefined);
  const [build, setBuild] = useState<BuildInfo | null | undefined>(undefined);
  const [runs, setRuns] = useState<Metrics[] | null | undefined>(undefined);
  const [failed, setFailed] = useState<unknown[]>([]);
  const [exit, setExit] = useState<{ clean: boolean; note: string } | null>(null);
  const jobs = useJobs();

  const load = () => {
    setFailed([]);
    const fail = (e: unknown) => setFailed((f) => [...f, e]);
    api.storeSummary().then(setRecords).catch((e) => { setRecords(null); fail(e); });
    api.codeSummary().then(setPairs).catch((e) => { setPairs(null); fail(e); });
    api.builds().then((b) => setBuild(b.builds[0] || null)).catch((e) => { setBuild(null); fail(e); });
    api.experiments().then((e) => setRuns(e.filter((x: any) => typeof x.macro_f1 === "number") as Metrics[])).catch((e) => { setRuns([]); fail(e); });
    api.lifecycle().then(setExit).catch(() => setExit(null));              // ignored: the notice about the last exit is an extra
  };
  useEffect(load, []);
  useOnActivate(load);

  const steps = pipelineSteps({ records, pairs, build, runs });
  const next = recommended(steps);
  const best = runs && runs.length ? runs.reduce((a, b) => (b.macro_f1 > a.macro_f1 ? b : a)) : null;
  const recent = jobs.slice(0, 5);
  const trouble = jobs.filter((j) => j.status === "error" || j.status === "interrupted").slice(0, 3);

  return (
    <div className="space-y-5">
      {failed.length > 0 && <ErrorState error={failed[0]} title={`${failed.length} of 4 summaries could not be loaded`} onRetry={load} />}

      <div className="grid gap-3 sm:grid-cols-2">
        <Link to="/projects" className="group flex items-center gap-3 border border-line bg-surface p-4 hover:border-brand-line hover:bg-surface-2">
          <FolderGit2 size={19} className="shrink-0 text-brand-strong" aria-hidden /><span className="min-w-0 flex-1">
            <strong className="block text-sm">Open Projects</strong><span className="text-[0.8rem] text-muted">Check dependencies, review source, and inspect changes.</span>
          </span><ArrowRight size={15} className="shrink-0 text-muted group-hover:text-brand-strong" aria-hidden />
        </Link>
        <Link to="/models" className="group flex items-center gap-3 border border-line bg-surface p-4 hover:border-brand-line hover:bg-surface-2">
          <Boxes size={19} className="shrink-0 text-brand-strong" aria-hidden /><span className="min-w-0 flex-1">
            <strong className="block text-sm">Manage models</strong><span className="text-[0.8rem] text-muted">Install and manage local Ollama models.</span>
          </span><ArrowRight size={15} className="shrink-0 text-muted group-hover:text-brand-strong" aria-hidden />
        </Link>
      </div>

      <Panel title="Data pipeline"
        right={next ? <button onClick={() => go(next.id === "collect" ? "collection" : next.id === "records" ? "data" : next.id === "pairs" ? "pairs" : next.id === "dataset" ? "build" : next.id === "train" ? "train" : "sandbox")}
          className="inline-flex items-center gap-1.5 bg-brand px-3 py-1.5 text-[0.82rem] font-semibold text-on-brand hover:bg-brand-hover">{next.cta}<ArrowRight size={14} aria-hidden /></button> : undefined}>
        <ol className="grid gap-2 md:grid-cols-3 xl:grid-cols-6" aria-label="Pipeline">
          {steps.map(s => (
            <li key={s.id} data-state={s.state} className={`border px-3 py-2.5 ${s.state === "next" ? "border-brand bg-brand-tint" : "border-line bg-surface-2"}`}>
              <div className="flex items-center gap-1.5 text-[0.68rem] font-semibold uppercase tracking-wider text-faint">
                <span className={s.state === "done" ? "text-ok" : s.state === "next" ? "text-brand-strong" : ""}>{ICON[s.state]}</span>{STATE_WORD[s.state]}
              </div>
              <div className="mt-1 text-[0.86rem] font-medium">{s.title}</div>
              <div className="mt-0.5 text-[0.76rem] leading-snug text-muted">{s.detail}</div>
            </li>
          ))}
        </ol>
      </Panel>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Severity classification results">
          {runs === undefined ? <Skeleton className="h-20 w-full" /> : best ? (
            <div className="space-y-3">
              <div>
                <div className="text-[0.72rem] uppercase tracking-wider text-faint">Critical vulnerabilities missed</div>
                <div className="tnum text-2xl font-semibold tracking-tight">{best.critical_false_negative_rate == null ? "—" : pct(best.critical_false_negative_rate)}</div>
                <p className="text-[0.76rem] text-muted"><Hint label={CRITICAL_MEANING}>{criticalCountText(criticalMiss(best))}</Hint>. A miss is a wrong lower severity or no answer.</p>
              </div>
              <div className="flex flex-wrap gap-x-6 gap-y-1">
                <Stat k="accuracy" v={pct(best.accuracy)} />
                <Stat k="macro F1" v={best.macro_f1.toFixed(3)} />
                <Stat k="evaluated on" v={best.split ? `${best.split} split` : "split not recorded"} />
              </div>
              <Dev><p className="font-mono text-[0.7rem] text-faint">run {best.tag}</p></Dev>
            </div>
          ) : <EmptyState icon={<Icon.results />} title="No evaluated run yet" hint="Train and evaluate a model to see how many Critical vulnerabilities it misses." />}
        </Panel>

        <Panel title="Latest dataset"
          right={<Link to="/build" className="text-[0.8rem] text-brand-strong hover:underline">Open datasets</Link>}>
          {build === undefined ? <Skeleton className="h-16 w-full" /> : build ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
                <Stat k="status" v={build.status === "frozen" ? "Frozen" : "Preview"} />
                <Stat k="records" v={num(build.counts?.records)} />
                <Stat k="training examples" v={num(build.counts?.views)} />
              </div>
              <ul className="space-y-1 text-[0.8rem]" aria-label="Review tiers">
                {Object.entries(build.tier_mix).map(([tier, count]) => (
                  <li key={tier} className="flex justify-between"><span className="text-muted">{TIER[tier] ?? tier.replace(/_/g, " ")}</span><span className="tnum">{num(count)}</span></li>
                ))}
              </ul>
              <Dev><p className="font-mono text-[0.7rem] text-faint">dataset_{build.tag}</p></Dev>
            </div>
          ) : <p className="text-[0.82rem] text-muted">No dataset has been built yet. Build one from the code pairs.</p>}
        </Panel>

        <DevicePanel />

        <Panel title="Recent jobs and alerts"
          right={<span className="text-[0.74rem] text-faint">{jobs.filter((j) => j.status === "running").length} running</span>}>
          {(trouble.length > 0 || (exit && !exit.clean)) && (
            <ul className="mb-3 space-y-1.5" aria-label="Alerts">
              {exit && !exit.clean && <li className="border border-warn-line bg-warn-tint px-3 py-1.5 text-[0.8rem] text-warn">{exit.note}</li>}
              {trouble.map((j) => (
                <li key={j.id} className="border border-crit-line bg-crit-tint px-3 py-1.5 text-[0.8rem] text-crit-soft">
                  {stageLabel(j.stage)} {j.status === "interrupted" ? "was cut off when the app closed" : "stopped with an error"}.
                </li>
              ))}
            </ul>
          )}
          {recent.length === 0 ? <p className="text-[0.82rem] text-muted">Nothing has run yet.</p> : (
            <ul className="space-y-1 text-[0.82rem]">
              {recent.map((j) => (
                <li key={j.id} className="flex items-center justify-between gap-3">
                  <span>{stageLabel(j.stage)}</span>
                  <span className="text-[0.74rem] text-muted">{j.status === "error" ? "failed" : j.status === "done" ? "finished" : j.status}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
      <Panel title="Advisory coverage" desc="Collected advisories by language, ecosystem, and severity.">
        <MixPanel />
      </Panel>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <div className="text-[0.72rem] text-muted">{k}</div>
      <div className="tnum text-[1.05rem] font-semibold tracking-tight">{v}</div>
    </div>
  );
}
