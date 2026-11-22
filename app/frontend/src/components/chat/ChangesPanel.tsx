import { useEffect, useRef, useState } from "react";
import { api, errorText, type AgentApplyListItem, type AgentCheckpoint, type AgentDiffFile, type AgentRefresh } from "../../lib/api";
import { useConfirm } from "../hud/AlertDialog";
import { Badge } from "../hud/Badge";
import { Dialog } from "../hud/Dialog";
import { DiffViewer } from "../hud/DiffViewer";
import { Checkbox } from "../kit/Toggles";
import { Button } from "../ui";

// The review of a Manual or Auto session's work: what the sandbox copy changed against the project as it was copied, one row per file (its diff opens in a window), and the four things the
// owner can do with it. Approve and apply all writes every change into the real project in one step, all or nothing (a removal only when it is ticked). Discard puts the sandbox copy back
// to the project as it was. Refresh from project brings in what the owner changed meanwhile and makes that the new starting point. Undo puts the originals back. Nothing here is reachable
// from a model: the page is the only caller of these routes.

const KIND_TONE: Record<AgentDiffFile["kind"], string> = { added: "border-diff-add text-diff-add", changed: "border-warn-line text-warn", deleted: "border-diff-del text-diff-del" };

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** What "Approve and apply all" would write and remove, from the rows as listed and the removals ticked. A file with no content to write (kept back, or unreadable) is not counted. */
export function planOf(files: AgentDiffFile[], ticked: Set<string>, leftOut: Set<string> = new Set()) {
  const writable = files.filter((f) => f.kind !== "deleted" && f.sha256_after !== null);
  const write = writable.filter((f) => !leftOut.has(f.file));
  const remove = files.filter((f) => f.kind === "deleted" && f.can_delete && ticked.has(f.file));
  const blocked = write.filter((f) => f.drift);                       // a file left out cannot stop the others, whatever happened to it in the project
  return { write, remove, blocked, left: writable.length - write.length };
}

