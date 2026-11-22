import { useEffect, useState } from "react";
import { Box, Cpu, HardDrive, MemoryStick } from "lucide-react";
import { api, sandbox as sandboxApi, type ComputeInfo } from "../lib/api";
import { Tooltip } from "./hud/Tooltip";

// What the machine has right now, in the header: cores, free memory, and the graphics card with its free video memory. It asks the backend
// every 30 seconds (the backend keeps the answer for the same time) and only while the window is visible, so it adds no load of its own.
//
// With a projectId (the Projects page passes its own ?project= while open), one more chip: CPU and memory of that project's agent sandbox, only while that container is actually
// running -- most projects have none yet, which is not shown as a chip at all, not as an empty or a zeroed one.

const gb = (mb: number) => (mb / 1024).toFixed(1);
type SandboxStats = { cpu_pct: number; mem_mb: number; mem_limit_mb: number };

export function DeviceStrip({ pollMs = 30_000, projectId }: { pollMs?: number; projectId?: string | null }) {
  const [info, setInfo] = useState<ComputeInfo | null>(null);
  const [sandboxStats, setSandboxStats] = useState<SandboxStats | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => { if (!document.hidden) api.compute().then((c) => { if (alive) setInfo(c); }).catch(() => undefined); };   // ignored: the strip keeps its last numbers
    load();
    const timer = window.setInterval(load, pollMs);
    return () => { alive = false; window.clearInterval(timer); };
  }, [pollMs]);
  useEffect(() => {
    if (!projectId) { setSandboxStats(null); return; }
    let alive = true;
    const load = async () => {
      if (document.hidden) return;
      try {
        const status = await api.agentSandboxStatus(projectId);
        const stats = status.running && status.container ? (await sandboxApi.stats(status.container)) as SandboxStats : null;
        if (alive) setSandboxStats(stats);
      } catch { if (alive) setSandboxStats(null); }                   // ignored: the chip just does not show while this cannot be read
    };
    void load();
    const timer = window.setInterval(load, pollMs);
    return () => { alive = false; window.clearInterval(timer); };
  }, [projectId, pollMs]);
  if (!info) return null;
  const gpu = info.gpu;
  const chip = "inline-flex items-center gap-1.5 whitespace-nowrap text-[0.72rem] text-muted";
  return (
    <div className="hidden items-center gap-4 rail:flex" aria-label="This computer">
      <Tooltip label={`${info.physical_cores} physical cores, ${info.logical_cores} threads; ${info.cpu_workers} workers for background jobs`}>
        <span tabIndex={0} className={chip}><Cpu size={13} aria-hidden />{info.physical_cores} cores</span>
      </Tooltip>
      {info.ram_gb != null && (
        <Tooltip label="Memory that is free now, of the total">
          <span tabIndex={0} className={chip}><MemoryStick size={13} aria-hidden />{info.ram_free_gb ?? "?"} of {info.ram_gb} GB free</span>
        </Tooltip>
      )}
      <Tooltip label={gpu.available ? `${gpu.name}: video memory that is free now, of the total` : (gpu.reason || "No graphics card is available to the app")}>
        <span tabIndex={0} className={chip}>
          <HardDrive size={13} aria-hidden />
          {gpu.available && gpu.total_mb ? `${gpu.name?.replace(/^NVIDIA /, "")} · ${gb(gpu.free_mb ?? 0)} of ${gb(gpu.total_mb)} GB free` : "No GPU"}
        </span>
      </Tooltip>
      {sandboxStats && (
        <Tooltip label="This project's agent sandbox: CPU and memory used right now, never the real project">
          <span tabIndex={0} className={chip}><Box size={13} aria-hidden />Agent {sandboxStats.cpu_pct}% · {sandboxStats.mem_mb}/{sandboxStats.mem_limit_mb} MB</span>
        </Tooltip>)}
    </div>
  );
}
