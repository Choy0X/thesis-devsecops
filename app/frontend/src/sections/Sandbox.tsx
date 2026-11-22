import { TablePager } from "../components/TablePager";
import { useWorkspaceState } from "../lib/workspaceState";
import { useEffect, useState } from "react";
import { sandbox, type Container } from "../lib/api";
import { Panel, Button, StatusPill, EmptyState, Skeleton, ErrorState } from "../components/ui";
import { TextField, Hint } from "../components/kit";
import { Icon } from "../components/Icon";
import { Terminal } from "../components/Terminal";
import { useWhileActive } from "../components/ActivePage";
import { useConfirm } from "../components/hud/AlertDialog";
import { Dialog } from "../components/hud/Dialog";
import { reconnectingSocket, stateWords, type SocketState } from "../lib/reconnectingSocket";
import { SandboxLimits } from "./SandboxPolicy";
import { SandboxFiles } from "./SandboxFiles";

export function Sandbox() {
  const [offset, setOffset] = useWorkspaceState("sandbox.offset", 0);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<any>(null);
  const [list, setList] = useState<Container[] | null>(null);
  const [sel, setSel] = useWorkspaceState<string | null>("sandbox.selected", null);
  const [recreated, setRecreated] = useState<string[] | null>(null);          // what to know about a container that was just created again

  const [error, setError] = useState<unknown>(null);
  const [cleanup, setCleanup] = useState<any>(null);
  const [cleanupError, setCleanupError] = useState<unknown>(null);
  const [cleanupNotice, setCleanupNotice] = useState('');
  const refreshCleanup = () => sandbox.cleanup().then((value) => { setCleanup(value); setCleanupError(null); }).catch(setCleanupError);
  const refresh = () => sandbox.containersPage({ limit: 100, offset }).then((l) => { setList(l.containers); setTotal(l.total); setError(null); }).catch(setError);
  useEffect(() => {
    sandbox.status().then(setStatus).catch(setError);
    refreshCleanup();
    refresh();
  }, []);
  useEffect(() => { refresh(); }, [offset]);
  useWhileActive(refresh, 3000);                    // the container list refreshes only while this page is on screen

  if (error != null && !status) {
    return (
      <Panel title="Sandbox" desc="Isolated Docker containers for testing and patch validation.">
        <ErrorState error={error} title="Could not reach the sandbox service" onRetry={() => { setError(null); sandbox.status().then(setStatus).catch(setError); refresh(); }} />
      </Panel>
    );
  }

  if (status && !status.docker_available) {
    return (
      <Panel title="Sandbox" desc="Isolated Docker containers for testing and patch validation.">
        <EmptyState icon={<Icon.box />} title="Docker is not available"
          hint="The Docker daemon is not reachable on this machine. Start Docker to create and manage sandbox containers; the rest of the app works without it." />
      </Panel>
    );
  }

  return (
    <>
    {(cleanupError || cleanupNotice || cleanup?.cleanup_errors?.length || Object.keys(cleanup?.pending_images || {}).length || Object.values(cleanup?.interactive_containers || {}).some((item: any) => item.cleanup_error) || cleanup?.interactive_proxy_error || cleanup?.watchdog_error) ? <Panel title="Docker cleanup">
      {cleanupError != null && <ErrorState error={cleanupError} title="Could not check Docker cleanup" onRetry={refreshCleanup} />}
      {cleanupNotice && <p>{cleanupNotice}</p>}
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(cleanup, null, 2)}</pre>
      <Button onClick={() => sandbox.retryCleanup().then((value) => { setCleanup(value); setCleanupError(null); }).catch(setCleanupError)}>Retry pending cleanup</Button>
    </Panel> : null}
    <div className="grid gap-5 lg:grid-cols-[320px_1fr]">
      <Panel title="Containers" desc={status ? `Docker ${status.version || ""}` : "Loading"}>
        {error != null && <div className="mb-3"><ErrorState error={error} title="The container list could not be refreshed" onRetry={refresh} /></div>}
        {!list ? <Skeleton className="h-24" /> : list.length === 0 ? (
          <EmptyState icon={<Icon.box />} title="No sandboxes" hint="Sandboxes start automatically when a chat task needs isolated execution." />
        ) : (
          <ul className="space-y-1.5">
            {list.map((c) => (
              <li key={c.id}>
                <button onClick={() => { setSel(c.id); setRecreated(null); }}
                  className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors duration-150 ${
                    sel === c.id ? "border-brand bg-brand-tint" : "border-line bg-surface-2 hover:border-line-strong"}`}>
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[0.8rem]">{c.name.replace(/^aivdf-/, "")}</span>
                    <StatusPill status={c.status === "running" ? "running" : c.status === "exited" ? "error" : "idle"} />
                  </div>
                  <div className="mt-0.5 font-mono text-[0.7rem] text-faint">{c.image} · {c.network}</div>
                </button>
              </li>
            ))}
          </ul>
        )}
        <TablePager offset={offset} total={total} onOffset={setOffset} />
      </Panel>

      <div className="min-w-0 space-y-5">
        {recreated && (
          <div role="status" aria-label="After the container was created again" className="border border-warn-line bg-warn-tint px-4 py-3 text-[0.8rem] text-warn">
            <div className="flex items-start justify-between gap-3">
              <ul className="space-y-1">{recreated.map((w) => <li key={w}>{w}</li>)}</ul>
              <Button variant="ghost" onClick={() => setRecreated(null)}>Dismiss</Button>
            </div>
          </div>
        )}
        {sel ? <SandboxDetail key={sel} cid={sel} status={list?.find((c) => c.id === sel)?.status || "unknown"} execution={list?.find((c) => c.id === sel)?.execution} onChanged={refresh} onDeleted={(notice) => { setCleanupNotice(notice || ''); setSel(null); refresh(); refreshCleanup(); }} onRecreated={(id, warnings) => { setSel(id); setRecreated(warnings); refresh(); }} />
          : <Panel title="Details"><EmptyState icon={<Icon.terminal />} title="Select a container"
              hint="Pick a sandbox to open a shell, manage files, view logs, snapshot or delete it." /></Panel>}
      </div>
    </div>
    </>
  );
}

export type Protection = { id: string; label: string; detail: string };

/** The protections every sandbox gets (read from the backend), plus the two that the person can change. */
export function HardeningChips({ fixed, network, readonly }: { fixed: Protection[]; network: boolean; readonly: boolean }) {
  const chip = "inline-flex items-center border px-2 py-0.5 text-[0.72rem]";
  return (
    <div className="space-y-2">
      <ul className="flex flex-wrap gap-1.5" aria-label="Protections">
        {fixed.map((h) => <li key={h.id} className={`${chip} border-line bg-surface-2 text-muted`}>{h.detail ? <Hint label={h.detail} className="no-underline">{h.label}</Hint> : h.label}</li>)}
        <li className={`${chip} ${readonly ? "border-line bg-surface-2 text-muted" : "border-warn-line bg-warn-tint text-warn"}`}>{readonly ? "Read-only files" : "Files can be written"}</li>
        <li className={`${chip} ${network ? "border-crit-line bg-crit-tint text-crit-soft" : "border-line bg-surface-2 text-muted"}`}>{network ? "Network on" : "No network"}</li>
      </ul>
      {network && (
        <p role="alert" className="border border-crit-line bg-crit-tint px-3 py-2 text-[0.78rem] text-crit-soft">
          With the network on, code in this sandbox can reach the internet and other computers on your network. Turn it on only for images and code you trust.
        </p>
      )}
    </div>
  );
}

export function SandboxDetail({ cid, status, execution, onChanged, onDeleted, onRecreated }: { cid: string; status: string; execution?: {active: boolean; owners: number; cleanup_error?: string | null}; onChanged: () => void; onDeleted: (notice?: string) => void; onRecreated: (id: string, warnings: string[]) => void }) {
  const [logs, setLogs] = useState("");
  const [stats, setStats] = useState<any>(null);
  const [actionOut, setActionOut] = useState("");
  const [busy, setBusy] = useState(false);

  const [link, setLink] = useState<{ state: SocketState; retryInMs?: number }>({ state: "connecting" });
  useEffect(() => {
    setActionOut(""); setLogs("");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return reconnectingSocket({
      url: `${proto}://${location.host}/ws/sandbox/${cid}/logs`,
      onState: (state, retryInMs) => setLink({ state, retryInMs }),
      onMessage: (data) => {
        const m = JSON.parse(data);
        if (m.type === "logs") setLogs(m.data);
        else if (m.type === "stats") setStats(m.data);
      },
    });
  }, [cid]);

  const act = async (a: string) => {
    setBusy(true);
    try {
      setActionOut(a === "delete" ? "Deletion in progress. Waiting for Docker confirmation..." : "Updating container state...");
      const result = await sandbox.action(cid, a);
      if (a === "delete") {
        if (result.deleted !== true) { setActionOut(result.reason || "Deletion was not confirmed. Retry when Docker is available."); return; }
        // A failed image cleanup remains visible even though the container is gone.
        if (result.image && !["removed", "not_attempted"].includes(result.image.status)) {
          onDeleted(`Container removed. Image: ${result.image.status}. ${result.image.reason || ""}`);
        } else onDeleted();
      } else { setActionOut(""); onChanged(); }
    } catch (e) { setActionOut(String(e)); } finally { setBusy(false); }
  };
  const confirm = useConfirm();
  const [naming, setNaming] = useState(false);
  const [snapName, setSnapName] = useState("aivdf-snapshot");
  const snapshot = async () => {
    setNaming(false);
    if (snapName.trim()) { const r = await sandbox.commit(cid, snapName.trim()); setActionOut(`snapshot -> ${r.image || JSON.stringify(r)}`); }
  };

  return (
    <div className="space-y-5">
      <Panel title="Controls" right={stats && (
        <span className="tnum text-[0.75rem] text-muted">CPU {stats.cpu_pct}% · {stats.mem_mb}/{stats.mem_limit_mb} MB</span>)}>
        <div className="flex flex-wrap gap-2">
          {(status === 'running' || status === 'paused') && <Button disabled={busy} onClick={() => act('stop')}>Stop</Button>}
          <Button onClick={() => setNaming(true)} icon={<Icon.box />}>Snapshot</Button>
          <Button disabled={busy} className="text-crit hover:border-crit"
            onClick={async () => {
              try {
                const preview = await sandbox.deletionPreview(cid);
                if (await confirm({ title: "Delete this sandbox?", tone: "critical", confirmLabel: "Delete the sandbox",
                  description: `Docker will remove the container filesystem. Workspace volumes and saved snapshots are kept. Associated image: ${preview.image.status}. ${preview.image.reason || ""}` })) await act("delete");
              } catch (e) { setActionOut(String(e)); }
            }}>Delete</Button>
        </div>
      </Panel>

      <Dialog open={naming} onOpenChange={setNaming} title="Snapshot the sandbox" description="Saves the container as an image you can start again.">
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); snapshot(); }}>
          <TextField label="Image name" value={snapName} onChange={(e) => setSnapName(e.target.value)} autoFocus spellCheck={false} inputClassName="font-mono" />
          <div className="flex justify-end gap-2"><Button type="button" onClick={() => setNaming(false)}>Cancel</Button><Button type="submit" variant="primary" disabled={!snapName.trim()}>Create the snapshot</Button></div>
        </form>
      </Dialog>

      <Panel title="Terminal" desc="Available while an executing chat task owns this sandbox.">
        {execution?.active && status === 'running' ? <Terminal cid={cid} /> : <p className="text-[0.8rem] text-muted">No executing chat task needs this sandbox. Files and checkpoints are preserved.</p>}
        {execution?.cleanup_error && <p role="alert" className="mt-2 text-[0.8rem] text-crit-soft">Cleanup needs retry: {execution.cleanup_error}</p>}
      </Panel>

      <SandboxFiles cid={cid} />

      {actionOut && (
        <Panel title="Action output">
          <pre className="max-h-48 overflow-auto rounded-lg border border-line bg-terminal p-3 font-mono text-[0.74rem] leading-relaxed text-muted">{actionOut}</pre>
        </Panel>
      )}

      <Panel title="Logs" desc="Live container output (streamed)." right={<span role="status" className={`text-[0.75rem] ${link.state === "reconnecting" ? "text-warn" : "text-muted"}`}>{stateWords(link.state, link.retryInMs)}</span>}>
        <pre className="max-h-56 overflow-auto rounded-lg border border-line bg-terminal p-3 font-mono text-[0.74rem] leading-relaxed text-muted">{logs || "no output"}</pre>
      </Panel>
    </div>
  );
}

