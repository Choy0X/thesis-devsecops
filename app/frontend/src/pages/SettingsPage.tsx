import { Bell, Cpu, HardDrive, KeyRound, Power, Sparkles, Wrench, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { SelectField, SettingNote, SettingRow, SettingsCard, Switch } from "../components/kit";
import { SettingsChanges } from "../components/SettingsChanges";
import { SettingsMenu } from "../components/SettingsMenu";
import { api } from "../lib/api";
import { useDevMode } from "../lib/devMode";
import { useTabParam } from "../lib/tabParam";
import { Database } from "../sections/Database";
import { Logs } from "../sections/Logs";
import { Settings } from "../sections/Settings";
import { Skills } from "../sections/Skills";

// The Settings page: a menu of groups on the left and one group at a time on the right. The group is in the address (`?tab=`), so a link to one (the Developer mode chip, the Jobs tray) opens
// it, and an address that names no group opens the first. Each group is a column of cards of the same width (SettingsCard), each card rows of the same two columns, so the controls of every
// card share one line.

export type SettingsGroup = "engines" | "sources" | "notify" | "storage" | "app" | "skills" | "developer";

export const GROUPS: { id: SettingsGroup; label: string; icon: LucideIcon; desc: string }[] = [
  { id: "engines", label: "Projects & tools", icon: Cpu, desc: "Manage the tools and permissions used in projects." },
  { id: "skills", label: "Skills", icon: Sparkles, desc: "Browse the app's project skills and manage your saved instructions." },
  { id: "sources", label: "Data sources", icon: KeyRound, desc: "Keys that raise the rate limits of the places the app reads from." },
  { id: "notify", label: "Notifications", icon: Bell, desc: "How the app tells you that something finished or needs you." },
  { id: "storage", label: "Storage and compute", icon: HardDrive, desc: "Where the files live and how many workers the machine uses." },
  { id: "app", label: "App and data", icon: Power, desc: "Restart or quit the app, and clear what it has stored." },
  { id: "developer", label: "Developer", icon: Wrench, desc: "What the pages hide by default, the window frame, the database and the activity log." },
];
const IDS = GROUPS.map((g) => g.id);

function DeveloperSwitch() {
  const [on, set] = useDevMode();
  return (
    <SettingsCard title="Developer mode" desc="Shows what the pages hide by default: ids, hashes, file paths, run folders, raw logs and the technical details of errors. It is kept on this computer only.">
      <SettingRow tight label="Show technical details" hint={on ? "On: the pages show ids, hashes, paths and raw logs." : "Off: the pages show plain words only."}>
        <Switch aria-label={`Developer mode ${on ? "is on" : "is off"}`} checked={on} onCheckedChange={set} />
      </SettingRow>
      <SettingNote>Every control of the interface in every state: <Link to="/kit" className="text-brand-strong hover:underline">the component gallery</Link>.</SettingNote>
    </SettingsCard>
  );
}

// The desktop shell opens its own frameless window and draws the title bar in the page; some setups prefer the operating system's frame. The choice
// is read when the window opens, so it applies after a restart.
function WindowFrame() {
  const [frame, setFrame] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => { api.getSettings().then((s: any) => setFrame(s.window_frame || "")).catch(() => setFrame("")); }, []);   // ignored: the control then shows the default
  if (frame === null) return null;
  const choose = async (value: string) => { setFrame(value); setSaved(false); try { await api.putSettings({ window_frame: value }); setSaved(true); } catch { setSaved(false); } };
  return (
    <SettingsCard title="Window frame" desc="The app draws its own title bar by default. If the window misbehaves on your computer, use the operating system's frame instead; the change applies the next time the app starts.">
      <SelectField label="Frame" value={frame} onValueChange={choose}
        options={[{ value: "", label: "App title bar (default)" }, { value: "native", label: "Operating system frame" }]}
        hint={saved ? "Saved. Restart the app to apply it." : "Applies the next time the app starts."} />
    </SettingsCard>
  );
}

export function SettingsPage() {
  const [group] = useTabParam(IDS);
  const current = GROUPS.find((g) => g.id === group) ?? GROUPS[0];
  return (
    <div className="mx-auto w-full max-w-[78rem] lg:grid lg:grid-cols-[12rem_minmax(0,1fr)] lg:gap-8">
      <SettingsMenu items={GROUPS} value={current.id} to={(id) => (id === IDS[0] ? "/settings" : `/settings?tab=${id}`)} />
      <div className="mt-4 min-w-0 space-y-5 lg:mt-0">
        <SettingsChanges />
        <header className="border-b border-line pb-4">
          <h2 className="text-lg font-semibold tracking-tight">{current.label}</h2>
          <p className="mt-1 max-w-[70ch] text-sm leading-relaxed text-muted">{current.desc}</p>
        </header>
        {current.id === "developer"
          ? <div className="space-y-5"><DeveloperSwitch /><WindowFrame /><Database /><Logs /></div>
          : current.id === "skills"
          ? <Skills />
          : <Settings group={current.id} />}
      </div>
    </div>
  );
}
