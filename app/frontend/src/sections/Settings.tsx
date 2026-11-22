import { ModelsFolderPanel } from "../components/ModelsFolderPanel";
import { PerformancePanel } from "../components/PerformancePanel";
import { DesktopNotificationsPanel, EmailPanel } from "../components/NotificationPanels";
import { PermissionsPanel } from "../components/PermissionsPanel";
import { ToolchainPanel } from "../components/ToolchainPanel";
import { SandboxNetworkPanel } from "../components/SandboxNetworkPanel";
import { McpServersPanel } from "../components/McpServersPanel";
import { AppPanel } from "../components/AppPanel";
import { HardResetPanel } from "../components/HardResetPanel";
import { CredPanel } from "../components/CredPanel";
import { WebSearchPanel } from "../components/WebSearchPanel";
import { useEffect, useState } from "react";
import { api, type Settings as S } from "../lib/api";
import { Skeleton, ErrorState } from "../components/ui";
import { StoragePanel, DatabaseStoragePanel, ComputePanel } from "./ComputePanels";

// The settings of one group of the Settings page (pages/SettingsPage has the menu and the Developer group). The settings are read once and every card keeps its own unsaved text, so an edit in
// one card is never lost by saving another.

export type General = "engines" | "sources" | "notify" | "storage" | "app";

export function Settings({ group = "engines" }: { group?: General }) {
  const [s, setS] = useState<S | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const loadSettings = () => { setLoadError(null); api.getSettings().then(setS).catch(setLoadError); };
  useEffect(loadSettings, []);

  if (!s && loadError) return <ErrorState error={loadError} title="Could not read the settings" onRetry={loadSettings} />;
  if (!s) return <div className="space-y-5"><Skeleton className="h-48" /><Skeleton className="h-48" /></div>;

  return (
    <div className="space-y-5">
      {group === "engines" && (
        <>
          <ToolchainPanel />
          <PermissionsPanel />
          <SandboxNetworkPanel s={s} onUpdated={setS} />
          <McpServersPanel />
        </>)}

      {group === "sources" && (
        <>
          <CredPanel
            title="NVD" desc="API key for CVE enrichment (raises the rate limit)."
            test="nvd" saved={s} onUpdated={setS}
            fields={[
              { k: "nvd_api_key", label: "API key", secret: true,
                hint: "Request a free key at nvd.nist.gov/developers/request-an-api-key" },
            ]}
          />
          <CredPanel
            title="GitHub" desc="Token for mining fix commits (raises the API limit from 60 to 5,000/hour)."
            test="github" saved={s} onUpdated={setS}
            fields={[
              { k: "github_token", label: "Personal access token", secret: true,
                hint: "A fine-grained token with public read access is enough. Without one, mining uses git (slower, no limit)." },
            ]}
          />
          <CredPanel
            title="Hugging Face" desc="Token for gated models and for downloads (public models need none)."
            test="huggingface" saved={s} onUpdated={setS}
            fields={[
              { k: "hf_token", label: "Access token", secret: true,
                hint: "A read token is enough. Some models are gated: accept their terms on the model's page as well as setting a token here." },
            ]}
          />
          <WebSearchPanel s={s} onUpdated={setS} />
          <p className="text-[0.78rem] leading-relaxed text-muted">
            Credentials are stored locally in <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[0.75rem]">app/backend/settings.json</code> (gitignored, never committed)
            and applied to the running backend. A local <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[0.75rem]">.env</code> still works as a fallback; values set here take precedence.
          </p>
        </>)}

      {group === "notify" && (
        <>
          <DesktopNotificationsPanel />
          <EmailPanel s={s} onUpdated={setS} />
        </>)}

      {group === "storage" && (
        <>
          <ComputePanel s={s} onUpdated={setS} />
          <StoragePanel s={s} onUpdated={setS} />
          <DatabaseStoragePanel s={s} onUpdated={setS} />
          <ModelsFolderPanel s={s} onUpdated={setS} />
          <PerformancePanel s={s} onUpdated={setS} />
        </>)}

      {group === "app" && (
        <>
          <AppPanel s={s} onUpdated={setS} />
          <HardResetPanel />
        </>)}
    </div>
  );
}
