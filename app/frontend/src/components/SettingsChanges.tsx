import { useEffect, useState } from "react";
import { api, errorText, type SettingsMigration } from "../lib/api";
import { Button, Panel } from "./ui";

// What a newer version of the app changed in the settings the person had saved (backend: settings_migrations.py). Each change says what was found and what was done, and stays here
// until it is acknowledged, so a setting that moved is never a surprise.
export function SettingsChanges() {
  const [items, setItems] = useState<SettingsMigration[]>([]);
  const [error, setError] = useState("");
  useEffect(() => { api.settingsMigrations().then((r) => setItems(r.migrations)).catch(() => undefined); }, []);        // ignored: without the list nothing is shown
  if (items.length === 0 && !error) return null;
  const acknowledge = async () => {
    try { await api.acknowledgeSettingsMigrations(items.map((m) => m.id)); setItems([]); setError(""); }
    catch (e) { setError(errorText(e)); }
  };
  return (
    <Panel title="What changed in your settings" desc="A newer version of the app moved or removed some of the settings you had saved. Nothing the app still uses was deleted."
      right={<Button variant="primary" onClick={acknowledge}>Got it</Button>}>
      <ul aria-label="Changes made to your settings" className="list-disc space-y-2 pl-5 text-[0.82rem]">
        {items.flatMap((m) => m.changes.map((c) => <li key={`${m.id}:${c}`}>{c}</li>))}
      </ul>
      {error && <p role="alert" className="mt-2 text-[0.8rem] text-crit-soft">{error}</p>}
    </Panel>
  );
}
