import { TablePager } from "../components/TablePager";
import { useWorkspaceState } from "../lib/workspaceState";
import { useCallback, useEffect, useMemo, useState } from "react";
import { errorText, sandbox, type SandboxEvent, type SandboxHistory, type SandboxRecord, type SandboxSample } from "../lib/api";
import { Panel, Button, Skeleton, ErrorState, EmptyState } from "../components/ui";
import { Input } from "../components/kit";
import { Curve, type Series } from "../components/charts/Curve";
import { useConfirm } from "../components/hud/AlertDialog";
import { useToast } from "../components/Toast";
import { Icon } from "../components/Icon";

// What happened in a container: the commands it ran, what came of them, how much CPU and memory it used, and named sessions saved from the record. The record is kept on this computer under the
// container's name, so it survives the container being created again or deleted; output a command printed is in it (the last part), which is why it is local and can be forgotten.

const stamp = (iso: string) => new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "medium" });

/** CPU and memory as two series over the seconds since the first sample. */
export function sampleSeries(samples: SandboxSample[]): { cpu: Series[]; memory: Series[] } {
  if (!samples.length) return { cpu: [], memory: [] };
  const t0 = Date.parse(samples[0].at);
  const at = (s: SandboxSample) => Math.round((Date.parse(s.at) - t0) / 1000);
  return {
    cpu: [{ name: "CPU %", points: samples.filter((s) => s.cpu_pct != null).map((s) => ({ x: at(s), y: s.cpu_pct as number })) }],
    memory: [{ name: "Memory MB", points: samples.filter((s) => s.mem_mb != null).map((s) => ({ x: at(s), y: s.mem_mb as number })) }],
  };
}

