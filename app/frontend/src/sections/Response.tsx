import { useEffect, useState } from "react";
import { api, errorText, type Project, type ResponseReport } from "../lib/api";
import { EmptyState, Field, Input, Panel, Skeleton } from "../components/ui";
import { Checkbox, SelectField } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { ChoiceCards, CODEBASE_DEPTHS, type Choice } from "./ProjectActions";
import { portProblem } from "./ProjectDeepScan";
import { ResponseReportView } from "./ResponseReport";

// The response orchestrator: pick a project, say what to look at and what to do about what is found. The findings are classified (a finding with no score is Unknown, never a quiet Warning), and the
// response is staged for a person: a patch candidate is tested in the closed container and never applied, an upgrade is a recommendation, and an email is sent only when mail is set up in Settings.

export const SCOPES: Choice[] = [
  { id: "dependencies", label: "Dependencies", text: "The packages the project uses that are known to be vulnerable." },
  { id: "codebase", label: "Codebase", text: "The project's own code, tests included." },
  { id: "both", label: "Both", text: "Dependencies and code together, in one report." },
];
export const MODES: Choice[] = [
  { id: "log", label: "Log", text: "Record every finding with its class. Nothing leaves this computer." },
  { id: "email", label: "Email", text: "Compose one summary message with no code in it, and send it only if mail is set up in Settings." },
  { id: "auto-patch", label: "Patch candidates", text: "For a finding in a file, a model proposes a patch that is tested in the closed container and stored for you to review; for a vulnerable package, the upgrade is recommended. Nothing is applied to your folders." },
];

export function Response() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState("");
  const [id, setId] = useState("");
  const [scope, setScope] = useState("both");
  const [mode, setMode] = useState("log");
  const [depth, setDepth] = useState("quick");
  const [assist, setAssist] = useState(false);
  const [remote, setRemote] = useState(false);
  const [command, setCommand] = useState("");
  const [port, setPort] = useState("");
  const [report, setReport] = useState<ResponseReport | null | undefined>(undefined);
  useEffect(() => { api.projects().then((r) => { setProjects(r.projects); if (r.projects[0]) setId((cur) => cur || r.projects[0].id); }).catch((e) => setError(errorText(e))); }, []);
  const load = (project: string) => {
    if (!project) return Promise.resolve();
    return api.projectScans(project).then((r) => {
      const mine = r.scans.find((s) => s.kind === "response");
      if (!mine) { setReport(null); return; }
      return api.projectScan<ResponseReport>(project, mine.scan_id).then(setReport);
    }).catch((e) => setError(errorText(e)));
  };
  useEffect(() => { setReport(undefined); load(id); }, [id]);                // eslint-disable-line react-hooks/exhaustive-deps

  if (error && !projects) return <Panel title="Response orchestrator"><p role="alert" className="text-[0.82rem] text-crit-soft">The projects could not be read: {error}</p></Panel>;
  if (!projects) return <Panel title="Response orchestrator"><Skeleton className="h-24 w-full" /></Panel>;
  if (projects.length === 0) return <Panel title="Response orchestrator"><EmptyState title="No project yet" hint="Add a project on the Projects tab first: the orchestrator works on a project you own or may test." /></Panel>;

  const full = depth === "full" && scope !== "dependencies";
  const bad = full ? portProblem(port) : "";
  return (
    <div className="space-y-5">
      <Panel title="Response orchestrator" desc="Collect the findings of a project, classify them into Recommendation, Warning and Critical, and respond. Everything it does is staged for you; nothing is applied to your folders.">
        <div className="space-y-4">
          <SelectField label="Project" aria-label="Project" value={id} onValueChange={setId} rowClassName="max-w-md" options={projects.map((p) => ({ value: p.id, label: p.name }))} />
          <ChoiceCards legend="What to look at" choices={SCOPES} value={scope} onChange={setScope} />
          {scope !== "dependencies" && <ChoiceCards legend="How deep to scan the code" choices={CODEBASE_DEPTHS} value={depth} onChange={setDepth} />}
          {full && (
            <div className="grid gap-3 sm:grid-cols-[1fr_9rem]">
              <Field label="How does it start? (optional)" hint="For the questions asked on the container's own loopback; leave empty to skip them.">
                <Input aria-label="Start command" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="python app.py" maxLength={500} autoComplete="off" spellCheck={false} />
              </Field>
              <Field label="Port" hint={bad || undefined}><Input aria-label="Port" value={port} onChange={(e) => setPort(e.target.value)} placeholder="8000" inputMode="numeric" autoComplete="off" /></Field>
            </div>)}
          <ChoiceCards legend="What to do about the findings" choices={MODES} value={mode} onChange={setMode} />
          <fieldset className="space-y-2 text-[0.82rem]">
            <legend className="mb-1 text-[0.8rem] font-medium">Language model</legend>
            <Checkbox checked={assist} onCheckedChange={setAssist} label="Estimate a class for a finding that has no score"
              description="The estimate is marked as an estimate; without this, such a finding is Unknown." />
            {(assist || mode === "auto-patch") && (
              <Checkbox checked={remote} onCheckedChange={setRemote} label="The model may be one that is not on this computer"
                description="Allow the words of a finding, and the lines of a file around it, to be sent to it. Off, nothing is sent to a model that is not here." />)}
          </fieldset>
          <RunConsole stage="respond_project" label="Run" disabled={!!bad} params={{ project: id }}
            launch={() => api.respondProject(id, { scope, mode, depth, assist, allow_remote: remote, start_command: command.trim(), port: full && port.trim() ? Number(port) : null })}
            onDone={() => { load(id); }} hint="The quick code scan takes seconds; the full one and candidate tests take minutes." />
        </div>
      </Panel>
      <Panel title="Latest report" desc="The newest response run of this project.">
        {report === undefined && <Skeleton className="h-24 w-full" />}
        {report === null && <p className="text-[0.8rem] text-muted">This project has had no response run yet.</p>}
        {report && <ResponseReportView r={report} />}
      </Panel>
    </div>
  );
}
