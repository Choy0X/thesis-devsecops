import * as RAlert from "@radix-ui/react-alert-dialog";
import { useState } from "react";
import type { ComputeChoice, ComputeChoices } from "../lib/api";
import { durationText, type Device } from "../lib/compute";
import { stageLabel } from "../lib/stageLabels";
import { cn } from "../lib/cn";
import { Button, Checkbox, Radio } from "./kit";
import { SCRIM_CLASS, ScrimMark } from "./hud/Scrim";

// Before a job that holds a model on a device, the person chooses where it runs. All three choices are always shown; one the job has no real path for, or that cannot be used now, is
// disabled with a one-line reason. The memory is an estimate from the model's own size, and the time is only what earlier runs here measured: nothing is promised, and no choice is
// presented as faster than another.

function Estimate({ c }: { c: ComputeChoice }) {
  const e = c.estimate;
  const memory = [e.vram_gb !== null ? `${e.vram_gb} GB on the card` : null, e.ram_gb !== null ? `${e.ram_gb} GB in memory` : null].filter(Boolean).join(", ");
  return (
    <div className="mt-1 space-y-0.5 text-[0.76rem] text-muted">
      <p>Memory (an estimate): {memory || "not estimated"}.{e.basis ? <span className="text-faint"> {e.basis}</span> : null}</p>
      <p>Time: {e.time ? <>about {durationText(e.time.median_seconds)} on {e.time.runs} earlier run{e.time.runs === 1 ? "" : "s"} here (the last took {durationText(e.time.last_seconds)}). <span className="text-faint">Not a prediction for this run.</span></> : <span>not measured yet on this device, so no time is given.</span>}</p>
    </div>);
}

export function ComputeDialog({ data, initial, onStart, onCancel }: { data: ComputeChoices; initial: Device | null; onStart: (device: Device, remember: boolean) => void; onCancel: () => void }) {
  const [device, setDevice] = useState<Device | null>(initial);
  const [remember, setRemember] = useState(false);
  const label = stageLabel(data.stage);
  return (
    <RAlert.Root open onOpenChange={(open) => { if (!open) onCancel(); }}>
      <ScrimMark />
      <RAlert.Portal>
        <RAlert.Overlay className={SCRIM_CLASS} />
        <RAlert.Content className="hud-brackets chamfer-r fixed left-1/2 top-1/2 z-dialog w-[min(36rem,94vw)] -translate-x-1/2 -translate-y-1/2 border border-line-strong bg-surface shadow-pop [--ch-line:var(--border-strong)]">
          <div className="space-y-3 p-5">
            <RAlert.Title className="text-[1rem] font-semibold tracking-[-0.01em]">Where should this run?</RAlert.Title>
            <RAlert.Description className="text-[0.82rem] text-muted">
              {label} runs a model. Choose the device that holds it. The numbers are estimates, and nothing here says that one choice is faster than another.
            </RAlert.Description>
            <div role="radiogroup" aria-label="Device" className="space-y-2">
              {data.choices.map((c) => (
                <label key={c.device} className={cn("block border px-3 py-2", c.available ? "cursor-pointer border-line hover:border-line-strong" : "border-line/60 opacity-70", device === c.device && "border-brand")}>
                  <span className="flex items-center gap-2 text-[0.85rem]">
                    <Radio name="device" value={c.device} checked={device === c.device} disabled={!c.available} aria-label={c.label} onChange={() => setDevice(c.device)} />
                    <b className="font-medium">{c.label}</b>
                  </span>
                  {!c.available && <p className="ml-6 mt-0.5 text-[0.78rem] text-warn">Not available: {c.reason}</p>}
                  {c.available && c.means && <p className="ml-6 mt-0.5 text-[0.78rem] text-muted">{c.means}</p>}
                  {c.available && <div className="ml-6"><Estimate c={c} /></div>}
                </label>))}
            </div>
            {typeof data.workers === "number" && <p className="text-[0.74rem] text-faint">The jobs that read and write data use up to {data.workers} worker processes at a time (Settings, Performance).</p>}
            <Checkbox checked={remember} onCheckedChange={setRemember} aria-label="Remember for this job type"
              label={`Remember this choice for ${label.toLowerCase()} (you can forget it in Settings, Performance)`} />
            <div className="flex justify-end gap-2 pt-2">
              <RAlert.Cancel asChild><Button variant="subtle">Cancel</Button></RAlert.Cancel>
              <RAlert.Action asChild>
                <Button variant="primary" disabled={!device} onClick={() => device && onStart(device, remember)}>
                  {device ? `Start on the ${device === "cpu" ? "processor" : "graphics card"}` : "Choose a device"}
                </Button>
              </RAlert.Action>
            </div>
          </div>
        </RAlert.Content>
      </RAlert.Portal>
    </RAlert.Root>
  );
}