export function ChangesPanel({ projectId, files, busy, onChanged }: { projectId: string; files: AgentDiffFile[]; busy: boolean; onChanged: () => void }) {
  const confirm = useConfirm();
  const alive = useRef(true);
  const [open, setOpen] = useState<AgentDiffFile | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [leftOut, setLeftOut] = useState<Set<string>>(new Set());          // files the owner unticked: not applied, and left as they are in the sandbox
  const [working, setWorking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshed, setRefreshed] = useState<AgentRefresh | null>(null);
  const [lastApplied, setLastApplied] = useState<AgentApplyListItem | null>(null);
  const [checkpoints, setCheckpoints] = useState<AgentCheckpoint[] | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const loadApplies = () => {
    void Promise.resolve().then(() => api.agentApplies(projectId)).then((r) => alive.current && setLastApplied(r.applies.find((a) => a.status === "applied") ?? null)).catch(() => undefined);       // ignored: no Undo is offered when this cannot be read
  };
  useEffect(loadApplies, [projectId]);                                       // eslint-disable-line react-hooks/exhaustive-deps; ignored: read once for this project
  // a removal that is no longer listed (or no longer allowed) is dropped from the ticks, so a stale tick can never be sent
  useEffect(() => {
    setTicked((t) => { const keep = new Set(files.filter((f) => f.kind === "deleted" && f.can_delete && t.has(f.file)).map((f) => f.file)); return keep.size === t.size ? t : keep; });
  }, [files]);

  useEffect(() => {                                                          // a file that is no longer listed is dropped from what was left out
    setLeftOut((l) => { const keep = new Set(files.filter((f) => l.has(f.file)).map((f) => f.file)); return keep.size === l.size ? l : keep; });
  }, [files]);

  const { write, remove, blocked, left } = planOf(files, ticked, leftOut);
  const nothing = write.length + remove.length === 0;
  const locked = busy || working !== null;
  const run = async (what: string, job: () => Promise<void>) => {
    setWorking(what); setError(null); setNotice(null); setRefreshed(null);
    try { await job(); } catch (e) { if (alive.current) setError(errorText(e)); } finally { if (alive.current) setWorking(null); }
  };

  const approveAll = async () => {
    const ok = await confirm({
      title: "Apply the session's changes to your project?",
      description: <>This writes {plural(write.length, "file")} into your real project{remove.length ? <> and removes {plural(remove.length, "file")} you ticked</> : null}{left ? <>; {plural(left, "file")} you left out {left === 1 ? "stays" : "stay"} in the sandbox</> : null}.
        Every file is checked first: if one changed in your project after the copy, nothing is written. You can undo it afterwards.</>,
      confirmLabel: `Apply ${plural(write.length + remove.length, "change")}`,
    });
    if (!ok) return;
    await run("apply", async () => {
      const done = await api.agentApproveAll(projectId, remove.map((f) => f.file), write.map((f) => ({ file: f.file, sha256_after: f.sha256_after })), [...leftOut]);
      if (!alive.current) return;
      setNotice(`Applied ${plural(done.files.length, "file")} to your project${done.deleted?.length ? ` and removed ${plural(done.deleted.length, "file")}` : ""}. Refresh from project to start the next change from here.`);
      setLastApplied({ id: done.id, created: done.created, status: "applied", files: done.files.length, applied_at: done.applied_at });
      onChanged();
    });
  };
  const discard = async () => {
    const ok = await confirm({ title: "Discard the session's changes?", description: "The sandbox copy goes back to the project as it was when it was copied. Your real project is not touched. The state before is kept as a checkpoint.",
      confirmLabel: "Discard changes" });
    if (!ok) return;
    await run("discard", async () => { const done = await api.agentDiscardChanges(projectId); if (alive.current) { setNotice(done.summary); onChanged(); } });
  };
  const refresh = () => run("refresh", async () => { const done = await api.agentRefresh(projectId); if (alive.current) { setRefreshed(done); onChanged(); } });
  const undo = async () => {
    if (!lastApplied) return;
    const ok = await confirm({ title: "Undo the last apply?", description: "The files are put back as they were before it, if they are still exactly as it wrote them. Nothing is changed if one was edited since.", confirmLabel: "Undo the apply" });
    if (!ok) return;
    await run("undo", async () => { await api.agentApplyRevert(projectId, lastApplied.id); if (alive.current) { setNotice("Put your project back as it was before the apply."); setLastApplied(null); loadApplies(); onChanged(); } });
  };
  const rollback = async (c: AgentCheckpoint) => {
    const ok = await confirm({ title: "Roll the sandbox back?", description: `The sandbox copy goes back to "${c.message}" (${c.hash}). Everything written after it is lost. Your real project is not touched.`, confirmLabel: "Roll back", tone: "critical" });
    if (!ok) return;
    await run("rollback", async () => { await api.agentRollback(projectId, c.root, c.hash); if (alive.current) { setNotice(`The sandbox is back at ${c.hash}.`); onChanged(); } });
  };
  const loadCheckpoints = () => { if (checkpoints === null) void Promise.resolve().then(() => api.agentCheckpoints(projectId)).then((r) => alive.current && setCheckpoints(r.checkpoints)).catch((e) => alive.current && setError(errorText(e))); };

  return (
    <div className="space-y-2">
      <ul className="space-y-1.5 text-[0.76rem]">
        {files.map((f) => (
          <li key={f.file} className="space-y-0.5">
            <div className="flex items-center justify-between gap-2">
              <span className="flex min-w-0 items-center gap-2">
                {f.kind !== "deleted" && f.sha256_after !== null && (
                  <Checkbox aria-label={`Apply ${f.rel}`} checked={!leftOut.has(f.file)} disabled={locked}
                    onCheckedChange={(on) => setLeftOut((l) => { const next = new Set(l); if (on) next.delete(f.file); else next.add(f.file); return next; })} />)}
                <button type="button" className="min-w-0 truncate text-left hover:underline focus-visible:underline" title={`Show the change to ${f.rel}`} onClick={() => setOpen(f)}>{f.rel}</button>
              </span>
              <Badge className={KIND_TONE[f.kind]}>{f.kind}</Badge>
            </div>
            {f.drift && <p className="text-[0.7rem] text-warn">Changed in your project since the copy. Refresh from project first.</p>}
            {!f.drift && f.note && <p className="text-[0.7rem] text-muted">{f.note}</p>}
            {f.kind === "deleted" && f.can_delete && (
              <Checkbox label="Also remove it from my project" checked={ticked.has(f.file)} disabled={locked}
                onCheckedChange={(on) => setTicked((t) => { const next = new Set(t); if (on) next.add(f.file); else next.delete(f.file); return next; })} />)}
          </li>
        ))}
      </ul>
      {left > 0 && <p className="text-[0.72rem] text-muted">{plural(left, "file")} left out: {left === 1 ? "it stays" : "they stay"} in the sandbox and {left === 1 ? "is" : "are"} not applied.</p>}
      {blocked.length > 0 && <p role="status" className="text-[0.72rem] text-warn">{plural(blocked.length, "file")} changed in your project after the copy, so nothing can be applied until you refresh from project.</p>}
      <div className="flex flex-wrap gap-1.5">
        <Button size="sm" variant="primary" loading={working === "apply"} disabled={locked || nothing || blocked.length > 0} onClick={() => void approveAll()}
          title={busy ? "Wait for the run to end." : undefined}>Approve and apply all</Button>
        <Button size="sm" variant="ghost" loading={working === "refresh"} disabled={locked} onClick={() => void refresh()}>Refresh from project</Button>
        <Button size="sm" variant="ghost" loading={working === "discard"} disabled={locked} onClick={() => void discard()}>Discard changes</Button>
        {lastApplied && <Button size="sm" variant="ghost" loading={working === "undo"} disabled={locked} onClick={() => void undo()}>Undo last apply</Button>}
      </div>
      {error && <p role="alert" className="text-[0.74rem] text-crit-soft">{error}</p>}
      {notice && <p role="status" className="text-[0.74rem] text-muted">{notice}</p>}
      {refreshed && (
        <div role="status" className="space-y-1 text-[0.74rem] text-muted">
          <p>{refreshed.summary}</p>
          {refreshed.conflicts.length > 0 && <p className="text-warn">Changed on both sides, left as the session has them: {refreshed.conflicts.join(", ")}.</p>}
        </div>)}
      <details onToggle={(e) => e.currentTarget.open && loadCheckpoints()} className="text-[0.74rem]">
        <summary className="cursor-pointer text-muted">Checkpoints</summary>
        {checkpoints === null ? <p className="mt-1 text-muted">Loading...</p> : checkpoints.length === 0 ? <p className="mt-1 text-muted">No checkpoint has been made yet.</p> : (
          <ul className="mt-1 space-y-1">
            {checkpoints.map((c) => (
              <li key={`${c.root}-${c.hash}`} className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate" title={`${c.message} (${c.hash})`}>{c.message}</span>
                <Button size="sm" variant="ghost" disabled={locked} onClick={() => void rollback(c)}>Roll back</Button>
              </li>
            ))}
          </ul>)}
      </details>
      <Dialog open={open !== null} onOpenChange={(o) => { if (!o) setOpen(null); }} title={open?.rel ?? "Change"} description={open ? `${open.kind} in the sandbox copy` : undefined} className="w-[min(64rem,96vw)]">
        {open && (open.diff ? <DiffViewer diff={open.diff} height={480} label={`Changes to ${open.rel}`} /> : <p className="text-[0.82rem] text-muted">{open.note ?? "There is no text change to show for this file."}</p>)}
      </Dialog>
    </div>
  );
}
