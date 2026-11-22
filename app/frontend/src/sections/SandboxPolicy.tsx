import { useCallback, useEffect, useState } from "react";
import { errorText, sandbox, type SandboxPolicy } from "../lib/api";
import { Panel, Button, Field, Input, Skeleton, ErrorState } from "../components/ui";
import { Checkbox } from "../components/kit";
import { useConfirm } from "../components/hud/AlertDialog";
import { useToast } from "../components/Toast";

// What a container may use and what it may reach. CPU share, memory and the process limit change in place; the network and a read-only root cannot be changed on a running container (Docker's
// rule), so changing them creates the container again from a snapshot of its files, after the person has been told what is lost.

const word = (n: number | null, unit: string) => (n == null ? "no limit" : `${n} ${unit}`);

export function SandboxLimits({ cid, onRecreated }: { cid: string; onRecreated: (id: string, warnings: string[]) => void }) {
  const [policy, setPolicy] = useState<SandboxPolicy | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [cpus, setCpus] = useState("");
  const [memory, setMemory] = useState("");
  const [pids, setPids] = useState("");
  const [network, setNetwork] = useState(false);
  const [readOnly, setReadOnly] = useState(false);
  const [busy, setBusy] = useState<"limits" | "recreate" | null>(null);
  const [problem, setProblem] = useState("");
  const confirm = useConfirm();
  const toast = useToast();

  const load = useCallback(() => sandbox.policy(cid).then((p) => {
    setPolicy(p); setError(null);
    setCpus(p.cpus == null ? "" : String(p.cpus)); setMemory(p.memory_mb == null ? "" : String(p.memory_mb)); setPids(p.pids == null ? "" : String(p.pids));
    setNetwork(p.network !== "none"); setReadOnly(p.read_only);
  }).catch(setError), [cid]);
  useEffect(() => { setPolicy(null); setProblem(""); load(); }, [load]);

  if (error != null && !policy) return <Panel title="Limits and network"><ErrorState error={error} title="The limits of this container could not be read" onRetry={load} /></Panel>;
  if (!policy) return <Panel title="Limits and network"><Skeleton className="h-24" /></Panel>;

  const b = policy.bounds;
  const figures = {
    cpus: cpus.trim() !== "" && Number(cpus) !== policy.cpus ? Number(cpus) : undefined,
    memory_mb: memory.trim() !== "" && Number(memory) !== policy.memory_mb ? Number(memory) : undefined,
    pids: pids.trim() !== "" && Number(pids) !== policy.pids ? Number(pids) : undefined,
  };
  const limitsChanged = Object.values(figures).some((v) => v !== undefined);
  const wasNetwork = policy.network !== "none";
  const isolationChanged = network !== wasNetwork || readOnly !== policy.read_only;

  const applyLimits = async () => {
    setBusy("limits"); setProblem("");
    try {
      await sandbox.setLimits(cid, Object.fromEntries(Object.entries(figures).filter(([, v]) => v !== undefined)));
      toast.success("The limits were changed on the running container.");
      await load();
    } catch (e) { setProblem(errorText(e)); } finally { setBusy(null); }
  };

  const recreate = async () => {
    const opening = network && !wasNetwork;
    const ok = await confirm({
      title: "Create this container again?", tone: opening ? "critical" : "normal", confirmLabel: "Create it again",
      description: (
        <span>
          Docker cannot change the network or the read-only setting of a running container. The files of this one are saved as an image, and a new container with the same name,
          limits and protections starts from it; the old one is then removed. Files are kept. Programs that are running and the open terminal are lost.
          {opening && " With the network on, code in it can reach the internet and other computers on your network."}
        </span>),
    });
    if (!ok) return;
    setBusy("recreate"); setProblem("");
    try {
      const r = await sandbox.recreate(cid, { ...(network !== wasNetwork ? { network } : {}), ...(readOnly !== policy.read_only ? { read_only: readOnly } : {}) });
      toast.success("The container was created again.");
      onRecreated(r.id, r.warnings);              // the page moves to the new container, which is a new panel: what to know about it is said by the page above
    } catch (e) { setProblem(errorText(e)); } finally { setBusy(null); }
  };

  return (
    <Panel title="Limits and network" desc={`Now: ${word(policy.cpus, "CPUs")}, ${word(policy.memory_mb, "MB of memory")}, ${word(policy.pids, "processes")}, ${wasNetwork ? "network on" : "no network"}, ${policy.read_only ? "read-only files" : "files can be written"}.`}>
      <div className="space-y-5">
        {policy.warnings.length > 0 && (
          <ul aria-label="What to know about this container's isolation" className="space-y-1.5">
            {policy.warnings.map((w) => <li key={w} className="border border-warn-line bg-warn-tint px-3 py-2 text-[0.78rem] text-warn">{w}</li>)}
          </ul>
        )}
        {!policy.editable && <p role="status" className="border border-line bg-surface-2 px-3 py-2 text-[0.8rem] text-muted">{policy.reason}</p>}

        <fieldset disabled={!policy.editable || busy != null} className="space-y-3">
          <legend className="mb-1 text-[0.8rem] font-medium">Change in place</legend>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="CPUs" hint={`${b.min_cpus} to ${b.max_cpus}`}>
              <Input type="number" step="0.25" min={b.min_cpus} max={b.max_cpus} value={cpus} onChange={(e) => setCpus(e.target.value)} className="tnum font-mono" />
            </Field>
            <Field label="Memory (MB)" hint={`${b.min_memory_mb} to ${b.max_memory_mb ?? "what Docker has"}`}>
              <Input type="number" step="64" min={b.min_memory_mb} max={b.max_memory_mb ?? undefined} value={memory} onChange={(e) => setMemory(e.target.value)} className="tnum font-mono" />
            </Field>
            <Field label="Processes" hint={`${b.min_pids} to ${b.max_pids}`}>
              <Input type="number" step="16" min={b.min_pids} max={b.max_pids} value={pids} onChange={(e) => setPids(e.target.value)} className="tnum font-mono" />
            </Field>
          </div>
          <div className="flex items-center gap-3">
            <Button variant="primary" disabled={!limitsChanged} loading={busy === "limits"} onClick={applyLimits}>Apply the limits</Button>
            <span className="text-[0.74rem] text-faint">Swap is set to the same figure as memory, so the container still cannot swap.</span>
          </div>
        </fieldset>

        <fieldset disabled={!policy.editable || busy != null} className="space-y-3 border-t border-line pt-4">
          <legend className="mb-1 text-[0.8rem] font-medium">Needs the container to be created again</legend>
          <div className="flex flex-wrap gap-6">
            <Checkbox checked={network} onCheckedChange={setNetwork} label="Network on" />
            <Checkbox checked={readOnly} onCheckedChange={setReadOnly} label="Read-only files" />
          </div>
          <Button disabled={!isolationChanged} loading={busy === "recreate"} onClick={recreate}>Create it again with these settings</Button>
        </fieldset>

        {problem && <p role="alert" className="text-[0.8rem] text-crit-soft">{problem}</p>}
      </div>
    </Panel>
  );
}
