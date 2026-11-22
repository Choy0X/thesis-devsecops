import { useEffect, useState } from "react";
import { api, errorText, type Project, type ProjectUpdate, type UpdateListItem } from "../lib/api";
import { Button, Panel, Skeleton } from "../components/ui";
import { Select, SelectField } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { useConfirm } from "../components/hud/AlertDialog";
import { DiffViewer } from "../components/hud/DiffViewer";
import { useToast } from "../components/Toast";
import { when } from "./ProjectScan";
import { UpdateTrialPanel } from "./UpdateTrial";

// Updating the dependencies of a project. The page makes a candidate on a copy of the manifests from the newest dependency scan, shows what would change and what the scan would say
// afterwards (by advisory matching only), and writes to the project's own files only when the person confirms the list of files. It never installs anything or runs a test, and
// never calls the candidate safe, correct or ready to merge.

export const STATUS_WORDS: Record<ProjectUpdate["status"], string> = { candidate: "A candidate: nothing in the project has changed", applied: "Applied to the project's files", reverted: "Applied and then undone" };
const MODES: [string, string][] = [["security", "Security fixes only"], ["all", "Everything that is outdated"]];

function Why({ c }: { c: ProjectUpdate["plan"]["changes"][number] }) {
  return <>{c.kind === "fix" ? `fixes ${c.advisories.join(", ") || "an advisory"}` : "a newer release"}</>;
}

