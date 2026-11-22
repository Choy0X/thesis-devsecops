import { Fragment } from "react";
import { Panel } from "../../components/ui";
import type { CampaignRow } from "../../lib/api";
import { Dev } from "../../lib/devMode";
import { num } from "../../lib/format";
import { stageWord } from "./campaignFormat";
import { STAGE_ABOUT } from "./startRequest";

// What the campaign was asked to do, as it was chosen: read-only. A campaign is not edited; to change the plan, start another.

const KNOB_WORDS: Record<string, string> = {
  mine_workers: "Commits mined at once", pause_seconds: "Pause between batches (seconds)", deps_workers: "Registries asked at once", recover_limit: "Most records to recover",
  recover_workers: "GitHub requests at once", quota: "Most commits per ecosystem", refresh: "Ask OSV for a newer dump first",
};
const STAGES = ["ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions", "enrich", "build"];

export function CampaignConfig({ campaign: c }: { campaign: CampaignRow }) {
  const plan = c.plan;
  if (!plan) return <Panel title="Plan"><p className="text-[0.84rem] text-muted">This campaign was made by the earlier kind of campaign. It ran the standard stages for each ecosystem, and its plan was not recorded.</p></Panel>;
  const on = STAGES.filter((s) => plan.stages[s]);
  return (
    <div className="space-y-5">
      <Panel title="Plan" desc="As it was chosen when the campaign started.">
        <dl className="grid grid-cols-[11rem_1fr] gap-x-4 gap-y-2 text-[0.84rem]">
          <dt className="text-muted">Ecosystems</dt><dd>{plan.ecosystems.join(", ")}</dd>
          <dt className="text-muted">Started from</dt><dd>{plan.preset ? plan.preset.replace(/_/g, " ") : "a custom set of stages"}</dd>
          {plan.build_profile && <><dt className="text-muted">Dataset profile</dt><dd>{plan.build_profile}</dd></>}
          {plan.stages.enrich && <><dt className="text-muted">Model-written context</dt><dd>{plan.context.profile.replace(/_/g, " ")}, {plan.context.workers} request{plan.context.workers === 1 ? "" : "s"} at once{plan.context.cap ? `, at most ${num(plan.context.cap)} pairs per ecosystem` : ""}</dd></>}
        </dl>
      </Panel>
      <Panel title="Stages" desc="In the order they run for each ecosystem.">
        <ul className="divide-y divide-line border border-line text-[0.82rem]">
          {STAGES.map((s) => (
            <li key={s} data-on={!!plan.stages[s]} className="flex gap-3 px-3 py-2">
              <span className="w-12 shrink-0 font-medium">{plan.stages[s] ? "On" : "Off"}</span>
              <span><span className="font-medium">{stageWord(s)}</span><span className="block text-[0.76rem] text-muted">{STAGE_ABOUT[s]}</span></span>
            </li>
          ))}
        </ul>
        {on.length === 0 && <p className="mt-2 text-[0.8rem] text-muted">No stage is on.</p>}
      </Panel>
      <Panel title="Settings">
        <dl className="grid grid-cols-[16rem_1fr] gap-x-4 gap-y-1.5 text-[0.84rem]">
          {Object.entries(plan.knobs).map(([k, v]) => (
            <Fragment key={k}><dt className="text-muted">{KNOB_WORDS[k] ?? k.replace(/_/g, " ")}</dt><dd className="tnum">{v === null ? "default" : v === true ? "yes" : v === false ? "no" : String(v)}</dd></Fragment>
          ))}
        </dl>
      </Panel>
      <Panel title="Admitted with" desc="What the campaign was judged to need of this computer when it started; a campaign that is paused holds none of it.">
        <dl className="grid grid-cols-[16rem_1fr] gap-x-4 gap-y-1.5 text-[0.84rem]">
          <dt className="text-muted">Processor cores</dt><dd className="tnum">{c.declared.cpu ?? "—"}</dd>
          <dt className="text-muted">Memory</dt><dd className="tnum">{c.declared.ram_gb != null ? `${c.declared.ram_gb} GB` : "—"}</dd>
          <dt className="text-muted">GitHub calls an hour</dt><dd className="tnum">{c.declared.github_per_hour != null ? num(c.declared.github_per_hour) : "—"}</dd>
          <dt className="text-muted">Disk kept free</dt><dd className="tnum">{c.declared.disk_gb != null ? `${c.declared.disk_gb} GB` : "—"}</dd>
        </dl>
        <Dev><p className="mt-3 break-all font-mono text-[0.7rem] text-faint">Its settings are also kept in experiments/campaigns/{c.campaign_id}/run_config.json (the model and its host when it writes context, never a key or a token).</p></Dev>
      </Panel>
    </div>
  );
}
