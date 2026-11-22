import { api, errorText, type Settings } from "../lib/api";
import { Button } from "./ui";
import { SettingRow, SettingsCard, Switch } from "./kit";
import { useToast } from "./Toast";

// Restart or quit the app from the page. Both go through the close guard (components/CloseGuard.tsx), the one place that asks about running jobs, runs the shutdown and shows its steps:
// the buttons only send it the same request a click on the window's close button does (Quit) or the one for a restart.

export function AppPanel({ s, onUpdated }: { s: Settings; onUpdated: (v: Settings) => void }) {
  const toast = useToast();
  const asks = s.confirm_quit !== "off";
  const save = async (ask: boolean) => {
    try { onUpdated(await api.putSettings({ confirm_quit: ask ? "" : "off" })); toast.success(ask ? "The app asks before it quits." : "The app quits without asking."); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
  };
  return (
    <SettingsCard title="Restart and quit" desc="Restart the app to bring a change that is read only at the start into effect (the database folder, the window frame), or quit it from here.">
      <SettingRow label="This app" hint="Both go through the same check as the window's close button: a running job is asked about first.">
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="subtle" onClick={() => window.dispatchEvent(new CustomEvent("app-reboot-request"))}>Restart the app</Button>
          <Button variant="subtle" onClick={() => window.dispatchEvent(new CustomEvent("app-close-request"))}>Quit the app</Button>
        </div>
      </SettingRow>
      <SettingRow tight label="Ask before quitting" hint="Closing the window asks Quit the app? when nothing is running. A running job is always asked about, whatever this says.">
        <Switch checked={asks} onCheckedChange={save} aria-label="Ask before quitting" />
      </SettingRow>
    </SettingsCard>
  );
}
