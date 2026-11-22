import { useEffect, useMemo, useState } from "react";
import { ErrorState, Input, Panel, Skeleton } from "../components/ui";
import { Dev } from "../lib/devMode";
import { Checkbox, Hint } from "../components/kit";
import { api, type TrainConfig, type TrainSetting } from "../lib/api";

// How a training run is configured, read from the configuration the experiments are pinned to: every setting with its value, its limits, and whether it
// may be changed in this phase. Read-only: this page shows what a run will use; choosing values for a run comes with the run setup.

export const words = (name: string) => name.replace(/_/g, " ");

export function valueText(v: unknown): string {
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (v === null || v === undefined) return "not set";
  if (Array.isArray(v)) return v.length === 0 ? "none" : v.map(valueText).join(", ");
  if (typeof v === "object") return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${words(k)} ${valueText(x)}`).join(", ");
  return String(v);
}

export function limitsText(s: TrainSetting): string {
  const parts: string[] = [];
  if (s.minimum !== undefined) parts.push(`at least ${s.minimum}`);
  if (s.above !== undefined) parts.push(`more than ${s.above}`);
  if (s.maximum !== undefined) parts.push(`at most ${s.maximum}`);
  if (s.below !== undefined) parts.push(`less than ${s.below}`);
  if (s.choices && s.choices.length > 1) parts.push(`one of ${s.choices.map(String).join(", ")}`);
  return parts.join(", ");
}

export function TrainSettings() {
  const [cfg, setCfg] = useState<TrainConfig | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [filter, setFilter] = useState("");
  const [onlyChangeable, setOnlyChangeable] = useState(false);
  const load = () => { setErr(null); api.trainConfig().then(setCfg).catch(setErr); };
  useEffect(load, []);
  const sections = useMemo(() => (cfg?.sections ?? []).map((s) => ({
    ...s, fields: s.fields.filter((f) => (!onlyChangeable || !f.fixed) && (`${s.title} ${words(f.name)} ${f.key}`).toLowerCase().includes(filter.trim().toLowerCase())),
  })).filter((s) => s.fields.length > 0), [cfg, filter, onlyChangeable]);

  return !cfg ? (err != null ? <ErrorState error={err} title="Could not read the training settings" onRetry={load} /> : <Skeleton className="h-48 w-full" />) : (
    <Panel title="Training settings" desc={`${cfg.counts.settings} settings: ${cfg.counts.changeable} can be changed, ${cfg.counts.fixed} are fixed in this phase.`}>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="min-w-[14rem] flex-1"><Input placeholder="Find a setting" aria-label="Find a setting" value={filter} onChange={(e) => setFilter(e.target.value)} /></div>
        <Checkbox checked={onlyChangeable} onCheckedChange={setOnlyChangeable} label="Only those that can be changed" />
      </div>
      {cfg.unresolved.length > 0 && <p className="mb-3 text-[0.78rem] text-muted">Not decided yet: {cfg.unresolved.length} values are filled in when a run starts, such as the exact model revision and the dataset version.</p>}
      <div className="space-y-2">
        {sections.map((s) => (
          <details key={s.key} open={filter.trim() !== ""} className="border border-line bg-surface-2">
            <summary className="cursor-pointer px-3 py-2 text-[0.84rem] font-medium">{s.title} <span className="text-[0.74rem] font-normal text-faint">({s.fields.length})</span></summary>
            <table className="w-full text-[0.78rem]">
              <caption className="sr-only">{s.title}</caption>
              <thead className="sr-only"><tr><th scope="col">Setting</th><th scope="col">Value</th><th scope="col">Limits</th><th scope="col">Can be changed</th></tr></thead>
              <tbody>
                {s.fields.map((f) => (
                  <tr key={f.key} className="border-t border-line align-top">
                    <th scope="row" className="w-1/3 px-3 py-1.5 text-left font-normal text-muted">{words(f.name)}<Dev><span className="ml-2 font-mono text-[0.68rem] text-faint">{f.key}</span></Dev></th>
                    <td className="px-3 py-1.5 font-mono text-[0.74rem] break-words">{valueText(f.value)}</td>
                    <td className="px-3 py-1.5 text-faint">{limitsText(f)}</td>
                    <td className="w-28 px-3 py-1.5 text-right">{f.fixed ? <Hint label={f.fixed_reason} className="border border-line px-1.5 py-0.5 text-[0.68rem] text-muted no-underline">Fixed</Hint> : <span className="text-[0.68rem] text-faint">Can change</span>}</td>
                  </tr>))}
              </tbody>
            </table>
          </details>
        ))}
        {sections.length === 0 && <p className="text-[0.82rem] text-muted">No setting matches.</p>}
      </div>
      <Dev><p className="mt-3 break-all font-mono text-[0.7rem] text-faint">configuration file sha256 {cfg.file_sha256}</p></Dev>
    </Panel>
  );
}
