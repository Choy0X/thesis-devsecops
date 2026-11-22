import { FlaskConical, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { api, errorText, type ToolchainTool } from "../lib/api";
import { Button, IconButton, SettingNote, SettingsCard } from "./kit";
import { useToast } from "./Toast";

const TOOLS: Record<string, { name: string; purpose: string }> = {
  rtk: { name: "RTK", purpose: "Condenses output from supported terminal commands." },
  ripgrep: { name: "ripgrep", purpose: "Searches code and project files." },
  git: { name: "Git", purpose: "Reads project history and manages local Git checkouts." },
};

function stateOf(tool: ToolchainTool) {
  if (!tool.supported) return { label: "Unsupported platform", tone: "text-muted" };
  if (tool.tested) return { label: "Ready", tone: "text-ok" };
  if (tool.preparing) return { label: "Preparing", tone: "text-muted" };
  return { label: "Unavailable", tone: "text-warn" };
}

export function ToolchainPanel() {
  const toast = useToast();
  const [tools, setTools] = useState<ToolchainTool[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const preparing = tools?.some((tool) => tool.preparing) ?? false;

  const load = () => {
    api.agentToolchain().then((data) => { setTools(data.tools); setError(""); }).catch((cause) => setError(errorText(cause)));
  };
  useEffect(load, []);
  useEffect(() => {
    if (!preparing) return;
    const timer = window.setInterval(load, 2000);
    return () => window.clearInterval(timer);
  }, [preparing]);

  const test = async (tool: ToolchainTool) => {
    setBusy(tool.tool);
    try {
      const data = await api.agentToolchainTest(tool.tool);
      setTools(data.tools);
      if (data.tools.find((entry) => entry.tool === tool.tool)?.tested) toast.success(`${tool.tool} passed its test.`);
      else toast.error(`${tool.tool} failed its test.`);
    } catch (cause) { toast.error(errorText(cause)); }
    finally { setBusy(null); }
  };

  return (
    <SettingsCard title="Command tools" desc="The app prepares and checks its own copies during setup."
      action={<IconButton label="Refresh command tool status" onClick={load}><RefreshCw size={15} aria-hidden /></IconButton>}>
      {error ? <SettingNote role="alert">Could not read command tool status. {error}</SettingNote> : null}
      {!tools && !error ? <SettingNote role="status">Checking command tools...</SettingNote> : null}
      {tools?.length === 0 ? <SettingNote>No command tools are configured.</SettingNote> : null}
      {!!tools?.length && <ul aria-label="Command tools" className="divide-y divide-line">
        {tools.map((tool) => {
          const state = stateOf(tool);
          const details = TOOLS[tool.tool];
          const problem = tool.preparation_error || (tool.installed && !tool.tested ? tool.notes[0] : null);
          return (
            <li key={tool.tool} className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <p className="text-sm font-medium">{details?.name ?? tool.tool}</p>
                  <span className="font-mono text-xs text-muted">{tool.wanted}</span>
                  <span role="status" className={`text-xs ${state.tone}`}>{state.label}</span>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted">{details?.purpose ?? tool.note}</p>
                {problem ? <p className="mt-1 break-words text-xs text-warn">{problem}</p> : null}
              </div>
              {tool.installed && <Button variant="subtle" size="sm" icon={<FlaskConical size={14} aria-hidden />}
                loading={busy === tool.tool} disabled={busy != null || tool.preparing} onClick={() => void test(tool)}>Test</Button>}
            </li>
          );
        })}
      </ul>}
    </SettingsCard>
  );
}
