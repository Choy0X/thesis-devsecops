import { ArrowLeft, Plus, Puzzle, RefreshCw, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/kit/Button";
import { SelectField } from "../../components/kit/Select";
import { TextareaField, TextField } from "../../components/kit/TextField";
import { get, write } from "./client";

const extensionTypes = { skill: "Local skill", mcp: "MCP server", plugin: "Plugin" };
const extensionOptions = Object.entries(extensionTypes).map(([value, label]) => ({ value, label }));
type ExtensionKind = keyof typeof extensionTypes;
type Extension = {
  id: string;
  kind: ExtensionKind;
  name: string;
  source: string;
  version: number;
  hash: string;
  trusted: boolean;
  enabled_projects: string[];
  errors?: string[];
};
const registry = "/api/ide/extensions";

export function Extensions({ projectId, turnActive }: { projectId: string; turnActive: boolean }) {
  const [extensions, setExtensions] = useState<Extension[]>([]);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [registering, setRegistering] = useState(false);
  const [kind, setKind] = useState<ExtensionKind>("skill");
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const [config, setConfig] = useState("");
  const [review, setReview] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ text: string; error?: boolean }>();
  const [busy, setBusy] = useState(false);
  const registerHeading = useRef<HTMLHeadingElement>(null);
  const addExtension = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    void get<{ extensions: Extension[] }>(registry, controller.signal)
      .then(result => { if (!controller.signal.aborted) setExtensions(result.extensions); })
      .catch(error => { if (!controller.signal.aborted) setLoadError(error instanceof Error ? error.message : String(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [reload]);

  function refresh() {
    setLoading(true);
    setLoadError("");
    setFeedback(undefined);
    setReview(null);
    setReload(value => value + 1);
  }

  async function change(action: () => Promise<Extension>, success: string, onSuccess?: () => void) {
    setBusy(true);
    setFeedback(undefined);
    try {
      const updated = await action();
      setExtensions(items => {
        const next = [...items.filter(item => item.id !== updated.id), updated];
        return next.sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name));
      });
      onSuccess?.();
      setFeedback({ text: success });
    } catch (error) {
      setFeedback({ text: error instanceof Error ? error.message : String(error), error: true });
    } finally {
      setBusy(false);
    }
  }

  function backToList() {
    setRegistering(false);
    setFeedback(undefined);
    setKind("skill");
    setName("");
    setSource("");
    setConfig("");
    requestAnimationFrame(() => addExtension.current?.focus());
  }

  function register() {
    void change(() => {
      let configuration: unknown = {};
      if (kind === "mcp") {
        try { configuration = JSON.parse(config); }
        catch { throw new Error("Enter valid JSON for the MCP configuration."); }
        if (!configuration || Array.isArray(configuration) || typeof configuration !== "object") {
          throw new Error("MCP configuration must be a JSON object.");
        }
      }
      return write<Extension>(registry, {
        kind,
        ...(kind !== "skill" && { name: name.trim() }),
        ...(kind !== "mcp" && { source: source.trim() }),
        ...(kind === "mcp" && { config: configuration }),
      });
    }, "Extension registered.", backToList);
  }

  const message = feedback && <p className={feedback.error ? "project-settings-message text-crit-soft" : "project-settings-message"}
    role={feedback.error ? "alert" : "status"}>{feedback.text}</p>;

  if (registering) return (
    <section className="project-settings-panel" aria-label="Add extension">
      <div className="project-settings-heading">
        <Button variant="ghost" size="sm" className="project-settings-back" disabled={busy} icon={<ArrowLeft size={14} aria-hidden />} onClick={backToList}>Back to extensions</Button>
        <h3 ref={registerHeading} tabIndex={-1}>Add extension</h3>
        <p>Add an extension to the shared catalog so it can be reviewed and enabled for a project.</p>
      </div>
      {message}
      <form className="project-settings-extension-form" onSubmit={event => { event.preventDefault(); register(); }}>
        <SelectField label="Extension type" value={kind} options={extensionOptions} disabled={busy}
          onValueChange={value => { setKind(value as ExtensionKind); setFeedback(undefined); }} />
        {kind !== "skill" && <TextField label="Name" required disabled={busy} value={name} onChange={event => setName(event.target.value)} />}
        {kind !== "mcp" && <TextField label={kind === "skill" ? "Local skill path" : "Plugin source"} required disabled={busy} value={source}
          hint={kind === "skill" ? "Choose the local SKILL.md file. Its name is read from the file." : "Use a local JavaScript or TypeScript file, or an exact npm version such as package-name@1.2.3."}
          onChange={event => setSource(event.target.value)} />}
        {kind === "mcp" && <TextareaField label="MCP configuration (JSON)" rows={6} required disabled={busy} value={config} inputClassName="font-mono"
          hint="Use a local config with a command array or a remote config with a URL."
          onChange={event => setConfig(event.target.value)} />}
        <div className="project-settings-actions">
          <Button variant="primary" type="submit" loading={busy}
            disabled={(kind !== "skill" && !name.trim()) || (kind === "mcp" ? !config.trim() : !source.trim())}>Register extension</Button>
        </div>
      </form>
    </section>
  );

  return (
    <section className="project-settings-panel" aria-label="Extensions and trust">
      <div className="project-settings-section-head">
        <div className="project-settings-heading"><h3>Extensions</h3><p>Review registered skills, MCP servers, and plugins for this project.</p></div>
        <Button ref={addExtension} size="sm" variant="primary" disabled={busy || loading} icon={<Plus size={14} aria-hidden />}
          onClick={() => {
            setRegistering(true);
            setFeedback(undefined);
            setReview(null);
            requestAnimationFrame(() => registerHeading.current?.focus());
          }}>Add extension</Button>
      </div>
      {message}
      {loading && <p role="status" className="project-settings-empty">Loading extensions...</p>}
      {loadError && <div className="project-settings-message project-settings-section-head"><p role="alert" className="text-crit-soft">{loadError}</p>
        <Button size="sm" disabled={busy} onClick={refresh}>Retry</Button></div>}
      {!loading && extensions.length > 0 && <div>
        <div className="project-settings-section-head project-settings-catalog-head">
          <h4>Registered extensions</h4>
          <Button variant="ghost" size="sm" disabled={busy} icon={<RefreshCw size={13} aria-hidden />} onClick={refresh}>Refresh</Button>
        </div>
        <ul className="project-settings-extension-list">{extensions.map(extension => {
          const enabled = extension.enabled_projects.includes(projectId);
          const hasErrors = !!extension.errors?.length;
          const trusted = extension.trusted && !hasErrors;
          const reviewing = review === extension.id;
          const status = hasErrors ? "Needs attention" : !trusted ? "Trust required" : enabled ? "Enabled for project" : "Trusted";
          return <li key={extension.id}>
            <div className="project-settings-extension-row">
              <div className="project-settings-extension-name">
                <h5>{extension.name}</h5>
                <p>{extensionTypes[extension.kind]} / Version {extension.version}</p>
                <code>{extension.source}</code>
              </div>
              <span className="project-settings-extension-state" data-enabled={trusted && enabled || undefined}>{status}</span>
              <div className="project-settings-extension-actions">
                <Button size="sm" variant={trusted || reviewing ? "ghost" : "subtle"} disabled={busy} aria-expanded={reviewing}
                  onClick={() => { setReview(reviewing ? null : extension.id); setFeedback(undefined); }}>
                  {reviewing ? "Hide details" : trusted || hasErrors ? "Version details" : "Review trust"}
                </Button>
                {(trusted || enabled) && <Button size="sm" disabled={busy || turnActive} onClick={() => void change(
                  () => write<Extension>(`/api/ide/projects/${encodeURIComponent(projectId)}/extensions/${encodeURIComponent(extension.id)}`, { enabled: !enabled }),
                  enabled ? "Extension disabled for this project." : "Extension enabled for this project.",
                )}>{enabled ? "Disable for project" : "Enable for project"}</Button>}
              </div>
            </div>
            {reviewing && <div className="project-settings-version">
              <dl><dt>Version</dt><dd>{extension.version}</dd><dt>SHA-256</dt><dd><code>{extension.hash}</code></dd></dl>
              {hasErrors && <p className="text-crit-soft">{extension.errors?.join(" ")}</p>}
              {!trusted && !hasErrors && <>
                <p>Trust this version only if you recognize its source. Enabling it allows the agent to use it in this project.</p>
                <Button size="sm" variant="primary" disabled={busy} icon={<ShieldCheck size={14} aria-hidden />} onClick={() => void change(
                  () => write<Extension>(`${registry}/${encodeURIComponent(extension.id)}/trust`, { version: extension.version, sha256: extension.hash }),
                  "Version trusted.", () => setReview(null),
                )}>Trust version {extension.version}</Button>
              </>}
            </div>}
          </li>;
        })}</ul>
      </div>}
      {!loading && !loadError && !extensions.length && <div className="project-settings-empty project-settings-extension-empty">
        <Puzzle size={22} aria-hidden /><div><h4>No extensions registered</h4><p>Add a local skill, MCP server, or plugin to get started.</p></div>
      </div>}
    </section>
  );
}