export function SandboxHistoryTab() {
  const [offset, setOffset] = useWorkspaceState("sandbox.history.offset", 0);
  const [total, setTotal] = useState(0);
  const [known, setKnown] = useState<SandboxRecord[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [name, setName] = useWorkspaceState<string | null>("sandbox.history.selected", null);
  const load = useCallback(() => sandbox.historyKnown({ limit: 100, offset }).then((d) => { setKnown(d.containers); setTotal(d.total ?? d.containers.length); setError(null); }).catch(setError), [offset]);
  useEffect(() => { load(); }, [load]);

  if (error != null && !known) return <Panel title="History"><ErrorState error={error} title="The records could not be read" onRetry={load} /></Panel>;
  if (!known) return <Panel title="History"><Skeleton className="h-24" /></Panel>;
  return (
    <div className="grid gap-5 lg:grid-cols-[300px_1fr]">
      <Panel title="Containers with a record" desc="Kept on this computer, deleted containers included. Keys, tokens and passwords in commands and output are masked as well as they can be, which is not a guarantee: forget a record to remove it.">
        {known.length === 0 ? <EmptyState icon={<Icon.terminal />} title="Nothing recorded yet" hint="Commands run in a sandbox, and its CPU and memory, are recorded here." /> : (
          <ul className="space-y-1.5">
            {known.map((r) => (
              <li key={r.name}>
                <button type="button" onClick={() => setName(r.name)} aria-pressed={name === r.name}
                  className={`w-full rounded-lg border px-3 py-2 text-left ${name === r.name ? "border-brand bg-brand-tint" : "border-line bg-surface-2 hover:border-line-strong"}`}>
                  <span className="font-mono text-[0.8rem]">{r.name.replace(/^aivdf-/, "")}</span>
                  <span className="mt-0.5 block text-[0.7rem] text-faint">{r.commands} commands, {r.sessions} sessions{r.last ? `, last ${stamp(r.last)}` : ""}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <TablePager offset={offset} total={total} onOffset={setOffset} />
      </Panel>
      {name ? <Record key={name} name={name} onForgotten={() => { setName(null); load(); }} onChanged={load} />
        : <Panel title="Record"><EmptyState icon={<Icon.terminal />} title="Select a container" hint="Pick one to see its commands, its CPU and memory over time, and its saved sessions." /></Panel>}
    </div>
  );
}

function Record({ name, onForgotten, onChanged }: { name: string; onForgotten: () => void; onChanged: () => void }) {
  const [data, setData] = useState<SandboxHistory | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [opened, setOpened] = useState<{ id: string; label: string; saved_at: string; events: SandboxEvent[] } | null>(null);
  const [label, setLabel] = useState("");
  const [problem, setProblem] = useState("");
  const confirm = useConfirm();
  const toast = useToast();
  const load = useCallback(() => sandbox.historyOf(name).then((d) => { setData(d); setError(null); }).catch(setError), [name]);
  useEffect(() => { load(); }, [load]);
  const series = useMemo(() => sampleSeries(data?.samples ?? []), [data]);

  if (error != null && !data) return <Panel title={name}><ErrorState error={error} title="This record could not be read" onRetry={load} /></Panel>;
  if (!data) return <Panel title={name}><Skeleton className="h-32" /></Panel>;

  const save = async () => {
    setProblem("");
    try { const r = await sandbox.saveSession(name, label); toast.success(`Saved "${r.label}" with ${r.events} events.`); setLabel(""); load(); onChanged(); } catch (e) { setProblem(errorText(e)); }
  };
  const open = async (id: string) => {
    setProblem("");
    try { setOpened(await sandbox.session(name, id)); } catch (e) { setProblem(errorText(e)); }
  };
  const remove = async (id: string, title: string) => {
    if (!(await confirm({ title: "Delete this session?", confirmLabel: "Delete the session", tone: "critical", description: `"${title}" is deleted. The record of the container is not touched.` }))) return;
    try { await sandbox.deleteSession(name, id); if (opened?.id === id) setOpened(null); load(); onChanged(); } catch (e) { setProblem(errorText(e)); }
  };
  const forget = async () => {
    if (!(await confirm({ title: "Forget this container's record?", confirmLabel: "Forget the record", tone: "critical",
      description: `The commands, the samples and the saved sessions of ${name} are deleted from this computer. The container itself is not touched.` }))) return;
    try { await sandbox.forget(name); toast.success("The record was deleted."); onForgotten(); } catch (e) { setProblem(errorText(e)); }
  };

  const shown = opened ? opened.events : data.events;
  return (
    <div className="space-y-5">
      <Panel title={name} desc={`${data.events.length} events recorded, ${data.samples.length} samples.`} right={<Button variant="ghost" className="text-crit" onClick={forget}>Forget the record</Button>}>
        {data.samples.length === 0
          ? <p className="text-[0.8rem] text-muted">No CPU or memory samples yet. They are taken while the container's page is open.</p>
          : <div className="grid gap-4 md:grid-cols-2">
              <Curve title="CPU" series={series.cpu} xLabel="s" yLabel="%" height={160} />
              <Curve title="Memory" series={series.memory} xLabel="s" yLabel="MB" height={160} />
            </div>}
      </Panel>

      <Panel title={opened ? `Session: ${opened.label}` : "Commands and changes"} desc={opened ? `Saved ${stamp(opened.saved_at)}. A copy; it does not change.` : "Newest last."}
        right={opened ? <Button variant="ghost" onClick={() => setOpened(null)}>Back to the live record</Button> : undefined}>
        {shown.length === 0 ? <p className="text-[0.8rem] text-muted">Nothing has been recorded yet.</p> : (
          <ol className="space-y-2" aria-label="Events">
            {shown.map((e, i) => <EventRow key={`${e.at}-${i}`} e={e} />)}
          </ol>
        )}
      </Panel>

      <Panel title="Sessions" desc="Keep a named copy of the record so far, to look at later.">
        <div className="space-y-3">
          <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (label.trim()) save(); }}>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Name of the session" aria-label="Name of the session" maxLength={80} className="w-64" />
            <Button type="submit" disabled={!label.trim()}>Save the session</Button>
          </form>
          {data.sessions.length === 0 ? <p className="text-[0.8rem] text-muted">No saved sessions.</p> : (
            <ul className="divide-y divide-line/60 text-[0.8rem]">
              {data.sessions.map((s) => (
                <li key={s.id} className="flex items-center justify-between gap-3 py-1.5">
                  <span><span className="font-medium">{s.label}</span> <span className="text-faint">{s.commands} commands, saved {stamp(s.saved_at)}</span></span>
                  <span className="flex gap-1"><Button variant="ghost" onClick={() => open(s.id)}>Open</Button><Button variant="ghost" className="text-crit" onClick={() => remove(s.id, s.label)}>Delete</Button></span>
                </li>
              ))}
            </ul>
          )}
          {problem && <p role="alert" className="text-[0.8rem] text-crit-soft">{problem}</p>}
        </div>
      </Panel>
    </div>
  );
}

const PLAIN: Record<string, string> = { created: "Created", start: "Started", stop: "Stopped", restart: "Restarted", deleted: "Deleted", limits: "Limits changed", recreated: "Created again", upload: "File put in" };

function EventRow({ e }: { e: SandboxEvent }) {
  if (e.kind !== "command") {
    const extra = e.kind === "upload" ? ` ${String(e.name ?? "")}` : e.kind === "limits" ? ` ${JSON.stringify(e.changed ?? {})}` : "";
    return <li className="flex gap-3 text-[0.78rem] text-muted"><span className="tnum shrink-0 text-faint">{stamp(e.at)}</span><span>{PLAIN[e.kind] ?? e.kind}{extra}</span></li>;
  }
  const failed = e.exit_code !== 0;
  return (
    <li className="text-[0.78rem]">
      <details>
        <summary className="flex cursor-pointer flex-wrap items-baseline gap-x-3 gap-y-0.5">
          <span className="tnum shrink-0 text-faint">{stamp(e.at)}</span>
          <code className="font-mono">{e.command}</code>
          <span className={failed ? "text-crit-soft" : "text-ok"}>{e.timed_out ? "timed out" : failed ? `exit ${e.exit_code}` : "ok"}</span>
          <span className="tnum text-faint">{e.seconds}s</span>
        </summary>
        <pre className="mt-1 max-h-48 overflow-auto rounded-lg border border-line bg-terminal p-3 font-mono text-[0.74rem] leading-relaxed text-muted">{e.output_tail || "no output"}</pre>
      </details>
    </li>
  );
}
