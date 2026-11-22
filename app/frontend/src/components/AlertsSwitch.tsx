import { useAlertsSwitch } from "../lib/jobAlerts";
import { errorText } from "../lib/apiError";
import { Switch } from "./kit";
import { useToast } from "./Toast";

/** "Tell me when a job finishes or stops": a message in the window and, when the window is behind another or minimised, a system notification. Saved in the settings; the Jobs tray
 *  and Settings both show this one. */
export function AlertsSwitch({ description, bare }: { description?: string; bare?: boolean }) {
  const [on, setOn] = useAlertsSwitch();
  const toast = useToast();
  return (
    <Switch checked={on} {...(bare ? { "aria-label": "Tell me when a job finishes or stops" } : { label: "Tell me when a job finishes or stops", description })}
      onCheckedChange={(next) => { setOn(next).catch((e) => toast.error(`Not saved: ${errorText(e)}`)); }} />
  );
}
