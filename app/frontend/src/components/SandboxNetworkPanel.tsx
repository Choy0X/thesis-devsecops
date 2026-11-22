import { api, errorText, type Settings as S } from "../lib/api";
import { RadioGroup, SettingNote, SettingRow, SettingsCard } from "./kit";
import { useToast } from "./Toast";

// The network of the agent's sandbox (Manual and Auto sessions run commands in a Docker container with a copy of the project). Off is the default and the safe state. The other choice is not
// a plain network: the container goes on an internal Docker network whose only other member is a proxy that forwards to public web addresses and nothing else. What that does not protect
// against is said here in plain words, because it is the one thing the owner has to decide with open eyes.

const OPTIONS = [
  { value: "off", label: "Off", description: "The sandbox has no network at all. Nothing it runs can reach the internet, this app or this computer's network. A package cannot be installed and a page cannot be fetched from it." },
  { value: "proxy", label: "Through a guarded proxy", description: "pip, npm, git over https and curl can reach public web addresses on ports 80 and 443. The proxy refuses this computer, the app's own address, the database, other containers, the local network, shared and cloud metadata addresses, and every other port." },
];

export function SandboxNetworkPanel({ s, onUpdated }: { s: S; onUpdated: (v: S) => void }) {
  const toast = useToast();
  const value = s.agent_sandbox_network === "proxy" ? "proxy" : "off";
  const save = async (next: string) => {
    if (next === value) return;
    try {
      onUpdated(await api.putSettings({ agent_sandbox_network: next }));
      toast.success(next === "proxy" ? "The sandbox can reach public web addresses through the proxy." : "The sandbox has no network.");
    } catch (e) { toast.error(`Not saved: ${errorText(e)}`); }
  };
  return (
    <SettingsCard title="Agent sandbox network"
      desc="Whether the commands an agent runs in its sandbox can reach the internet. Plan sessions never use the sandbox, so this does not apply to them.">
      <SettingRow label="Network" hint="Turning it off takes the way out away from every sandbox at once, even in the middle of a session. Turning it on reaches a project's sandbox at the start of its next session. Either way the container is made again on the same files, so nothing a session wrote is lost.">
        <RadioGroup legend="Network" hideLegend value={value} options={OPTIONS} onValueChange={save} />
      </SettingRow>
      {value === "proxy" && (
        <SettingNote role="note" className="text-warn">
          The proxy keeps the sandbox away from this computer and its network. It does not keep the project's own code away from the internet: code that runs in the sandbox can send what is in the sandbox
          (the copy of the project and what its tools read) to any public address on port 80 or 443. Turn it on for a project whose code you trust with that, and off again when you no longer need it.
        </SettingNote>
      )}
    </SettingsCard>
  );
}
