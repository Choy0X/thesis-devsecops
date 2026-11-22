import { useEffect, useState } from "react";
import { api, type DeviceInfo } from "../lib/api";
import { Panel, Skeleton } from "./ui";

// The computer in numbers: processor, memory, every disk and the graphics card. The same figures the header strip summarises, with the disks and
// the reason when there is no graphics card.
const gbText = (x: number | null | undefined) => (x == null ? "?" : x.toLocaleString("en-US", { maximumFractionDigits: 1 }));
const mbToGb = (mb: number) => (mb / 1024).toFixed(1);

export function DevicePanel() {
  const [info, setInfo] = useState<DeviceInfo | null | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    api.device().then((d) => { if (alive) setInfo(d); }).catch(() => { if (alive) setInfo(null); });   // ignored: the panel then says it could not read the machine
    return () => { alive = false; };
  }, []);
  return (
    <Panel title="This computer" desc="What is available for jobs and models right now.">
      {info === undefined ? <Skeleton className="h-24 w-full" /> : info === null ? <p className="text-[0.82rem] text-muted">The computer's figures could not be read.</p> : (
        <dl className="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-2 text-[0.82rem]">
          <dt className="text-muted">Processor</dt>
          <dd>{info.cpu.physical_cores} cores ({info.cpu.logical_cores} threads), {info.cpu.workers} workers for jobs</dd>
          <dt className="text-muted">Memory</dt>
          <dd>{gbText(info.ram.free_gb)} of {gbText(info.ram.total_gb)} GB free</dd>
          <dt className="text-muted">Disks</dt>
          <dd>{info.disks.length === 0 ? "none found" : (
            <ul className="space-y-0.5">{info.disks.map((d) => <li key={d.drive}>{d.drive} {gbText(d.free_gb)} of {gbText(d.total_gb)} GB free</li>)}</ul>)}</dd>
          <dt className="text-muted">Graphics</dt>
          <dd>{info.gpu.available ? (
            <ul className="space-y-0.5">{info.gpu.cards.map((c) => (
              <li key={c.name}>{c.name}: {mbToGb(c.free_mb)} of {mbToGb(c.total_mb)} GB video memory free, {c.utilization_pct}% busy</li>))}</ul>
          ) : <span className="text-muted">{info.gpu.reason}</span>}</dd>
          <dt className="text-muted">GPU mode</dt>
          <dd>Idle <span className="text-faint">(no model is using the graphics card)</span></dd>
        </dl>
      )}
    </Panel>
  );
}
