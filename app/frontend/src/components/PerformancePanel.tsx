import { useState } from "react";
import { api, errorText, type Settings } from "../lib/api";
import { Button } from "./ui";
import { SelectField, SettingNote, SettingRow, SettingsCard } from "./kit";
import { useToast } from "./Toast";
import { rememberedOf, rememberedText } from "../lib/compute";
import { stageLabel } from "../lib/stageLabels";

// Hardware acceleration and the number of jobs at once. "Compute" decides whether anything is probed, planned for or run on the graphics card (jobs started afterwards);
// "drawing the window" decides whether the window itself uses the card, and is read when the app starts.

const ON_OFF = [{ value: "", label: "On" }, { value: "off", label: "Off" }];
export const JOB_CHOICES = ["", ...Array.from({ length: 16 }, (_, i) => String(i + 1))];
const JOB_OPTIONS = JOB_CHOICES.map((v) => ({ value: v, label: v === "" ? "Default (4)" : v }));

export function PerformancePanel({ s, onUpdated }: { s: Settings; onUpdated: (v: Settings) => void }) {
  const toast = useToast();
  const [compute, setCompute] = useState<string>(s.compute_gpu || "");
  const [ui, setUi] = useState<string>(s.ui_gpu || "");
  const [jobs, setJobs] = useState<string>(s.max_jobs || "");
  const [busy, setBusy] = useState(false);
  const dirty = compute !== (s.compute_gpu || "") || ui !== (s.ui_gpu || "") || jobs !== (s.max_jobs || "");
  const [changedUi, setChangedUi] = useState(false);                      // saved in this visit: the window still looks as it did until the app is started again
  const restart = changedUi || ui !== (s.ui_gpu || "");
  const remembered = Object.entries(rememberedOf(s.compute_remember));
  const forget = async (stage: string) => {
    const rest = Object.fromEntries(remembered.filter(([k]) => k !== stage));
    try { onUpdated(await api.putSettings({ compute_remember: rememberedText(rest as never) } as Partial<Settings>)); toast.success(`${stageLabel(stage)} will ask where to run again.`); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
  };
  const save = async () => {
    setBusy(true);
    try {
      const uiChanged = ui !== (s.ui_gpu || "");
      onUpdated(await api.putSettings({ compute_gpu: compute, ui_gpu: ui, max_jobs: jobs } as Partial<Settings>));
      if (uiChanged) setChangedUi(true);
      toast.success(uiChanged ? "Saved. The window changes at the next start." : "Performance settings saved.");
    }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
    finally { setBusy(false); }
  };
  return (
    <SettingsCard title="Performance" desc="How much of this computer the app may use."
      footer={<Button variant="primary" loading={busy} disabled={!dirty} onClick={save}>Save</Button>}>
      <SelectField label="Graphics card for compute" value={compute} options={ON_OFF} onValueChange={setCompute}
        hint="Training, evaluation and the local models. Off: nothing runs on the card." />
      <SelectField label="Graphics card for the window" value={ui} options={ON_OFF} onValueChange={setUi}
        hint="Drawing the interface. Turn it off if the window flickers or stays blank." />
      <SelectField label="Jobs at once" value={jobs} options={JOB_OPTIONS} onValueChange={setJobs} hint="How many jobs may run together. Empty: 4." />
      {compute === "off" && <SettingNote>With compute off, a training or evaluation on the card is refused, a model is served on the processor, and the memory checks assume no card.</SettingNote>}
      {restart && <SettingNote className="text-warn">The window changes at the next start of the app.</SettingNote>}
      <SettingRow label="Where jobs run" hint="Jobs that hold a model on a device ask where to run. A choice you asked to remember is used without asking until you forget it here.">
        <div aria-label="Remembered compute choices">
          {remembered.length === 0
            ? <p className="text-[0.8rem] text-faint">Nothing is remembered: every such job asks.</p>
            : (
              <ul className="space-y-1">
                {remembered.map(([stage, device]) => (
                  <li key={stage} className="flex items-center justify-between gap-3 text-[0.82rem]">
                    <span>{stageLabel(stage)}: <b className="font-medium">{device === "cpu" ? "processor" : device === "gpu" ? "graphics card" : "processor and card"}</b></span>
                    <Button variant="ghost" aria-label={`Forget the choice for ${stageLabel(stage)}`} onClick={() => void forget(stage)}>Forget</Button>
                  </li>))}
              </ul>)}
        </div>
      </SettingRow>
    </SettingsCard>
  );
}