export function UpdateView({ u, projectName, onChanged }: { u: ProjectUpdate; projectName: string; onChanged: (next?: ProjectUpdate | null) => void }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const names = u.files.map((f) => f.file);
  const pre = u.predicted;
  const run = async (fn: () => Promise<ProjectUpdate | { ok: boolean }>, done: string, discarded = false) => {
    setBusy(true);
    try { const r = await fn(); toast.success(done); onChanged(discarded ? null : (r as ProjectUpdate)); }
    catch (e) { toast.error(errorText(e)); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    const ok = await confirm({ title: `Write ${names.length} file${names.length === 1 ? "" : "s"} into ${projectName}?`,
      description: (<div className="space-y-2"><p>These files in your project will be changed. The originals are kept, so you can undo it while the files are as written.</p>
        <ul className="list-disc pl-5 font-mono text-[0.78rem]" aria-label="Files that will be written">{names.map((n) => <li key={n}>{n}</li>)}</ul>
        <p>Lock files are not changed. This is a candidate for you to review: it does not say that the project works with the new versions.</p></div>),
      confirmLabel: "Write these files", cancelLabel: "Not now", tone: "critical" });
    if (ok) await run(() => api.applyProjectUpdate(u.project_id, u.id, names), "The files were written. Regenerate your lock files and run your tests.");
  };
  const undo = async () => {
    const ok = await confirm({ title: "Undo this update?", description: "The original files are put back, if they are still exactly as this update wrote them.", confirmLabel: "Put the originals back", cancelLabel: "Keep it" });
    if (ok) await run(() => api.revertProjectUpdate(u.project_id, u.id), "The originals are back.");
  };
  const discard = async () => {
    const ok = await confirm({ title: "Discard this candidate?", description: "The copy and the diff are deleted from the app. Nothing in your project is touched.", confirmLabel: "Discard it", cancelLabel: "Keep it" });
    if (ok) await run(() => api.discardProjectUpdate(u.project_id, u.id), "Discarded.", true);
  };
  return (
    <div className="space-y-3 text-[0.82rem]">
      <p><span className="font-medium">{STATUS_WORDS[u.status]}</span> <span className="text-muted">· {u.mode_words} · planned {when(u.created)}{u.applied_at ? ` · written ${when(u.applied_at)}` : ""}</span></p>
      <p className="text-muted">{u.words}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-[0.8rem]" aria-label="Planned changes">
          <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wide text-faint"><th className="py-2 pr-3 font-medium">Package</th><th className="py-2 pr-3 font-medium">From</th>
            <th className="py-2 pr-3 font-medium">To</th><th className="py-2 pr-3 font-medium">Size of the step</th><th className="py-2 font-medium">Why</th></tr></thead>
          <tbody>
            {u.plan.changes.map((c) => (
              <tr key={`${c.ecosystem}:${c.name}`} className="border-b border-line/60 align-top">
                <td className="py-2 pr-3 font-mono text-[0.76rem]">{c.name} <span className="font-sans text-faint">{c.ecosystem}</span></td><td className="tnum py-2 pr-3">{c.from}</td><td className="tnum py-2 pr-3">{c.to}</td>
                <td className={`py-2 pr-3 ${c.bump === "major" ? "text-warn" : ""}`}>{c.bump}{c.bump === "major" ? ": may break the code that uses it" : ""}</td><td className="py-2"><Why c={c} /></td>
              </tr>))}
          </tbody>
        </table>
      </div>
      {u.plan.not_updated.length > 0 && (
        <details><summary className="cursor-pointer text-muted">{u.plan.not_updated.length} not updated, and why</summary>
          <ul className="mt-1 space-y-0.5 text-muted">{u.plan.not_updated.map((n) => <li key={`${n.ecosystem}:${n.name}`}><span className="font-mono text-[0.76rem] text-fg">{n.name}</span> {n.version ?? ""}: {n.reason}</li>)}</ul></details>)}
      {u.skipped.length > 0 && <ul className="space-y-0.5 text-warn" aria-label="Left as they are">{u.skipped.map((s, i) => <li key={i}><span className="font-mono text-[0.76rem]">{s.file}</span>: {s.reason}</li>)}</ul>}
      {u.locks_to_regenerate.length > 0 && <p role="status" className="text-warn">Not edited, because only your package manager can write them: {u.locks_to_regenerate.join(", ")}. Regenerate {u.locks_to_regenerate.length === 1 ? "it" : "them"} after applying.</p>}

      <div className="border border-line bg-surface-2 px-3 py-2.5" aria-label="What the scan would say afterwards">
        <p className="font-medium">Predicted after the update</p>
        <p>{pre.advisories_fixed.length} advisor{pre.advisories_fixed.length === 1 ? "y" : "ies"} no longer {pre.advisories_fixed.length === 1 ? "affects" : "affect"} the changed packages
          {pre.advisories_remaining.length ? `; ${pre.advisories_remaining.length} still ${pre.advisories_remaining.length === 1 ? "affects" : "affect"} them` : ""}
          {pre.advisories_introduced.length ? `; ${pre.advisories_introduced.length} newly ${pre.advisories_introduced.length === 1 ? "affects" : "affect"} them (${pre.advisories_introduced.join(", ")})` : ""}.</p>
        <p className="text-[0.76rem] text-faint">{pre.words}</p>
      </div>

      <div className="space-y-1.5" aria-label="The changes to the files">
        {u.files.map((f) => (
          <details key={f.file}><summary className="cursor-pointer"><span className="font-mono text-[0.78rem]">{f.file}</span> <span className="text-muted">({f.edits.length} line{f.edits.length === 1 ? "" : "s"})</span></summary>
            <div className="mt-1"><DiffViewer diff={f.diff} height={240} label={`Changes to ${f.file}`} /></div></details>))}
      </div>

      {u.status === "candidate" && <UpdateTrialPanel u={u} />}

      <div className="flex flex-wrap gap-2 pt-1">
        {u.status === "candidate" && <Button variant="primary" disabled={busy} onClick={apply}>Apply to the project's files</Button>}
        {u.status === "applied" && <Button disabled={busy} onClick={undo}>Undo</Button>}
        {u.status !== "applied" && <Button variant="ghost" disabled={busy} onClick={discard}>Discard</Button>}
      </div>
    </div>
  );
}

/** The update panel of a project: make a plan from the newest dependency scan, and read, apply, undo or discard an update. */
export function ProjectUpdates({ project, onChanged }: { project: Project; onChanged?: () => void }) {
  const [mode, setMode] = useState<"security" | "all">("security");
  const [list, setList] = useState<UpdateListItem[] | null>(null);
  const [chosen, setChosen] = useState("");
  const [update, setUpdate] = useState<ProjectUpdate | null>(null);
  const [error, setError] = useState("");
  const load = (pick?: string) => api.projectUpdates(project.id).then((r) => {
    setList(r.updates);
    const id = pick ?? (r.updates.find((x) => x.id === chosen) ? chosen : r.updates[0]?.id ?? "");
    setChosen(id);
    if (id) return api.projectUpdate(project.id, id).then(setUpdate);
    setUpdate(null);
  }).catch((e) => setError(errorText(e)));
  useEffect(() => { load(); }, [project.id]);                                 // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Panel title="Update dependencies" desc="Plans newer versions from the newest dependency scan and shows what would change. Nothing in the project changes until you apply it, and nothing is installed or run.">
      <div className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <SelectField label="What to update" aria-label="What to update" value={mode} onValueChange={(v) => setMode(v as "security" | "all")} rowClassName="min-w-[14rem]"
            options={MODES.map(([v, label]) => ({ value: v, label }))} />
          <div className="min-w-0 flex-1"><RunConsole stage="plan_project_update" label="Plan an update" params={{ project: project.id, mode }} launch={() => api.planProjectUpdate(project.id, mode)}
            onDone={() => { load(""); onChanged?.(); }} hint="Reads the newest dependency scan and makes a candidate on a copy of the manifests. Everything outdated needs a scan that looked up newer releases." /></div>
        </div>
        {error && <p role="alert" className="text-[0.8rem] text-crit-soft">{error}</p>}
        {list === null && !error && <Skeleton className="h-16 w-full" />}
        {list && list.length === 0 && <p className="text-[0.8rem] text-muted">No update has been planned for this project yet.</p>}
        {list && list.length > 1 && (
          <div className="flex items-center gap-2 text-[0.78rem] text-muted">Update
            <Select aria-label="Update" value={chosen} onValueChange={(v) => load(v)} className="min-w-[20rem]"
              options={list.map((x) => ({ value: x.id, label: `${when(x.created)} · ${x.mode_words.toLowerCase()} · ${x.changes} dependencies · ${x.status}` }))} />
          </div>)}
        {update && <UpdateView u={update} projectName={project.name} onChanged={(next) => { if (next === null) load(""); else if (next) { setUpdate(next); load(next.id); } onChanged?.(); }} />}
      </div>
    </Panel>
  );
}
