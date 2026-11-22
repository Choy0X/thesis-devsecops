import { api, errorText, type Settings } from "../lib/api";
import { SelectField, SettingNote, SettingRow, SettingsCard } from "./kit";
import { AlertsSwitch } from "./AlertsSwitch";
import { useCredForm, type FieldDef } from "./CredPanel";
import { useToast } from "./Toast";

// How the app tells a person that a job ended. Desktop notifications are a switch (a message in the window and, when the window is behind another or minimised, one from the system); email
// goes through a mail server of the person's own, so the choice of when to write is offered only once a server is set up.

export const CHOICES = [{ value: "", label: "Off" }, { value: "failed", label: "Failed" }, { value: "all", label: "All" }];

const SMTP_FIELDS: FieldDef[] = [
  { k: "smtp_host", label: "Host", placeholder: "smtp.example.com" },
  { k: "smtp_port", label: "Port", placeholder: "587" },
  { k: "smtp_user", label: "Username" },
  { k: "smtp_password", label: "Password", secret: true },
  { k: "smtp_from", label: "From", placeholder: "alerts@example.com" },
  { k: "smtp_to", label: "Alert recipient", placeholder: "you@example.com" },
];

export function DesktopNotificationsPanel() {
  return (
    <SettingsCard title="Desktop notifications" desc="Tells you on whatever page you are on when a job finishes or stops.">
      <SettingRow tight label="Tell me when a job finishes or stops"
        hint="A message in the window and, when the window is behind another or minimised, a notification from the system. The message carries no ids or paths.">
        <AlertsSwitch bare />
      </SettingRow>
    </SettingsCard>
  );
}

export function EmailPanel({ s, onUpdated }: { s: Settings; onUpdated: (v: Settings) => void }) {
  const toast = useToast();
  const configured = !!s.smtp_host;
  const form = useCredForm({ title: "Email server", testTitle: "the email server", test: "smtp", testLabel: "Send a test email", fields: SMTP_FIELDS, saved: s, onUpdated });
  const choose = async (notify_email: string) => {
    try { onUpdated(await api.putSettings({ notify_email })); toast.success(notify_email ? "You will get a letter when a job ends." : "No letter will be sent."); }
    catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
  };
  return (
    <>
      <SettingsCard title="Email server" desc="A mail server of your own, for the jobs you leave running. The letter names the job and, for a failure, what went wrong; it does not include the job's settings."
        footer={form.footer}>
        {form.rows}
      </SettingsCard>
      {form.dialog}
      <SettingsCard title="Email when a job ends" desc="Which jobs write to you. It is offered once the email server is set up.">
        {configured ? (
          <SelectField label="Email when a job ends" value={s.notify_email || ""} options={CHOICES} onValueChange={choose}
            hint="Off: no letter. Failed: a letter when a job fails. All: a letter when a job fails and when a job that took a while finishes. A job you cancelled yourself never sends one, and a job that finished in under 20 seconds does not either, unless it failed." />
        ) : (
          <SettingNote role="status">Set up the email server above to be told by email when a job ends.</SettingNote>
        )}
      </SettingsCard>
    </>
  );
}
